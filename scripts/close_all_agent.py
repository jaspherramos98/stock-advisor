"""
Force-close EVERY open option position on the AGENTIC pilot account — a clean-slate reset.

Unlike scripts/agentic_options.py (which only closes positions whose exit rule fired), this
sells-to-close ALL open option positions unconditionally, at the current bid (marketable limit).
It mirrors the proven close path in alerts.agentic_options._run_exits: review-first,
trading-guards vetted, `is_error` honored, DRY_RUN-safe.

DRY_RUN-safe: with config.DRY_RUN=True it PRINTS the sell-to-close orders it would place and
sends nothing. Flip config.DRY_RUN=False (edit config.py) to actually sell — during market hours.

Kill switch: `agentic_halt.flag` in the repo root halts before placing anything.

Usage:
    venv\\Scripts\\python.exe scripts\\close_all_agent.py          # preview (DRY_RUN, places nothing)
    venv\\Scripts\\python.exe scripts\\close_all_agent.py --live    # SELL FOR REAL (market hours only)

`--live` scopes DRY_RUN=False to just this run (no global config edit), so the rest of the
system stays DRY_RUN-safe afterward.
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from trading_guards import OptionOrderIntent, GuardState
from alerts.agentic_options import _halted, _market_open, _HALT_FLAG


def main() -> int:
    from ingestion import robinhood_mcp as mcp
    from ingestion import mcp_auth, options_data as od

    if "--live" in sys.argv:
        config.DRY_RUN = False  # scope real placement to this run only; global default stays safe

    if _halted():
        print(f"HALTED — kill switch present ({_HALT_FLAG}). Remove it to run.")
        return 1
    if not mcp.is_available():
        print("SKIP — USE_MCP off.")
        return 1
    acct = mcp.agentic_account_number()
    if not acct:
        print("SKIP — no agentic account.")
        return 1
    if not config.DRY_RUN and not _market_open():
        print("SKIP — market closed. Options close in regular hours only.")
        return 1

    bp = mcp.fetch_buying_power(acct) or 0.0
    print(f"== Close ALL agentic option positions ==  DRY_RUN={config.DRY_RUN} | acct {acct} | BP ${bp:.2f}")

    data = mcp._data(mcp._unwrap_tool_result(
        mcp_auth.call_tool("get_option_positions", {"account_number": acct, "nonzero": True})))
    rows = data.get("positions", []) if isinstance(data, dict) else []
    if not rows:
        print("  nothing open — already flat.")
        return 0

    state = GuardState(start_equity=bp)
    from storage import peak_tracker
    closed, failed = 0, 0
    for p in rows:
        try:
            oid = p.get("option_id") or p.get("option") or p.get("id")
            qty = int(float(p.get("quantity") or 0))
            if not oid or qty < 1:
                continue
            quote = od.fetch_quote(oid) or {}
            bid = float(quote.get("bid_price") or quote.get("mark_price") or 0)
            exp = p.get("expiration_date") or p.get("expiration")
            sym = p.get("chain_symbol") or "?"
            intent = OptionOrderIntent(
                underlying=sym, option_id=oid, right=(p.get("type") or "call").lower(),
                side="sell", position_effect="close", quantity=qty,
                price=round(bid, 2), direction="credit", expiration=exp,
                reason="manual reset: close all", client_id=f"closeall-{oid}",
            )
            print(f"  {sym} x{qty} @ bid {bid} -> sell-to-close (${bid*100*qty:.0f})")
            res = mcp.place_option_order(intent, state, buying_power=bp, account_number=acct)
            print("     ->", res)
            if res.get("status") in ("placed", "dry_run"):
                peak_tracker.clear_peak(oid)  # forget the high-water mark; fresh start
                closed += 1
            else:
                failed += 1
        except Exception as e:  # noqa: BLE001 — one bad position must not abort the rest
            print(f"  ERROR closing a position — {e}")
            failed += 1

    print(f"\nSummary: {closed} close order(s) sent, {failed} failed. "
          f"{'(DRY_RUN — nothing real placed)' if config.DRY_RUN else ''}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
