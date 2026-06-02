import os
import finnhub
from dotenv import load_dotenv

load_dotenv()


def get_client():
    return finnhub.Client(api_key=os.getenv("FINNHUB_API_KEY"))


def fetch_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Fetches current price data for a list of tickers from Finnhub.
    Returns a dict keyed by ticker with price details inside.

    Example return value:
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
    client  = get_client()
    results = {}

    for ticker in tickers:
        try:
            quote = client.quote(ticker)

            # Finnhub quote fields:
            # c = current price, d = change, dp = % change
            # h = high, l = low, o = open, pc = previous close
            price      = quote.get("c", 0.0)
            change     = quote.get("d", 0.0)
            change_pct = quote.get("dp", 0.0)
            high       = quote.get("h", 0.0)
            low        = quote.get("l", 0.0)

            # If price is 0 the market is closed or ticker is invalid
            if price == 0:
                results[ticker] = None
                continue

            results[ticker] = {
                "price":      round(price, 2),
                "change":     round(change, 2),
                "change_pct": round(change_pct, 2),
                "high":       round(high, 2),
                "low":        round(low, 2),
            }

        except Exception as e:
            print(f"Price fetch error for {ticker}: {e}")
            results[ticker] = None

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"Prices: fetched {fetched}/{len(tickers)} tickers successfully")
    return results

import time as _time
from datetime import timedelta

def fetch_price_history(tickers: list[str], asset_type: str = "stock") -> dict[str, dict]:
    """
    Fetches 14 days of daily price history using Yahoo Finance (yfinance).
    Free, no API key needed, works for stocks and crypto.
    Returns trend analysis Claude can use to calibrate exit conditions.
    """
    import yfinance as yf

    # Yahoo Finance uses different symbols for crypto
    CRYPTO_YAHOO_MAP = {
        "BTC":  "BTC-USD",
        "ETH":  "ETH-USD",
        "SOL":  "SOL-USD",
        "BNB":  "BNB-USD",
        "XRP":  "XRP-USD",
    }

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

            # Fetch 20 days to ensure we get at least 14 trading days
            hist = yf.Ticker(yahoo_symbol).history(period="20d", interval="1d")

            if hist.empty or len(hist) < 3:
                print(f"Price history: insufficient data for {ticker}")
                results[ticker] = None
                continue

            # Take last 14 rows
            hist = hist.tail(14)

            closes = hist["Close"].tolist()
            highs  = hist["High"].tolist()
            lows   = hist["Low"].tolist()

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
            }

            print(f"Price history: {ticker} — {trend} {pct_change:+.1f}% over 14d, "
                  f"volatility: {results[ticker]['volatility']}, "
                  f"avg daily range: {avg_daily_range_pct:.1f}%")

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