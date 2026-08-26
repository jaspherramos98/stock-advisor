"""
Run one cycle of the autonomous options agent (Phase 2) on the AGENTIC pilot account.

DRY_RUN-safe: with config.DRY_RUN=True it logs the option orders it WOULD place (entries +
exits) and sends nothing. Flip config.DRY_RUN=False to trade for real (during market hours).

Kill switch: create a file `agentic_halt.flag` in the repo root to halt immediately.

Usage:
    venv\\Scripts\\python.exe scripts\\agentic_options.py
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from alerts.agentic_options import run_options_agent


def main() -> int:
    print(f"== Autonomous options agent ==  (DRY_RUN={config.DRY_RUN})")
    out = run_options_agent(verbose=True)
    print("\nSummary:")
    if out.get("status"):
        print("  ", out)
    else:
        for e in out.get("exits", []):
            print("  EXIT ", e)
        for e in out.get("entries", []):
            print("  ENTRY", e)
        if not out.get("exits") and not out.get("entries"):
            print("   nothing actionable this cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
