import os
from dotenv import load_dotenv

load_dotenv()


# Yahoo Finance uses different symbols for crypto. Used by both the yfinance
# price fallback and the 14-day history fetch.
CRYPTO_YAHOO_MAP = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "BNB":  "BNB-USD",
    "XRP":  "XRP-USD",
}


def _yfinance_quote(ticker: str) -> dict | None:
    """
    Fallback current-price fetch via Yahoo Finance for tickers Robinhood
    doesn't carry (most crypto, some ETFs). Returns the same dict shape as
    fetch_prices, or None if unavailable.
    """
    import yfinance as yf

    yahoo_symbol = CRYPTO_YAHOO_MAP.get(ticker, ticker)

    try:
        hist = yf.Ticker(yahoo_symbol).history(period="2d", interval="1d")
        if hist.empty:
            return None

        last_price = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_price
        day_high   = float(hist["High"].iloc[-1])
        day_low    = float(hist["Low"].iloc[-1])

        if last_price == 0:
            return None

        change     = last_price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        return {
            "price":      round(last_price, 2),
            "change":     round(change, 2),
            "change_pct": round(change_pct, 2),
            "high":       round(day_high, 2),
            "low":        round(day_low, 2),
        }
    except Exception as e:
        print(f"yfinance price fetch error for {ticker}: {e}")
        return None


def fetch_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Fetches current price data for a list of tickers.

    Robinhood is the primary source — it's the actual trading platform so
    prices match exactly, and it has no free-tier quote cap like Finnhub did.
    Any ticker Robinhood can't resolve (most crypto, occasional ETFs) falls
    back to Yahoo Finance.

    Returns a dict keyed by ticker with price details inside:
    {
        "AAPL": {
            "price":   189.42,
            "change":  1.23,
            "change_pct": 0.65,
            "high":    190.10,
            "low":     187.80,
        },
        ...
    }
    """
    if not tickers:
        return {}

    results: dict[str, dict] = {}

    # --- Primary: Robinhood ---
    try:
        from ingestion.robinhood import is_available, fetch_quotes
        if is_available():
            rh_results = fetch_quotes(tickers)
            for ticker in tickers:
                if rh_results.get(ticker):
                    results[ticker] = rh_results[ticker]
    except Exception as e:
        print(f"Prices: Robinhood quote source unavailable — {e}")

    # --- Fallback: Yahoo Finance for anything Robinhood didn't cover ---
    remaining = [t for t in tickers if t not in results]
    for ticker in remaining:
        results[ticker] = _yfinance_quote(ticker)

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"Prices: fetched {fetched}/{len(tickers)} tickers successfully")
    return results


def _compute_technicals(closes, highs, lows, volumes) -> dict:
    """
    Computes standard technical indicators from daily OHLCV history
    (lists ordered oldest → newest). Pure deterministic math on real prices —
    no opinion, fully "fact-checked". Any indicator lacking enough history is None.
    Kept separate from yfinance so it can be unit-tested with synthetic data.
    """
    import pandas as pd

    close = pd.Series([float(c) for c in closes], dtype="float64")
    n = len(close)
    last = float(close.iloc[-1])
    out: dict = {}

    # RSI(14) with Wilder smoothing — >70 overbought, <30 oversold
    if n >= 15:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        ag = float(gain.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
        al = float(loss.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
        if al == 0:
            rsi_val = 100.0 if ag > 0 else 50.0  # no losses → max; flat → neutral
        else:
            rsi_val = 100 - (100 / (1 + ag / al))
        out["rsi_14"] = round(rsi_val, 1)
    else:
        out["rsi_14"] = None

    # MACD (12/26/9) — trend/momentum state + recent crossover
    if n >= 26:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal
        out["macd_state"] = "bullish" if macd_line.iloc[-1] > signal.iloc[-1] else "bearish"
        if len(hist) >= 2 and (hist.iloc[-1] > 0) != (hist.iloc[-2] > 0):
            out["macd_cross"] = "bullish crossover" if hist.iloc[-1] > 0 else "bearish crossover"
        else:
            out["macd_cross"] = "none recent"
    else:
        out["macd_state"] = None
        out["macd_cross"] = None

    # Moving averages — trend context
    sma50 = float(close.tail(50).mean()) if n >= 50 else None
    sma200 = float(close.tail(200).mean()) if n >= 200 else None
    out["sma_50"] = round(sma50, 2) if sma50 else None
    out["sma_200"] = round(sma200, 2) if sma200 else None
    out["price_vs_sma50"] = None if sma50 is None else ("above" if last >= sma50 else "below")
    out["price_vs_sma200"] = None if sma200 is None else ("above" if last >= sma200 else "below")
    if sma50 and sma200:
        out["ma_trend"] = "golden cross (50>200, bullish)" if sma50 > sma200 else "death cross (50<200, bearish)"
    else:
        out["ma_trend"] = None

    # 52-week range (intraday highs/lows)
    try:
        hi = max(float(h) for h in highs)
        lo = min(float(l) for l in lows)
        out["high_52w"] = round(hi, 2)
        out["low_52w"] = round(lo, 2)
        out["pct_from_52w_high"] = round((last - hi) / hi * 100, 1) if hi else None
        out["pct_from_52w_low"] = round((last - lo) / lo * 100, 1) if lo else None
    except (ValueError, ZeroDivisionError):
        out.update(high_52w=None, low_52w=None, pct_from_52w_high=None, pct_from_52w_low=None)

    # Volume vs 30-day average — is the move backed by real participation?
    if volumes and len(volumes) >= 5:
        vol = pd.Series([float(v) for v in volumes], dtype="float64")
        avg30 = float(vol.tail(30).mean())
        out["vol_vs_avg"] = round(float(vol.iloc[-1]) / avg30, 2) if avg30 else None
    else:
        out["vol_vs_avg"] = None

    return out


def _compute_rrg(etf_closes, bench_closes, window: int = 50, mom_window: int = 10) -> dict:
    """
    Simplified JdK-style Relative Rotation Graph (RRG) for one ETF vs a benchmark.
    Inputs are aligned daily closes (oldest → newest, same dates).

    - RS-Ratio    = 100 × relative / SMA(relative): >100 means the ETF is
      outperforming its own recent relative trend vs the benchmark.
    - RS-Momentum = 100 × RS-Ratio / SMA(RS-Ratio): >100 means that relative
      strength is still accelerating.
    - Quadrant    = the classic RRG read (Leading / Weakening / Lagging / Improving).
    - rel_perf_63d = plain relative return vs the benchmark over ~3 months (intuitive %).

    Pure deterministic math on real prices — fully unit-testable, no opinion.
    Returns all-None if there isn't enough aligned history.
    """
    import pandas as pd

    e = pd.Series([float(c) for c in etf_closes], dtype="float64")
    b = pd.Series([float(c) for c in bench_closes], dtype="float64")
    n = min(len(e), len(b))
    out = {"rs_ratio": None, "rs_momentum": None, "quadrant": None, "rel_perf_63d": None}
    if n < window + mom_window + 1:
        return out

    e = e.iloc[-n:].reset_index(drop=True)
    b = b.iloc[-n:].reset_index(drop=True)

    relative        = e / b
    rs_ratio_series = 100 * relative / relative.rolling(window).mean()
    rs_mom_series   = 100 * rs_ratio_series / rs_ratio_series.rolling(mom_window).mean()

    rs_ratio = rs_ratio_series.iloc[-1]
    rs_mom   = rs_mom_series.iloc[-1]
    if pd.isna(rs_ratio) or pd.isna(rs_mom):
        return out
    rs_ratio = float(rs_ratio)
    rs_mom   = float(rs_mom)

    if rs_ratio >= 100 and rs_mom >= 100:
        quad = "Leading"
    elif rs_ratio >= 100 and rs_mom < 100:
        quad = "Weakening"
    elif rs_ratio < 100 and rs_mom < 100:
        quad = "Lagging"
    else:
        quad = "Improving"

    look     = min(63, n - 1)
    rel_perf = ((e.iloc[-1] / e.iloc[-1 - look]) - (b.iloc[-1] / b.iloc[-1 - look])) * 100

    return {
        "rs_ratio":     round(rs_ratio, 1),
        "rs_momentum":  round(rs_mom, 1),
        "quadrant":     quad,
        "rel_perf_63d": round(float(rel_perf), 1),
    }


def fetch_etf_relative_strength(etf_tickers: list[str], benchmark: str = "SPY") -> dict[str, dict]:
    """
    Computes each ETF's relative rotation vs a benchmark (default SPY) from ~1y of
    Yahoo Finance history. Aligns each ETF to the benchmark on shared trading days,
    then runs `_compute_rrg`. ETFs that error out or lack history map to None.

    ETFs are macro/thematic, not single-catalyst, so rotation vs the market is the
    right lens — this is the R3 analog of fundamentals (which are meaningless for funds).
    """
    import pandas as pd
    import yfinance as yf

    results: dict[str, dict | None] = {}
    if not etf_tickers:
        return results

    try:
        bench_hist = yf.Ticker(benchmark).history(period="1y", interval="1d")
        if bench_hist.empty or len(bench_hist) < 60:
            print(f"ETF RS: benchmark {benchmark} history unavailable — skipping rotation.")
            return results
        bench_close = bench_hist["Close"]
    except Exception as e:
        print(f"ETF RS: benchmark fetch error — {e}")
        return results

    for ticker in etf_tickers:
        try:
            hist = yf.Ticker(ticker).history(period="1y", interval="1d")
            if hist.empty or len(hist) < 60:
                results[ticker] = None
                continue
            # Align ETF and benchmark on shared dates only.
            joined = pd.DataFrame({"e": hist["Close"], "b": bench_close}).dropna()
            if len(joined) < 60:
                results[ticker] = None
                continue
            rrg = _compute_rrg(joined["e"].tolist(), joined["b"].tolist())
            results[ticker] = rrg if rrg.get("quadrant") else None
            if results[ticker]:
                print(f"ETF RS: {ticker} — {rrg['quadrant']} | RS-Ratio {rrg['rs_ratio']} "
                      f"RS-Mom {rrg['rs_momentum']} | {rrg['rel_perf_63d']:+}% vs {benchmark} (3mo)")
        except Exception as e:
            print(f"ETF RS error for {ticker}: {e}")
            results[ticker] = None

    fetched = sum(1 for v in results.values() if v)
    print(f"ETF RS: computed rotation for {fetched}/{len(etf_tickers)} ETFs vs {benchmark}")
    return results


def fetch_price_history(tickers: list[str], asset_type: str = "stock") -> dict[str, dict]:
    """
    Fetches ~1 year of daily price history using Yahoo Finance (yfinance).
    Free, no API key needed, works for stocks and crypto. Returns 14-day trend
    metrics PLUS technical indicators (RSI, MACD, SMA50/200, 52w range, volume)
    that Claude can use to calibrate entries/exits.
    """
    import yfinance as yf

    results = {}

    for ticker in tickers:
        try:
            # Map ticker to Yahoo Finance symbol
            if asset_type == "crypto":
                yahoo_symbol = CRYPTO_YAHOO_MAP.get(ticker)
                if not yahoo_symbol:
                    print(f"Price history: no Yahoo mapping for {ticker}, skipping.")
                    continue
            else:
                yahoo_symbol = ticker

            # Fetch ~1 year so there's enough history for SMA200 / RSI / MACD.
            hist = yf.Ticker(yahoo_symbol).history(period="1y", interval="1d")

            if hist.empty or len(hist) < 3:
                print(f"Price history: insufficient data for {ticker}")
                results[ticker] = None
                continue

            # --- 14-day trend metrics (unchanged): most recent 14 rows ---
            recent = hist.tail(14)
            closes = recent["Close"].tolist()
            highs  = recent["High"].tolist()
            lows   = recent["Low"].tolist()

            first_price = closes[0]
            last_price  = closes[-1]
            high_14d    = max(highs)
            low_14d     = min(lows)
            pct_change  = ((last_price - first_price) / first_price) * 100

            # Average daily range as volatility measure
            daily_ranges        = [h - l for h, l in zip(highs, lows)]
            avg_daily_range_pct = (sum(daily_ranges) / len(daily_ranges) / last_price) * 100

            # Trend direction
            if pct_change > 3:
                trend = "uptrend"
            elif pct_change < -3:
                trend = "downtrend"
            else:
                trend = "sideways"

            # Distance from 14-day high and low
            pct_from_high = ((last_price - high_14d) / high_14d) * 100
            pct_from_low  = ((last_price - low_14d) / low_14d) * 100

            # --- Technical indicators: computed from the full ~1y series ---
            tech = _compute_technicals(
                hist["Close"].tolist(),
                hist["High"].tolist(),
                hist["Low"].tolist(),
                hist["Volume"].tolist() if "Volume" in hist else [],
            )

            results[ticker] = {
                "ticker":              ticker,
                "current_price":       round(last_price, 4),
                "trend_14d":           trend,
                "pct_change_14d":      round(pct_change, 2),
                "high_14d":            round(high_14d, 4),
                "low_14d":             round(low_14d, 4),
                "pct_from_high":       round(pct_from_high, 2),
                "pct_from_low":        round(pct_from_low, 2),
                "avg_daily_range_pct": round(avg_daily_range_pct, 2),
                "volatility":          "high" if avg_daily_range_pct > 3 else "medium" if avg_daily_range_pct > 1.5 else "low",
                "data_points":         len(closes),
                **tech,
            }

            print(f"Price history: {ticker} — {trend} {pct_change:+.1f}% over 14d, "
                  f"RSI {tech.get('rsi_14')}, MACD {tech.get('macd_state')}, "
                  f"vol vs avg {tech.get('vol_vs_avg')}")

        except Exception as e:
            print(f"Price history error for {ticker}: {e}")
            results[ticker] = None

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"Price history: fetched {fetched}/{len(tickers)} tickers successfully")
    return results

if __name__ == "__main__":
    # Test current prices
    print("--- Current prices ---")
    test_tickers = ["AAPL", "NVDA", "TSLA", "MSFT"]
    prices = fetch_prices(test_tickers)
    for ticker, data in prices.items():
        if data:
            arrow = "▲" if data["change"] >= 0 else "▼"
            print(f"{ticker}: ${data['price']} {arrow} {data['change_pct']:+.2f}%")
        else:
            print(f"{ticker}: price unavailable")

    # Test stock price history
    print("\n--- Stock price history ---")
    stock_history = fetch_price_history(["AAPL", "NVDA", "TSLA"])
    for ticker, data in stock_history.items():
        if data:
            print(f"\n{ticker}: {data['trend_14d']} | {data['pct_change_14d']:+.1f}% | "
                  f"volatility: {data['volatility']} | avg daily range: {data['avg_daily_range_pct']:.1f}%")

    # Test crypto price history
    print("\n--- Crypto price history ---")
    crypto_history = fetch_price_history(["BTC", "ETH"], asset_type="crypto")
    for ticker, data in crypto_history.items():
        if data:
            print(f"\n{ticker}: {data['trend_14d']} | {data['pct_change_14d']:+.1f}% | "
                  f"volatility: {data['volatility']} | avg daily range: {data['avg_daily_range_pct']:.1f}%")