"""
Dashboard rerun benchmark — measures what a click / tab switch actually costs.

Streamlit re-executes dashboard/app.py top-to-bottom on every interaction, so the time of a
warm rerun IS the click latency the user feels. This runs the app headless (Streamlit AppTest)
once cold, then several warm reruns, and prints the timings plus any app exceptions.

Uses LIVE data sources (MCP, yfinance, Google Sheets) — so it needs network + a valid MCP token,
and it is NOT part of CI. Use it for before/after numbers on performance changes.

Usage:
    venv\\Scripts\\python.exe scripts\\bench_dashboard.py            # 3 warm reruns
    venv\\Scripts\\python.exe scripts\\bench_dashboard.py 5          # 5 warm reruns
"""
import os
import statistics
import sys
import time

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streamlit.testing.v1 import AppTest


def main() -> int:
    warm_runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    at = AppTest.from_file("dashboard/app.py", default_timeout=300)

    t = time.perf_counter()
    at.run()
    cold = time.perf_counter() - t

    warm = []
    for _ in range(warm_runs):
        t = time.perf_counter()
        at.run()
        warm.append(time.perf_counter() - t)

    print("\n== Dashboard rerun benchmark ==")
    print(f"cold load        : {cold:6.2f}s")
    print(f"warm rerun (click): median {statistics.median(warm):.2f}s  "
          f"min {min(warm):.2f}s  max {max(warm):.2f}s  (n={warm_runs})")
    print(f"exceptions       : {len(at.exception)}")
    for e in at.exception[:5]:
        print("  EXC:", str(e.message)[:200])
    return 1 if at.exception else 0


if __name__ == "__main__":
    raise SystemExit(main())
