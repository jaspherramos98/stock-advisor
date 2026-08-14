"""
Robinhood Trading MCP client — the official/sanctioned execution + read path (Path B).

WHY THIS EXISTS: the existing ingestion/robinhood.py uses the *unofficial* robin_stocks
library, which (a) violates Robinhood's ToS for automated orders and (b) re-triggers a
device-approval challenge every few days (the 429 loop). Robinhood's first-party Trading
MCP is OAuth-based (refresh tokens → no re-login churn) and sanctioned for agents. This
module talks to it. It trades/reads ONLY the dedicated *Agentic* account, never the main
account — that boundary is Robinhood-enforced, not ours.

DESIGN (see the plan file "APPROVED DIRECTION"):
  - All MCP + OAuth code is isolated HERE (same rule robin_stocks stays only in
    ingestion/robinhood.py). Callers see the same read interface as robin_stocks:
    fetch_positions() / fetch_buying_power() / fetch_quotes() — so swapping is a config flag.
  - config.USE_MCP gates whether this path is used at all (default False → today's Argus).
  - config.DRY_RUN gates execution: True → an ALLOWED order is logged, not sent. Default True.
  - trading_guards.check_order() vets every order in BOTH dry and live runs.

STATUS: read/order plumbing is scaffolded but the actual MCP tool calls are NOT wired yet —
they are blocked on the OAuth-handshake spike (plan gate #1: which tool names + order types
the server exposes). _call_tool() is the single chokepoint to fill in once the spike returns
`tools/list`. Until then, reads degrade to empty (like robin_stocks logged-out) and order
placement only works under DRY_RUN. Nothing here can place a live order yet.
"""
from __future__ import annotations

import config
from trading_guards import GuardState, OrderIntent, check_order, record_placed

# Tool names the Robinhood MCP exposes. PLACEHOLDERS — fill from the spike's `tools/list`
# output (plan gate #1). Kept as constants so wiring is a one-place edit.
_TOOL_POSITIONS = "get_positions"       # TODO(spike): confirm exact tool name
_TOOL_BUYING_POWER = "get_buying_power"  # TODO(spike): confirm exact tool name
_TOOL_QUOTES = "get_quotes"             # TODO(spike): confirm exact tool name
_TOOL_PLACE_ORDER = "place_order"       # TODO(spike): confirm exact tool name + arg schema


class MCPNotWired(RuntimeError):
    """Raised when a live MCP call is attempted before the spike has wired _call_tool.
    Reads catch this and degrade to empty; live orders surface it (dry-run never hits it)."""


def is_available() -> bool:
    """True only when the MCP path is switched on. Callers use this to decide whether to
    prefer this module over the robin_stocks reads. Does not prove the OAuth session is
    live — that's checked lazily on first _call_tool."""
    return bool(getattr(config, "USE_MCP", False))


def _call_tool(name: str, arguments: dict | None = None):
    """Single chokepoint for every Robinhood MCP tool invocation.

    Delegates to the OAuth transport in ingestion.mcp_auth (imported lazily so CI, which
    doesn't install `mcp`, can still import this module). Non-interactive: uses the stored
    token (silent refresh); never launches a browser from here. Returns the tool result
    already unwrapped into a plain dict/list for the normalizers.

    Raises MCPNotWired if the SDK isn't installed or no session is stored (login not done) —
    reads catch it and degrade to empty; orders surface it.
    """
    try:
        from ingestion import mcp_auth
    except ImportError as e:
        raise MCPNotWired(f"mcp SDK not installed — {e}") from e

    try:
        result = mcp_auth.call_tool(name, arguments)
    except mcp_auth.NotAuthenticated as e:
        raise MCPNotWired(str(e)) from e
    return _unwrap_tool_result(result)


def _unwrap_tool_result(result):
    """Turn an MCP CallToolResult into a plain Python object. Prefers structuredContent;
    else parses the text content blocks as JSON; else returns the raw text/result."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # Some servers wrap the payload as {"result": ...}; unwrap that common shape.
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


# --- reads: mirror ingestion/robinhood.py shapes so callers swap cleanly --------------

def fetch_positions() -> list[dict]:
    """Same shape as ingestion.robinhood.fetch_positions (ticker/company_name/shares/
    avg_cost/current_price/amount_invested/equity/pnl_pct). Degrades to [] until wired."""
    if not is_available():
        return []
    try:
        raw = _call_tool(_TOOL_POSITIONS)
    except MCPNotWired as e:
        print(f"Robinhood MCP positions: {e}")
        return []
    except Exception as e:  # noqa: BLE001 — never crash a dashboard rerun on a read
        print(f"Robinhood MCP positions: fetch failed — {e}")
        return []
    return _normalize_positions(raw)


def fetch_buying_power() -> float | None:
    """Same contract as ingestion.robinhood.fetch_buying_power: dollars or None. Reads the
    AGENTIC account's buying power. Degrades to None until wired."""
    if not is_available():
        return None
    try:
        raw = _call_tool(_TOOL_BUYING_POWER)
    except MCPNotWired as e:
        print(f"Robinhood MCP buying power: {e}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Robinhood MCP buying power: fetch failed — {e}")
        return None
    return _normalize_buying_power(raw)


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """Same shape as ingestion.robinhood.fetch_quotes: {ticker: {price,change,change_pct,
    high,low}} or {ticker: None}. Degrades to {} until wired."""
    if not tickers or not is_available():
        return {}
    try:
        raw = _call_tool(_TOOL_QUOTES, {"symbols": list(tickers)})
    except MCPNotWired as e:
        print(f"Robinhood MCP quotes: {e}")
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"Robinhood MCP quotes: fetch failed — {e}")
        return {}
    return _normalize_quotes(tickers, raw)


# --- normalizers: parse whatever the MCP returns into Argus's existing shapes ----------
# Schemas are unknown until the spike; these are written defensively and MUST be revisited
# against the real payloads (they currently assume dict-ish records with common field names).

def _normalize_positions(raw) -> list[dict]:
    positions: list[dict] = []
    for item in raw or []:
        try:
            ticker = item.get("symbol") or item.get("ticker")
            shares = float(item.get("quantity", 0) or 0)
            if not ticker or shares <= 0:
                continue
            avg_cost = float(item.get("average_cost", item.get("average_buy_price", 0)) or 0)
            current = float(item.get("price", 0) or 0)
            positions.append({
                "ticker":          ticker,
                "company_name":    item.get("name", ticker),
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


def _normalize_buying_power(raw) -> float | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float, str)):
            return round(float(raw), 2)
        for field in ("buying_power", "cash", "cash_available"):
            if isinstance(raw, dict) and raw.get(field) is not None:
                return round(float(raw[field]), 2)
    except (ValueError, TypeError):
        return None
    return None


def _normalize_quotes(tickers: list[str], raw) -> dict[str, dict]:
    # Accept either a dict keyed by symbol or a list aligned to `tickers`.
    by_symbol: dict = {}
    if isinstance(raw, dict):
        by_symbol = raw
    elif isinstance(raw, list):
        by_symbol = {t: q for t, q in zip(tickers, raw)}

    results: dict[str, dict] = {}
    for ticker in tickers:
        q = by_symbol.get(ticker)
        if not q or not isinstance(q, dict):
            results[ticker] = None
            continue
        try:
            last = float(q.get("last_price", q.get("price", 0)) or 0)
            prev = float(q.get("previous_close", 0) or 0)
            if last == 0:
                results[ticker] = None
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

def place_order(intent: OrderIntent, state: GuardState, buying_power: float) -> dict:
    """Vet an order through trading_guards, then either DRY_RUN-log it or (live) send it.

    Returns a result dict: {status, reason, intent, dry_run}. status is one of
    'rejected' (a guard blocked it), 'dry_run' (allowed + logged, not sent), or 'placed'
    (live send — not reachable until _call_tool is wired). Never raises on a guard
    rejection; a live-send failure propagates so the caller can halt.
    """
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

    # Live path — blocked until the spike wires _call_tool. record_placed only after a
    # confirmed send so the idempotency/counter state can't run ahead of reality.
    result = _call_tool(_TOOL_PLACE_ORDER, _order_args(intent))
    record_placed(intent, state)
    print(f"[ORDER PLACED] {intent.side} {intent.ticker} :: {intent.reason}")
    return {"status": "placed", "reason": "sent", "intent": intent, "dry_run": False, "raw": result}


def _order_args(intent: OrderIntent) -> dict:
    """Map an OrderIntent to the MCP place_order argument schema. PLACEHOLDER field names —
    confirm against the real tool schema from the spike (plan gate #1)."""
    args = {"symbol": intent.ticker, "side": intent.side, "type": intent.order_type}
    if intent.dollars is not None:
        args["amount"] = intent.dollars
    if intent.quantity is not None:
        args["quantity"] = intent.quantity
    if intent.limit_price is not None:
        args["limit_price"] = intent.limit_price
    if intent.stop_price is not None:
        args["stop_price"] = intent.stop_price
    if intent.client_id:
        args["client_order_id"] = intent.client_id
    return args
