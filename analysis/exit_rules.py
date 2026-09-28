"""
Exit rules for SHARE positions — one parser + one decision, shared by the exit-alert checker
(alerts/exit_checker.py) and the agent's stock leg.

parse_exit_condition turns the analyst's free-text exit ("target 8% gain, stop loss at 4%, exit in
2 weeks") into numbers. It replaced exit_checker's inline regexes, which had a real bug: the stop
percentage was also matched as a GAIN target (its stop-context lookup searched for "4.0%" in text
that says "4%"), so "target 8% gain, stop loss at 4%" fired "gain target reached" at +4%.

equity_exit_decision is the stock analog of options_strategies.option_exit_decision: pure, no I/O.
Pure module — unit-tested, safe to import anywhere.
"""
from __future__ import annotations

import re

# "stop loss at 4%", "stop-loss 4%", "stop at 4%", "stop 4" (the loosest of the three old parsers).
_STOP_RE = re.compile(r"stop(?:[\s-]*loss)?(?:\s*at)?\s*(\d+(?:\.\d+)?)\s*%?")
_TARGET_RE = re.compile(r"target\s*(?:of\s*)?\+?(\d+(?:\.\d+)?)\s*%")
_GAIN_RE = re.compile(r"\+?(\d+(?:\.\d+)?)\s*%\s*(?:gain|rise|profit|upside|up)\b")
_TIME_RES = ((re.compile(r"(\d+)\s*week"), 7), (re.compile(r"(\d+)\s*day"), 1),
             (re.compile(r"(\d+)\s*month"), 30))


def parse_exit_condition(text: str | None) -> dict:
    """{'target_pct', 'stop_pct', 'max_days'} from an exit_condition string; each None if absent.

    target = the first explicit "target X%"; else the smallest "X% gain/rise/profit/upside/up" (the
    first level that would be hit). A BARE percentage is never a target — real exits carry
    explanatory numbers ("ATR-sized: avg daily range is 2.2%") that must not become a sell level.
    max_days = the earliest of any week/day/month limit."""
    t = (text or "").lower()
    stop = _STOP_RE.search(t)
    stop_pct = float(stop.group(1)) if stop else None
    # Blank out the stop phrase so its number can never be read as a target.
    scan = t[:stop.start()] + " " * (stop.end() - stop.start()) + t[stop.end():] if stop else t
    explicit = _TARGET_RE.search(scan)
    gains = [float(m.group(1)) for m in _GAIN_RE.finditer(scan)]
    target = float(explicit.group(1)) if explicit else (min(gains) if gains else None)
    days = [int(m.group(1)) * mult for rx, mult in _TIME_RES for m in [rx.search(t)] if m]
    return {"target_pct": target,
            "stop_pct": stop_pct,
            "max_days": min(days) if days else None}


# Fallback when a position has no parseable exit (e.g. bought by hand): regular-tier band from the
# analyst's rough sanity bands (CLAUDE.md "Exit Targets" — regular 6-10% / stops 2-4%), time-boxed.
STOCK_EXIT_DEFAULT = {"target_pct": 8.0, "stop_pct": 4.0, "max_days": 30}


def equity_exit_decision(entry_price: float, price: float, rule: dict | None = None,
                         peak_price: float | None = None, days_held: int | None = None) -> tuple[str, str]:
    """('hold'|'close', reason) for a LONG share position. Order: hard stop → trailing stop →
    target → time limit.

    Trailing (chandelier-style, sized by the same ATR stop): once the position has been up at least
    one stop-distance (1R), sell if it falls one stop-distance off its peak. Volatility-sized, so
    ordinary noise doesn't shake it out, but a winner can't round-trip into a loser. Otherwise the
    plan's target stands — the scorecard showed letting trades reach their band beats closing early.
    Missing rule fields fall back to STOCK_EXIT_DEFAULT."""
    r = {**STOCK_EXIT_DEFAULT, **{k: v for k, v in (rule or {}).items() if v is not None}}
    if not entry_price or entry_price <= 0 or not price or price <= 0:
        return ("hold", "no cost basis / price")
    change = (price - entry_price) / entry_price * 100
    stop, target = r["stop_pct"], r["target_pct"]

    if change <= -stop:
        return ("close", f"{change:.1f}% ≤ stop -{stop:g}%")
    if peak_price and peak_price > entry_price:
        peak_gain = (peak_price - entry_price) / entry_price * 100
        off_peak = (peak_price - price) / peak_price * 100
        if peak_gain >= stop and off_peak >= stop:
            return ("close", f"trail: {change:+.1f}% ({off_peak:.1f}% off peak +{peak_gain:.1f}%)")
    if change >= target:
        return ("close", f"+{change:.1f}% ≥ target {target:g}%")
    if days_held is not None and r["max_days"] is not None and days_held >= r["max_days"]:
        return ("close", f"held {days_held}d ≥ {r['max_days']}d limit")
    return ("hold", f"{change:+.1f}%")
