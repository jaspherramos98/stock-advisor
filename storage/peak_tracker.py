"""
High-water-mark tracker for option positions — enables a TRAILING take-profit.

The fixed +60% exit made the agent give back gains ("it hit +130% of value then round-tripped
to a loss"). A trailing lock instead remembers the PEAK mark of each open contract and sells
when it pulls back a set % from that peak — capturing the rip without waiting for a fixed target.

Keyed by option_id, persisted to peaks.json (repo root, gitignored). Shared by the paper AND
live exit paths so both get the trailing behavior. Pure file I/O; safe to import anywhere.
"""
from __future__ import annotations

import json
import os

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "peaks.json")


def _load() -> dict:
    try:
        with open(_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def update_peak(option_id: str, mark: float) -> float:
    """Record the latest mark and return the peak (max) seen for this contract."""
    if not option_id or mark is None:
        return mark or 0.0
    d = _load()
    peak = max(float(d.get(option_id, 0.0)), float(mark))
    if peak != d.get(option_id):
        d[option_id] = peak
        _save(d)
    return peak


def get_peak(option_id: str) -> float | None:
    v = _load().get(option_id)
    return float(v) if v is not None else None


def clear_peak(option_id: str) -> None:
    """Forget a contract's peak once it's closed (so a re-buy of the same contract starts fresh)."""
    d = _load()
    if option_id in d:
        del d[option_id]
        _save(d)
