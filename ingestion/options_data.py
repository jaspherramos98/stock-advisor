"""
Option chain / contract selection for the autonomous options agent (Phase 2).

Resolves a strategy's abstract target ("~4% OTM call, 21-35 DTE") into a concrete, tradable,
liquid Robinhood option contract, via the MCP option-data tools. All MCP access goes through
ingestion.robinhood_mcp._call_tool (which honors is_error + the OAuth session). The pure
selection helpers (_dte / pick_expiration / pick_contract_by_moneyness / liquidity_ok) take
plain data and are unit-tested without the network.

Confirmed live shapes (2026-08-14):
  get_option_chains(underlying_symbol=…)      → data.chains[].{id, symbol, expiration_dates[]}
  get_option_instruments(chain_symbol, expiration_dates=<str>, type='call'|'put')
                                              → data.instruments[].{id, strike_price, type,
                                                 expiration_date, state, tradability}
  get_option_quotes(instrument_ids=[…])       → data.results[].quote.{bid_price, ask_price,
                                                 mark_price, delta, implied_volatility,
                                                 break_even_price, …}
"""
from __future__ import annotations

import datetime as _dt


# --- pure selection helpers (unit-tested) ---------------------------------------------

def _dte(expiration: str, today: _dt.date | None = None) -> int | None:
    """Days-to-expiration for a 'YYYY-MM-DD' string. None if unparseable."""
    try:
        exp = _dt.date.fromisoformat(expiration)
    except (ValueError, TypeError):
        return None
    today = today or _dt.date.today()
    return (exp - today).days


def pick_expiration(dates: list[str], dte_min: int, dte_max: int,
                    today: _dt.date | None = None) -> str | None:
    """Choose the expiration whose DTE is inside [dte_min, dte_max] and closest to the middle
    of that window (a stable, predictable pick). None if nothing falls in range."""
    target = (dte_min + dte_max) / 2
    best, best_gap = None, None
    for d in dates or []:
        dte = _dte(d, today)
        if dte is None or dte < dte_min or dte > dte_max:
            continue
        gap = abs(dte - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = d, gap
    return best


def pick_contract_by_moneyness(contracts: list[dict], spot: float, right: str,
                               otm_pct: float) -> dict | None:
    """Pick the contract closest to `otm_pct` out-of-the-money.

    call → target strike = spot × (1 + otm_pct/100), choose the nearest tradable strike ≥ target
    put  → target strike = spot × (1 − otm_pct/100), choose the nearest tradable strike ≤ target
    (so the chosen contract is at least the requested distance OTM, not accidentally ITM).
    Falls back to the single nearest strike to target if none sit on the OTM side."""
    if not contracts or not spot or spot <= 0:
        return None
    right = right.lower()
    target = spot * (1 + otm_pct / 100.0) if right == "call" else spot * (1 - otm_pct / 100.0)

    tradable = []
    for c in contracts:
        if (c.get("state") or "active") != "active":
            continue
        if (c.get("tradability") or "tradable") != "tradable":
            continue
        try:
            strike = float(c.get("strike_price"))
        except (ValueError, TypeError):
            continue
        tradable.append((strike, c))
    if not tradable:
        return None

    otm_side = [(s, c) for s, c in tradable if (s >= target if right == "call" else s <= target)]
    pool = otm_side or tradable
    strike, contract = min(pool, key=lambda sc: abs(sc[0] - target))
    return {**contract, "strike": strike}


def liquidity_ok(quote: dict, max_spread_pct: float = 25.0, min_bid: float = 0.05) -> bool:
    """Reject un-fillable / rip-off contracts: a positive bid and a bid/ask spread within
    max_spread_pct of the mid. Not a risk cap — mechanics hygiene so the agent doesn't torch
    money on a $0-bid or 50%-wide contract."""
    try:
        bid = float(quote.get("bid_price") or 0)
        ask = float(quote.get("ask_price") or 0)
    except (ValueError, TypeError):
        return False
    if bid < min_bid or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    return ((ask - bid) / mid * 100.0) <= max_spread_pct


# --- live fetches (MCP) ----------------------------------------------------------------

def _call(name: str, args: dict):
    from ingestion import robinhood_mcp as mcp
    return mcp._data(mcp._call_tool(name, args))


def fetch_expirations(underlying: str) -> list[str]:
    data = _call("get_option_chains", {"underlying_symbol": underlying})
    chains = data.get("chains", []) if isinstance(data, dict) else []
    for ch in chains:
        if (ch.get("symbol") or "").upper() == underlying.upper():
            return ch.get("expiration_dates", []) or []
    return chains[0].get("expiration_dates", []) if chains else []


def fetch_contracts(underlying: str, expiration: str, right: str) -> list[dict]:
    data = _call("get_option_instruments",
                 {"chain_symbol": underlying, "expiration_dates": expiration, "type": right.lower()})
    return (data.get("instruments") or data.get("results") or []) if isinstance(data, dict) else []


def fetch_quote(instrument_id: str) -> dict | None:
    data = _call("get_option_quotes", {"instrument_ids": [instrument_id]})
    rows = data.get("results", []) if isinstance(data, dict) else []
    if not rows:
        return None
    q = rows[0].get("quote") if isinstance(rows[0], dict) else None
    return q or None


def contract_cost(quote_or_mark, contracts: int = 1) -> float:
    """Premium in dollars for `contracts` of a single-leg option: price × 100 × contracts.
    Accepts a quote dict (uses ask, the fillable buy price) or a bare price."""
    if isinstance(quote_or_mark, dict):
        try:
            price = float(quote_or_mark.get("ask_price") or quote_or_mark.get("mark_price") or 0)
        except (ValueError, TypeError):
            price = 0.0
    else:
        price = float(quote_or_mark or 0)
    return round(price * 100 * max(1, contracts), 2)


def select_contract(underlying: str, spot: float, right: str,
                    dte_min: int, dte_max: int, otm_pct: float,
                    max_spread_pct: float = 25.0, max_premium: float | None = None) -> dict | None:
    """Full resolution: chain → expiration in [dte_min,dte_max] → strike ~otm_pct OTM → quote →
    liquidity check → (optional) affordability. Returns {instrument_id, symbol, right, strike,
    expiration, bid, ask, mark, delta, break_even, cost_1x} or None. `max_premium` (dollars) drops
    a contract whose 1-lot cost (ask×100) exceeds available buying power — essential on a small
    pilot where most contracts are unaffordable."""
    exps = fetch_expirations(underlying)
    exp = pick_expiration(exps, dte_min, dte_max)
    if not exp:
        return None
    contracts = fetch_contracts(underlying, exp, right)
    chosen = pick_contract_by_moneyness(contracts, spot, right, otm_pct)
    if not chosen:
        return None
    quote = fetch_quote(chosen["id"])
    if not quote or not liquidity_ok(quote, max_spread_pct=max_spread_pct):
        return None
    cost_1x = contract_cost(quote, 1)
    if max_premium is not None and cost_1x > max_premium:
        return None
    return {
        "instrument_id": chosen["id"],
        "symbol": underlying,
        "right": right.lower(),
        "strike": chosen["strike"],
        "expiration": chosen.get("expiration_date", exp),
        "bid": float(quote.get("bid_price") or 0),
        "ask": float(quote.get("ask_price") or 0),
        "mark": float(quote.get("mark_price") or 0),
        "delta": quote.get("delta"),
        "break_even": quote.get("break_even_price"),
        "cost_1x": cost_1x,
    }
