"""
Systematic strategy runner (overhaul Phase 2) — trades config.AGENT_STRATEGY's rule (strategy/rules.py) once a
month in the agentic account, live or paper. It replaces the old "pipeline pick → trade" entries; the backtest
that chose the rule is backtest/strategy_backtest.py.

ONE cycle (`run`, called by scripts/run_agent.py every ~20 min in market hours):
  1. Due? Once per calendar month (state: strategy_state.json, paper twin in paper scope). The first market-hours
     cycle of a month rebalances — normally the 1st trading day; a missed day (PC asleep) catches up next cycle.
  2. Signal: the rule on daily ADJUSTED closes through YESTERDAY (same inputs as the backtest; no lookahead).
  3. Plan: target weights × (buying power + the strategy's own holdings) → sells first (full exits sell every
     share), then dollar buys. Moves under $1 are skipped (Robinhood's dollar-order minimum).
  4. Orders go through the broker's place_order → trading_guards (daily caps, loss kill switch, buying power) +
     broker review (live) + DRY_RUN — the strategy only raises the single-name cap for itself (one ETF by design).
  5. Decision log: one `rebalance` record with the signal inputs, targets, holdings and order results — the
     "why" the dashboard shows. The month is marked done only when every order went through; otherwise the next
     cycle re-plans from the ACTUAL positions (so a retry can't double up a filled order).

Only the rule's own tickers are touched: anything else in the account (positions left from the old pick-based agent,
hand-bought shares) is left to the old exit pass / the user. Its cash is used at the next rebalance.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import config
from strategy.rules import STRATEGIES, History, align

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_state.json")
MIN_ORDER = 1.00              # Robinhood dollar-order minimum
HISTORY_DAYS = 420            # calendar days of closes to fetch — ≥ the rule's 200 trading days + slack


def _state_path() -> str:
    from agent_mode import state_file
    return state_file(_FILE)


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except OSError as e:
        print(f"strategy: could not save state — {e}")


def market_open() -> bool:
    try:
        import market_hours as mh
        return mh.market_session()["status"] in ("open", "open_half_day")
    except Exception:  # noqa: BLE001 — unknown session → don't trade
        return False


def rebalance_due(state: dict, today: date) -> bool:
    """Pure: not yet rebalanced this calendar month."""
    return state.get("last_rebalance_month") != today.strftime("%Y-%m")


def target_weights(name: str, prices: dict[str, list[tuple[date, float]]], today: date) -> tuple[dict, dict]:
    """Pure: (weights, signal inputs) from the named rule on closes strictly BEFORE `today`."""
    factory, tickers = STRATEGIES[name]
    calendar = sorted({d for t in tickers for d, _ in prices.get(t, []) if d < today})
    series = align({t: prices.get(t, []) for t in tickers}, calendar)
    hist = History(series, len(calendar) - 1)
    weights = factory()(hist)
    inputs = {}
    for t in tickers:
        closes = hist.closes(t)
        if len(closes) >= 200:
            inputs[t] = {"close": round(closes[-1], 2), "sma200": round(sum(closes[-200:]) / 200, 2)}
    inputs["asof"] = calendar[-1].isoformat() if calendar else None
    return weights, inputs


def plan_orders(target: dict[str, float], held: dict[str, dict], buying_power: float,
                min_order: float = MIN_ORDER) -> list[dict]:
    """Pure: orders that move the strategy's holdings to `target`.
    held: {ticker: {"shares", "price"}} for the rule's tickers only. Equity = buying power + those holdings.
    → [{"ticker", "side", "quantity" | "dollars", "price"}], sells first. A full exit sells every share (no
    dust); a partial trim sells shares worth the excess. Buys are scaled to the cash the plan will have."""
    value = {t: h["shares"] * h["price"] for t, h in held.items() if h.get("shares") and h.get("price")}
    equity = buying_power + sum(value.values())
    sells, buys = [], []
    for t, v in value.items():
        want = target.get(t, 0.0) * equity
        if want <= 0:
            sells.append({"ticker": t, "side": "sell", "quantity": held[t]["shares"], "price": held[t]["price"]})
        elif v - want >= min_order:
            sells.append({"ticker": t, "side": "sell", "quantity": round((v - want) / held[t]["price"], 6),
                          "price": held[t]["price"]})
    cash = buying_power + sum(o["quantity"] * o["price"] for o in sells)
    for t, w in target.items():
        gap = w * equity - value.get(t, 0.0)
        if gap >= min_order:
            buys.append({"ticker": t, "side": "buy", "dollars": gap})
    need = sum(o["dollars"] for o in buys)
    scale = min(1.0, cash / need) if need else 1.0          # never plan more than the cash on hand
    buys = [{**o, "dollars": round(o["dollars"] * scale, 2)} for o in buys]
    return sells + [o for o in buys if o["dollars"] >= min_order]


def _fetch_closes(tickers, today: date) -> dict[str, list[tuple[date, float]]]:
    """Daily adjusted closes (yfinance — the backtest's source) for the last HISTORY_DAYS. Missing → []."""
    import yfinance as yf
    from ingestion.prices import _yahoo_symbol
    out = {}
    for t in tickers:
        try:
            h = yf.Ticker(_yahoo_symbol(t)).history(start=(today - timedelta(days=HISTORY_DAYS)).isoformat(),
                                                     interval="1d", auto_adjust=True)
            out[t] = [(ix.date(), float(c)) for ix, c in h["Close"].items() if c == c]
        except Exception as e:  # noqa: BLE001
            print(f"strategy: price history failed for {t} — {e}")
            out[t] = []
    return out


def run(broker=None, verbose: bool = False, today: date | None = None, fetch=_fetch_closes) -> dict:
    """One strategy cycle against `broker` (None → the real Robinhood MCP; a PaperBroker → paper fills).
    → {"status": "not_due" | "skipped" | "rebalanced" | "partial", ...}. Never raises on a data/order problem."""
    from ingestion import robinhood_mcp
    from storage import decision_log
    from trading_guards import GuardState, OrderIntent
    mcp = broker or robinhood_mcp
    name = getattr(config, "AGENT_STRATEGY", None)
    if name not in STRATEGIES:
        return {"status": "skipped", "reason": f"no strategy configured ({name!r})"}
    today = today or date.today()
    state = _load()
    if not rebalance_due(state, today):
        return {"status": "not_due"}
    if not market_open():
        return {"status": "skipped", "reason": "market closed — fractional/dollar orders fill in regular hours"}
    acct = mcp.agentic_account_number()
    if not acct:
        return {"status": "skipped", "reason": "no agentic account"}

    _, tickers = STRATEGIES[name]
    prices = fetch(tickers, today)
    if not prices.get(tickers[0]):                             # the rule's main ticker must have history
        return {"status": "skipped", "reason": "no price history — retry next cycle"}
    target, inputs = target_weights(name, prices, today)
    held = {p["ticker"]: {"shares": p.get("shares"), "price": p.get("current_price")}
            for p in mcp.fetch_positions(acct) if p.get("ticker") in tickers}
    bp = mcp.fetch_buying_power(acct) or 0.0
    orders = plan_orders(target, held, bp)
    equity = bp + sum(h["shares"] * h["price"] for h in held.values() if h["shares"] and h["price"])
    guard = GuardState(start_equity=equity, max_single_fraction=1.0)
    month = today.strftime("%Y-%m")
    results = []
    for o in orders:
        if o["side"] == "buy":
            if any(r["side"] == "sell" and r["status"] in ("placed", "dry_run") for r in results):
                bp = mcp.fetch_buying_power(acct) or 0.0                # after sells, spend what's really there
            dollars = round(min(o["dollars"], bp), 2)
            if dollars < MIN_ORDER:
                results.append({**o, "status": "skipped", "reason": "not enough cash after sells"})
                continue
            intent = OrderIntent(ticker=o["ticker"], side="buy", dollars=dollars, reason=f"{name} rebalance",
                                 client_id=f"reb-{name}-{month}-{o['ticker']}-buy")
        else:
            intent = OrderIntent(ticker=o["ticker"], side="sell", quantity=o["quantity"],
                                 reason=f"{name} rebalance", client_id=f"reb-{name}-{month}-{o['ticker']}-sell")
        res = mcp.place_order(intent, guard, buying_power=bp, account_number=acct)
        results.append({**o, "status": res.get("status"), "reason": res.get("reason")})
        if verbose:
            print(f"  {o['side']} {o['ticker']} → {res.get('status')} ({res.get('reason')})")

    ok = all(r["status"] in ("placed", "dry_run") for r in results)
    decision_log.record("rebalance", name, "rebalanced" if ok else "partial",
                        reason=", ".join(f"{t} {w:.0%}" for t, w in target.items()) or "all cash",
                        key=f"strategy:{name}", inputs={"signal": inputs, "target": target, "equity": round(equity, 2),
                                                         "held": held, "buying_power": round(bp, 2)},
                        orders=results)
    if ok:
        _save({**state, "last_rebalance_month": month, "last_rebalance_at": datetime.now().isoformat(timespec="seconds"),
               "target": target})
    return {"status": "rebalanced" if ok else "partial", "target": target, "orders": results}


def run_paper(verbose: bool = False) -> dict:
    """`run` against the paper book (paper scope → paper state file + decision-log mode "paper")."""
    import agent_mode
    from alerts.paper_broker import PaperBroker
    with agent_mode.paper_scope():
        return {**run(PaperBroker(), verbose), "mode": "paper"}
