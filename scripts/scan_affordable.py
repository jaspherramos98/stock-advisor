"""
Scan which tickers have an option contract AFFORDABLE for the agentic pilot.

For each candidate it prices a representative ~30-DTE, ~4% OTM call and put and flags whether
one contract (ask × 100) fits the agentic account's buying power. Read-only — places nothing.

Candidates: tickers on the command line, else today's pipeline recs + chat 'Buy' suggestions +
a preset of liquid cheap-underlying names (where $25-ish options actually exist).

Usage:
    venv\\Scripts\\python.exe scripts\\scan_affordable.py                 (auto candidates)
    venv\\Scripts\\python.exe scripts\\scan_affordable.py F SOFI PLTR NIO  (explicit)
"""
import json
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

# Liquid, low-priced underlyings where sub-$25 contracts commonly exist.
_CHEAP_PRESET = ["F", "SOFI", "PLTR", "NIO", "T", "BAC", "AAL", "SNAP", "PLUG", "RIVN",
                 "HOOD", "INTC", "CCL", "WBD", "MARA", "RIOT"]


def _auto_candidates() -> list[str]:
    out = []
    try:
        recs = (json.load(open(os.path.join(_REPO, "pipeline_cache.json"), encoding="utf-8"))
                or {}).get("recommendations", [])
        out += [r.get("ticker") for r in recs if r.get("ticker")]
    except Exception:  # noqa: BLE001
        pass
    try:
        from storage.entry_watch import get_chat_suggestions
        out += [s.get("ticker") for s in get_chat_suggestions() if s.get("ticker")]
    except Exception:  # noqa: BLE001
        pass
    out += _CHEAP_PRESET
    seen, uniq = set(), []
    for t in out:
        t = (t or "").upper()
        if t and t not in seen:
            seen.add(t); uniq.append(t)
    return uniq


def main() -> int:
    from ingestion import robinhood_mcp as mcp
    from ingestion import account_reads as ar
    from ingestion import options_data as od

    if not mcp.is_available():
        print("USE_MCP off / not logged in."); return 1
    acct = mcp.agentic_account_number()
    bp = (mcp.fetch_buying_power(acct) or 0.0) if acct else 0.0
    print(f"Agentic buying power: ${bp:.2f}\n")

    tickers = [t.upper() for t in sys.argv[1:]] or _auto_candidates()[:16]
    print(f"{'TICKER':<7} {'SPOT':>9}  ~4%OTM CALL (30D)  [+ = 1 contract <= ${bp:.0f}]", flush=True)
    print("-" * 60, flush=True)
    affordable = []
    for t in tickers:
        spot = (ar.quotes([t]).get(t) or {}).get("price")
        if not spot:
            print(f"{t:<7} {'—':>9}  (no quote)", flush=True)
            continue
        c = od.select_contract(t, spot, "call", 21, 45, 4.0, max_premium=None)
        if not c:
            print(f"{t:<7} {spot:>9.2f}  none", flush=True)
            continue
        fits = c["cost_1x"] <= bp
        if fits:
            affordable.append(t)
        print(f"{t:<7} {spot:>9.2f}  {'+' if fits else 'x'} {c['strike']:g}C "
              f"${c['cost_1x']:.0f} exp {c['expiration'][5:]}", flush=True)

    _aff = ", ".join(affordable) or "none"
    print(f"\nAffordable now (<= ${bp:.0f}): {_aff}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
