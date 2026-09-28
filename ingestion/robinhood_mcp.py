"""
Robinhood Trading MCP client — the official/sanctioned execution + read path (Path B).

WHY THIS EXISTS: the existing ingestion/robinhood.py uses the *unofficial* robin_stocks
library, which (a) violates Robinhood's ToS for automated orders and (b) re-triggers a
device-approval challenge every few days (the 429 loop). Robinhood's first-party Trading
MCP is OAuth-based (refresh tokens → no re-login churn) and sanctioned for agents.

ACCOUNT SCOPE (confirmed live 2026-08-14 via get_accounts):
  - READS span ALL brokerage accounts, including the MAIN account (agentic_allowed=false).
    So the dashboard can read your real holdings/buying power here → drop robin_stocks for
    reads → the 429 is gone. Reads default to the MAIN (is_default, non-agentic) account.
  - ORDERS are accepted ONLY on the dedicated AGENTIC account (agentic_allowed=true);
    place_equity_order rejects non-agentic accounts. So auto-exit via native stops only
    works for positions HELD in the agentic account; main-account holdings are read-only here.

DESIGN (see the plan file "APPROVED DIRECTION"):
  - All MCP + OAuth code is isolated HERE + ingestion/mcp_auth.py. Callers see the same read
    interface as robin_stocks (fetch_positions / fetch_buying_power / fetch_quotes) so swapping
    is a config flag.
  - config.USE_MCP gates whether this path is used at all (default False → today's Argus).
  - config.DRY_RUN gates execution: True → an ALLOWED order is logged, not sent. Default True.
  - trading_guards.check_order() vets every order in BOTH dry and live runs.

Reads degrade to empty (never raise) when USE_MCP is off, the SDK is absent, or no session is
stored — and never launch a browser (auth is only via scripts/mcp_login.py).
"""
from __future__ import annotations

import uuid

import config
from trading_guards import GuardState, OrderIntent, check_order, record_placed

# Confirmed Robinhood MCP tool names (get_accounts tools/list, 2026-08-14).
_TOOL_ACCOUNTS = "get_accounts"
_TOOL_POSITIONS = "get_equity_positions"
_TOOL_PORTFOLIO = "get_portfolio"          # holds buying_power + total_value
_TOOL_QUOTES = "get_equity_quotes"
_TOOL_REVIEW_ORDER = "review_equity_order"  # pre-trade simulation (confirm-first)
_TOOL_PLACE_ORDER = "place_equity_order"    # requires an agentic_allowed=true account
_TOOL_REVIEW_OPTION = "review_option_order"
_TOOL_PLACE_OPTION = "place_option_order"   # single-leg, requires option_level_2 + agentic

# OrderIntent.order_type -> place_equity_order 'type'. Native stop support = set-and-forget exits.
_ORDER_TYPE_MAP = {
    "market": "market",
    "limit": "limit",
    "stop": "stop_market",
    "stop_market": "stop_market",
    "stop_limit": "stop_limit",
}

MIN_DOLLAR_ORDER = 1.00   # broker minimum for dollar-based orders (EQUITY_DOLLAR_BASED_MINIMUM_AMOUNT_ERROR)


class MCPNotWired(RuntimeError):
    """Raised when a live MCP call can't be made (SDK absent or no stored session). Reads
    catch this and degrade to empty; live orders surface it (dry-run never hits it)."""


class MCPToolError(RuntimeError):
    """Raised when a tool call returns is_error=True (e.g. an order rejected server-side).
    Without this, an error response would be mistaken for success — the false 'placed' bug."""


def is_available() -> bool:
    """True only when the MCP path is switched on. Does not prove a session is live — that's
    checked lazily on first _call_tool (which degrades cleanly if not)."""
    return bool(getattr(config, "USE_MCP", False))


def _call_tool(name: str, arguments: dict | None = None):
    """Single chokepoint for every Robinhood MCP tool invocation. Delegates to the OAuth
    transport in ingestion.mcp_auth (lazy import → CI, which lacks `mcp`, still imports this
    module). Non-interactive: uses the stored token (silent refresh), never a browser. Returns
    the result already unwrapped to a plain dict/list."""
    try:
        from ingestion import mcp_auth
    except ImportError as e:
        raise MCPNotWired(f"mcp SDK not installed — {e}") from e

    try:
        result = mcp_auth.call_tool(name, arguments)
    except mcp_auth.NotAuthenticated as e:
        raise MCPNotWired(str(e)) from e
    # A tool can fail without raising — it returns is_error=True with the reason in content.
    # Surface that as an exception so a rejected order is never mistaken for a placed one.
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise MCPToolError(f"{name} error: {_unwrap_tool_result(result)}")
    return _unwrap_tool_result(result)


def _unwrap_tool_result(result):
    """Turn an MCP CallToolResult into a plain Python object. Prefers structuredContent;
    else parses the text content blocks as JSON; else returns the raw text/result."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    content = getattr(result, "content", None)
    if content:
        import json
        texts = [t for block in content if (t := getattr(block, "text", None)) is not None]
        joined = "\n".join(texts).strip()
        if joined:
            try:
                return json.loads(joined)
            except (ValueError, TypeError):
                return joined
    return result


def _data(payload):
    """The MCP wraps tool payloads as {"data": {...}, "guide": "..."}. Return the data body."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# --- account resolution ---------------------------------------------------------------
# Cached per process; account membership changes rarely and every read needs a number.
_accounts_cache: list | None = None


def _accounts(force: bool = False) -> list:
    global _accounts_cache
    if _accounts_cache is not None and not force:
        return _accounts_cache
    data = _data(_call_tool(_TOOL_ACCOUNTS))
    accts = data.get("accounts", []) if isinstance(data, dict) else []
    _accounts_cache = accts
    return accts


def _main_account_number() -> str | None:
    """The user's primary self-directed account (holds the real portfolio). Prefer the
    default, non-agentic account; fall back to the first non-agentic; then the first."""
    accts = _accounts()
    for a in accts:
        if a.get("is_default") and not a.get("agentic_allowed"):
            return a.get("account_number")
    for a in accts:
        if not a.get("agentic_allowed"):
            return a.get("account_number")
    return accts[0].get("account_number") if accts else None


def agentic_account_number() -> str | None:
    """The dedicated Agentic account — the ONLY one that accepts orders via the MCP."""
    for a in _accounts():
        if a.get("agentic_allowed"):
            return a.get("account_number")
    return None


# --- reads: mirror ingestion/robinhood.py shapes so callers swap cleanly --------------

def fetch_positions(account_number: str | None = None) -> list[dict]:
    """Same shape as ingestion.robinhood.fetch_positions. Defaults to the MAIN account.
    MCP positions lack live price/name, so we enrich current_price via fetch_quotes and
    compute equity/pnl. Degrades to [] on any failure."""
    if not is_available():
        return []
    try:
        acct = account_number or _main_account_number()
        if not acct:
            return []
        rows: list[dict] = []
        cursor = None
        for _ in range(10):  # bounded pagination
            args = {"account_number": acct}
            if cursor:
                args["cursor"] = cursor
            data = _data(_call_tool(_TOOL_POSITIONS, args))
            rows.extend(data.get("positions", []) if isinstance(data, dict) else [])
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not cursor:
                break
    except MCPNotWired as e:
        print(f"Robinhood MCP positions: {e}")
        return []
    except Exception as e:  # noqa: BLE001 — never crash a dashboard rerun on a read
        print(f"Robinhood MCP positions: fetch failed — {e}")
        return []
    symbols = [r.get("symbol") for r in rows if r.get("symbol")]
    quotes = fetch_quotes(symbols) if symbols else {}
    return _normalize_positions(rows, quotes)


def fetch_option_positions(account_number: str) -> list[dict]:
    """Open (nonzero) option position rows for an account, as get_option_positions returns them.
    RAISES on a read failure — callers decide (the exit pass skips the cycle, the held check assumes none)."""
    data = _data(_call_tool("get_option_positions", {"account_number": account_number, "nonzero": True}))
    return [p for p in (data.get("positions", []) if isinstance(data, dict) else []) if p]


def fetch_buying_power(account_number: str | None = None) -> float | None:
    """Same contract as ingestion.robinhood.fetch_buying_power: dollars or None. Defaults to
    the MAIN account (matches the dashboard budget semantics). Pass the agentic account to
    size MCP orders."""
    if not is_available():
        return None
    try:
        acct = account_number or _main_account_number()
        if not acct:
            return None
        data = _data(_call_tool(_TOOL_PORTFOLIO, {"account_number": acct}))
    except MCPNotWired as e:
        print(f"Robinhood MCP buying power: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Robinhood MCP buying power: fetch failed — {e}")
        return None
    return _normalize_buying_power(data)


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """Same shape as ingestion.robinhood.fetch_quotes: {ticker: {price,change,change_pct,
    high,low}} or {ticker: None}. Degrades to {}."""
    if not tickers or not is_available():
        return {}
    try:
        data = _data(_call_tool(_TOOL_QUOTES, {"symbols": list(tickers)}))
    except MCPNotWired as e:
        print(f"Robinhood MCP quotes: {e}")
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"Robinhood MCP quotes: fetch failed — {e}")
        return {}
    return _normalize_quotes(tickers, data)


# --- normalizers: parse the real MCP payloads into Argus's existing shapes -------------

def _to_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _normalize_positions(rows, quotes: dict | None = None) -> list[dict]:
    """Pure: build Argus position dicts from MCP rows + a quotes map (no network). current_price
    falls back to avg_cost when a quote is missing so equity/pnl stay defined."""
    quotes = quotes or {}
    positions: list[dict] = []
    for item in rows or []:
        try:
            ticker = item.get("symbol")
            shares = _to_float(item.get("quantity"))
            if not ticker or shares <= 0:
                continue
            avg_cost = _to_float(item.get("average_buy_price"))
            q = quotes.get(ticker) or {}
            current = _to_float(q.get("price")) or avg_cost
            positions.append({
                "ticker":          ticker,
                "company_name":    ticker,  # MCP position rows carry no name
                "shares":          shares,
                "avg_cost":        avg_cost,
                "current_price":   current,
                "amount_invested": round(shares * avg_cost, 2),
                "equity":          round(shares * current, 2),
                "pnl_pct":         round(((current - avg_cost) / avg_cost * 100) if avg_cost else 0.0, 2),
            })
        except (ValueError, TypeError, AttributeError):
            continue
    return positions


def _normalize_buying_power(data) -> float | None:
    if not isinstance(data, dict):
        return None
    bp = data.get("buying_power")
    # get_portfolio nests it as {"buying_power": {"buying_power": "25.0000", ...}}
    if isinstance(bp, dict):
        bp = bp.get("buying_power")
    if bp is None:
        bp = data.get("cash")
    try:
        return round(float(bp), 2) if bp is not None else None
    except (ValueError, TypeError):
        return None


def _normalize_quotes(tickers: list[str], data) -> dict[str, dict]:
    results: dict[str, dict] = {t: None for t in tickers}
    rows = data.get("results", []) if isinstance(data, dict) else []
    by_symbol = {}
    for r in rows:
        quote = r.get("quote") if isinstance(r, dict) else None
        if isinstance(quote, dict) and quote.get("symbol"):
            by_symbol[quote["symbol"]] = quote
    for ticker in tickers:
        q = by_symbol.get(ticker)
        if not q:
            continue
        try:
            # Prefer the extended/overnight print when present (matches robin_stocks behavior).
            last = _to_float(q.get("last_non_reg_trade_price")) or _to_float(q.get("last_trade_price"))
            prev = _to_float(q.get("previous_close")) or _to_float(q.get("adjusted_previous_close"))
            if last == 0:
                continue
            change = last - prev if prev else 0.0
            results[ticker] = {
                "price":      round(last, 2),
                "change":     round(change, 2),
                "change_pct": round((change / prev * 100) if prev else 0.0, 2),
                "high":       0.0,
                "low":        0.0,
                # Inside market (0.0 when absent) — the paper broker fills buys at the ask, sells at the bid.
                "bid":        round(_to_float(q.get("bid_price")), 4),
                "ask":        round(_to_float(q.get("ask_price")), 4),
            }
        except (ValueError, TypeError):
            results[ticker] = None
    return results


# --- fills → day-trade (PDT) budget ----------------------------------------------------

# Order states that can still fill. An UNFILLED opening order is treated as opened today: if it
# fills later today, a same-day exit would be a day trade, so it must hold its reservation now.
_WORKING_STATES = ("new", "queued", "confirmed", "unconfirmed", "partially_filled")


def _pending_open(key: str, pending_qty: float, today) -> list[dict]:
    return ([{"key": key, "effect": "open", "qty": pending_qty, "ts": "~pending", "date": today}]
            if pending_qty > 0 else [])


def _fills_from_option_orders(orders: list, today) -> list[dict]:
    """Pure: option order rows → PDT fills (one per execution, + a pseudo-fill for any still-working
    opening quantity). Keyed by option_id; the broker's trade_date wins, else the Eastern date of
    the execution timestamp."""
    from market_hours import et_date
    out = []
    for o in orders or []:
        working = (o.get("state") or "").lower() in _WORKING_STATES
        for leg in o.get("legs") or []:
            effect = leg.get("position_effect")
            if effect not in ("open", "close"):
                continue
            key = f"opt:{leg.get('option_id')}"
            for ex in leg.get("executions") or []:
                ts = ex.get("timestamp")
                day = ex.get("trade_date")
                out.append({"key": key, "effect": effect, "qty": _to_float(ex.get("quantity")),
                            "ts": ts, "date": _dt_date(day) if day else et_date(ts)})
            if working and effect == "open":
                out += _pending_open(key, _to_float(o.get("pending_quantity")), today)
    return out


def _fills_from_equity_orders(orders: list, today) -> list[dict]:
    """Pure: equity order rows → PDT fills. Long-only account: buy opens, sell closes. Equity
    executions carry no trade_date → Eastern date of the timestamp. Still-working buys → pseudo-fill."""
    from market_hours import et_date
    out = []
    for o in orders or []:
        effect = {"buy": "open", "sell": "close"}.get((o.get("side") or "").lower())
        if not effect:
            continue
        key = f"eq:{o.get('instrument_id') or o.get('symbol')}"
        for ex in o.get("executions") or []:
            ts = ex.get("timestamp")
            out.append({"key": key, "effect": effect, "qty": _to_float(ex.get("quantity")),
                        "ts": ts, "date": et_date(ts)})
        if effect == "open" and (o.get("state") or "").lower() in _WORKING_STATES:
            # Dollar-based orders have no share quantity until filled → reserve with a nominal 1.
            remaining = _to_float(o.get("quantity")) - _to_float(o.get("cumulative_quantity"))
            out += _pending_open(key, remaining if remaining > 0 else 1.0, today)
    return out


def _dt_date(s: str):
    import datetime as _dt
    try:
        return _dt.date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def day_trade_budget(account_number: str, account_equity: float) -> int | None:
    """Live PDT budget for the agentic account: new positions that may be opened now (None =
    unlimited, ≥$25k). Reads the broker's own fill history (options + equities) so manual trades
    and restarts are counted. RAISES on a read failure — callers must fail CLOSED (no entries)."""
    import market_hours as mh
    from trading_guards import PDT_WINDOW_DAYS, day_trade_budget as _budget
    import datetime as _dt
    window = mh.recent_trading_days(PDT_WINDOW_DAYS)
    # Filter is on created_at, but a GTC order created earlier can FILL inside the window → 10 days slack.
    since = (min(window) - _dt.timedelta(days=10)).isoformat()
    opt = _data(_call_tool("get_option_orders", {"account_number": account_number, "created_at_gte": since}))
    eq = _data(_call_tool("get_equity_orders", {"account_number": account_number, "created_at_gte": since}))
    today = mh._now_et().date()
    fills = (_fills_from_option_orders((opt or {}).get("orders"), today)
             + _fills_from_equity_orders((eq or {}).get("orders"), today))
    return _budget(fills, window, today, account_equity)


# --- orders: guarded, DRY_RUN-safe -----------------------------------------------------

def place_order(intent: OrderIntent, state: GuardState, buying_power: float,
                account_number: str | None = None, review: bool = True) -> dict:
    """Vet an equity order (shape + trading_guards), preview it with review_equity_order, then either
    DRY_RUN-log it or (live) send it to the AGENTIC account. Returns {status, reason, intent, dry_run}
    with status 'rejected' | 'review_failed' | 'dry_run' | 'placed'. Never raises on a guard/review
    rejection; a live-send failure propagates so the caller can halt.

    Review policy (same as options): a broker alert BLOCKS a buy (don't add risk the broker flags);
    a sell proceeds with the alert logged (never trap an exit — the broker hard-rejects illegal ones)."""
    shape_error = _order_shape_error(intent)
    verdict = check_order(intent, state, buying_power)
    reason = shape_error or (None if verdict.allowed else verdict.reason)
    if reason:
        print(f"[ORDER REJECTED] {intent.side} {intent.ticker} — {reason}")
        return {"status": "rejected", "reason": reason, "intent": intent, "dry_run": config.DRY_RUN}

    acct = account_number
    if review:
        tag = f"{intent.side} {intent.ticker} {intent.order_type}"
        try:
            acct = acct or agentic_account_number()
            if not acct:
                raise MCPNotWired("no agentic account to review against")
            preview = _call_tool(_TOOL_REVIEW_ORDER, _order_args(intent, acct))
        except Exception as e:  # noqa: BLE001 — a failed review must not place the order
            print(f"[REVIEW FAILED] {tag} — {e}")
            return {"status": "review_failed", "reason": str(e), "intent": intent, "dry_run": config.DRY_RUN}
        alert = _order_alert(preview)
        print(f"[REVIEW] {tag} — checks: {alert or 'none'}")
        if alert and intent.side == "buy":
            return {"status": "rejected", "reason": f"broker pre-trade alert: {alert}",
                    "intent": intent, "dry_run": config.DRY_RUN}

    if config.DRY_RUN:
        record_placed(intent, state)
        print(
            f"[DRY_RUN ORDER] {intent.side} {intent.ticker} "
            f"{intent.order_type} dollars={intent.dollars} qty={intent.quantity} "
            f"stop={intent.stop_price} limit={intent.limit_price} :: {intent.reason}"
        )
        return {"status": "dry_run", "reason": "logged, not sent", "intent": intent, "dry_run": True}

    # Live path — orders only go to the agentic account. record_placed only after a confirmed
    # send so the idempotency/counter state can't run ahead of reality.
    acct = acct or agentic_account_number()
    if not acct:
        raise MCPNotWired("no agentic account available to place orders")
    ref_id = _new_ref_id()
    result = _call_tool(_TOOL_PLACE_ORDER, {**_order_args(intent, acct), "ref_id": ref_id})
    record_placed(intent, state)
    print(f"[ORDER PLACED] {intent.side} {intent.ticker} ref {ref_id} :: {intent.reason}")
    return {"status": "placed", "reason": "sent", "intent": intent, "dry_run": False,
            "ref_id": ref_id, "raw": result}


def place_option_order(intent, state: GuardState, buying_power: float,
                       account_number: str | None = None, review: bool = True) -> dict:
    """Place a single-leg option order on the AGENTIC account, review-first + DRY_RUN-safe +
    guarded (trading_guards.check_option_order). Returns {status, reason, intent, dry_run} with
    status 'rejected' | 'review_failed' | 'dry_run' | 'placed'. Live-send failures propagate."""
    from trading_guards import check_option_order
    verdict = check_option_order(intent, state, buying_power)
    if not verdict.allowed:
        print(f"[OPTION REJECTED] {intent.side} {intent.underlying} {intent.right} — {verdict.reason}")
        return {"status": "rejected", "reason": verdict.reason, "intent": intent, "dry_run": config.DRY_RUN}

    acct = account_number or agentic_account_number()
    if not acct:
        raise MCPNotWired("no agentic account available for options")
    args = _option_args(intent, acct)
    tag = f"{intent.quantity} {intent.underlying} {intent.strike}{intent.right[0].upper()} @ {intent.price}"

    # Confirm-first: review before placing (both dry and live) to surface pre-trade alerts.
    # A broker alert BLOCKS an opening order (don't add risk the broker is flagging: BP, PDT, halt).
    # A closing order still proceeds with a warning: a missed exit is worse than the alert, and the
    # broker hard-rejects a truly illegal order anyway.
    if review:
        try:
            preview = _call_tool(_TOOL_REVIEW_OPTION, args)
        except Exception as e:  # noqa: BLE001 — a failed review must not place the order
            print(f"[OPTION REVIEW FAILED] {intent.underlying} — {e}")
            return {"status": "review_failed", "reason": str(e), "intent": intent, "dry_run": config.DRY_RUN}
        alert = _order_alert(preview)
        print(f"[OPTION REVIEW] {intent.side} {tag} — checks: {alert or 'none'}")
        if alert and intent.position_effect == "open":
            return {"status": "rejected", "reason": f"broker pre-trade alert: {alert}",
                    "intent": intent, "dry_run": config.DRY_RUN}

    if config.DRY_RUN:
        record_placed(intent, state)
        print(f"[DRY_RUN OPTION] {intent.side}/{intent.position_effect} {tag} "
              f"exp {intent.expiration} premium ${intent.premium} :: {intent.reason}")
        return {"status": "dry_run", "reason": "logged, not sent", "intent": intent, "dry_run": True}

    ref_id = _new_ref_id()   # place-only: review_option_order's schema has no ref_id
    result = _call_tool(_TOOL_PLACE_OPTION, {**args, "ref_id": ref_id})
    record_placed(intent, state)
    print(f"[OPTION PLACED] {intent.side}/{intent.position_effect} {tag} ref {ref_id} :: {intent.reason}")
    return {"status": "placed", "reason": "sent", "intent": intent, "dry_run": False,
            "ref_id": ref_id, "raw": result}


def _order_alert(preview) -> str | None:
    """The broker's pre-trade alert from a review_*_order payload, or None when clean.

    Payload shape (verified live): {"data": {"order_checks": {} | {"alertType": "...", ...}}}.
    Reading order_checks at the top level (the old bug) always saw nothing, so no alert ever blocked."""
    data = _data(preview)
    checks = data.get("order_checks") if isinstance(data, dict) else None
    if not checks:
        return None
    if isinstance(checks, dict):
        return checks.get("alertType") or str(checks)
    return str(checks)


def _option_args(intent, account_number: str) -> dict:
    """Map an OptionOrderIntent to the review/place_option_order leg schema (string values)."""
    args: dict = {
        "account_number": account_number,
        "legs": [{"option_id": intent.option_id, "side": intent.side,
                  "position_effect": intent.position_effect, "ratio_quantity": 1}],
        "type": intent.order_type,
        "quantity": str(intent.quantity),
        "time_in_force": intent.time_in_force,
        "direction": intent.direction,
    }
    if intent.price is not None:
        args["price"] = f"{intent.price:.2f}"
    return args


def _new_ref_id() -> str:
    """Broker idempotency key for ONE placement. The server dedups by ref_id, so a deterministic
    key (the old client_id, e.g. 'stop-F-11.5') could swallow a legitimate re-placement of the
    same order on a later day. A fresh UUID per placement is correct because placement is never
    auto-retried (mcp_session.is_retryable). Local dedup stays on intent.client_id (trading_guards)."""
    return str(uuid.uuid4())


def _fmt_qty(q: float) -> str:
    """Shares as the broker wants them: ≤6 decimals, no float noise ('0.30000000000000004')."""
    return f"{q:.6f}".rstrip("0").rstrip(".")


def _order_shape_error(intent: OrderIntent) -> str | None:
    """Pure: why the broker would refuse this equity order's SHAPE, or None if it's legal.

    Rules verified live in S0 (2026-09-27). review_equity_order does NOT reliably catch these
    (it previewed a fractional limit buy as fine), so they're enforced here before anything is sent."""
    otype = _ORDER_TYPE_MAP.get(intent.order_type)
    if otype is None:
        return f"unknown order_type {intent.order_type!r}"
    has_qty, has_dollars = intent.quantity is not None, intent.dollars is not None
    if has_qty == has_dollars:
        return "exactly one of quantity or dollars is required"
    if has_dollars:
        if otype != "market":
            return "dollar-based orders must be market orders"
        if intent.dollars < MIN_DOLLAR_ORDER:
            return f"dollar amount below the ${MIN_DOLLAR_ORDER:.2f} broker minimum"
    elif intent.quantity <= 0:
        return "quantity must be positive"
    elif intent.quantity != int(intent.quantity) and otype != "market":
        return "fractional shares must be market orders (no limit/stop on fractions)"
    if otype in ("limit", "stop_limit") and intent.limit_price is None:
        return f"{otype} order needs a limit_price"
    if otype in ("stop_market", "stop_limit") and intent.stop_price is None:
        return f"{otype} order needs a stop_price"
    return None


def _order_args(intent: OrderIntent, account_number: str) -> dict:
    """Map a shape-valid OrderIntent (see _order_shape_error) to the review/place_equity_order
    schema (all values are strings). ref_id is added by place_order only — review has none."""
    args: dict = {
        "account_number": account_number,
        "symbol": intent.ticker,
        "side": intent.side,
        "type": _ORDER_TYPE_MAP[intent.order_type],
    }
    if intent.dollars is not None:
        args["dollar_amount"] = f"{intent.dollars:.2f}"
    else:
        args["quantity"] = _fmt_qty(intent.quantity)
    if intent.limit_price is not None:
        args["limit_price"] = f"{intent.limit_price:.2f}"
    if intent.stop_price is not None:
        args["stop_price"] = f"{intent.stop_price:.2f}"
    if intent.time_in_force:
        args["time_in_force"] = intent.time_in_force
    return args
