"""
Today's pipeline result on disk — pipeline_cache.json (+ pipeline_cache_backup.json).

ONE writer/reader for every producer and consumer: the dashboard's "Run pipeline" button, the headless
morning run (scripts/run_pipeline.py), and — by format — the agent (alerts/agentic_options._signals) and
the entry checker. The `date` field is load-bearing: readers ignore a cache from another day.
Before this module existed the dashboard was the only writer, so `main.py` runs never reached the agent.

HISTORY: the cache is overwritten every run, so every run is ALSO appended to pipeline_history/<date>.jsonl
(gitignored, one JSON line per run, ~7 KB each). Nothing else keeps past recommendations — this is what a
later judge replay / pipeline scorecard reads (`read_history`). An archive failure never blocks the cache.

An empty run never replaces today's non-empty cache (`save` returns False); `logged_run()` copies a
dashboard run's output to pipeline.log (gitignored) so the reason for an empty run isn't lost.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(_REPO, "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(_REPO, "pipeline_cache_backup.json")
HISTORY_DIR = os.path.join(_REPO, "pipeline_history")
LOG_FILE = os.path.join(_REPO, "pipeline.log")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def save(recommendations: list, prices: dict, last_run: str) -> bool:
    """Write today's result; False = kept the existing cache instead. An EMPTY run never replaces a
    non-empty one from today: the analyst turns every failure (bad JSON, API error) into [], and on
    2026-10-05 one such run wiped the day's 12 ideas, leaving the agent blind. The empty run is still
    archived. Otherwise the previous cache (if it had recommendations) becomes the backup, so a run
    that dies mid-write can't leave the day with nothing."""
    existing = _try_load(CACHE_FILE)
    run = {"date": _today(), "last_run": last_run, "recommendations": recommendations, "prices": prices}
    if not recommendations and existing and existing.get("date") == run["date"] \
            and existing.get("recommendations"):
        _archive(run)
        return False
    if existing and existing.get("recommendations"):
        try:
            with open(CACHE_BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f)
        except OSError:  # a failed backup must not block writing the new cache
            pass
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(run, f)
    _archive(run)
    return True


@contextmanager
def logged_run():
    """Copy everything printed during a pipeline run into pipeline.log (appended, timestamped header).
    The dashboard runs hidden (argus_silent.vbs), so without this an empty run's reason — printed by the
    analyst — is lost. The console still gets the output. Log failures never break the run."""
    try:
        log = open(LOG_FILE, "a", encoding="utf-8")
        log.write(f"\n==== {datetime.now():%Y-%m-%d %H:%M:%S} dashboard pipeline run ====\n")
    except OSError:
        yield
        return
    with log, redirect_stdout(_Tee(sys.stdout, log)):
        yield


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, text):
        for s in self._streams:
            try:
                s.write(text)
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except (OSError, ValueError):
                pass


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
