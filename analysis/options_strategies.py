"""
Options strategy library for the autonomous agent (Phase 2) — codified playbooks.

A "strategy" maps an Argus directional signal (+ market regime) to a concrete option PLAN:
which right (call/put), the DTE window, how far OTM, how much of buying power to deploy, and
the exit rule. Strategies are tried in priority order; the first that `applies` wins.

Everything here is PURE (no network, no LLM) so it's unit-tested. The live loop
(alerts/agentic_options.py) feeds it signals + regime and executes the plan via options_data +
robinhood_mcp.place_option_order.

Signal = an Argus recommendation dict: direction ('buy'|'short'), conviction (0-100), risk_level,
ticker. Regime = {'risk': 'risk_on'|'neutral'|'risk_off'} from ingestion.prices.fetch_market_regime.

AGGRESSIVE BY DESIGN (disposable pilot): short-DTE OTM contracts, size up to the full allocation.
This is high-variance / most likely -EV — the point is max upside on money the user can lose.
"""
from __future__ import annotations

import math

# Universal fallback exit policy. Standard option management: cut losses, lock gains with a
# TRAILING take-profit (sell after a pullback from the peak — captures the rip instead of
# waiting for a fixed target and round-tripping), a hard target as a backstop, and never hold
# into expiry decay.
#   trail_activate  — start trailing once the position has been up this % (arm the lock)
#   trail_giveback  — once armed, sell if the mark falls this % BELOW its peak
DEFAULT_EXIT = {"profit_pct": 80, "stop_pct": 50, "close_dte": 2,
                "trail_activate": 25, "trail_giveback": 20}


def _right(direction: str) -> str | None:
    return {"buy": "call", "short": "put"}.get((direction or "").lower())


def catalyst_momentum(signal: dict, regime: dict) -> dict | None:
    """Directional swing on a strong recent catalyst: slightly-OTM call/put, ~3-6 weeks out.
    The bread-and-butter play — fires on any solid conviction, any regime."""
    right = _right(signal.get("direction"))
    if not right or (signal.get("conviction") or 0) < 70:
        return None
    return {"strategy": "catalyst_momentum", "right": right,
            "dte_min": 21, "dte_max": 45, "otm_pct": 4.0, "alloc_pct": 0.5,
            "exit": {"profit_pct": 60, "stop_pct": 50, "close_dte": 7}}


def short_dte_momentum(signal: dict, regime: dict) -> dict | None:
    """Most aggressive: high-conviction + risk-on regime → further-OTM weekly. Lottery-ticket
    convexity — big upside, usually expires worthless. Only when the tape is with us."""
    right = _right(signal.get("direction"))
    if not right or (signal.get("conviction") or 0) < 80:
        return None
    if (regime or {}).get("risk") != "risk_on":
        return None
    return {"strategy": "short_dte_momentum", "right": right,
            "dte_min": 3, "dte_max": 12, "otm_pct": 6.0, "alloc_pct": 1.0,
            "exit": {"profit_pct": 100, "stop_pct": 60, "close_dte": 1}}


def pre_earnings_iv(signal: dict, regime: dict) -> dict | None:
    """LOTTERY / usually -EV: buy directional OTM options 1-7 days BEFORE a report, expiry just
    after it. IV crush after the print means you can be right on direction and still lose — the
    user accepted this as a max-upside gamble on disposable capital. Needs days_to_earnings."""
    right = _right(signal.get("direction"))
    dte_e = signal.get("days_to_earnings")
    if not right or dte_e is None or not (1 <= dte_e <= 7) or (signal.get("conviction") or 0) < 75:
        return None
    return {"strategy": "pre_earnings_iv", "right": right,
            "dte_min": max(dte_e + 1, 3), "dte_max": dte_e + 14, "otm_pct": 7.0, "alloc_pct": 1.0,
            "exit": {"profit_pct": 100, "stop_pct": 60, "close_dte": 1}}


def post_earnings_momentum(signal: dict, regime: dict) -> dict | None:
    """Ride the drift AFTER a fresh report (last 3 days) in the signal's direction — avoids IV
    crush (vol already collapsed). Needs days_since_earnings. The safe way to play earnings."""
    right = _right(signal.get("direction"))
    dse = signal.get("days_since_earnings")
    if not right or dse is None or dse > 3 or (signal.get("conviction") or 0) < 70:
        return None
    return {"strategy": "post_earnings_momentum", "right": right,
            "dte_min": 14, "dte_max": 30, "otm_pct": 4.0, "alloc_pct": 0.5,
            "exit": {"profit_pct": 60, "stop_pct": 50, "close_dte": 7}}


def mean_reversion(signal: dict, regime: dict) -> dict | None:
    """Fade an RSI extreme in the signal's direction: oversold (RSI ≤ 35) bullish → call,
    overbought (RSI ≥ 65) bearish → put. Needs rsi. Lower conviction bar — the technical setup
    carries it."""
    rsi = signal.get("rsi")
    direction = (signal.get("direction") or "").lower()
    if rsi is None or (signal.get("conviction") or 0) < 60:
        return None
    if direction == "buy" and rsi <= 35:
        right = "call"
    elif direction == "short" and rsi >= 65:
        right = "put"
    else:
        return None
    return {"strategy": "mean_reversion", "right": right,
            "dte_min": 14, "dte_max": 30, "otm_pct": 3.0, "alloc_pct": 0.5,
            "exit": {"profit_pct": 50, "stop_pct": 40, "close_dte": 5}}


# Priority order: earnings-context plays first (they self-gate on data presence), then the
# momentum/technical plays, then the catalyst catch-all. The loop tries each until one yields a
# tradable/affordable contract.
STRATEGIES = [pre_earnings_iv, post_earnings_momentum, short_dte_momentum,
              mean_reversion, catalyst_momentum]


def applicable_plans(signal: dict, regime: dict) -> list[dict]:
    """All strategy plans that apply to this signal, in priority order. The loop tries each in
    turn until one yields a tradable/affordable contract (so an illiquid short-DTE pick falls
    through to the catalyst swing instead of dropping the signal)."""
    return [plan for strat in STRATEGIES if (plan := strat(signal, regime))]


def select_strategy(signal: dict, regime: dict) -> dict | None:
    """The single highest-priority plan that applies, or None."""
    plans = applicable_plans(signal, regime)
    return plans[0] if plans else None


def size_contracts(alloc_pct: float, buying_power: float, cost_1x: float) -> int:
    """How many contracts to buy: floor(alloc_pct × buying_power / cost_1x), but at least 1 if a
    single contract is affordable (aggressive — take the shot). 0 only if unaffordable. Capped so
    total premium never exceeds buying power."""
    if not cost_1x or cost_1x <= 0 or buying_power < cost_1x:
        return 0
    n = int(math.floor((alloc_pct * buying_power) / cost_1x))
    if n < 1:
        n = 1
    return min(n, int(math.floor(buying_power / cost_1x)))


def option_exit_decision(entry_price: float, current_mark: float, dte: int | None,
                         exit_rule: dict | None = None,
                         peak_mark: float | None = None) -> tuple[str, str]:
    """Decide ('hold'|'close', reason) for an open long option.
    entry_price/current_mark/peak_mark are per-contract prices (e.g. 0.18). Order of checks:
    hard stop → TRAILING take-profit (lock gains after a pullback from peak) → hard target
    backstop → expiry. peak_mark is the high-water mark (from storage.peak_tracker); pass None
    to disable trailing."""
    rule = exit_rule or DEFAULT_EXIT
    if not entry_price or entry_price <= 0:
        if dte is not None and dte <= rule["close_dte"]:
            return ("close", f"{dte} DTE ≤ {rule['close_dte']} (no basis)")
        return ("hold", "no cost basis")

    change_pct = (current_mark - entry_price) / entry_price * 100
    # 1) hard stop — protect first
    if change_pct <= -rule["stop_pct"]:
        return ("close", f"{change_pct:.0f}% ≤ stop -{rule['stop_pct']}%")

    # 2) trailing take-profit — once we've been up trail_activate%, sell if the mark has fallen
    #    trail_giveback% below its peak (captures the rip; don't wait for the fixed target).
    ta, tg = rule.get("trail_activate"), rule.get("trail_giveback")
    if peak_mark and ta is not None and tg is not None and peak_mark > entry_price:
        peak_gain = (peak_mark - entry_price) / entry_price * 100
        if peak_gain >= ta:
            giveback = (peak_mark - current_mark) / peak_mark * 100
            if giveback >= tg:
                return ("close", f"trail: +{change_pct:.0f}% (locked, off peak +{peak_gain:.0f}%)")

    # 3) hard target backstop
    if change_pct >= rule["profit_pct"]:
        return ("close", f"+{change_pct:.0f}% ≥ target {rule['profit_pct']}%")
    # 4) expiry guard
    if dte is not None and dte <= rule["close_dte"]:
        return ("close", f"{dte} DTE ≤ {rule['close_dte']}")
    return ("hold", f"{change_pct:+.0f}%")
