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


if __name__ == "__main__":
    test_tickers = ["AAPL", "NVDA", "TSLA", "MSFT"]
    prices = fetch_prices(test_tickers)
    for ticker, data in prices.items():
        if data:
            arrow = "▲" if data["change"] >= 0 else "▼"
            print(f"{ticker}: ${data['price']} {arrow} {data['change_pct']:+.2f}%")
        else:
            print(f"{ticker}: price unavailable")