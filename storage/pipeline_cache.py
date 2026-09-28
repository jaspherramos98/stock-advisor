"""
Today's pipeline result on disk — pipeline_cache.json (+ pipeline_cache_backup.json).

ONE writer/reader for every producer and consumer: the dashboard's "Run pipeline" button, the headless
morning run (scripts/run_pipeline.py), and — by format — the agent (alerts/agentic_options._signals) and
the entry checker. The `date` field is load-bearing: readers ignore a cache from another day.
Before this module existed the dashboard was the only writer, so `main.py` runs never reached the agent.

HISTORY: the cache is overwritten every run, so every run is ALSO appended to pipeline_history/<date>.jsonl
(gitignored, one JSON line per run, ~7 KB each). Nothing else keeps past recommendations — this is what a
later judge replay / pipeline scorecard reads (`read_history`). An archive failure never blocks the cache.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(_REPO, "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(_REPO, "pipeline_cache_backup.json")
HISTORY_DIR = os.path.join(_REPO, "pipeline_history")


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
    run = {"date": _today(), "last_run": last_run, "recommendations": recommendations, "prices": prices}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(run, f)
    _archive(run)


def _archive(run: dict) -> None:
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        rec = {**run, "archived_at": datetime.now().isoformat(timespec="seconds")}
        with open(os.path.join(HISTORY_DIR, f"{run['date']}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except (OSError, TypeError, ValueError) as e:
        print(f"pipeline_cache: could not archive the run — {e}")


def read_history(since: date | None = None, until: date | None = None) -> list[dict]:
    """Every archived run (oldest first), optionally within [since, until]. Skips unreadable lines."""
    try:
        names = sorted(n for n in os.listdir(HISTORY_DIR) if n.endswith(".jsonl"))
    except OSError:
        return []
    out = []
    for name in names:
        day = name[:-len(".jsonl")]
        if (since and day < since.isoformat()) or (until and day > until.isoformat()):
            continue
        try:
            with open(os.path.join(HISTORY_DIR, name), encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out


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
