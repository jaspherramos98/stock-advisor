"""
Agent stock book — remembers the PLAN behind each share (or coin) position the agent opens.

The broker knows what the agentic account holds and at what average cost, but not WHY: which exit
condition the entry signal carried ("target 8% gain, stop loss at 4%") or when the agent opened it
(for the time exit). This records that at entry so the exit pass (analysis.exit_rules
.equity_exit_decision) trades the plan the position was opened on. A holding with no record (bought
by hand) falls back to STOCK_EXIT_DEFAULT.

Keyed by ticker (one agent position per name — the entry loop never stacks), one file per leg:
agent_stocks.json for shares, agent_crypto.json for coins (a coin and a stock can share a ticker, and each
leg has its own $ cap). Both gitignored. Only placements are recorded (a dry run changes nothing); a paper
cycle's placements go to the paper_* twins (agent_mode.paper_scope).
"""
from __future__ import annotations

import json
import os
from datetime import date

from agent_mode import state_file

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILE = os.path.join(_REPO, "agent_stocks.json")
_CRYPTO_FILE = os.path.join(_REPO, "agent_crypto.json")


def _path(leg: str) -> str:
    return state_file(_CRYPTO_FILE if leg == "crypto" else _FILE)


def _load(leg: str = "stock") -> dict:
    try:
        with open(_path(leg), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(book: dict, leg: str = "stock") -> None:
    try:
        with open(_path(leg), "w", encoding="utf-8") as f:
            json.dump(book, f, indent=1)
    except OSError as e:
        print(f"agent_stock_book: could not save — {e}")


def record_entry(ticker: str, exit_condition: str | None, source: str | None = None,
                 conviction: float | None = None, opened: date | None = None,
                 dollars: float | None = None, leg: str = "stock") -> None:
    """Remember the plan for a newly opened position (overwrites a stale record for the ticker).
    `dollars` = the entry cost, which the leg's $ cap (config.AGENT_STOCK/CRYPTO_BUDGET_CAP) counts."""
    book = _load(leg)
    book[ticker.upper()] = {"exit_condition": exit_condition or "", "source": source or "pipeline",
                            "conviction": conviction, "opened": (opened or date.today()).isoformat(),
                            "dollars": dollars}
    _save(book, leg)


def invested(leg: str = "stock") -> float:
    """Entry cost of every position the agent believes it holds in this leg — what the cap counts. Read
    from the book (not the broker) so an order placed seconds ago counts before its fill shows up."""
    return round(sum(float(e.get("dollars") or 0) for e in _load(leg).values()), 2)


def get_entry(ticker: str, leg: str = "stock") -> dict | None:
    return _load(leg).get(ticker.upper())


def all_entries(leg: str = "stock") -> dict:
    """{TICKER: entry} for every position the agent believes it opened in this leg."""
    return _load(leg)


def forget(ticker: str, leg: str = "stock") -> None:
    """Drop a record once the position is closed (a later re-entry starts a fresh plan)."""
    book = _load(leg)
    if book.pop(ticker.upper(), None) is not None:
        _save(book, leg)


def days_held(entry: dict | None, today: date | None = None) -> int | None:
    """Whole days since the recorded open, or None when unknown."""
    try:
        opened = date.fromisoformat((entry or {}).get("opened", ""))
    except ValueError:
        return None
    return ((today or date.today()) - opened).days
