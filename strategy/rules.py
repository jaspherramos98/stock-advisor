"""
Rule-based strategies for backtest/strategy_backtest.py — each is `fn(hist) -> {ticker: weight}`.

Pure price rules (no LLM, no news), rebalanced monthly. Parameters are FIXED here before the out-of-sample run
(overhaul plan, Phase 1) — tuning them on the results would be curve-fitting.
"""
from __future__ import annotations

from statistics import mean

CASH = "BIL"                     # 1–3 month T-bill ETF (from 2007-05; before that idle cash earns 0%)
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]   # the 11 SPDR sectors
TREND_SMA = 200                  # days
MOMENTUM_6M = (126,)             # ~6 months of trading days
MOMENTUM_BLEND = (63, 126, 252)  # 3 / 6 / 12 months, averaged
TOP_N = 3


def buy_hold(ticker: str):
    """Always 100% `ticker`."""
    return lambda hist: {ticker: 1.0}


def above_sma(closes: list[float], days: int = TREND_SMA) -> bool | None:
    """Pure: last close above its `days`-day simple average? None when there isn't enough history."""
    if len(closes) < days:
        return None
    return closes[-1] > mean(closes[-days:])


def momentum(closes: list[float], lookbacks=MOMENTUM_6M) -> float | None:
    """Pure: average % return over the lookbacks (each = closes[-1] / closes[-1-n] − 1). None if too short."""
    if len(closes) <= max(lookbacks):
        return None
    return mean(closes[-1] / closes[-1 - n] - 1 for n in lookbacks)


def trend_filter(ticker: str, days: int = TREND_SMA, cash: str = CASH):
    """Hold `ticker` while its close is above its `days`-day average, else T-bills (Faber-style trend rule).
    Not enough history yet → hold the ticker (the rule can't say 'risk off' without data)."""
    def rule(hist):
        up = above_sma(hist.closes(ticker), days)
        return {ticker: 1.0} if up is not False else {cash: 1.0}
    return rule


def sector_rotation(universe=tuple(SECTORS), lookbacks=MOMENTUM_6M, top_n: int = TOP_N,
                    days: int = TREND_SMA, cash: str = CASH):
    """Rank the sectors that exist and have enough history by momentum; hold the top N equally, but each slot only
    while that sector is above its `days`-day average — otherwise that slot sits in T-bills."""
    def rule(hist):
        scored = []
        for t in universe:
            if not hist.available(t):
                continue
            closes = hist.closes(t)
            m = momentum(closes, lookbacks)
            if m is not None:
                scored.append((m, t, above_sma(closes, days)))
        picks = sorted(scored, reverse=True)[:top_n]
        if not picks:
            return {cash: 1.0}
        weights: dict[str, float] = {}
        slot = 1.0 / top_n
        for _, t, up in picks:
            key = t if up is not False else cash
            weights[key] = weights.get(key, 0.0) + slot
        if len(picks) < top_n:                       # fewer eligible sectors than slots → rest in T-bills
            weights[cash] = weights.get(cash, 0.0) + slot * (top_n - len(picks))
        return weights
    return rule
