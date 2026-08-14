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

# Universal fallback exit policy applied to any open option position the loop can't attribute to
# a specific strategy (v1: we don't persist per-position strategy). Standard option management:
# take profits, cut losses, and never hold into expiry decay.
DEFAULT_EXIT = {"profit_pct": 60, "stop_pct": 50, "close_dte": 2}


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


# Priority order: try the most aggressive that qualifies first.
STRATEGIES = [short_dte_momentum, catalyst_momentum]


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
                         exit_rule: dict | None = None) -> tuple[str, str]:
    """Decide ('hold'|'close', reason) for an open long option, per its exit rule.
    entry_price/current_mark are per-contract prices (e.g. 0.18). Closes on profit target,
    stop, or approaching expiry (decay/assignment guard)."""
    rule = exit_rule or DEFAULT_EXIT
    if not entry_price or entry_price <= 0:
        # No cost basis → fall back to the time guard only.
        if dte is not None and dte <= rule["close_dte"]:
            return ("close", f"{dte} DTE ≤ {rule['close_dte']} (no basis)")
        return ("hold", "no cost basis")
    change_pct = (current_mark - entry_price) / entry_price * 100
    if change_pct >= rule["profit_pct"]:
        return ("close", f"+{change_pct:.0f}% ≥ target {rule['profit_pct']}%")
    if change_pct <= -rule["stop_pct"]:
        return ("close", f"{change_pct:.0f}% ≤ stop -{rule['stop_pct']}%")
    if dte is not None and dte <= rule["close_dte"]:
        return ("close", f"{dte} DTE ≤ {rule['close_dte']}")
    return ("hold", f"{change_pct:+.0f}%")
