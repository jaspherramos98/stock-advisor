"""
Close tracked positions the user already sold on Robinhood (sidebar "Sync positions").

Sync used to only ADD new holdings; a position sold in the Robinhood app stayed "open" in Argus until it
was closed by hand. Now: a tracked open long that's no longer held is matched to the account's executed
sells since it was opened, and closed at their quantity-weighted average fill price — so realized P&L
and the scorecard use the real exit.

Never guesses: no matching sell (a transfer, or a sale before the position was tracked) → left open and
reported as unresolved. Stocks and coins are matched SEPARATELY (equity positions vs equity sells, crypto
holdings vs crypto sells) — a coin never appears in the equity read, so mixing them would make every coin
look "sold", and SOL is both a stock and a coin. A side whose holdings read failed is skipped entirely.
Shorts are skipped (Argus shorts are tracked ideas, not broker shorts).
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


def sold_positions(open_positions: list[dict], held_tickers, sells: list[dict]) -> tuple[list[dict], list[str]]:
    """Pure: (closes [{ticker, price, qty, filled_at}], unresolved tickers) for tracked open longs that
    are no longer held. A sell counts only if it filled after the position was opened. One asset class
    per call (positions, holdings and sells all stocks, or all coins)."""
    held = {t.upper() for t in held_tickers}
    closes, unresolved = [], []
    for p in open_positions:
        t = (p.get("ticker") or "").upper()
        if not t or p.get("direction", "buy") != "buy" or t in held:
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


def close_sold(held_stocks: set | None, held_crypto: set | None) -> dict:
    """Close every tracked position sold on Robinhood. Each held set must come from a SUCCESSFUL read; pass
    None when that read failed (or returned nothing) and that side is left alone — an empty read on error
    would make everything look sold.
    → {"closed": [{ticker, pnl_pct}], "unresolved": [ticker], "errors": [str]}"""
    from ingestion.account_reads import filled_crypto_sells, filled_sells
    from ingestion.coingecko import is_crypto
    from storage.positions import close_position, get_open_positions

    out = {"closed": [], "unresolved": [], "errors": []}
    open_positions = get_open_positions()
    held_all = {t.upper() for t in (held_stocks or set()) | (held_crypto or set())}
    candidates = [p for p in open_positions if (p.get("ticker") or "").upper() not in held_all]
    if not candidates:
        return out
    starts = [d for d in (_aware(p.get("opened_at")) for p in candidates) if d]
    since = (min(starts) - timedelta(days=1)).date().isoformat() if starts else "2000-01-01"

    crypto_sells = filled_crypto_sells(since) if held_crypto is not None else None
    coins = {t.upper() for t in held_crypto or ()} | {s["ticker"] for s in crypto_sells or []}
    is_coin = lambda p: is_crypto(p.get("ticker")) or (p.get("ticker") or "").upper() in coins  # noqa: E731
    sides = (("stocks", [p for p in open_positions if not is_coin(p)], held_stocks, filled_sells),
             ("crypto", [p for p in open_positions if is_coin(p)], held_crypto, lambda _: crypto_sells))
    for name, positions, held, read_sells in sides:
        if not any((p.get("ticker") or "").upper() not in held_all for p in positions):
            continue                                   # everything on this side is still held
        sells = read_sells(since) if held is not None else None
        if sells is None:
            out["errors"].append(f"{name} not checked — Robinhood holdings or order history unreadable")
            continue
        closes, unresolved = sold_positions(positions, held, sells)
        out["unresolved"] += unresolved
        for c in closes:
            rec = close_position(c["ticker"], f"Sold on Robinhood ({str(c['filled_at'])[:10]})", c["price"])
            out["closed"].append({"ticker": c["ticker"], "pnl_pct": (rec or {}).get("pnl_pct")})
    return out
