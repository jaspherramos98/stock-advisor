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

import config
from trading_guards import GuardState, OrderIntent, check_order, record_placed

# Confirmed Robinhood MCP tool names (get_accounts tools/list, 2026-08-14).
_TOOL_ACCOUNTS = "get_accounts"
_TOOL_POSITIONS = "get_equity_positions"
_TOOL_PORTFOLIO = "get_portfolio"          # holds buying_power + total_value
_TOOL_QUOTES = "get_equity_quotes"
_TOOL_REVIEW_ORDER = "review_equity_order"  # pre-trade simulation (confirm-first)
_TOOL_PLACE_ORDER = "place_equity_order"    # requires an agentic_allowed=true account

# OrderIntent.order_type -> place_equity_order 'type'. Native stop support = set-and-forget exits.
_ORDER_TYPE_MAP = {
    "market": "market",
    "limit": "limit",
    "stop": "stop_market",
    "stop_market": "stop_market",
    "stop_limit": "stop_limit",
}


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
            }
        except (ValueError, TypeError):
            results[ticker] = None
    return results


# --- orders: guarded, DRY_RUN-safe -----------------------------------------------------

def place_order(intent: OrderIntent, state: GuardState, buying_power: float,
                account_number: str | None = None) -> dict:
    """Vet an order through trading_guards, then either DRY_RUN-log it or (live) send it to
    the AGENTIC account. Returns {status, reason, intent, dry_run}: 'rejected' (a guard
    blocked it), 'dry_run' (allowed + logged, not sent), or 'placed'. Never raises on a guard
    rejection; a live-send failure propagates so the caller can halt."""
    verdict = check_order(intent, state, buying_power)
    if not verdict.allowed:
        print(f"[ORDER REJECTED] {intent.side} {intent.ticker} — {verdict.reason}")
        return {"status": "rejected", "reason": verdict.reason, "intent": intent, "dry_run": config.DRY_RUN}

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
    acct = account_number or agentic_account_number()
    if not acct:
        raise MCPNotWired("no agentic account available to place orders")
    result = _call_tool(_TOOL_PLACE_ORDER, _order_args(intent, acct))
    record_placed(intent, state)
    print(f"[ORDER PLACED] {intent.side} {intent.ticker} :: {intent.reason}")
    return {"status": "placed", "reason": "sent", "intent": intent, "dry_run": False, "raw": result}


def _order_args(intent: OrderIntent, account_number: str) -> dict:
    """Map an OrderIntent to the place_equity_order schema (all values are strings)."""
    otype = _ORDER_TYPE_MAP.get(intent.order_type, "market")
    args: dict = {
        "account_number": account_number,
        "symbol": intent.ticker,
        "side": intent.side,
        "type": otype,
    }
    if intent.dollars is not None and otype == "market":
        args["dollar_amount"] = f"{intent.dollars:.2f}"
    if intent.quantity is not None:
        args["quantity"] = str(intent.quantity)
    if intent.limit_price is not None:
        args["limit_price"] = f"{intent.limit_price:.2f}"
    if intent.stop_price is not None:
        args["stop_price"] = f"{intent.stop_price:.2f}"
    if intent.time_in_force:
        args["time_in_force"] = intent.time_in_force
    if intent.client_id:
        args["ref_id"] = intent.client_id
    return args
