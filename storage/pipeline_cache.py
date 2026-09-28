"""
Today's pipeline result on disk — pipeline_cache.json (+ pipeline_cache_backup.json).

ONE writer/reader for every producer and consumer: the dashboard's "Run pipeline" button, the headless
morning run (scripts/run_pipeline.py), and — by format — the agent (alerts/agentic_options._signals) and
the entry checker. The `date` field is load-bearing: readers ignore a cache from another day.
Before this module existed the dashboard was the only writer, so `main.py` runs never reached the agent.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(_REPO, "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(_REPO, "pipeline_cache_backup.json")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def save(recommendations: list, prices: dict, last_run: str) -> None:
    """Write today's result. The previous cache (if it had recommendations) becomes the backup, so a
    run that dies mid-write can't leave the day with nothing."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("recommendations"):
                with open(CACHE_BACKUP_FILE, "w", encoding="utf-8") as f:
                    json.dump(existing, f)
        except Exception:  # noqa: BLE001 — a corrupt old cache must not block writing the new one
            pass
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": _today(), "last_run": last_run,
                   "recommendations": recommendations, "prices": prices}, f)


def _try_load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_today() -> tuple[dict | None, bool]:
    """(today's cache or None, came_from_backup). A cache from another day is never returned."""
    data = _try_load(CACHE_FILE)
    if data and data.get("date") == _today():
        return data, False
    backup = _try_load(CACHE_BACKUP_FILE)
    if backup and backup.get("date") == _today():
        return backup, True
    return None, False
