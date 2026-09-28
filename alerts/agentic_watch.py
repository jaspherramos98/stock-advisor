"""
Watch triggers → agent candidates (judgment plan J2b).

A 'watch' recommendation carries a "buy when" entry_trigger ("Pullback to support ~$321.88", "Break
and close above $344.03"). Until now those only fed the email alerts (alerts/entry_checker.py); the
agent traded buy/short recommendations only. This turns a watch whose trigger has FIRED into a
candidate for the entry loop — and, through it, for the Sonnet judge (analysis/agent_judge.py), which
weighs the parts of the trigger a price check can't see ("on volume", "stabilization candle"):

  SOURCES     today's watch recommendations with conviction ≥ WATCH_MIN_CONVICTION (the analyst's own
              low scores stay out), plus the user's PINNED watches at any conviction (a pin is an explicit
              opt-in). Pins store no conviction → PINNED_CONVICTION for ranking/sizing.
  FIRED       reuses entry_checker's parser (_parse_triggers / _is_hit), then requires the price to still
              be NEAR the level: a breakout at most BAND_PCT above it (don't chase a move that already
              ran), a pullback at most BAND_PCT below it (a crash through support is a broken setup).
  CLOSE       a trigger that says "close"/"closes" (a daily-close condition) only counts in the last
              CLOSE_WINDOW_MINUTES of the session — the nearest a 20-min poll gets to a real close,
              instead of buying an intraday wick.
  SHARES ONLY signals are marked shares_only: agentic_stocks.route never sends them to options.
              They ride the stock budget cap and the PDT guard; the exit plan is the watch's exit_condition.
"""
from __future__ import annotations

import re
from datetime import datetime

WATCH_MIN_CONVICTION = 50
PINNED_CONVICTION = 50
BAND_PCT = 3.0
CLOSE_WINDOW_MINUTES = 30

_CLOSE_RE = re.compile(r"\bclos(?:e|es|ing)\b")


def needs_close(text: str | None) -> bool:
    return bool(_CLOSE_RE.search((text or "").lower()))


def fired(text: str | None, price: float | None, near_close: bool) -> str | None:
    """Pure: a description of the condition that fired, or None. See module docstring for the rules."""
    from alerts.entry_checker import _is_hit, _parse_triggers
    if not text or not price:
        return None
    if needs_close(text) and not near_close:
        return None
    band = BAND_PCT / 100.0
    for direction, level in _parse_triggers(text):
        if not _is_hit(direction, level, price):
            continue
        if direction == "above" and price <= level * (1 + band):
            return f"broke above ${level:g} (now ${price:g})"
        if direction == "below" and price >= level * (1 - band):
            return f"pulled back to ${level:g} (now ${price:g})"
    return None


def is_near_close(now_et: datetime | None = None) -> bool:
    """True in the last CLOSE_WINDOW_MINUTES of today's regular session (1:00 PM ET on half-days)."""
    import market_hours as mh
    now = now_et or mh._now_et()
    session = mh.market_session(now)
    if session["status"] not in ("open", "open_half_day"):
        return False
    close_minute = (13 if session.get("is_early") else 16) * 60
    minutes_left = close_minute - (now.hour * 60 + now.minute)
    return 0 <= minutes_left <= CLOSE_WINDOW_MINUTES


def candidates(recs: list[dict] | None, pinned: list[dict] | None) -> list[dict]:
    """Pure: watch ideas eligible to become buys (before the price check), one per ticker — a pin wins
    over a recommendation for the same ticker (explicit intent, and it survives pipeline reruns)."""
    out: dict[str, dict] = {}
    for p in pinned or []:
        t = (p.get("ticker") or "").upper()
        if t and p.get("trigger_text"):
            out[t] = {"ticker": t, "entry_trigger": p["trigger_text"], "conviction": PINNED_CONVICTION,
                      "exit_condition": p.get("exit_condition") or "", "risk_level": "medium",
                      "source": "pinned-watch"}
    for r in recs or []:
        t = (r.get("ticker") or "").upper()
        trig = (r.get("entry_trigger") or "").strip()
        conv = r.get("conviction") or 0
        if (not t or t in out or (r.get("direction") or "").lower() != "watch"
                or not trig or trig.lower() in ("now", "n/a") or conv < WATCH_MIN_CONVICTION):
            continue
        out[t] = {**{k: r.get(k) for k in ("company_name", "risk_level", "asset_type", "entry_rationale",
                                            "exit_condition", "catalyst_date", "highly_recommended")},
                  "ticker": t, "entry_trigger": trig, "conviction": conv, "source": "watch"}
    return list(out.values())


def watch_signals(exclude: set[str] | None = None, verbose: bool = False) -> list[dict]:
    """Live: buy signals for today's watches/pins whose trigger has fired right now."""
    from ingestion import account_reads as ar
    from storage import pipeline_cache
    from storage.entry_watch import get_pinned

    cache, _ = pipeline_cache.load_today()
    cands = [c for c in candidates((cache or {}).get("recommendations"), get_pinned())
             if c["ticker"] not in (exclude or set())]
    if not cands:
        return []
    quotes = ar.quotes([c["ticker"] for c in cands]) or {}
    close_ok = is_near_close()
    out = []
    for c in cands:
        price = (quotes.get(c["ticker"]) or {}).get("price")
        why = fired(c["entry_trigger"], price, close_ok)
        if verbose:
            print(f"  watch {c['ticker']} ({c['source']}, conv {c['conviction']}): "
                  f"{why or 'not fired'} — '{c['entry_trigger'][:70]}'")
        if why:
            out.append({**c, "direction": "buy", "shares_only": True, "trigger_fired": why})
    return out
