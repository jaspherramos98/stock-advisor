"""
Stock (share) leg of the autonomous agent — routing, sizing and entries (S2). Exits: S3.

ROUTING (approved plan, conviction-based): the aggressive options leg takes the strongest ideas —
highly_recommended or conviction ≥ OPTION_CONVICTION — plus every bearish idea (shares can't be
shorted on this account, so bearish = puts). Other buys become share positions (the core). An
options-routed buy with no affordable contract falls back to shares rather than being dropped. Crypto
never routes to shares (the MCP can't trade it). With config.AGENT_TRADE_STOCKS off, everything
routes to options — the agent behaves exactly as before.

SIZING: Argus's own pyramid (calculator.portfolio.calculate_allocations — 20/55/25 risk tiers,
conviction within a tier, 40% single-name cap) over ALL of the cycle's buy signals, on the AGENTIC
buying power — one shared pool, so the pipeline's MAIN-account dollar sizes are never reused here.

ORDERS: every share buy is a DOLLAR-based market order (fractional-friendly, fills immediately in
regular hours). A resting limit that doesn't fill would leave the name looking un-held next cycle and
invite a duplicate buy; the orders here are small enough that market slippage is noise.
"""
from __future__ import annotations

import config
from trading_guards import OrderIntent

OPTION_CONVICTION = 75


def _conviction(sig: dict) -> float:
    c = sig.get("conviction")
    if c is None:
        c = (sig.get("confidence_score") or 0) * 100
    try:
        return float(c)
    except (TypeError, ValueError):
        return 0.0


def stocks_enabled(override: bool | None = None) -> bool:
    return bool(getattr(config, "AGENT_TRADE_STOCKS", False)) if override is None else override


def route(sig: dict, stocks: bool) -> str | None:
    """'option' | 'stock' | None (not tradeable by the agent)."""
    direction = (sig.get("direction") or "").lower()
    if direction == "short":
        return "option"                                   # bearish → puts (no share shorting)
    if direction != "buy":
        return None
    if not stocks:
        return "option"
    if sig.get("highly_recommended") or _conviction(sig) >= OPTION_CONVICTION:
        return "option"
    return "stock" if can_hold_shares(sig) else None


def can_hold_shares(sig: dict) -> bool:
    return (sig.get("asset_type") or "stock").lower() != "crypto" and (sig.get("direction") or "").lower() == "buy"


def rank(signals: list[dict]) -> list[dict]:
    """Best idea first across both legs: conviction high→low (stable, so source priority breaks ties)."""
    return sorted(signals, key=_conviction, reverse=True)


def size_buys(signals: list[dict], buying_power: float) -> dict[str, float]:
    """{TICKER: dollars} — the pyramid over every buy signal, on the agentic buying power."""
    import math
    from calculator.portfolio import calculate_allocations
    from trading_guards import MAX_SINGLE_ORDER_FRACTION
    buys = [dict(s, direction="buy") for s in signals if can_hold_shares(s) and s.get("ticker")]
    if not buys:
        return {}
    # The allocator rounds to the nearest cent, so a name sized exactly AT the 40% cap comes back a
    # cent over it (16.768 → 16.77) and the order guard rejects it. Clamp to the cap, floored.
    cap = math.floor(MAX_SINGLE_ORDER_FRACTION * buying_power * 100) / 100
    return {(a.get("ticker") or "").upper(): min(a.get("dollar_amount") or 0.0, cap)
            for a in calculate_allocations(buys, buying_power)}


def plan_buy(sig: dict, dollars: float, avail: float) -> OrderIntent | None:
    """Pure: the share-buy intent for a signal, or None when it can't be a real order (below the
    broker's $1 dollar-order minimum after capping to what's still available this cycle)."""
    import math
    from ingestion.robinhood_mcp import MIN_DOLLAR_ORDER
    ticker = (sig.get("ticker") or "").upper()
    # Floor to the cent — rounding UP can push a name sized exactly at the 40% cap one cent over it.
    amount = math.floor(min(dollars or 0.0, avail) * 100) / 100
    if not ticker or amount < MIN_DOLLAR_ORDER:
        return None
    from datetime import date
    return OrderIntent(ticker=ticker, side="buy", dollars=amount,
                       reason=f"stock ({sig.get('source') or 'pipeline'}, conv {sig.get('conviction')})",
                       client_id=f"stk-{ticker}-{date.today().isoformat()}")


def held_tickers(mcp, acct) -> set[str]:
    """Tickers the agentic account already holds as shares — never stack a second entry."""
    try:
        return {(p.get("ticker") or "").upper() for p in mcp.fetch_positions(acct) if p.get("ticker")}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_stocks: equity positions read failed — {e}")
        return set()


def enter(mcp, acct, sig: dict, dollars: float, avail: float, state, verbose: bool) -> tuple[dict | None, float]:
    """Place (or dry-run) one share buy. Returns (result row | None if nothing to place, $ committed).
    A LIVE placement records the entry's exit plan in storage.agent_stock_book for the exit pass."""
    intent = plan_buy(sig, dollars, avail)
    if intent is None:
        if verbose:
            print(f"  {sig.get('ticker')}: stock size ${dollars or 0:.2f} below the $1 minimum (skip)")
        return None, 0.0
    res = mcp.place_order(intent, state, buying_power=avail, account_number=acct)
    row = {"ticker": intent.ticker, "leg": "stock", "dollars": intent.dollars,
           **{k: res[k] for k in ("status", "reason")}}
    if res["status"] == "placed":
        from storage.agent_stock_book import record_entry
        record_entry(intent.ticker, sig.get("exit_condition"), sig.get("source"), sig.get("conviction"))
    return row, (intent.dollars if res["status"] in ("placed", "dry_run") else 0.0)
