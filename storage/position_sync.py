"""
Close tracked positions the user already sold on Robinhood (sidebar "Sync positions").

Sync used to only ADD new holdings; a position sold in the Robinhood app stayed "open" in Argus until it
was closed by hand. Now: a tracked open long that's no longer held is matched to the account's executed
sells since it was opened, and closed at their quantity-weighted average fill price — so realized P&L
and the scorecard use the real exit.

Never guesses: no matching sell (a transfer, or a sale before the position was tracked) → left open and
reported as unresolved. Crypto is skipped (the main account's coins aren't in the equity position read,
so every coin would look "sold"). Shorts are skipped (Argus shorts are tracked ideas, not broker shorts).
`sold_positions` is pure; `close_sold` does the reads + writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _aware(ts: str | None) -> datetime | None:
    """ISO timestamp → aware datetime. Naive values are local time (positions.json writes datetime.now())."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def sold_positions(open_positions: list[dict], held_tickers, sells: list[dict],
                   skip_tickers=frozenset()) -> tuple[list[dict], list[str]]:
    """Pure: (closes [{ticker, price, qty, filled_at}], unresolved tickers) for tracked open longs that
    are no longer held. A sell counts only if it filled after the position was opened."""
    held = {t.upper() for t in held_tickers}
    skip = {t.upper() for t in skip_tickers}
    closes, unresolved = [], []
    for p in open_positions:
        t = (p.get("ticker") or "").upper()
        if not t or p.get("direction", "buy") != "buy" or t in held or t in skip:
            continue
        opened = _aware(p.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc)
        fills = [s for s in sells
                 if s["ticker"] == t and (_aware(s.get("filled_at")) or opened) >= opened]
        qty = sum(s["qty"] for s in fills)
        if not qty:
            unresolved.append(t)
            continue
        closes.append({"ticker": t, "qty": qty,
                       "price": round(sum(s["qty"] * s["price"] for s in fills) / qty, 4),
                       "filled_at": max(str(s.get("filled_at") or "") for s in fills)})
    return closes, unresolved


def close_sold(held_tickers) -> dict:
    """Close every tracked position sold on Robinhood. `held_tickers` must come from a SUCCESSFUL position
    read (an empty read on error would make everything look sold — the caller gates on that).
    → {"closed": [{ticker, pnl_pct}], "unresolved": [ticker], "error": str | None}"""
    from ingestion.account_reads import filled_sells
    from ingestion.coingecko import TICKER_TO_COINGECKO_ID
    from storage.positions import close_position, get_open_positions

    open_positions = get_open_positions()
    candidates = [p for p in open_positions if (p.get("ticker") or "").upper() not in
                  {t.upper() for t in held_tickers}]
    if not candidates:
        return {"closed": [], "unresolved": [], "error": None}
    starts = [d for d in (_aware(p.get("opened_at")) for p in candidates) if d]
    since = (min(starts) - timedelta(days=1)).date().isoformat() if starts else "2000-01-01"
    sells = filled_sells(since)
    if sells is None:
        return {"closed": [], "unresolved": [], "error": "could not read Robinhood order history"}
    closes, unresolved = sold_positions(open_positions, held_tickers, sells,
                                        skip_tickers=TICKER_TO_COINGECKO_ID.keys())
    closed = []
    for c in closes:
        rec = close_position(c["ticker"], f"Sold on Robinhood ({str(c['filled_at'])[:10]})", c["price"])
        closed.append({"ticker": c["ticker"], "pnl_pct": (rec or {}).get("pnl_pct")})
    return {"closed": closed, "unresolved": unresolved, "error": None}
