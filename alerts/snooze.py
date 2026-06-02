import os
import json
from datetime import datetime, timedelta

SNOOZE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "snoozed_alerts.json"
)


def load_snoozed() -> dict:
    """Returns a dict of ticker -> snooze_until datetime string."""
    if not os.path.exists(SNOOZE_FILE):
        return {}
    try:
        with open(SNOOZE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_snoozed(snoozed: dict):
    with open(SNOOZE_FILE, "w") as f:
        json.dump(snoozed, f, indent=2)


def snooze_ticker(ticker: str, days: int = 1):
    """
    Snoozes alerts for a ticker for the given number of days.
    The exit checker will skip this ticker until the snooze expires.
    """
    snoozed = load_snoozed()
    until   = (datetime.now() + timedelta(days=days)).isoformat()
    snoozed[ticker] = until
    save_snoozed(snoozed)
    print(f"Snooze: {ticker} snoozed for {days} day(s) until {until[:10]}.")


def dismiss_ticker(ticker: str):
    """
    Permanently dismisses alerts for a ticker until manually re-enabled.
    Use this when you've already acted on an alert and don't need reminders.
    """
    snooze_ticker(ticker, days=36500)  # 100 years = effectively permanent
    print(f"Snooze: {ticker} alerts dismissed permanently.")


def is_snoozed(ticker: str) -> bool:
    """Returns True if this ticker is currently snoozed."""
    snoozed = load_snoozed()
    if ticker not in snoozed:
        return False
    until = datetime.fromisoformat(snoozed[ticker])
    if datetime.now() < until:
        return True
    # Snooze expired — clean it up
    del snoozed[ticker]
    save_snoozed(snoozed)
    return False


def clear_snooze(ticker: str):
    """Re-enables alerts for a previously snoozed ticker."""
    snoozed = load_snoozed()
    if ticker in snoozed:
        del snoozed[ticker]
        save_snoozed(snoozed)
        print(f"Snooze: {ticker} alerts re-enabled.")