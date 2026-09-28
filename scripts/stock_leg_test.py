"""
S5 live test of the agent's STOCK leg — open a few small share positions inside the stock budget cap,
then let the normal agent cycle manage them (exit pass, resting stops, Sell now).

What it proves on real fills: review-first dollar buys, the stock book recording each plan, the exit
pass judging them every cycle, resting GTC stops on any whole-share part, and (when an exit fires or
you click Sell now) cancel-stop → confirm → sell.

Picks: today's share-eligible buy signals first (best conviction first); if there are fewer than
needed, padded with broad ETFs (FALLBACK) so the multi-position test still happens. The cap
(config.AGENT_STOCK_BUDGET_CAP, default $20 here) is split EQUALLY across the positions — a mechanics
test, not a sizing test. The number of positions also respects the PDT day-trade budget.

Usage (repo root):
    venv\\Scripts\\python.exe scripts\\stock_leg_test.py            # DRY: shows what it would buy
    venv\\Scripts\\python.exe scripts\\stock_leg_test.py --live     # REAL orders (market hours only)
    venv\\Scripts\\python.exe scripts\\stock_leg_test.py --verify   # read-only report of the test book

Automatic one-shot at the next market open: create `stock_leg_test.pending` in the repo root. The
scheduled "Argus Options Agent" runner (scripts/run_agent.py) runs this ONCE, LIVE, on its first armed
in-hours cycle and deletes the marker BEFORE buying (a crash can never repeat the buys).
"""
from __future__ import annotations

import math
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

MAX_POSITIONS = 3
DEFAULT_CAP = 20.0
FALLBACK = ("SPY", "QQQ", "IWM")          # broad, liquid, fractional — the lowest-risk padding
FALLBACK_EXIT = "target 8% gain, stop loss at 4%"


def pick(signals: list[dict], held: set[str], n: int) -> list[dict]:
    """Pure: up to n share-eligible buy signals (best first), padded with FALLBACK ETFs."""
    from alerts import agentic_stocks as stk
    out = [s for s in stk.rank(signals)
           if stk.can_hold_shares(s) and (s.get("ticker") or "").upper() not in held][:n]
    taken = {(s.get("ticker") or "").upper() for s in out}
    for t in FALLBACK:
        if len(out) >= n:
            break
        if t not in taken and t not in held:
            out.append({"ticker": t, "direction": "buy", "conviction": None, "source": "stock-leg-test",
                        "exit_condition": FALLBACK_EXIT})
    return out


def split_evenly(room: float, n: int) -> float:
    """Pure: dollars per position, floored to the cent (0 when nothing fits)."""
    return math.floor(room / n * 100) / 100 if n > 0 and room > 0 else 0.0


def run_test(live: bool, verbose: bool = True) -> dict:
    from alerts import agentic_stocks as stk
    from alerts.agentic_options import _entry_budget, _halted, _market_open, _signals
    from ingestion import robinhood_mcp as mcp
    from storage.agent_stock_book import invested
    from trading_guards import GuardState

    if _halted():
        return {"status": "halted", "reason": "kill switch present"}
    acct = mcp.agentic_account_number() if mcp.is_available() else None
    if not acct:
        return {"status": "skipped", "reason": "no agentic account / MCP off"}
    if live and not _market_open():
        return {"status": "skipped", "reason": "market closed — share orders place in regular hours"}

    prev = config.DRY_RUN
    config.DRY_RUN = not live            # scoped to this run; restored below
    try:
        bp = mcp.fetch_buying_power(acct) or 0.0
        room = stk.stock_room(invested(), stk.budget_cap() or DEFAULT_CAP)
        pdt = _entry_budget(mcp, acct, bp, verbose)
        n = MAX_POSITIONS if pdt is None else min(MAX_POSITIONS, pdt)
        picks = pick(_signals(), stk.held_tickers(mcp, acct), n)
        each = split_evenly(min(room, bp), len(picks))
        if verbose:
            print(f"== Stock-leg test ==  LIVE={live} | BP ${bp:.2f} | cap room ${room:.2f} | "
                  f"PDT slots {pdt} | {len(picks)} × ${each:.2f}")
        state = GuardState(start_equity=bp)
        rows, avail = [], bp
        for sig in picks:
            row, spent = stk.enter(mcp, acct, sig, each, avail, state, verbose)
            avail -= spent
            if row:
                rows.append(row)
        return {"status": "done", "live": live, "each": each, "entries": rows}
    finally:
        config.DRY_RUN = prev


def verify() -> None:
    """Read-only: what the agent holds from the test/stock leg, its plan, decision and resting stops."""
    from alerts import agentic_stocks as stk
    from ingestion import robinhood_mcp as mcp
    from storage import agent_stock_book as book
    from storage.peak_tracker import get_peak
    acct = mcp.agentic_account_number()
    plans = book.all_entries()
    stops = stk._open_sell_stops(mcp, acct)
    print(f"Stock book: {len(plans)} plan(s), ${book.invested():.2f} committed "
          f"(cap {stk.budget_cap()})")
    for p in mcp.fetch_positions(acct):
        t = p["ticker"].upper()
        plan = plans.get(t)
        decision = stk.decide_exit(p, plan, get_peak(f"eq:{t}")) if plan else ("—", "not agent-managed")
        stop = stops.get(t)
        print(f"  {t}: {p['shares']:g} sh avg ${p['avg_cost']:.2f} now ${p['current_price']:.2f} "
              f"({p['pnl_pct']:+.1f}%) | agent: {decision[0]} ({decision[1]}) | resting stop: "
              f"{'yes (' + stop['tif'] + ')' if stop else 'no'}")
    for t in set(plans) - {p['ticker'].upper() for p in mcp.fetch_positions(acct)}:
        print(f"  {t}: in the book but not held (unfilled, sold, or its stop filled)")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify()
    else:
        print(run_test(live="--live" in sys.argv))
