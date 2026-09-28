"""
PaperBroker — stands in for ingestion/robinhood_mcp in a paper cycle, so paper runs the SAME agent code as
live (alerts/agentic_options.run_options_agent: signals, routing, sizing, the $20 stock cap, PDT, the shadow
judge, the decision log, exits) and only the fills are virtual.

Same call surface the agent uses on the live module: is_available, agentic_account_number,
fetch_buying_power, fetch_positions, fetch_option_positions, day_trade_budget, place_order,
place_option_order. Orders pass the SAME trading_guards checks as live (plus the equity order-shape rule),
then fill against storage/paper_book at LIVE prices:
  - options: at the intent's limit price — the ask on an entry, the bid on an exit (what the agent sends);
  - shares: buys at the live ask, sells at the live bid (last price if the book side is missing).
No broker review (it would run against the real account). No resting stops — the same as live for the
FRACTIONAL positions the $20 cap produces (Robinhood allows stops on whole shares only); a whole-share paper
position is protected only by the poll's hard stop, so a gap through the stop fills at the next cycle's price.
Market data (quotes, option chains) is read live via the real MCP.
"""
from __future__ import annotations

from ingestion import robinhood_mcp as live
from storage import paper_book as book
from trading_guards import check_option_order, check_order, record_placed

PAPER_ACCOUNT = "PAPER"


def fill_price(quote: dict | None, side: str) -> float | None:
    """Where a market order would fill: a buy at the ask, a sell at the bid (the spread is a real cost
    of every round trip). Falls back to the last price when the side of the book is missing."""
    q = quote or {}
    return (q.get("ask") if side == "buy" else q.get("bid")) or q.get("price") or None


class PaperBroker:
    PAPER = True
    MIN_DOLLAR_ORDER = live.MIN_DOLLAR_ORDER

    def is_available(self) -> bool:
        return live.is_available()          # quotes + option data still come from the MCP

    def agentic_account_number(self) -> str:
        return PAPER_ACCOUNT

    def fetch_buying_power(self, account_number: str | None = None) -> float:
        return book.cash()

    def fetch_positions(self, account_number: str | None = None) -> list[dict]:
        """Paper share positions in live's shape (ticker/shares/avg_cost/current_price/equity/pnl_pct)."""
        held = book.get_book()["shares"]
        rows = [{"symbol": t, "quantity": h["shares"], "average_buy_price": h["avg_cost"]} for t, h in held.items()]
        return live._normalize_positions(rows, live.fetch_quotes(list(held)) if held else {})

    def fetch_option_positions(self, account_number: str | None = None) -> list[dict]:
        """Paper contracts in get_option_positions' row shape. average_open_price is PER SHARE (the exit
        pass divides values > 5 by 100 — a >$5/share contract is far beyond the pilot's cash)."""
        return [{"chain_symbol": p["ticker"], "option_id": p["option_id"], "quantity": p["qty"],
                 "average_open_price": p["entry_price"], "expiration_date": p["expiration"], "type": "long"}
                for p in book.get_book()["options"]]

    def day_trade_budget(self, account_number: str, account_equity: float) -> int | None:
        """Same PDT rule as live, over the paper fills (so paper opens only what live could have)."""
        import market_hours as mh
        from trading_guards import PDT_WINDOW_DAYS, day_trade_budget
        fills = [dict(f, date=mh.et_date(f["ts"])) for f in book.get_book()["fills"]]
        return day_trade_budget(fills, mh.recent_trading_days(PDT_WINDOW_DAYS), mh._now_et().date(), account_equity)

    def place_order(self, intent, state, buying_power: float, account_number: str | None = None) -> dict:
        reason = live._order_shape_error(intent)
        if not reason:
            verdict = check_order(intent, state, buying_power)
            reason = None if verdict.allowed else verdict.reason
        if not reason and intent.order_type != "market":
            reason = "paper fills market orders only (no resting stops/limits)"
        price = None
        if not reason:
            price = fill_price(live.fetch_quotes([intent.ticker]).get(intent.ticker), intent.side)
            if not price:
                reason = "no live price to fill against"
        if not reason:
            if intent.side == "buy":
                reason = book.buy_shares(intent.ticker, intent.dollars, price)
            elif book.sell_shares(intent.ticker, intent.quantity, price, intent.reason) is None:
                reason = "no paper shares to sell"
        return self._result(intent, state, reason, f"{intent.side} {intent.ticker} @ ${price}")

    def place_option_order(self, intent, state, buying_power: float, account_number: str | None = None) -> dict:
        verdict = check_option_order(intent, state, buying_power)
        reason = None if verdict.allowed else verdict.reason
        if not reason and intent.position_effect == "open":
            reason = book.open_option({"option_id": intent.option_id, "ticker": intent.underlying,
                                       "right": intent.right, "strike": intent.strike,
                                       "expiration": intent.expiration, "strategy": intent.reason,
                                       "qty": intent.quantity, "entry_price": intent.price})
        elif not reason and book.close_option(intent.option_id, intent.price, intent.reason) is None:
            reason = "no paper contract to close"
        return self._result(intent, state, reason, f"{intent.side} {intent.quantity} {intent.underlying} @ {intent.price}")

    @staticmethod
    def _result(intent, state, reason: str | None, tag: str) -> dict:
        if reason:
            print(f"[PAPER REJECTED] {tag} — {reason}")
            return {"status": "rejected", "reason": reason, "intent": intent, "dry_run": False}
        record_placed(intent, state)
        print(f"[PAPER FILL] {tag} :: {intent.reason}")
        return {"status": "placed", "reason": "paper fill", "intent": intent, "dry_run": False}
