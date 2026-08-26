"""
Account-read dispatcher — one place that decides WHERE Argus reads account data from.

Two backends, selected by config.USE_MCP:
  - USE_MCP True  → the official Robinhood Trading MCP (ingestion.robinhood_mcp). OAuth +
                    refresh tokens → NO device-approval 429. Reads the MAIN account by
                    default (the user's real book; the agentic account is a separate pilot).
  - USE_MCP False → the unofficial robin_stocks path (ingestion.robinhood) — today's Argus.

Deliberately NO cross-fallback: when USE_MCP is on we do NOT quietly fall back to
robin_stocks, because that would re-trigger the exact 429/device-approval login this swap
exists to kill. An MCP read that fails returns empty/None; the dashboard already renders
$0 / "unavailable" gracefully. News stays on robin_stocks (the MCP has no news endpoint);
that path is untouched — this module only covers buying power / positions / quotes.

Callers (dashboard/app.py, ingestion/prices.py) import from HERE instead of ingestion.robinhood
so the source is a single flag, not scattered branches.
"""
from __future__ import annotations

import config


def _use_mcp() -> bool:
    return bool(getattr(config, "USE_MCP", False))


def is_available() -> bool:
    """Whether account reads can be served right now. For MCP, true only when a stored OAuth
    session exists (cheap file check, no network, never opens a browser). For robin_stocks,
    whether credentials are configured."""
    if _use_mcp():
        try:
            from ingestion import mcp_auth
        except ImportError:
            return False
        return mcp_auth.has_session()
    from ingestion.robinhood import is_available as _rh_available
    return _rh_available()


def buying_power() -> float | None:
    """Live buying power in dollars, or None if unreadable."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_buying_power()
    from ingestion.robinhood import fetch_buying_power
    return fetch_buying_power()


def positions() -> list[dict]:
    """Open positions (normalized dicts: ticker/company_name/shares/avg_cost/current_price/
    amount_invested/equity/pnl_pct)."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_positions()
    from ingestion.robinhood import fetch_positions
    return fetch_positions()


def quotes(tickers: list[str]) -> dict[str, dict]:
    """Quotes keyed by ticker: {price,change,change_pct,high,low} or None per ticker."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_quotes(tickers)
    from ingestion.robinhood import fetch_quotes
    return fetch_quotes(tickers)
