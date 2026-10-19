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


def filled_sells(since: str) -> list[dict] | None:
    """Executed sells since `since` (YYYY-MM-DD): [{ticker, qty, price, filled_at}]. MCP only — the
    robin_stocks path doesn't read order history, so it returns None (= unknown, close nothing)."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_filled_sells(since)
    return None


def crypto_quotes(tickers: list[str]) -> dict[str, dict]:
    """Coin quotes {TICKER: {price (mark), bid, ask}} — MCP only ({} otherwise; fetch_prices then falls
    back to Yahoo '<COIN>-USD'). quotes() routes coins here automatically."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_crypto_quotes(tickers)
    return {}


def crypto_positions() -> list[dict] | None:
    """Main-account crypto holdings (same keys as positions() + asset_type). None = unreadable — the
    robin_stocks path isn't wired for this, so it reports unknown rather than "holds nothing"."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        try:
            acct = robinhood_mcp._main_account_number()
        except Exception as e:  # noqa: BLE001
            print(f"Robinhood MCP crypto positions: account read failed — {e}")
            return None
        return robinhood_mcp.fetch_crypto_positions(acct, strict=True)
    return None


def filled_crypto_sells(since: str) -> list[dict] | None:
    """Executed crypto sells since `since` (YYYY-MM-DD): [{ticker, qty, price, filled_at}]. MCP only."""
    if _use_mcp():
        from ingestion import robinhood_mcp
        return robinhood_mcp.fetch_filled_crypto_sells(since)
    return None


def quotes(tickers: list[str]) -> dict[str, dict]:
    """Quotes keyed by ticker: {price,change,change_pct,high,low} or None per ticker.
    Coins (`coingecko.is_crypto`) go to the CRYPTO quote, never the equity one: ETH / BTC / XRP are also ETF
    tickers, and the equity quote priced the coin ETH at the ETF's $22.88 (2026-10-19). A coin's entry carries
    the mark price + bid/ask; change/high/low are 0.0 (= unknown — fetch_prices fills them from Yahoo)."""
    from ingestion.coingecko import is_crypto
    coins = [t for t in tickers if is_crypto(t)]
    stocks = [t for t in tickers if not is_crypto(t)]
    out: dict[str, dict] = {}
    if stocks:
        if _use_mcp():
            from ingestion import robinhood_mcp
            out.update(robinhood_mcp.fetch_quotes(stocks))
        else:
            from ingestion.robinhood import fetch_quotes
            out.update(fetch_quotes(stocks))
    if coins:
        cq = crypto_quotes(coins)
        for t in coins:
            q = cq.get(t.upper())
            out[t] = {**q, "change": 0.0, "change_pct": 0.0, "high": 0.0, "low": 0.0} if q else None
    return out
