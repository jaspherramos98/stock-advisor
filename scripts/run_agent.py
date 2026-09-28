"""
Scheduled runner for the autonomous agent (Phase 2).

Called every ~20 min by the "Argus Options Agent" task (via run_agent_silent.vbs, hidden).
Safe to run around the clock — it self-gates on US market hours and exits quietly off-hours.

What it does is decided by the agent MODE (agent_mode.py — set from the dashboard Agent tab):
  - off   → nothing (not even exits).
  - paper → a paper-book cycle (no money), plus live EXITS for any real positions the agent still
            holds from an earlier live period (so switching to paper never strands them).
  - live  → a real cycle: exits, then options + share entries (shares capped by
            config.AGENT_STOCK_BUDGET_CAP).
Credit: entries also halt when the LLM ledger hits its reserve (llm_budget).

Logs actions to agent_scheduler.log (UTF-8). Routine "nothing happened" cycles log one heartbeat
line, so the file stays readable.

Manual run:   venv\\Scripts\\python.exe scripts\\run_agent.py   (acts per the current mode)
"""
import datetime as _dt
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

import agent_mode
import config
from alerts.agentic_options import run_options_agent, run_paper_agent

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


def _report(tag: str, out: dict) -> None:
    status = out.get("status")
    if status:  # halted / no-account / market-closed-live
        _log(f"[{tag}] {status}: {out.get('reason', '')}")
        return
    entries, exits = out.get("entries", []), out.get("exits", [])
    halted = out.get("entries_halted")
    if entries or exits or halted:
        _log(f"[{tag}] entries={entries} exits={exits}" + (f" [{halted}]" if halted else ""))
    elif tag != "LIVE-EXITS":   # the paper-mode exit pass is silent unless it did something
        _log(f"[{tag}] market open — no actionable signal this cycle")


def main() -> int:
    mode = agent_mode.get_mode()
    tag = mode.upper()
    if mode == "off":
        _log(f"[{tag}] agent is off — skip")
        return 0
    # Off-hours we don't run (prices move in-session) — just a one-line heartbeat.
    if not _market_open():
        _log(f"[{tag}] heartbeat — market closed, skip")
        return 0

    config.DRY_RUN = False   # this process only exists to run the cycle the mode asked for
    if mode == "live":
        _report(tag, run_options_agent(verbose=False))
    else:
        _report("LIVE-EXITS", run_options_agent(verbose=False, entries=False))
        _report(tag, run_paper_agent(verbose=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
