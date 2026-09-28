"""
Headless pipeline run — what the dashboard's "Run pipeline" button does, with no UI and no prompts.

Used by market_open.bat (the "Argus Market Open" task, 6:30 AM PT). Runs ingestion → scoring → Claude
analysis → live prices, then writes today's pipeline_cache.json through storage.pipeline_cache (the
same writer as the dashboard), which is what the agent trades from and what the dashboard loads.

`main.py` can't do this job: it prompts for a budget with input() (a hidden scheduled run would hang
forever) and never writes the cache (so the agent never saw its output).

Skips NYSE holidays/weekends (no LLM spend on a closed market) unless --force.
Asset classes match the dashboard defaults: stocks on, ETFs/crypto off (flags below override).
Exit code: 0 = cache written (or closed-market skip), 1 = failure (logged by the caller).

    venv\\Scripts\\python.exe scripts\\run_pipeline.py [--force] [--etfs] [--crypto]
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")   # pipeline prints non-ASCII
    except Exception:  # noqa: BLE001
        pass


def is_trading_day(today=None) -> bool:
    import market_hours as mh
    day = today or mh._now_et().date()
    return day.weekday() < 5 and day not in mh.nyse_holidays(day.year)


def main(argv: list[str]) -> int:
    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    if "--force" not in argv and not is_trading_day():
        print(f"[{stamp}] run_pipeline: market closed today — skipped (use --force to run anyway)")
        return 0
    from dotenv import load_dotenv
    load_dotenv()
    from ingestion.prices import fetch_prices
    from main import run_ingestion_and_analysis
    from storage import pipeline_cache

    try:
        recs = run_ingestion_and_analysis(include_stocks=True, include_etfs="--etfs" in argv,
                                          include_crypto="--crypto" in argv)
        prices = fetch_prices([r["ticker"] for r in recs if r.get("ticker")]) if recs else {}
        pipeline_cache.save(recs, prices, datetime.now().strftime("%B %d, %Y at %I:%M %p"))
    except Exception as e:  # noqa: BLE001 — report + non-zero exit; the caller logs it
        print(f"[{stamp}] run_pipeline: FAILED — {e}")
        return 1
    buys = [r["ticker"] for r in recs if r.get("direction") in ("buy", "short")]
    print(f"[{stamp}] run_pipeline: {len(recs)} recommendations cached; buy/short: {buys or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
