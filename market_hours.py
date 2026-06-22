"""
Shared US (NYSE) market-session logic: holidays, half-day early closes, and the
current session status in Eastern time.

Single source of truth used by:
  - the dashboard header badge + chatbot context (timing advice to the session), and
  - the exit-alert checker (so it never tells you to act when you can't).

Built on pandas' holiday primitives (Good Friday from Easter, NYSE weekend-observance
rules) — no extra dependency. Holiday/early-close tables are cached per year. Nothing
here raises for the caller: `market_session()` degrades to weekday/time logic if the
holiday calendar can't be built.
"""

from functools import lru_cache
from datetime import datetime, time as _time


@lru_cache(maxsize=8)
def nyse_holidays(year: int) -> dict:
    """
    {date: holiday_name} of NYSE full-closure holidays for `year`. Good Friday is
    derived from Easter; weekend observance matches NYSE (Sat→Fri for most; New Year's
    only shifts Sun→Mon, since NYSE stays open the preceding Friday when Jan 1 is a
    Saturday). Cached per year.
    """
    from pandas.tseries.holiday import (
        AbstractHolidayCalendar, Holiday, nearest_workday, sunday_to_monday,
        USMartinLutherKingJr, USPresidentsDay, USMemorialDay, USLaborDay,
        USThanksgivingDay, GoodFriday,
    )

    class _NYSECal(AbstractHolidayCalendar):
        rules = [
            Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
            USMartinLutherKingJr,
            USPresidentsDay,
            GoodFriday,
            USMemorialDay,
            Holiday("Juneteenth", month=6, day=19, start_date="2021-06-18", observance=nearest_workday),
            Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
            USLaborDay,
            USThanksgivingDay,
            Holiday("Christmas", month=12, day=25, observance=nearest_workday),
        ]

    series = _NYSECal().holidays(start=f"{year}-01-01", end=f"{year}-12-31", return_name=True)
    return {ts.date(): name for ts, name in series.items()}


@lru_cache(maxsize=8)
def nyse_early_closes(year: int) -> frozenset:
    """
    Dates NYSE closes early (1:00 PM ET) for `year`: the day after Thanksgiving,
    Christmas Eve, and July 3 — each only when it's a weekday and not itself a full
    holiday. Cached per year.
    """
    import datetime as _dt
    import pandas as pd
    from pandas.tseries.holiday import USThanksgivingDay

    closes = set()
    tg = USThanksgivingDay.dates(f"{year}-01-01", f"{year}-12-31")
    if len(tg):
        closes.add((tg[0] + pd.Timedelta(days=1)).date())  # Friday after Thanksgiving
    for m, d in ((12, 24), (7, 3)):
        try:
            day = _dt.date(year, m, d)
            if day.weekday() < 5:
                closes.add(day)
        except ValueError:
            pass
    closes -= set(nyse_holidays(year))  # a full holiday is never an "early close"
    return frozenset(closes)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()  # fall back to local time if tz data is unavailable


def market_session(now: datetime = None) -> dict:
    """
    Structured snapshot of the current US market session. Returns a dict:
      status:        open | open_half_day | pre_market | after_hours |
                     after_hours_half_day | closed_weekend | closed_holiday | closed
      is_open:       True only during the live regular (or half-day) session
      tradeable:     True during regular OR extended (pre/after) hours — i.e. an order
                     could be placed now (extended = thin liquidity)
      holiday_name:  str or None
      is_early:      half-day today
      stamp:         "Monday 2026-06-22 10:05 AM ET"
      badge:         compact UI badge string, e.g. "🟢 Market open"
      line:          full human sentence for the chatbot context
      action_note:   guidance for acting on an exit right now ("", or a caveat)
    """
    now = now or _now_et()
    stamp = now.strftime("%A %Y-%m-%d %I:%M %p ET")
    t, wd, today = now.time(), now.weekday(), now.date()

    def out(status, badge, line, is_open=False, tradeable=False, holiday_name=None,
            is_early=False, action_note=""):
        return {
            "status": status, "is_open": is_open, "tradeable": tradeable,
            "holiday_name": holiday_name, "is_early": is_early, "stamp": stamp,
            "badge": badge, "line": line, "action_note": action_note,
        }

    if wd >= 5:
        return out("closed_weekend", "🔴 Closed (weekend)",
                   f"MARKET STATUS: CLOSED (weekend) — {stamp}. US stocks/ETFs are closed until the "
                   f"next weekday session (9:30 AM ET). Crypto trades 24/7.",
                   action_note="Market is closed for the weekend — you can't act until the next "
                               "session opens (9:30 AM ET). Re-confirm at the open.")

    try:
        holidays     = nyse_holidays(today.year)
        early_closes = nyse_early_closes(today.year)
    except Exception:
        holidays, early_closes = {}, frozenset()

    if today in holidays:
        name = holidays[today]
        return out("closed_holiday", f"🔴 Closed ({name})",
                   f"MARKET STATUS: CLOSED ({name} — market holiday) — {stamp}. US stocks/ETFs are "
                   f"closed today. Crypto trades 24/7.", holiday_name=name,
                   action_note=f"Market is closed today ({name}) — you can't act until the next "
                               f"session. Re-confirm at the next open.")

    is_early = today in early_closes
    closed_note = ("Market is closed right now — you can't act until the next session opens "
                   "(9:30 AM ET). Re-confirm at the open; overnight prices can revert.")
    ext_note = ("Only extended-hours trading is available right now (thin liquidity) — use a limit "
                "order, or wait for the regular session.")

    if _time(4, 0) <= t < _time(9, 30):
        extra = " NOTE: half-day — early close at 1:00 PM ET." if is_early else ""
        return out("pre_market", "🟡 Pre-market",
                   f"MARKET STATUS: PRE-MARKET — {stamp}. Regular session opens 9:30 AM ET; pre-market "
                   f"liquidity is thin and gaps are common.{extra}",
                   tradeable=True, is_early=is_early, action_note=ext_note)

    if is_early:
        if _time(9, 30) <= t < _time(13, 0):
            return out("open_half_day", "🟢 Open (½ day · 1 PM ET close)",
                       f"MARKET STATUS: OPEN — HALF DAY — {stamp}. US stocks/ETFs trading now but close "
                       f"early at 1:00 PM ET (not 4:00).",
                       is_open=True, tradeable=True, is_early=True)
        if _time(13, 0) <= t < _time(20, 0):
            return out("after_hours_half_day", "🟡 After-hours (½ day)",
                       f"MARKET STATUS: AFTER-HOURS (half-day) — {stamp}. The shortened session closed at "
                       f"1:00 PM ET; after-hours liquidity is thin.",
                       tradeable=True, is_early=True, action_note=ext_note)
        return out("closed", "🔴 Closed",
                   f"MARKET STATUS: CLOSED — {stamp}. Today was a half-day (1:00 PM ET close). "
                   f"Crypto trades 24/7.", is_early=True, action_note=closed_note)

    if _time(9, 30) <= t < _time(16, 0):
        return out("open", "🟢 Market open",
                   f"MARKET STATUS: OPEN — regular session — {stamp}. US stocks/ETFs are trading now.",
                   is_open=True, tradeable=True)
    if _time(16, 0) <= t < _time(20, 0):
        return out("after_hours", "🟡 After-hours",
                   f"MARKET STATUS: AFTER-HOURS — {stamp}. Regular session closed at 4:00 PM ET; "
                   f"after-hours liquidity is thin and gaps are common.",
                   tradeable=True, action_note=ext_note)
    return out("closed", "🔴 Closed",
               f"MARKET STATUS: CLOSED — {stamp}. US stocks/ETFs are closed (next regular session "
               f"9:30 AM ET). Crypto trades 24/7.", action_note=closed_note)


def market_status_line(now: datetime = None) -> str:
    """The full human-readable status sentence (for the chatbot context)."""
    return market_session(now)["line"]


def is_market_open(now: datetime = None) -> bool:
    """True only during the live regular (or half-day) session."""
    return market_session(now)["is_open"]


if __name__ == "__main__":
    import datetime as _dt
    s = market_session()
    print(s["badge"], "|", s["status"])
    print(s["line"])
    print("2026 holidays:")
    for d, n in nyse_holidays(2026).items():
        print(" ", d, d.strftime("%a"), n)
    print("2026 early closes:", sorted(nyse_early_closes(2026)))
