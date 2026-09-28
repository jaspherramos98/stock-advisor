"""
Crypto leg of the autonomous agent (K2) — the stock leg's twin for coins.

ROUTING: agentic_stocks.route sends every crypto BUY here (crypto is never shorted, and has no options).
SIZING: Argus's pyramid over the cycle's crypto buys, on min(buying power, config.AGENT_CRYPTO_BUDGET_CAP) —
its own cap, separate from the stock cap, from the same shared buying-power pool. 0 = leg off.
ORDERS: dollar-based MARKET buys via robinhood_mcp.place_crypto_order (preview-first) — or the paper broker.
EXITS (run_exits, every cycle): only coins the agent opened (agent_stock_book leg="crypto") → the same
equity_exit_decision as shares (hard stop → ATR trail → target → time limit) against the recorded plan, with a
trailing peak keyed "cr:TICKER". A close = market sell of the whole quantity. No resting stops (poll only).
PDT does not apply to crypto, so crypto entries never use the day-trade budget.

Honest cost: Robinhood crypto is market-maker priced with a ~2% bid/ask spread, so every round trip starts
~2% down — the leg is here to be SEEN working (paper first), not because it has a proven edge.
"""
from __future__ import annotations

import config
from trading_guards import OrderIntent

LEG = "crypto"


def can_hold_crypto(sig: dict) -> bool:
    return ((sig.get("asset_type") or "").lower() == "crypto"
            and (sig.get("direction") or "").lower() == "buy" and bool(sig.get("ticker")))


def budget_cap() -> float | None:
    return getattr(config, "AGENT_CRYPTO_BUDGET_CAP", None)


def peak_key(ticker: str) -> str:
    return f"cr:{(ticker or '').upper()}"


def size_buys(signals: list[dict], buying_power: float) -> dict[str, float]:
    """{TICKER: dollars} for the cycle's crypto buys, on the smaller of buying power and the crypto cap."""
    from alerts.agentic_stocks import size_buys as pyramid
    cap = budget_cap()
    pool = min(buying_power, cap) if cap is not None else buying_power
    return pyramid(signals, pool, eligible=can_hold_crypto) if pool > 0 else {}


def plan_buy(sig: dict, dollars: float, avail: float) -> OrderIntent | None:
    """Pure: the crypto buy intent, or None below the $1 dollar-order minimum."""
    import math
    from datetime import date
    from ingestion.robinhood_mcp import MIN_DOLLAR_ORDER
    ticker = (sig.get("ticker") or "").upper()
    amount = math.floor(min(dollars or 0.0, avail) * 100) / 100
    if not ticker or amount < MIN_DOLLAR_ORDER:
        return None
    return OrderIntent(ticker=ticker, side="buy", dollars=amount,
                       reason=f"crypto ({sig.get('source') or 'pipeline'}, conv {sig.get('conviction')})",
                       client_id=f"cr-{ticker}-{date.today().isoformat()}")


def held_tickers(mcp, acct) -> set[str]:
    """Coins the agentic account already holds — never stack a second entry."""
    try:
        return {(p.get("ticker") or "").upper() for p in mcp.fetch_crypto_positions(acct) if p.get("ticker")}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_crypto: positions read failed — {e}")
        return set()


def enter(mcp, acct, sig: dict, dollars: float, avail: float, state, verbose: bool,
          room: float = float("inf")) -> tuple[dict | None, float]:
    """Place (or dry-run) one crypto buy. Returns (result row | None, $ committed). A placement records the
    plan + cost in the crypto book (the paper copy inside a paper cycle)."""
    intent = plan_buy(sig, dollars, min(avail, room))
    if intent is None:
        if verbose:
            print(f"  {sig.get('ticker')}: crypto size ${dollars or 0:.2f} below the $1 minimum (skip)")
        return None, 0.0
    res = mcp.place_crypto_order(intent, state, buying_power=avail, account_number=acct)
    row = {"ticker": intent.ticker, "leg": LEG, "dollars": intent.dollars, **{k: res[k] for k in ("status", "reason")}}
    if res["status"] == "placed":
        from storage.agent_stock_book import record_entry
        record_entry(intent.ticker, sig.get("exit_condition"), sig.get("source"), sig.get("conviction"),
                     dollars=intent.dollars, leg=LEG)
    return row, (intent.dollars if res["status"] in ("placed", "dry_run") else 0.0)


def close_position(mcp, acct, pos: dict, equity: float, reason: str, state=None) -> dict:
    """Market-sell a whole coin position. A placed close forgets the plan + peak."""
    from datetime import date
    from storage import agent_stock_book as book, decision_log as dlog, peak_tracker
    from trading_guards import GuardState
    t = pos["ticker"].upper()
    intent = OrderIntent(ticker=t, side="sell", quantity=pos["shares"], reason=f"exit: {reason}",
                         client_id=f"crexit-{t}-{date.today().isoformat()}")
    res = mcp.place_crypto_order(intent, state or GuardState(start_equity=equity), buying_power=equity,
                                 account_number=acct)
    avg, px = pos.get("avg_cost"), pos.get("current_price")
    dlog.record("exit", t, res["status"], reason, key=peak_key(t), entry_id=dlog.last_entry_id(peak_key(t)),
                leg=LEG, inputs={"avg_cost": avg, "price": px, "quantity": pos.get("shares")},
                pnl_pct=round((px - avg) / avg * 100, 1) if avg and px else None, detail=res.get("reason"))
    if res["status"] == "placed":
        book.forget(t, leg=LEG)
        peak_tracker.clear_peak(peak_key(t))
    return {"close": t, "leg": LEG, "reason": reason, "status": res["status"], "detail": res.get("reason")}


def run_exits(mcp, acct, equity: float, verbose: bool) -> list[dict]:
    """Exit pass over the agent's coins. Rows use the shared exit shape ({"close": TICKER, ...})."""
    from datetime import date
    from alerts.agentic_stocks import decide_exit
    from storage import agent_stock_book as book, peak_tracker
    from trading_guards import GuardState

    entries = book.all_entries(leg=LEG)
    if not entries:
        return []                                   # nothing the agent owns → no reads at all
    positions = [p for p in mcp.fetch_crypto_positions(acct) if (p.get("ticker") or "").upper() in entries]
    if not positions:
        return []                                   # gone, or the read failed — never wipe the book on []
    state, today, results = GuardState(start_equity=equity), date.today(), []
    for p in positions:
        t = p["ticker"].upper()
        try:
            peak = peak_tracker.update_peak(peak_key(t), p.get("current_price"))
            action, reason = decide_exit(p, entries.get(t), peak, today)
            if verbose:
                print(f"  {t} (crypto): {p['shares']} @ avg {p['avg_cost']} now {p['current_price']} "
                      f"peak {peak} → {action} ({reason})")
            if action == "close":
                results.append(close_position(mcp, acct, p, equity, reason, state=state))
        except Exception as e:  # noqa: BLE001 — one bad position must not abort the rest
            print(f"agentic_crypto: exit pass failed for {t} — {e}")
    return results
