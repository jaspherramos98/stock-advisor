"""
Entry-watch storage — the "buy when" side of alerting.

Holds two things:
  1. `chat_suggestions` — the LAST set of buy/watch ideas Argus chat gave the user.
     Each new suggestion set REPLACES the previous one (the user asked for alerts on
     the *last* suggestion, not a growing pile of stale ones).
  2. `notified` — a {ticker|source: date} record so an entry trigger only emails once
     per day, mirroring how exit_checker uses `alerts_sent`.

Recommendation-sourced triggers are NOT stored here — they're read live from
pipeline_cache.json by alerts/entry_checker.py. Only chat output needs persisting,
because chat replies are otherwise ephemeral.
"""
import json
import os
from datetime import datetime

ENTRY_WATCH_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "entry_watch.json"
)

_EMPTY = {"chat_suggestions": [], "notified": {}}


def load_entry_watch() -> dict:
    """Loads the entry-watch file, returning a valid empty structure on any failure."""
    if not os.path.exists(ENTRY_WATCH_FILE):
        return dict(_EMPTY)
    try:
        with open(ENTRY_WATCH_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(_EMPTY)
        data.setdefault("chat_suggestions", [])
        data.setdefault("notified", {})
        return data
    except Exception as e:
        print(f"Entry watch load error: {e}")
        return dict(_EMPTY)


def save_entry_watch(data: dict):
    try:
        with open(ENTRY_WATCH_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Entry watch save error: {e}")


def set_chat_suggestions(suggestions: list[dict]) -> int:
    """
    Replaces the stored chat suggestions with the latest set (the user wants alerts on
    Argus chat's LAST suggestion). Each item: {ticker, action, trigger_text}.
    Clears any 'chat' notify records so a re-suggested ticker can alert again.
    Returns how many were stored.
    """
    data = load_entry_watch()
    stamped = []
    for s in suggestions:
        ticker = (s.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        stamped.append({
            "ticker":       ticker,
            "action":       s.get("action", "watch"),
            "trigger_text": s.get("trigger_text", ""),
            "created_at":   datetime.now().isoformat(),
        })
    data["chat_suggestions"] = stamped
    data["notified"] = {k: v for k, v in data.get("notified", {}).items()
                        if not k.endswith("|chat")}
    save_entry_watch(data)
    return len(stamped)


def get_chat_suggestions() -> list[dict]:
    return load_entry_watch().get("chat_suggestions", [])


def was_notified_today(ticker: str, source: str) -> bool:
    """True if this ticker/source already fired an entry alert today."""
    today = datetime.now().strftime("%Y-%m-%d")
    return load_entry_watch().get("notified", {}).get(f"{ticker}|{source}") == today


def mark_notified(ticker: str, source: str):
    """Records that this ticker/source alerted today, so it won't repeat."""
    data = load_entry_watch()
    data.setdefault("notified", {})[f"{ticker}|{source}"] = datetime.now().strftime("%Y-%m-%d")
    save_entry_watch(data)
