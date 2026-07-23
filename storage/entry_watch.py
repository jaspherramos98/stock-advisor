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

_EMPTY = {"chat_suggestions": [], "pinned": [], "notified": {}}


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
        data.setdefault("pinned", [])
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


# ── Pinned watches ────────────────────────────────────────────────────────────
# A recommendation's entry_trigger normally lives only in pipeline_cache.json, which
# is overwritten on every pipeline run — so a watch you're waiting on silently
# disappears. Pinning copies that trigger here, where it survives reruns until the
# user removes it. The pinned level is kept EXACTLY as pinned (it's the level the
# user chose); it is not refreshed when the ticker reappears in a later run.

def get_pinned() -> list[dict]:
    return load_entry_watch().get("pinned", [])


def is_pinned(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    return any(p.get("ticker") == t for p in get_pinned())


def add_pinned(ticker: str, company_name: str, trigger_text: str,
               exit_condition: str = "") -> bool:
    """Pins a watch so the entry checker keeps monitoring it across pipeline reruns.
    No-op (returns False) if the ticker is already pinned or has no trigger."""
    t = (ticker or "").strip().upper()
    if not t or not (trigger_text or "").strip() or is_pinned(t):
        return False
    data = load_entry_watch()
    data.setdefault("pinned", []).append({
        "ticker":         t,
        "company_name":   company_name or t,
        "trigger_text":   trigger_text.strip(),
        "exit_condition": exit_condition or "",
        "pinned_at":      datetime.now().strftime("%Y-%m-%d"),
    })
    save_entry_watch(data)
    return True


def remove_pinned(ticker: str) -> bool:
    """Unpins a watch and clears its notify record so re-pinning can alert again."""
    t = (ticker or "").strip().upper()
    data = load_entry_watch()
    before = len(data.get("pinned", []))
    data["pinned"] = [p for p in data.get("pinned", []) if p.get("ticker") != t]
    data["notified"] = {k: v for k, v in data.get("notified", {}).items()
                        if k != f"{t}|pinned"}
    save_entry_watch(data)
    return len(data["pinned"]) < before


def was_notified_today(ticker: str, source: str) -> bool:
    """True if this ticker/source already fired an entry alert today."""
    today = datetime.now().strftime("%Y-%m-%d")
    return load_entry_watch().get("notified", {}).get(f"{ticker}|{source}") == today


def mark_notified(ticker: str, source: str):
    """Records that this ticker/source alerted today, so it won't repeat."""
    data = load_entry_watch()
    data.setdefault("notified", {})[f"{ticker}|{source}"] = datetime.now().strftime("%Y-%m-%d")
    save_entry_watch(data)
