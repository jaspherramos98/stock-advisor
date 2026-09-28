"""
Agent stock book — remembers the PLAN behind each share position the agent opens.

The broker knows what the agentic account holds and at what average cost, but not WHY: which exit
condition the entry signal carried ("target 8% gain, stop loss at 4%") or when the agent opened it
(for the time exit). This records that at entry so the exit pass (analysis.exit_rules
.equity_exit_decision) trades the plan the position was opened on. A holding with no record (bought
by hand) falls back to STOCK_EXIT_DEFAULT.

Keyed by ticker (one agent position per name — the entry loop never stacks). Persisted to
agent_stocks.json (repo root, gitignored). Only LIVE placements are recorded; a dry run changes nothing.
"""
from __future__ import annotations

import json
import os
from datetime import date

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_stocks.json")


def _load() -> dict:
    try:
        with open(_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(book: dict) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(book, f, indent=1)
    except OSError as e:
        print(f"agent_stock_book: could not save — {e}")


def record_entry(ticker: str, exit_condition: str | None, source: str | None = None,
                 conviction: float | None = None, opened: date | None = None) -> None:
    """Remember the plan for a newly opened position (overwrites a stale record for the ticker)."""
    book = _load()
    book[ticker.upper()] = {"exit_condition": exit_condition or "", "source": source or "pipeline",
                            "conviction": conviction, "opened": (opened or date.today()).isoformat()}
    _save(book)


def get_entry(ticker: str) -> dict | None:
    return _load().get(ticker.upper())


def all_entries() -> dict:
    """{TICKER: entry} for every position the agent believes it opened."""
    return _load()


def forget(ticker: str) -> None:
    """Drop a record once the position is closed (a later re-entry starts a fresh plan)."""
    book = _load()
    if book.pop(ticker.upper(), None) is not None:
        _save(book)


def days_held(entry: dict | None, today: date | None = None) -> int | None:
    """Whole days since the recorded open, or None when unknown."""
    try:
        opened = date.fromisoformat((entry or {}).get("opened", ""))
    except ValueError:
        return None
    return ((today or date.today()) - opened).days
