"""
Agentic protective stops (R25, Path B / Design A) — the #2 driver: auto-exit WITHOUT watching.

Places a STANDING native stop order (GTC stop_market) for each position held in the dedicated
AGENTIC account, so Robinhood auto-sells if the stop is hit — no polling, no 24/7 attention.
Only the agentic account is covered (the MCP rejects orders on the main account); main-book
positions stay manual.

SAFETY:
  - Confirm-first: each stop is previewed via review_equity_order before placement.
  - DRY_RUN (config): place_order logs the intended stop and sends nothing when DRY_RUN=True.
  - trading_guards vets every order (idempotency/day-cap/etc.).
  - Whole-share only: Robinhood allows stop orders only on whole-share positions, so a purely
    fractional holding (<1 share) is skipped (it would need a market-sell-on-trigger path).

The stop distance comes from R24 structure (ingestion.prices key_levels.stop_pct_atr) applied to
the position's live price — the same ATR-sized stop the analyst uses — so it's per-chart, not a
flat %. Pure planning (plan_protective_stops) is unit-tested; the orchestration reads live data.
"""
from __future__ import annotations

import math

import config
from trading_guards import GuardState, OrderIntent


def _stop_price(current_price: float, stop_pct: float) -> float | None:
    """Long protective stop price: current × (1 − stop_pct/100), rounded to cents."""
    if not current_price or stop_pct is None or stop_pct <= 0:
        return None
    return round(current_price * (1 - stop_pct / 100.0), 2)


def plan_protective_stops(positions: list[dict], stop_pct_by_ticker: dict[str, float]) -> list[OrderIntent]:
    """Pure: build sell GTC stop_market OrderIntents for whole-share agentic positions.

    positions          : agentic positions (ticker, shares, current_price).
    stop_pct_by_ticker : {ticker: ATR stop %} from R24 structure.
    Skips fractional-only holdings (<1 whole share) and tickers with no usable stop %. The
    client_id encodes ticker + stop price so re-running can't double-place the same stop."""
    intents: list[OrderIntent] = []
    for p in positions or []:
        ticker = p.get("ticker")
        shares = p.get("shares") or 0
        whole = int(math.floor(shares))
        if not ticker or whole < 1:
            continue  # fractional-only → stop orders not allowed; needs market-sell-on-trigger
        stop_pct = stop_pct_by_ticker.get(ticker)
        stop_px = _stop_price(p.get("current_price"), stop_pct)
        if stop_px is None:
            continue
        intents.append(OrderIntent(
            ticker=ticker,
            side="sell",
            quantity=whole,
            order_type="stop",              # → place_equity_order type 'stop_market'
            stop_price=stop_px,
            time_in_force="gtc",            # MUST rest until hit — gfd would expire at the close
            reason=f"protective stop ~{stop_pct}% (ATR) on {whole} sh",
            client_id=f"stop-{ticker}-{stop_px}",
        ))
    return intents


_OPEN_STATES = ("queued", "confirmed", "unconfirmed", "open")


def _open_stops(orders_payload) -> dict[str, dict]:
    """Map ticker → its OPEN stop sell order details ({order_id, tif}). Used to skip tickers
    already protected by a GTC stop, and to cancel-and-replace a non-GTC (gfd) one."""
    out: dict[str, dict] = {}
    data = orders_payload.get("data") if isinstance(orders_payload, dict) else None
    rows = (data or {}).get("orders", []) if isinstance(data, dict) else []
    for o in rows:
        state = (o.get("state") or "").lower()
        otype = (o.get("type") or "").lower()
        side = (o.get("side") or "").lower()
        sym = o.get("symbol")
        # Robinhood reports a stop as type='market'/'limit' WITH a stop_price set — so detect
        # by the stop_price, not the type string ('stop' in type alone misses real stops).
        try:
            has_stop_price = float(o.get("stop_price") or 0) > 0
        except (ValueError, TypeError):
            has_stop_price = False
        is_stop = has_stop_price or "stop" in otype
        if sym and side == "sell" and is_stop and state in _OPEN_STATES:
            out[sym] = {"order_id": o.get("id") or o.get("order_id"),
                        "tif": (o.get("time_in_force") or "").lower()}
    return out


def sync_protective_stops(verbose: bool = True) -> list[dict]:
    """Place a protective GTC stop for each agentic position that lacks one.

    Reads the agentic account's positions + open orders, computes each ATR stop from R24
    structure, previews via review_equity_order, then place_order (DRY_RUN-safe + guarded).
    Returns a list of per-position result dicts. Read/label only under DRY_RUN — nothing is
    sent while config.DRY_RUN is True."""
    from ingestion import robinhood_mcp as mcp
    from ingestion import mcp_auth

    if not mcp.is_available():
        return [{"status": "skipped", "reason": "USE_MCP off"}]

    acct = mcp.agentic_account_number()
    if not acct:
        return [{"status": "skipped", "reason": "no agentic account"}]

    # stop_market is regular-hours-only — Robinhood rejects it pre/post-market. Don't attempt
    # a placement off-hours (it would only produce a confusing rejection). Reads above are fine.
    if not config.DRY_RUN:
        try:
            import market_hours as mh
            status = mh.market_session()["status"]
            if status not in ("open", "open_half_day"):
                return [{"status": "skipped",
                         "reason": f"market {status} — stop_market places only in regular hours (9:30–16:00 ET)"}]
        except Exception as e:  # noqa: BLE001 — if the calendar fails, fall through and let RH decide
            print(f"agentic_stops: market-hours check failed — {e}")

    positions = mcp.fetch_positions(acct)
    if not positions:
        return [{"status": "noop", "reason": "no agentic positions to protect"}]

    # Existing open stop orders: a GTC stop means the ticker is already protected (skip); a
    # non-GTC (gfd) stop must be cancelled + replaced (it would expire at the close).
    try:
        orders = mcp._unwrap_tool_result(mcp_auth.call_tool("get_equity_orders", {"account_number": acct}))
        open_stops = _open_stops(orders)
    except Exception as e:  # noqa: BLE001
        print(f"agentic_stops: could not read existing orders — {e}")
        open_stops = {}

    protected = {t for t, s in open_stops.items() if s.get("tif") == "gtc"}
    to_replace = {t: s for t, s in open_stops.items() if s.get("tif") and s["tif"] != "gtc"}

    # Cancel stale non-GTC stops so we can re-place them as GTC (DRY_RUN logs the intent).
    for ticker, s in to_replace.items():
        oid = s.get("order_id")
        if not oid:
            continue
        if config.DRY_RUN:
            print(f"[DRY_RUN CANCEL] stale {s.get('tif')} stop on {ticker} (order {oid})")
        else:
            try:
                mcp_auth.call_tool("cancel_equity_order", {"account_number": acct, "order_id": oid})
                print(f"[CANCELLED] stale {s.get('tif')} stop on {ticker} (order {oid})")
            except Exception as e:  # noqa: BLE001
                print(f"agentic_stops: cancel failed for {ticker} — {e}; leaving as-is, skipping")
                protected.add(ticker)  # couldn't cancel → don't stack a second stop

    # R24 ATR stop % per ticker from live structure.
    stop_pct_by_ticker: dict[str, float] = {}
    try:
        from ingestion.prices import fetch_price_history
        hist = fetch_price_history([p["ticker"] for p in positions], asset_type="stock") or {}
        for t, d in hist.items():
            kl = (d or {}).get("key_levels") or {}
            if kl.get("stop_pct_atr") is not None:
                stop_pct_by_ticker[t] = kl["stop_pct_atr"]
    except Exception as e:  # noqa: BLE001
        print(f"agentic_stops: structure lookup failed — {e}")

    to_place = [p for p in positions if p["ticker"] not in protected]
    intents = plan_protective_stops(to_place, stop_pct_by_ticker)

    # Guard state sized to the agentic account value (denominator for caps/kill-switch).
    start_equity = sum((p.get("equity") or 0) for p in positions) or 0.0
    state = GuardState(start_equity=start_equity)

    results: list[dict] = []
    for intent in intents:
        # Confirm-first: preview the exact stop before (maybe) placing it.
        try:
            preview = mcp._unwrap_tool_result(mcp_auth.call_tool("review_equity_order", {
                "account_number": acct, "symbol": intent.ticker, "side": "sell",
                "type": "stop_market", "quantity": str(intent.quantity),
                "stop_price": f"{intent.stop_price:.2f}",
                "time_in_force": intent.time_in_force or "gtc",
            }))
            checks = ((preview.get("data") or {}).get("order_checks")) if isinstance(preview, dict) else None
            if verbose:
                print(f"[REVIEW] sell {intent.quantity} {intent.ticker} stop @ ${intent.stop_price} "
                      f"— checks: {checks or 'none'}")
        except Exception as e:  # noqa: BLE001 — a failed preview must not place the order
            print(f"agentic_stops: review failed for {intent.ticker} — {e}; skipping")
            results.append({"ticker": intent.ticker, "status": "review_failed", "reason": str(e)})
            continue

        try:
            res = mcp.place_order(intent, state, buying_power=start_equity, account_number=acct)
            results.append({"ticker": intent.ticker, "stop_price": intent.stop_price,
                            **{k: res[k] for k in ("status", "reason")}})
        except Exception as e:  # noqa: BLE001 — surface a rejection, don't abort the whole run
            print(f"[ORDER FAILED] {intent.ticker} stop @ ${intent.stop_price} — {e}")
            results.append({"ticker": intent.ticker, "stop_price": intent.stop_price,
                            "status": "failed", "reason": str(e)})

    return results
