"""
Stock (share) leg of the autonomous agent — routing, sizing and entries (S2) + exits (S3).

ROUTING (approved plan, conviction-based): the aggressive options leg takes the strongest ideas —
highly_recommended or conviction ≥ OPTION_CONVICTION — plus every bearish idea (shares can't be
shorted on this account, so bearish = puts). Other buys become share positions (the core). An
options-routed buy with no affordable contract falls back to shares rather than being dropped. Crypto
never routes to shares (the MCP can't trade it). The stock leg is always on; in live mode its total
entry cost is capped by config.AGENT_STOCK_BUDGET_CAP.

SIZING: Argus's own pyramid (calculator.portfolio.calculate_allocations — 20/55/25 risk tiers,
conviction within a tier, 40% single-name cap) over ALL of the cycle's buy signals, on the AGENTIC
buying power — one shared pool, so the pipeline's MAIN-account dollar sizes are never reused here.

ORDERS: every share buy is a DOLLAR-based market order (fractional-friendly, fills immediately in
regular hours). A resting limit that doesn't fill would leave the name looking un-held next cycle and
invite a duplicate buy; the orders here are small enough that market slippage is noise.

EXITS (run_exits, every cycle): ONLY positions the agent opened (storage.agent_stock_book) — a share
you bought by hand in the agentic account is never touched. Each is judged by
analysis.exit_rules.equity_exit_decision against the plan it was opened on (its exit_condition), with
a trailing peak from storage.peak_tracker (key "eq:TICKER"). A close = cancel any resting stop FIRST
(open sell orders reserve the shares, so the sell would be refused), wait for the cancel to confirm,
then a market sell of the whole position. While held, the WHOLE-share part also carries a resting GTC
stop_market at the plan's stop (entry × (1 − stop%)) so it's protected when the PC is off; a fractional
remainder can't carry one (broker rule) and relies on the poll.
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


def stock_room(invested: float, cap: float | None) -> float:
    """$ the stock leg may still commit under config.AGENT_STOCK_BUDGET_CAP (inf when uncapped)."""
    return float("inf") if cap is None else max(0.0, round(cap - (invested or 0.0), 2))


def budget_cap() -> float | None:
    return getattr(config, "AGENT_STOCK_BUDGET_CAP", None)


def route(sig: dict) -> str | None:
    """'option' | 'stock' | 'crypto' | None (not tradeable by the agent). Crypto buys go to the crypto leg
    (alerts/agentic_crypto.py); crypto is never shorted and has no options here."""
    direction = (sig.get("direction") or "").lower()
    if (sig.get("asset_type") or "").lower() == "crypto":
        return "crypto" if direction == "buy" else None
    if direction == "short":
        return "option"                                   # bearish → puts (no share shorting)
    if direction != "buy":
        return None
    if sig.get("shares_only"):                            # a fired watch trigger — never an option
        return "stock" if can_hold_shares(sig) else None
    if sig.get("highly_recommended") or _conviction(sig) >= OPTION_CONVICTION:
        return "option"
    return "stock" if can_hold_shares(sig) else None


def can_hold_shares(sig: dict) -> bool:
    return (sig.get("asset_type") or "stock").lower() != "crypto" and (sig.get("direction") or "").lower() == "buy"


def rank(signals: list[dict]) -> list[dict]:
    """Best idea first across both legs: conviction high→low (stable, so source priority breaks ties)."""
    return sorted(signals, key=_conviction, reverse=True)


def size_buys(signals: list[dict], buying_power: float, eligible=None) -> dict[str, float]:
    """{TICKER: dollars} — the pyramid over every buy signal `eligible` accepts (default: share buys), on
    the given buying power. The crypto leg passes its own filter + cap."""
    import math
    from calculator.portfolio import calculate_allocations
    from trading_guards import MAX_SINGLE_ORDER_FRACTION
    eligible = eligible or can_hold_shares
    buys = [dict(s, direction="buy") for s in signals if eligible(s) and s.get("ticker")]
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


def enter(mcp, acct, sig: dict, dollars: float, avail: float, state, verbose: bool,
          room: float = float("inf")) -> tuple[dict | None, float]:
    """Place (or dry-run) one share buy. Returns (result row | None if nothing to place, $ committed).
    `avail` = buying power left this cycle (what the order guard checks); `room` = what the stock
    budget cap still allows. A placement records the entry's plan + cost in agent_stock_book (the paper
    copy inside a paper cycle)."""
    intent = plan_buy(sig, dollars, min(avail, room))
    if intent is None:
        if verbose:
            print(f"  {sig.get('ticker')}: stock size ${dollars or 0:.2f} below the $1 minimum (skip)")
        return None, 0.0
    res = mcp.place_order(intent, state, buying_power=avail, account_number=acct)
    row = {"ticker": intent.ticker, "leg": "stock", "dollars": intent.dollars,
           **{k: res[k] for k in ("status", "reason")}}
    if res["status"] == "placed":
        from storage.agent_stock_book import record_entry
        record_entry(intent.ticker, sig.get("exit_condition"), sig.get("source"), sig.get("conviction"),
                     dollars=intent.dollars)
    return row, (intent.dollars if res["status"] in ("placed", "dry_run") else 0.0)


# --- exits (S3) ---------------------------------------------------------------------------------

CANCEL_WAIT_SECONDS = 8   # how long to wait for a cancelled stop to release its shares before selling


def protective_stop_price(avg_cost: float, stop_pct: float | None) -> float | None:
    """The plan's stop as a price: entry × (1 − stop%), to the cent. None when unknowable."""
    if not avg_cost or avg_cost <= 0 or not stop_pct or stop_pct <= 0:
        return None
    return round(avg_cost * (1 - stop_pct / 100.0), 2)


def decide_exit(pos: dict, entry: dict | None, peak: float | None, today=None) -> tuple[str, str]:
    """Pure: ('hold'|'close', reason) for one agent share position against its recorded plan."""
    from analysis.exit_rules import equity_exit_decision, parse_exit_condition
    from storage.agent_stock_book import days_held
    rule = parse_exit_condition((entry or {}).get("exit_condition"))
    return equity_exit_decision(pos.get("avg_cost"), pos.get("current_price"), rule,
                                peak_price=peak, days_held=days_held(entry, today))


def _is_paper(mcp) -> bool:
    return getattr(mcp, "PAPER", False)


def _open_sell_stops(mcp, acct) -> dict[str, dict]:
    """{TICKER: {order_id, tif}} for working stop sells on the agentic account ({} if unreadable, and
    always {} for the paper broker — paper has no resting orders)."""
    from alerts.agentic_stops import _open_stops
    if _is_paper(mcp):
        return {}
    try:
        return {t.upper(): s for t, s in _open_stops(mcp._call_tool("get_equity_orders", {
            "account_number": acct})).items()}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_stocks: open-orders read failed — {e}")
        return {}


def _cancel_and_wait(mcp, acct, order_id: str) -> bool:
    """Cancel a resting order and wait until the broker reports it no longer working (its shares are
    released). DRY_RUN logs only. False = couldn't confirm → the caller must NOT sell this cycle."""
    import time
    if config.DRY_RUN:
        print(f"[DRY_RUN CANCEL] resting stop {order_id}")
        return True
    try:
        mcp._call_tool("cancel_equity_order", {"account_number": acct, "order_id": order_id})
        deadline = time.monotonic() + CANCEL_WAIT_SECONDS
        while time.monotonic() < deadline:
            rows = (mcp._data(mcp._call_tool("get_equity_orders", {
                "account_number": acct, "order_id": order_id})) or {}).get("orders") or []
            if rows and (rows[0].get("state") or "").lower() in ("cancelled", "filled", "rejected", "failed"):
                return True
            time.sleep(1)
    except Exception as e:  # noqa: BLE001
        print(f"agentic_stocks: cancel of {order_id} failed — {e}")
    return False


def close_position(mcp, acct, pos: dict, equity: float, reason: str, state=None,
                   stops: dict | None = None) -> dict:
    """Sell ONE share position in full: cancel its resting stop and wait for the broker to confirm
    (else 'deferred' — the stop still reserves the shares), then a market sell. A placed close forgets
    the agent's plan + peak. Shared by the exit pass and the dashboard's Close-now button."""
    from datetime import date
    from storage import agent_stock_book as book, peak_tracker
    from trading_guards import GuardState
    t = pos["ticker"].upper()
    stop = (stops if stops is not None else _open_sell_stops(mcp, acct)).get(t)
    if stop and stop.get("order_id") and not _cancel_and_wait(mcp, acct, stop["order_id"]):
        return {"close": t, "leg": "stock", "status": "deferred",
                "reason": "resting stop not confirmed cancelled — retry next cycle"}
    intent = OrderIntent(ticker=t, side="sell", quantity=pos["shares"], reason=f"exit: {reason}",
                         client_id=f"stkexit-{t}-{date.today().isoformat()}")
    res = mcp.place_order(intent, state or GuardState(start_equity=equity), buying_power=equity,
                          account_number=acct)
    from storage import decision_log as dlog
    avg, px = pos.get("avg_cost"), pos.get("current_price")
    dlog.record("exit", t, res["status"], reason, key=f"eq:{t}", entry_id=dlog.last_entry_id(f"eq:{t}"),
                inputs={"avg_cost": avg, "price": px, "shares": pos.get("shares")},
                pnl_pct=round((px - avg) / avg * 100, 1) if avg and px else None, detail=res.get("reason"))
    if res["status"] == "placed":
        book.forget(t)
        peak_tracker.clear_peak(f"eq:{t}")
    return {"close": t, "leg": "stock", "reason": reason, "status": res["status"],
            "detail": res.get("reason")}


def run_exits(mcp, acct, equity: float, verbose: bool) -> list[dict]:
    """Exit pass over the agent's share positions (see module docstring). Rows use the options exit
    shape ({"close": TICKER, ...}) so the entry pass's anti-churn exclude covers both legs."""
    import math
    from storage import agent_stock_book as book, peak_tracker
    from trading_guards import GuardState

    entries = book.all_entries()
    if not entries:
        return []                                   # nothing the agent owns → no reads at all
    positions = [p for p in mcp.fetch_positions(acct) if (p.get("ticker") or "").upper() in entries]
    if not positions:
        # Either every agent position is gone (a resting stop filled / sold by hand) or the read
        # failed — fetch_positions returns [] for both, so leave the book alone rather than wipe it.
        return []
    stops = _open_sell_stops(mcp, acct)
    state = GuardState(start_equity=equity)
    results: list[dict] = []
    from datetime import date
    today = date.today()

    for p in positions:
        t = p["ticker"].upper()
        try:
            entry = entries.get(t)
            peak = peak_tracker.update_peak(f"eq:{t}", p.get("current_price"))
            action, reason = decide_exit(p, entry, peak, today)
            if verbose:
                print(f"  {t}: {p['shares']} sh @ avg {p['avg_cost']} now {p['current_price']} "
                      f"peak {peak} → {action} ({reason})")
            if action == "close":
                results.append(close_position(mcp, acct, p, equity, reason, state=state, stops=stops))
                continue
            whole = math.floor(p["shares"])
            stop_pct = parse_stop_pct(entry)
            stop_px = protective_stop_price(p.get("avg_cost"), stop_pct)
            # J3: the rules hold — if news / an unusual move / imminent earnings fired, have the judge
            # review the thesis (SHADOW: logged only, the position is untouched).
            from datetime import datetime
            from analysis.holding_review import review
            from storage.agent_stock_book import days_held
            opened = (entry or {}).get("opened")
            review({"ticker": t, "key": f"eq:{t}", "kind": "stock", "price": p.get("current_price"),
                    "entry": p.get("avg_cost"), "now": p.get("current_price"), "pnl_pct": p.get("pnl_pct"),
                    "days_held": days_held(entry, today), "plan": (entry or {}).get("exit_condition"),
                    "current_stop": stop_px, "peak": peak,
                    "opened_at": datetime.fromisoformat(opened) if opened else None},
                   holdings={x["ticker"].upper() for x in positions}, verbose=verbose)
            # Holding: make sure the whole-share part rests on a GTC stop at the plan's stop (live only —
            # paper has no resting orders; the poll's hard stop above covers it).
            if (not _is_paper(mcp) and whole >= 1 and stop_px and t not in stops
                    and stop_px < (p.get("current_price") or 0)):
                intent = OrderIntent(ticker=t, side="sell", quantity=whole, order_type="stop",
                                     stop_price=stop_px, time_in_force="gtc",
                                     reason=f"protective stop {stop_pct:g}% under entry",
                                     client_id=f"stkstop-{t}-{stop_px}")
                res = mcp.place_order(intent, state, buying_power=equity, account_number=acct)
                from storage import decision_log as dlog
                dlog.record("stop", t, res["status"], intent.reason, key=f"eq:{t}",
                            inputs={"avg_cost": p.get("avg_cost"), "stop_price": stop_px, "shares": whole})
                results.append({"stop": t, "leg": "stock", "stop_price": stop_px,
                                **{k: res[k] for k in ("status", "reason")}})
        except Exception as e:  # noqa: BLE001 — one bad position must not abort the rest
            print(f"agentic_stocks: exit pass failed for {t} — {e}")
    return results


def parse_stop_pct(entry: dict | None) -> float:
    """The plan's stop % for a recorded entry (STOCK_EXIT_DEFAULT when the plan has none)."""
    from analysis.exit_rules import STOCK_EXIT_DEFAULT, parse_exit_condition
    return parse_exit_condition((entry or {}).get("exit_condition"))["stop_pct"] or STOCK_EXIT_DEFAULT["stop_pct"]
