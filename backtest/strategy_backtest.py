"""
Strategy backtester — does a rule-based, monthly-rebalanced portfolio beat SPY after costs?

The overhaul plan (2026-10-20) replaces "LLM picks → trades" with a systematic strategy, IF one beats SPY. This is
the referee: it runs a strategy over real daily adjusted closes (dividends included) and reports return AND risk
against buy-and-hold SPY.

  - A strategy is a pure function `fn(hist) -> {ticker: weight}` (weights ≤ 1 in total; the rest is idle cash at 0%).
    `hist` only exposes closes through the PRIOR trading day — the strategy can't see the rebalance day (no lookahead).
  - Rebalance on the first trading day of each month, at that day's close. Between rebalances the holdings drift.
  - Cost: `cost_bps` of every dollar traded (spread + slippage; Robinhood has no commission).
  - A ticker with no data yet (a fund that didn't exist) is unavailable to the strategy and can't be held.

No LLM, no network here (prices are injected) — the runner (scripts/backtest_strategies.py) downloads them.
Like backtest/exit_backtest.py, it measures the RULE, not Argus's news calls.
"""
from __future__ import annotations

import math
from datetime import date
from statistics import mean, pstdev

from strategy.rules import History, align

TRADING_DAYS = 252


def _is_rebalance_day(calendar: list[date], i: int) -> bool:
    return i == 0 or calendar[i].month != calendar[i - 1].month


def run(prices: dict[str, list[tuple[date, float]]], strategy, start: date, end: date,
        calendar_ticker: str = "SPY", cost_bps: float = 5.0) -> dict:
    """Pure: simulate `strategy` from `start` to `end` on the trading days of `calendar_ticker`.
    Starts with 1.0 in cash; the first trading day is a rebalance. → {"curve": [(date, value)], "turnover": $,
    "trades": n, "rebalances": n}"""
    full = [d for d, _ in prices[calendar_ticker]]
    series = align(prices, full)
    idx = [i for i, d in enumerate(full) if start <= d <= end]
    if not idx:
        return {"curve": [], "turnover": 0.0, "trades": 0, "rebalances": 0}
    held: dict[str, float] = {}              # ticker → dollars held
    cash, turnover, trades, rebalances = 1.0, 0.0, 0, 0
    curve = []
    for k, i in enumerate(idx):
        if k:                                 # mark holdings to today's close
            for t in list(held):
                prev, now = series[t][i - 1], series[t][i]
                if prev and now:
                    held[t] *= now / prev
        value = cash + sum(held.values())
        if k == 0 or _is_rebalance_day(full, i):
            target = _clean_weights(strategy(History(series, i - 1)), series, i)
            new = {t: w * value for t, w in target.items()}
            traded = sum(abs(new.get(t, 0.0) - held.get(t, 0.0)) for t in set(new) | set(held))
            trades += sum(1 for t in set(new) | set(held) if abs(new.get(t, 0.0) - held.get(t, 0.0)) > 1e-9)
            cost = traded * cost_bps / 10_000
            turnover += traded
            rebalances += 1
            value -= cost
            held = {t: v * (value / (value + cost)) for t, v in new.items() if v > 0}
            cash = value - sum(held.values())
        curve.append((full[i], cash + sum(held.values())))
    return {"curve": curve, "turnover": turnover, "trades": trades, "rebalances": rebalances}


def _clean_weights(weights: dict[str, float], series, i: int) -> dict[str, float]:
    """Drop tickers with no price today or non-positive weight; scale down if the total exceeds 1 (no leverage)."""
    w = {t: float(x) for t, x in (weights or {}).items() if x and x > 0 and series.get(t) and series[t][i]}
    total = sum(w.values())
    return {t: x / total for t, x in w.items()} if total > 1 else w


# --- metrics ---------------------------------------------------------------------------------

def _daily_returns(curve):
    return [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:]) if a]


def max_drawdown(curve) -> float:
    """Pure: worst peak-to-trough fall, as a negative %."""
    peak, worst = -math.inf, 0.0
    for _, v in curve:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst * 100


def monthly_returns(curve) -> dict[tuple[int, int], float]:
    """Pure: {(year, month): % return}, from the last value of the previous month to the last of this one."""
    last: dict[tuple[int, int], float] = {}
    for d, v in curve:
        last[(d.year, d.month)] = v
    keys = sorted(last)
    first_v = curve[0][1] if curve else None
    out, prev = {}, first_v
    for k in keys:
        if prev:
            out[k] = (last[k] / prev - 1) * 100
        prev = last[k]
    return out


def metrics(result: dict, benchmark: dict | None = None) -> dict:
    """Pure: summary stats of a run(); vs a benchmark run() when given. Sharpe uses a 0% risk-free rate."""
    curve = result["curve"]
    if len(curve) < 2:
        return {}
    years = (curve[-1][0] - curve[0][0]).days / 365.25
    total = curve[-1][1] / curve[0][1] - 1
    rets = _daily_returns(curve)
    vol = pstdev(rets) * math.sqrt(TRADING_DAYS) if len(rets) > 1 else 0.0
    avg_value = mean(v for _, v in curve)
    out = {"start": curve[0][0], "end": curve[-1][0], "years": round(years, 1),
           "total_pct": round(total * 100, 1),
           "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if years > 0 else None,
           "max_dd_pct": round(max_drawdown(curve), 1), "vol_pct": round(vol * 100, 1),
           "sharpe": round(mean(rets) * TRADING_DAYS / vol, 2) if vol else None,
           "turnover_per_yr": round(result["turnover"] / avg_value / years, 2) if years > 0 else None,
           "trades_per_yr": round(result["trades"] / years, 1) if years > 0 else None}
    if benchmark:
        mine, theirs = monthly_returns(curve), monthly_returns(benchmark["curve"])
        common = [k for k in mine if k in theirs]
        out["months_beat_bench_pct"] = round(sum(mine[k] > theirs[k] for k in common) / len(common) * 100) \
            if common else None
    return out
