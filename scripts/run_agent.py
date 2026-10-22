"""
Scheduled runner for the autonomous agent (Phase 2).

Called every ~20 min by the "Argus Options Agent" task (via run_agent_silent.vbs, hidden).
Safe to run around the clock — it self-gates on US market hours and exits quietly off-hours.

What it does is decided by the agent MODE (agent_mode.py — set from the dashboard Agent tab):
  - off   → nothing (not even exits).
  - paper → a paper-book cycle (no money), plus live EXITS for any real positions the agent still
            holds from an earlier live period (so switching to paper never strands them).
  - live  → a real cycle: exits, then options + share + crypto entries (shares/crypto capped by
            config.AGENT_STOCK_BUDGET_CAP / AGENT_CRYPTO_BUDGET_CAP).
Outside market hours (crypto trades 24/7): only the crypto EXIT pass runs — live, plus paper in paper mode.
It makes no calls at all while the agent holds no coins.
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
from alerts.agentic_options import run_crypto_exits, run_options_agent, run_paper_agent, run_paper_crypto_exits

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
    elif not tag.endswith("EXITS"):   # exit-only passes are silent unless they did something
        _log(f"[{tag}] market open — no actionable signal this cycle")


def main() -> int:
    mode = agent_mode.get_mode()
    tag = mode.upper()
    if mode == "off":
        _log(f"[{tag}] agent is off — skip")
        return 0
    config.DRY_RUN = False   # this process only exists to run the cycle the mode asked for
    # Off-hours: stocks/options don't trade, but crypto does 24/7 — run ONLY the crypto exit pass (free when the
    # agent holds no coins), then a one-line heartbeat.
    if not _market_open():
        _report("LIVE-CRYPTO-EXITS", run_crypto_exits(verbose=False))
        if mode == "paper":
            _report("PAPER-CRYPTO-EXITS", run_paper_crypto_exits(verbose=False))
        _log(f"[{tag}] heartbeat — market closed (crypto exits only)")
        return 0

    # config.AGENT_STRATEGY set → the systematic strategy opens positions (monthly rebalance); the old pick-based
    # cycle runs exits-only so whatever it opened winds down.
    strategy = getattr(config, "AGENT_STRATEGY", None)
    legacy = f"{tag}-EXITS" if strategy else tag       # an exits-only pass logs only when it does something
    if mode == "live":
        _report(legacy, run_options_agent(verbose=False, entries=not strategy))
        if strategy:
            from strategy import rebalancer
            _report_strategy(tag, rebalancer.run())
    else:
        _report("LIVE-EXITS", run_options_agent(verbose=False, entries=False))
        _report(legacy, run_paper_agent(verbose=False, entries=not strategy))
        if strategy:
            from strategy import rebalancer
            _report_strategy(tag, rebalancer.run_paper())
    return 0


def _report_strategy(tag: str, out: dict) -> None:
    """Silent on the ~600 cycles a month with nothing due; one line when it rebalances, skips or fails."""
    status = out.get("status")
    if status == "not_due":
        return
    if status in ("rebalanced", "partial"):
        orders = [f"{o['side']} {o['ticker']} {o.get('dollars') or o.get('quantity')} → {o['status']}"
                  for o in out.get("orders", [])]
        _log(f"[{tag}-STRATEGY] {status}: target={out.get('target')} orders={orders or 'none needed'}")
    else:
        _log(f"[{tag}-STRATEGY] {status}: {out.get('reason', '')}")


if __name__ == "__main__":
    raise SystemExit(main())
