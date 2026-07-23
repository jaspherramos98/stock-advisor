"""
Entry ("buy when") checker — the bullish counterpart to alerts/exit_checker.py.

Exit checker answers "should I get OUT?". This answers "is the thing I was waiting
for to get IN actually here?" — so a watch idea doesn't quietly pass its trigger
while the user isn't looking.

Two candidate sources:
  1. RECOMMENDATIONS — watch recs in pipeline_cache.json. Their `entry_trigger` is
     already anchored to real computed support/resistance (R7), e.g.
     "Pulls back to support near $314.91 ..., or breaks above $328.04 on volume".
  2. ARGUS CHAT — the last buy/watch action list Argus chat gave, persisted by the
     /chat proxy into storage/entry_watch.py.

A trigger string can carry BOTH a pullback (below) and a breakout (above) price;
either one firing is a hit. Owned tickers are skipped (you're already in), and each
ticker/source alerts at most once per day.

Pure price math — no LLM tokens.
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re

from ingestion.prices import fetch_prices
from market_hours import market_session
from storage.entry_watch import get_chat_suggestions, was_notified_today, mark_notified

CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pipeline_cache.json"
)

# Words that mark the price after them as a BREAKOUT (buy when price rises to it)
# vs a PULLBACK (buy when price falls to it). Whichever keyword sits closest before
# the "$" wins, so a two-sided trigger string parses into two separate conditions.
_ABOVE_KW = ("above", "break", "breaks", "breakout", "over", "exceeds", "reclaims")
_BELOW_KW = ("support", "pull", "pulls", "pullback", "dip", "dips", "below",
             "near", "back to", "falls", "retest", "drops")

_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def _parse_triggers(text: str) -> list[tuple]:
    """
    Extract every ("above"|"below", price) condition from an entry-trigger string.

    Direction is decided per price by whichever keyword appears CLOSEST before it, so
    "Pulls back to support near $314.91 ... or breaks above $328.04" yields
    [("below", 314.91), ("above", 328.04)]. Prices with no directional keyword in the
    preceding window default to "above" (a breakout read), which is the common phrasing.
    """
    if not text:
        return []

    out = []
    low = text.lower()
    for m in _PRICE_RE.finditer(text):
        try:
            price = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if price <= 0:
            continue

        window = low[max(0, m.start() - 60):m.start()]
        above_at = max((window.rfind(k) for k in _ABOVE_KW), default=-1)
        below_at = max((window.rfind(k) for k in _BELOW_KW), default=-1)
        direction = "below" if below_at > above_at else "above"
        out.append((direction, round(price, 2)))

    return out


def _is_hit(direction: str, trigger_price: float, current_price: float) -> bool:
    """A breakout fires when price reaches/exceeds it; a pullback when price falls to it."""
    if direction == "above":
        return current_price >= trigger_price
    return current_price <= trigger_price


def _candidates_from_recommendations() -> list[dict]:
    """Watch recs (with a real entry_trigger) out of today's pipeline cache."""
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        recs = cache.get("recommendations", []) if isinstance(cache, dict) else (cache or [])
    except Exception as e:
        print(f"Entry checker: could not read pipeline cache — {e}")
        return []

    out = []
    for r in recs:
        ticker = (r.get("ticker") or "").strip().upper()
        trigger = (r.get("entry_trigger") or "").strip()
        # 'buy'/'short' recs say "now" — nothing to wait for. Only watches have a trigger.
        if not ticker or not trigger or trigger.lower() in ("now", "n/a", ""):
            continue
        out.append({
            "ticker":       ticker,
            "company_name": r.get("company_name", ticker),
            "trigger_text": trigger,
            "source":       "recommendation",
        })
    return out


def _candidates_from_chat() -> list[dict]:
    """The last buy/watch ideas Argus chat gave (persisted by the /chat proxy)."""
    out = []
    for s in get_chat_suggestions():
        trigger = (s.get("trigger_text") or "").strip()
        if not s.get("ticker") or not trigger:
            continue
        out.append({
            "ticker":       s["ticker"],
            "company_name": s["ticker"],
            "trigger_text": trigger,
            "source":       "chat",
        })
    return out


def run_entry_checks() -> list[dict]:
    """
    Main entry point. Checks every pending entry trigger (recommendations + last chat
    suggestion) against live prices and returns alert dicts for the ones that fired.
    Alert shape matches exit_checker so notifier/run_checks can handle both.
    """
    candidates = _candidates_from_recommendations() + _candidates_from_chat()
    if not candidates:
        print("Entry checker: no pending entry triggers.")
        return []

    # Don't suggest buying something already held — that's the exit checker's job now.
    try:
        from storage.positions import get_open_positions
        owned = {(p.get("ticker") or "").upper() for p in get_open_positions()}
    except Exception:
        owned = set()

    pending = [c for c in candidates if c["ticker"] not in owned]
    if not pending:
        print("Entry checker: all entry candidates are already owned.")
        return []

    tickers = sorted({c["ticker"] for c in pending})
    print(f"Entry checker: checking {len(pending)} entry trigger(s) across {len(tickers)} ticker(s)...")
    prices = fetch_prices(tickers)

    alerts = []
    for c in pending:
        ticker = c["ticker"]
        if was_notified_today(ticker, c["source"]):
            continue

        price_data = prices.get(ticker)
        if not price_data:
            continue
        current = price_data["price"]

        for direction, trigger_price in _parse_triggers(c["trigger_text"]):
            if not _is_hit(direction, trigger_price, current):
                continue
            label = "broke above" if direction == "above" else "pulled back to"
            src = "Argus chat" if c["source"] == "chat" else "today's recommendations"
            alerts.append({
                "ticker":        ticker,
                "alert_type":    "entry_trigger",
                "message":       (
                    f"🎯 BUY TRIGGER for {ticker}. Price {label} your entry level: "
                    f"${current:.2f} vs trigger ${trigger_price:.2f}. "
                    f"From {src} — \"{c['trigger_text']}\". "
                    f"Check the setup still holds before entering."
                ),
                "current_price": current,
                "trigger_price": trigger_price,
                "direction":     direction,
                "source":        c["source"],
                "entry_trigger": c["trigger_text"],
            })
            mark_notified(ticker, c["source"])
            break  # one alert per ticker/source per day

    # Session awareness — same treatment exit alerts get, so the user never acts on an
    # unexecutable signal (a trigger hit after hours can't be bought until the open).
    if alerts:
        sess = market_session()
        for a in alerts:
            a["market_status"] = sess["status"]
            a["actionable_now"] = sess["is_open"]
            if sess["action_note"]:
                a["message"] += f"\n⏰ {sess['action_note']}"

    print(f"Entry checker: {len(alerts)} entry trigger(s) fired.")
    return alerts


if __name__ == "__main__":
    fired = run_entry_checks()
    if fired:
        print("\n--- Entry triggers hit ---")
        for a in fired:
            print(f"\n[{a['source'].upper()}] {a['ticker']}")
            print(f"  {a['message']}")
    else:
        print("No entry triggers hit.")
