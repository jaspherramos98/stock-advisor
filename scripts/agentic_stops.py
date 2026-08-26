"""
Place protective GTC stops on the AGENTIC account's positions (R25, #2 driver).

Runs alerts.agentic_stops.sync_protective_stops(): reads the agentic account's holdings,
computes each ATR-sized stop from R24 structure, previews via review_equity_order, and
places a native GTC stop_market per whole-share position — DRY_RUN-safe (config.DRY_RUN=True
logs the intended stops and sends nothing).

Usage:
    venv\\Scripts\\python.exe scripts\\agentic_stops.py

Flip config.DRY_RUN=False only after inspecting the DRY_RUN log across a few positions and
you're ready to place real resting stops. Confirm-first review runs either way.
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from alerts.agentic_stops import sync_protective_stops


def main() -> int:
    print(f"== Agentic protective stops ==  (DRY_RUN={config.DRY_RUN})\n")
    results = sync_protective_stops(verbose=True)
    print("\nResults:")
    for r in results:
        print(f"  {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
