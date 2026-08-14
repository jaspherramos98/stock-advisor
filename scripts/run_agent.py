"""
Scheduled runner for the autonomous options agent (Phase 2).

Called every ~20 min by the "Argus Options Agent" task (via run_agent_silent.vbs, hidden).
Safe to run around the clock — it self-gates on US market hours and exits quietly off-hours.

LIVE vs DRY is decided by an ARM flag, NOT by editing code:
  - agent_live.arm present  → LIVE (real option orders).  ← you create this to "let it fly"
  - agent_live.arm absent   → DRY_RUN (logs what it would trade, places nothing).  ← default
Kill switch: agentic_halt.flag present → halt immediately (checked inside run_options_agent).
Credit: entries also halt when the LLM ledger hits its reserve (llm_budget).

Logs actions to agent_scheduler.log (UTF-8). Routine "nothing happened" cycles are not logged,
so the file stays readable.

Manual test (DRY):   venv\\Scripts\\python.exe scripts\\run_agent.py
Arm LIVE:            echo armed > agent_live.arm      (delete the file to disarm)
"""
import datetime as _dt
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import config
from alerts.agentic_options import run_options_agent

_ARM = os.path.join(_REPO, "agent_live.arm")
_LOG = os.path.join(_REPO, "agent_scheduler.log")


def _log(msg: str) -> None:
    line = f"[{_dt.datetime.now():%Y-%m-%d %H:%M:%S %p PT}] {msg}\n"
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        _trim()
    except OSError:
        pass


def _trim(keep: int = 500) -> None:
    """Keep only the last `keep` lines so a heartbeat-per-cycle log can't grow unbounded."""
    try:
        with open(_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep:])
    except OSError:
        pass


def _market_open() -> bool:
    try:
        import market_hours as mh
        return mh.market_session()["status"] in ("open", "open_half_day")
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    armed = os.path.exists(_ARM)
    mode = "LIVE" if armed else "DRY"

    # Heartbeat every cycle so the log always exists + ticks (visible liveness). Off-hours we
    # don't run the agent (options fill in regular hours) — just a one-line skip.
    if not _market_open():
        _log(f"[{mode}] heartbeat — market closed, skip")
        return 0

    config.DRY_RUN = not armed
    out = run_options_agent(verbose=False)

    status = out.get("status")
    if status:  # halted / no-account / market-closed-live
        _log(f"[{mode}] {status}: {out.get('reason', '')}")
        return 0

    entries, exits = out.get("entries", []), out.get("exits", [])
    halted = out.get("entries_halted")
    if entries or exits or halted:
        _log(f"[{mode}] entries={entries} exits={exits}" + (f" [{halted}]" if halted else ""))
    else:
        _log(f"[{mode}] market open — no actionable signal this cycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
