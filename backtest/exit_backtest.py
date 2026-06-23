"""
Exit-band backtester — validate Argus's target/stop percentages on real price paths.

WHAT THIS DOES (and is honest about NOT doing):
- Given an entry and a band ("target X% / stop Y%"), it walks FORWARD on real daily
  OHLC and records the outcome (target hit / stop hit / time exit) + realized P&L.
  Aggregated over many sampled entries on a ticker, it gives win rate, average P&L,
  and expectancy per dollar risked for that band — so the bands stop being arbitrary.
- It does NOT model Argus's news-catalyst entry selection and does NOT replay the LLM.
  Entries here are SAMPLED at a fixed cadence, not real Argus signals. So this measures
  "is this stop/target band sane vs alternatives on this stock's behaviour", NOT
  "does Argus make money." That (full point-in-time news + LLM replay) is a separate,
  much larger effort.

No LLM tokens. Price history via yfinance (free). Walks forward only — no lookahead.
"""


def simulate_trade(highs, lows, closes, entry_price, target_pct, stop_pct,
                   max_days, direction="buy") -> dict:
    """
    Simulate one trade walking forward from entry over the given OHLC window (lists
    ordered entry+1 → forward, same length). Returns {outcome, pnl_pct, days}.

    - Long:  target if a day's HIGH ≥ entry×(1+t); stop if its LOW ≤ entry×(1−s).
    - Short: target if a day's LOW ≤ entry×(1−t) (profit on a fall);
             stop  if its HIGH ≥ entry×(1+s).
    - If BOTH target and stop are reachable on the same bar, assume the STOP filled
      first (conservative — no optimistic intrabar assumption).
    - No exit within max_days → close at that day's close (time exit).

    pnl_pct is the realized return in the trade's favour (positive = profit for the
    chosen direction).
    """
    t, s = target_pct / 100.0, stop_pct / 100.0
    if direction == "short":
        target_price, stop_price = entry_price * (1 - t), entry_price * (1 + s)
    else:
        target_price, stop_price = entry_price * (1 + t), entry_price * (1 - s)

    horizon = min(max_days, len(highs))
    for i in range(horizon):
        hi, lo = highs[i], lows[i]
        if direction == "short":
            hit_target = lo <= target_price
            hit_stop   = hi >= stop_price
        else:
            hit_target = hi >= target_price
            hit_stop   = lo <= stop_price

        if hit_stop:                      # conservative: stop wins a same-bar tie
            return {"outcome": "stop", "pnl_pct": -round(stop_pct, 2), "days": i + 1}
        if hit_target:
            return {"outcome": "target", "pnl_pct": round(target_pct, 2), "days": i + 1}

    # Time exit at the last available close in the horizon.
    if horizon == 0:
        return {"outcome": "none", "pnl_pct": 0.0, "days": 0}
    exit_close = closes[horizon - 1]
    raw = (exit_close - entry_price) / entry_price * 100
    pnl = -raw if direction == "short" else raw
    return {"outcome": "time", "pnl_pct": round(pnl, 2), "days": horizon}


def summarize(trades: list[dict]) -> dict:
    """Aggregate simulate_trade results into win rate / avg P&L / expectancy."""
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    pnls = [t["pnl_pct"] for t in trades]
    return {
        "trades":      n,
        "win_rate":    round(len(wins) / n * 100, 1),
        "target_hits": sum(1 for t in trades if t["outcome"] == "target"),
        "stop_hits":   sum(1 for t in trades if t["outcome"] == "stop"),
        "time_exits":  sum(1 for t in trades if t["outcome"] == "time"),
        "avg_pnl":     round(sum(pnls) / n, 2),
        "avg_days":    round(sum(t["days"] for t in trades) / n, 1),
    }


def backtest_exit_bands(ticker, target_pct, stop_pct, max_days=20, step=5,
                        period="2y", direction="buy") -> dict:
    """
    Sample an entry every `step` trading days over `period` of real history, simulate
    the target/stop band forward `max_days`, and aggregate. Returns the summary plus
    the band parameters. yfinance only; returns {"trades": 0} on no data.
    """
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=period, interval="1d")
    if hist.empty or len(hist) < max_days + step:
        return {"ticker": ticker, "trades": 0}

    highs  = hist["High"].tolist()
    lows   = hist["Low"].tolist()
    closes = hist["Close"].tolist()
    n = len(closes)

    trades = []
    for entry_idx in range(0, n - max_days - 1, step):
        entry_price = closes[entry_idx]
        fwd = slice(entry_idx + 1, entry_idx + 1 + max_days)
        trades.append(simulate_trade(
            highs[fwd], lows[fwd], closes[fwd],
            entry_price, target_pct, stop_pct, max_days, direction,
        ))

    out = summarize(trades)
    out.update({"ticker": ticker, "target_pct": target_pct, "stop_pct": stop_pct,
                "max_days": max_days, "direction": direction})
    return out


if __name__ == "__main__":
    # Compare Argus's regular-buy band vs HR band on a couple names (real prices).
    bands = [
        ("AAPL", 8, 3),   # regular buy
        ("AAPL", 15, 5),  # highly-recommended
        ("SPY", 8, 3),
        ("SPY", 3, 1.5),
    ]
    print(f"{'Ticker':6} {'tgt/stop':9} {'n':>4} {'win%':>6} {'avgP&L':>7} {'tgt':>4} {'stop':>4} {'time':>4} {'days':>5}")
    for tk, tgt, stp in bands:
        r = backtest_exit_bands(tk, tgt, stp)
        if r.get("trades"):
            print(f"{tk:6} {f'{tgt}/{stp}':9} {r['trades']:>4} {r['win_rate']:>6} "
                  f"{r['avg_pnl']:>7} {r['target_hits']:>4} {r['stop_hits']:>4} "
                  f"{r['time_exits']:>4} {r['avg_days']:>5}")
        else:
            print(f"{tk:6} {f'{tgt}/{stp}':9}  no data")
