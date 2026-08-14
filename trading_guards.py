"""
Broker-agnostic order-safety guardrails for Argus automated execution.

Pure logic, no network — testable without a broker session (same split rationale as
chat_budget.py / market_hours.py). Every order intent passes through check_order() before
it can reach a live broker, so a bug upstream in the pipeline can't place unbounded,
duplicated, or oversized orders. These guards apply in BOTH dry and live runs so the
dry-run log reflects exactly what a live run would allow (config.DRY_RUN only decides
whether an *allowed* order is actually sent).

Limits are deliberately conservative — this rides a small, net-flat, unproven-edge
account (see the plan file). Tightening is fine; loosening needs a reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# --- limits ---------------------------------------------------------------------------
MAX_ORDERS_PER_DAY = 10            # hard cap on placements per trading day
DAILY_LOSS_LIMIT_PCT = 0.05        # halt NEW orders once day P&L <= -5% of start equity
MIN_BUYING_POWER = 10.0            # mirror calculator.portfolio.MIN_ALLOCATION_BUDGET
MAX_SINGLE_ORDER_FRACTION = 0.40   # mirror calculator.portfolio.MAX_SINGLE_ALLOCATION


@dataclass
class OrderIntent:
    """A single intended order. `client_id` is the idempotency key — a stable hash of the
    trade decision so a loop restart can't double-place the same order."""
    ticker: str
    side: str                          # "buy" | "sell"
    dollars: float | None = None       # notional for buys (fractional-friendly)
    quantity: float | None = None      # shares for sells
    order_type: str = "market"         # market | limit | stop | stop_limit
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str | None = None   # 'gtc' | 'gfd'; None → broker default (gfd). Protective
                                       # stops MUST be 'gtc' or they expire at the close.
    reason: str = ""                   # entry rationale / exit trigger — for the audit log
    client_id: str = ""                # idempotency key


@dataclass
class GuardState:
    """Per-day mutable execution state. Rebuilt each trading day; `start_equity` is the
    account equity at session start (the denominator for the loss limit + single-name cap)."""
    start_equity: float
    orders_today: int = 0
    day_pnl: float = 0.0               # realized + unrealized P&L so far today (signed $)
    placed_client_ids: set = field(default_factory=set)


@dataclass
class GuardResult:
    allowed: bool
    reason: str


def check_order(intent: OrderIntent, state: GuardState, buying_power: float) -> GuardResult:
    """Return whether `intent` may be placed given today's state and live buying power.

    Order of checks is intentional: kill-switch / cap conditions that block EVERYTHING come
    first, then side-specific validation. Never raises — a malformed intent returns
    allowed=False with a reason, so the caller logs and skips rather than crashing the loop.
    """
    # Idempotency — never place the same decision twice (survives a loop restart).
    if intent.client_id and intent.client_id in state.placed_client_ids:
        return GuardResult(False, "duplicate client_id — already placed")

    # Daily order cap.
    if state.orders_today >= MAX_ORDERS_PER_DAY:
        return GuardResult(False, f"daily order cap {MAX_ORDERS_PER_DAY} reached")

    # Daily-loss kill switch — halts NEW orders (exits you place by hand still go through
    # the broker directly; this only gates the automated path).
    if state.start_equity > 0 and state.day_pnl <= -DAILY_LOSS_LIMIT_PCT * state.start_equity:
        return GuardResult(False, f"daily loss limit hit ({DAILY_LOSS_LIMIT_PCT:.0%})")

    if intent.side == "buy":
        if intent.dollars is None or intent.dollars <= 0:
            return GuardResult(False, "buy intent missing a positive dollar amount")
        if buying_power < MIN_BUYING_POWER:
            return GuardResult(False, f"buying power < ${MIN_BUYING_POWER:.0f}")
        if intent.dollars > buying_power:
            return GuardResult(False, "order exceeds available buying power")
        if state.start_equity > 0 and intent.dollars > MAX_SINGLE_ORDER_FRACTION * state.start_equity:
            return GuardResult(False, f"order exceeds single-name cap {MAX_SINGLE_ORDER_FRACTION:.0%}")
    elif intent.side == "sell":
        if intent.quantity is None or intent.quantity <= 0:
            return GuardResult(False, "sell intent missing a positive quantity")
    else:
        return GuardResult(False, f"unknown side '{intent.side}'")

    return GuardResult(True, "ok")


def record_placed(intent: OrderIntent, state: GuardState) -> None:
    """Mark an order as placed: increment the day counter and remember its client_id so a
    retry can't duplicate it. Call this only after check_order() allowed it AND it was sent
    (or dry-run logged) — never before, or the counters drift."""
    state.orders_today += 1
    if intent.client_id:
        state.placed_client_ids.add(intent.client_id)
