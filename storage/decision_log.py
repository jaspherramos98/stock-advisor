"""
Agent decision log — every decision the autonomous agent makes, with the inputs behind it (plan J0).

Append-only JSON Lines at agent_decisions.jsonl (repo root, gitignored). One record per decision:

    {"id", "ts", "mode": "live"|"dry", "kind": "entry"|"exit"|"stop"|"halt",
     "ticker", "key", "action", "reason", "inputs": {...}, ...extra}

  - kind "entry": EVERY candidate the entry loop considers — taken ("placed"/"dry_run") or not
    ("skip" with why: already held, no affordable contract, PDT budget, below the $1 minimum, a guard
    or broker-review rejection). Silent skips were invisible before this.
  - kind "exit": a close the exit pass fired, with entry → exit price and P&L %, linked to the entry
    record that opened it (`entry_id`, looked up by `key`: the option_id, or "eq:TICKER" for shares).
  - kind "stop": a protective stop placed for a share position.

This is the measuring stick for the judgment plan: the J4 shadow review compares what the mechanical
rules did against what the judge would have done, trade by trade. Records are never rewritten; a
failure to write never blocks trading (it prints and moves on).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_decisions.jsonl")


def _mode() -> str:
    import config
    return "dry" if getattr(config, "DRY_RUN", True) else "live"


def record(kind: str, ticker: str | None, action: str, reason: str = "", key: str | None = None,
           inputs: dict | None = None, **extra) -> str:
    """Append one decision; returns its id. Never raises."""
    rec = {"id": uuid.uuid4().hex[:12], "ts": datetime.now().isoformat(timespec="seconds"),
           "mode": _mode(), "kind": kind, "ticker": (ticker or "").upper() or None,
           "key": key, "action": action, "reason": reason or "", "inputs": inputs or {}, **extra}
    try:
        with open(_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError as e:
        print(f"decision_log: could not write — {e}")
    return rec["id"]


def read(limit: int | None = None, mode: str | None = None) -> list[dict]:
    """Records oldest → newest (the last `limit` if given), optionally only one mode. Skips bad lines."""
    out: list[dict] = []
    try:
        with open(_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if mode is None or rec.get("mode") == mode:
                    out.append(rec)
    except OSError:
        return []
    return out[-limit:] if limit else out


def last_entry_id(key: str, mode: str | None = None) -> str | None:
    """Id of the most recent TAKEN entry for `key` — links an exit to the decision that opened it."""
    for rec in reversed(read(mode=mode)):
        if rec.get("kind") == "entry" and rec.get("key") == key and rec.get("action") in ("placed", "dry_run"):
            return rec["id"]
    return None


def signal_inputs(sig: dict) -> dict:
    """The signal fields worth keeping with a decision (not the whole rec — rationale text is long)."""
    keep = ("direction", "conviction", "confidence_score", "highly_recommended", "risk_level", "source",
            "asset_type", "exit_condition", "entry_trigger", "catalyst_date", "rsi", "days_to_earnings")
    return {k: sig.get(k) for k in keep if sig.get(k) is not None}
