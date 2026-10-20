"""
Run the overhaul's candidate strategies against SPY and print / save the results table (Phase 1).

Periods are fixed up front: IN-SAMPLE 2005–2015 (where the rules' parameters could have been chosen) and
OUT-OF-SAMPLE 2016–today (the honest test). A strategy "beats SPY" only if it does so out-of-sample, judged on
return AND risk. Free data (yfinance adjusted closes, dividends included), no LLM, $0 per run.

    venv\\Scripts\\python.exe scripts\\backtest_strategies.py [--out backtest_strategy_results.md] [--cost-bps 5]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest import strategies as S                     # noqa: E402
from backtest.strategy_backtest import metrics, run      # noqa: E402

PERIODS = {"in-sample 2005–2015": (date(2005, 1, 1), date(2015, 12, 31)),
           "out-of-sample 2016–now": (date(2016, 1, 1), date.today())}
POLITICIAN_START = date(2023, 2, 8)          # NANC (Democrat-trade ETF) inception; KRUZ (Republican) was delisted

CANDIDATES = {
    "SPY buy & hold (benchmark)": S.buy_hold("SPY"),
    "QQQ buy & hold": S.buy_hold("QQQ"),
    "Trend filter: SPY > 200d else T-bills": S.trend_filter("SPY"),
    "Trend filter: QQQ > 200d else T-bills": S.trend_filter("QQQ"),
    "Sector rotation: top 3 by 6-mo, 200d filter": S.sector_rotation(lookbacks=S.MOMENTUM_6M),
    "Sector rotation: top 3 by 3/6/12-mo, 200d filter": S.sector_rotation(lookbacks=S.MOMENTUM_BLEND),
}
COLUMNS = [("cagr_pct", "CAGR %"), ("total_pct", "Total %"), ("max_dd_pct", "Max DD %"), ("vol_pct", "Vol %"),
           ("sharpe", "Sharpe"), ("months_beat_bench_pct", "Months > SPY %"), ("trades_per_yr", "Trades/yr"),
           ("turnover_per_yr", "Turnover/yr")]


def download(tickers: list[str]) -> dict[str, list[tuple[date, float]]]:
    """Daily ADJUSTED closes (splits + dividends) for each ticker, full history. Missing → []."""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            h = yf.Ticker(t).history(period="max", interval="1d", auto_adjust=True)
            out[t] = [(ix.date(), float(c)) for ix, c in h["Close"].items() if c == c]
        except Exception as e:  # noqa: BLE001
            print(f"download failed for {t}: {e}")
            out[t] = []
        print(f"  {t}: {len(out[t])} days from {out[t][0][0] if out[t] else '—'}")
    return out


def table(rows: list[tuple[str, dict]]) -> str:
    head = "| Strategy | " + " | ".join(label for _, label in COLUMNS) + " |"
    sep = "|---|" + "---:|" * len(COLUMNS)
    body = ["| " + name + " | " + " | ".join("" if m.get(k) is None else str(m[k]) for k, _ in COLUMNS) + " |"
            for name, m in rows]
    return "\n".join([head, sep, *body])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backtest_strategy_results.md")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    args = ap.parse_args(argv)

    print("Downloading prices…")
    prices = download(sorted({"SPY", "QQQ", S.CASH, "NANC", *S.SECTORS}))
    report = [f"# Strategy backtest — {date.today()}",
              f"Monthly rebalance (first trading day, at the close; signals use the prior close). Cost "
              f"{args.cost_bps:g} bp per $ traded. Idle cash 0% (T-bills via {S.CASH} once it exists, 2007-05). "
              "Sharpe uses a 0% risk-free rate. Adjusted closes (dividends reinvested)."]
    for label, (start, end) in PERIODS.items():
        bench = run(prices, CANDIDATES["SPY buy & hold (benchmark)"], start, end, cost_bps=args.cost_bps)
        rows = [(name, metrics(run(prices, fn, start, end, cost_bps=args.cost_bps),
                             None if name.startswith("SPY buy") else bench))
                for name, fn in CANDIDATES.items()]
        report += [f"\n## {label}", table(rows)]

    nanc = run(prices, S.buy_hold("NANC"), POLITICIAN_START, date.today(), cost_bps=args.cost_bps)
    spy = run(prices, S.buy_hold("SPY"), POLITICIAN_START, date.today(), cost_bps=args.cost_bps)
    report += [f"\n## Politician copy (proxy) — since {POLITICIAN_START}",
               "Free congressional-trade datasets are gone (House/Senate Stock Watcher → 403), and disclosures lag "
               "weeks–months, so the professional implementation is the test: NANC (tracks Democrat members' "
               "disclosed trades, 0.74% fee). KRUZ (Republican side) has been delisted.",
               table([("NANC buy & hold", metrics(nanc, spy)), ("SPY buy & hold", metrics(spy))]),
               "\n## Post-earnings drift — not tested",
               "Not testable reliably with the data on hand: Robinhood's earnings history covers 8 quarters, the "
               "universe would be today's survivors only, and recent studies find the drift largely gone in large "
               "caps (Martineau, 'Rest in Peace PEAD'). A number from this would mislead more than inform."]
    text = "\n".join(report) + "\n"
    print("\n" + text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Saved → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
