"""
Performance scorecard — the honest read on whether Argus is actually making money.

WHY THIS EXISTS:
Win rate alone hides the truth. A 44%-win-rate book can be net positive (a few big
winners carry it) or net negative — and "wins ≈ losses, feels flat" is the classic
signature of *cutting trades early*: scratching losers at -1% before the stop, and
bailing winners before the target. This module exposes exactly that:

  - net $ and the SHARE OF PROFIT from the single best trade (concentration risk),
  - payoff ratio (avg win vs avg loss) and profit factor,
  - and the headline DISCIPLINE metric — avg P&L of trades LET RUN to their
    target/stop vs trades MANUALLY CLOSED before either band was reached.

All pure logic (unit-tested, no network/LLM). `spy_benchmark` is the one optional
network helper (yfinance) — answers "would just holding SPY have beaten this?".
"""
import re

# Treat a realized P&L within this fraction of the planned band as "the band was hit"
# (prices rarely land exactly on the level; 90% of target counts as reaching it).
_BAND_TOLERANCE = 0.9


def parse_band(exit_condition: str):
    """
    Pull (target_pct, stop_pct) out of an exit_condition string such as
    "target 8% gain, stop loss at 4%". Returns (None, None) for either part it
    can't find. Works for both longs and shorts (same wording; P&L is already
    stored in the trade's favour by close_position)."""
    text = (exit_condition or "").lower()
    target = stop = None

    # "target 8%" or, when phrased without the word target, "... 10% gain".
    m = re.search(r"target\s*([\d.]+)\s*%", text) or re.search(r"([\d.]+)\s*%\s*gain", text)
    if m:
        target = float(m.group(1))

    m = re.search(r"stop(?:\s*loss)?(?:\s*at)?\s*([\d.]+)\s*%", text)
    if m:
        stop = float(m.group(1))

    return target, stop


def classify_exit(position: dict) -> str | None:
    """
    Classify how a closed trade actually ended, relative to its own plan:
      "target"  — realized P&L reached ~the gain target (let the winner run)
      "stop"    — realized loss reached ~the stop (the plan's loss, taken as designed)
      "early"   — closed BEFORE either band (the discipline leak: scratched early)
    Returns None if there's no numeric P&L to judge.
    """
    pnl = position.get("pnl_pct")
    if not isinstance(pnl, (int, float)):
        return None

    target, stop = parse_band(position.get("exit_condition"))
    if target is not None and pnl >= target * _BAND_TOLERANCE:
        return "target"
    if stop is not None and pnl <= -stop * _BAND_TOLERANCE:
        return "stop"
    return "early"


def _avg(xs):
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def compute_scorecard(closed: list[dict]) -> dict:
    """
    Aggregate closed positions into the honest scorecard. Counts only trades with a
    numeric pnl_pct; dollar metrics use the subset that also has pnl_dollars.
    Returns {"trades": 0} when there's nothing decided yet.
    """
    rows = [p for p in (closed or []) if isinstance(p.get("pnl_pct"), (int, float))]
    if not rows:
        return {"trades": 0}

    pnls   = [p["pnl_pct"] for p in rows]
    wins   = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]

    avg_win  = _avg(wins)
    avg_loss = _avg(losses)
    # Payoff ratio: how many $ won per $ lost on the average trade. >1 means winners
    # are bigger than losers (you can be profitable below 50% win rate).
    payoff   = round(avg_win / abs(avg_loss), 2) if losses else None
    win_rate = round(len(wins) / len(rows) * 100)
    # Expectancy per trade (in %): the long-run avg you make per trade taken.
    expectancy = round(sum(pnls) / len(rows), 2)

    # ── Dollar metrics (only trades that recorded amount/close) ──────────────
    dollar_rows = [p for p in rows if isinstance(p.get("pnl_dollars"), (int, float))]
    d = [p["pnl_dollars"] for p in dollar_rows]
    net_dollars   = round(sum(d), 2)
    gross_profit  = round(sum(x for x in d if x > 0), 2)
    gross_loss    = round(sum(x for x in d if x < 0), 2)
    profit_factor = round(gross_profit / abs(gross_loss), 2) if gross_loss else None

    # Concentration: what share of all profit came from the single best trade?
    # High share = the edge is one lucky trade, not a repeatable process.
    top_trade = None
    top_share = None
    if dollar_rows:
        best = max(dollar_rows, key=lambda p: p["pnl_dollars"])
        if best["pnl_dollars"] > 0 and gross_profit > 0:
            top_trade = {"ticker": best.get("ticker"), "pnl_dollars": round(best["pnl_dollars"], 2)}
            top_share = round(best["pnl_dollars"] / gross_profit * 100)

    # ── Discipline: let-run vs closed-early ──────────────────────────────────
    classes = [(p, classify_exit(p)) for p in rows]
    let_run = [p["pnl_pct"] for p, c in classes if c in ("target", "stop")]
    early   = [p["pnl_pct"] for p, c in classes if c == "early"]

    return {
        "trades":        len(rows),
        "win_rate":      win_rate,
        "avg_win_pct":   avg_win,
        "avg_loss_pct":  avg_loss,
        "payoff_ratio":  payoff,
        "expectancy_pct": expectancy,
        "net_dollars":   net_dollars,
        "gross_profit":  gross_profit,
        "gross_loss":    gross_loss,
        "profit_factor": profit_factor,
        "top_trade":     top_trade,
        "top_trade_profit_share": top_share,
        "discipline": {
            "let_run_count":  len(let_run),
            "let_run_avg":    _avg(let_run),
            "early_count":    len(early),
            "early_avg":      _avg(early),
        },
    }


def spy_benchmark(closed: list[dict]) -> dict | None:
    """
    Opportunity-cost check: for every $-realized trade, what would the SAME dollars
    held in SPY over the SAME entry→exit dates have returned? Sum it and compare to
    what Argus actually made. Best-effort: needs yfinance + the trade to have
    entry_date, closed_at, and amount_invested. Returns None if it can't be computed.
    """
    rows = [
        p for p in (closed or [])
        if isinstance(p.get("pnl_dollars"), (int, float))
        and isinstance(p.get("amount_invested"), (int, float))
        and p.get("entry_date") and p.get("closed_at")
    ]
    if not rows:
        return None

    try:
        import datetime as dt
        import yfinance as yf

        def _d(s):
            return dt.date.fromisoformat(str(s)[:10])

        starts = [_d(p["entry_date"]) for p in rows]
        ends   = [_d(p["closed_at"]) for p in rows]
        lo, hi = min(starts), max(ends)

        hist = yf.Ticker("SPY").history(
            start=lo.isoformat(),
            end=(hi + dt.timedelta(days=4)).isoformat(),
            interval="1d",
        )
        if hist.empty:
            return None
        closes = hist["Close"]
        # Map each date to the nearest available trading-day close at-or-after it.
        dates = [c.date() for c in closes.index]

        def _close_on_or_after(day):
            for i, dd in enumerate(dates):
                if dd >= day:
                    return float(closes.iloc[i])
            return None

        spy_net = 0.0
        used = 0
        for p in rows:
            entry_px = _close_on_or_after(_d(p["entry_date"]))
            exit_px  = _close_on_or_after(_d(p["closed_at"]))
            if not entry_px or not exit_px:
                continue
            ret = (exit_px - entry_px) / entry_px
            spy_net += p["amount_invested"] * ret
            used += 1

        if not used:
            return None

        argus_net = round(sum(p["pnl_dollars"] for p in rows), 2)
        return {
            "trades":    used,
            "argus_net": argus_net,
            "spy_net":   round(spy_net, 2),
            "edge":      round(argus_net - spy_net, 2),
        }
    except Exception as e:
        print(f"SPY benchmark failed: {e}")
        return None


if __name__ == "__main__":
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "positions.json")
    with open(path) as f:
        positions = json.load(f)
    closed = [p for p in positions if p.get("status") == "closed"]

    sc = compute_scorecard(closed)
    print(json.dumps(sc, indent=2))
    bench = spy_benchmark(closed)
    print("SPY benchmark:", json.dumps(bench, indent=2) if bench else "n/a")
