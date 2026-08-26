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
class OptionOrderIntent:
    """A single-leg option order (Level 2: long/short call/put). Premium = price × 100 × qty."""
    underlying: str
    option_id: str
    right: str                         # 'call' | 'put'
    side: str                          # 'buy' | 'sell'
    position_effect: str               # 'open' | 'close'
    quantity: int = 1                  # contracts
    order_type: str = "limit"          # limit | market | stop_limit | stop_market
    price: float | None = None         # per-contract limit price (× 100 = per-contract $)
    direction: str = "debit"           # 'debit' (buy) | 'credit' (sell)
    time_in_force: str = "gfd"
    strike: float | None = None        # for logging/audit only
    expiration: str | None = None      # for logging/audit only
    reason: str = ""
    client_id: str = ""

    @property
    def premium(self) -> float:
        return round((self.price or 0) * 100 * max(1, self.quantity), 2)


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


def check_option_order(intent: OptionOrderIntent, state: GuardState, buying_power: float) -> GuardResult:
    """Vet a single-leg option order. Position SIZE is uncapped by design (pilot) — the only
    money bound is you can't spend more premium than the buying power on an OPEN buy. The rest
    are correctness guards: idempotency, the runaway per-day order cap, and basic well-formedness."""
    if intent.client_id and intent.client_id in state.placed_client_ids:
        return GuardResult(False, "duplicate client_id — already placed")
    if state.orders_today >= MAX_ORDERS_PER_DAY:
        return GuardResult(False, f"daily order cap {MAX_ORDERS_PER_DAY} reached (runaway guard)")
    if intent.side not in ("buy", "sell"):
        return GuardResult(False, "malformed option intent (side)")
    if intent.position_effect not in ("open", "close"):
        return GuardResult(False, "position_effect must be open|close")
    # `right` (call/put) only matters for OPENING new positions. A CLOSE sells what's held by
    # option_id, and Robinhood's get_option_positions reports `type` as long/short (direction),
    # not call/put — so don't demand a valid right to close (that was blocking real exits).
    if intent.position_effect == "open" and intent.right not in ("call", "put"):
        return GuardResult(False, "malformed option intent (right)")
    if intent.quantity < 1:
        return GuardResult(False, "quantity must be >= 1 contract")
    if intent.order_type in ("limit", "stop_limit") and (intent.price is None or intent.price <= 0):
        return GuardResult(False, "limit order needs a positive price")
    # Buying to OPEN spends premium — can't exceed available buying power.
    if intent.side == "buy" and intent.position_effect == "open":
        if buying_power < intent.premium:
            return GuardResult(False, f"premium ${intent.premium:.2f} exceeds buying power ${buying_power:.2f}")
    return GuardResult(True, "ok")


def record_placed(intent, state: GuardState) -> None:
    """Mark an order as placed: increment the day counter and remember its client_id so a
    retry can't duplicate it. Call this only after check_order() allowed it AND it was sent
    (or dry-run logged) — never before, or the counters drift."""
    state.orders_today += 1
    if intent.client_id:
        state.placed_client_ids.add(intent.client_id)
