import finnhub
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from dotenv import load_dotenv
from storage.watchlist import get_tickers

load_dotenv()


def get_client():
    return finnhub.Client(api_key=os.getenv("FINNHUB_API_KEY"))


def fetch_market_news(client) -> list[dict]:
    """Fetches general market-wide financial news."""
    try:
        news    = client.general_news("general", min_id=0)
        results = []
        for item in news[:15]:
            results.append({
                "source":      item.get("source", "Finnhub"),
                "title":       item.get("headline", "No title"),
                "summary":     item.get("summary", "No summary")[:500],
                "url":         item.get("url", ""),
                "published":   datetime.fromtimestamp(item.get("datetime", 0)).isoformat(),
                "source_type": "finnhub_general",
                "fetched_at":  datetime.now().isoformat(),
            })
        print(f"Finnhub general news: fetched {len(results)} articles")
        return results
    except Exception as e:
        print(f"Finnhub general news error: {e}")
        return []


def fetch_company_news(client, asset_type: str = "stocks") -> list[dict]:
    """
    Fetches news specifically about each ticker in the given asset type.
    Works for both stocks and ETFs since both trade on exchanges
    and have Finnhub company news endpoints.
    """
    today          = datetime.now().strftime("%Y-%m-%d")
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    tickers        = get_tickers(asset_type)
    source_type    = "finnhub_etf" if asset_type == "etfs" else "finnhub_company"

    results = []
    for ticker in tickers:
        try:
            news = client.company_news(ticker, _from=three_days_ago, to=today)
            for item in news[:3]:
                results.append({
                    "source":      item.get("source", "Finnhub"),
                    "title":       item.get("headline", "No title"),
                    "summary":     item.get("summary", "No summary")[:500],
                    "url":         item.get("url", ""),
                    "ticker":      ticker,
                    "asset_type":  asset_type,
                    "published":   datetime.fromtimestamp(item.get("datetime", 0)).isoformat(),
                    "source_type": source_type,
                    "fetched_at":  datetime.now().isoformat(),
                })
        except Exception as e:
            print(f"Finnhub company news error for {ticker}: {e}")
            continue

    print(f"Finnhub {asset_type} news: fetched {len(results)} articles")
    return results


def fetch_crypto_news(client) -> list[dict]:
    """
    Fetches crypto-specific news from Finnhub.
    Uses the crypto news category endpoint and also checks
    company news for each crypto symbol on the watch list.
    """
    results = []

    # General crypto news category
    try:
        news = client.general_news("crypto", min_id=0)
        for item in news[:10]:
            results.append({
                "source":      item.get("source", "Finnhub"),
                "title":       item.get("headline", "No title"),
                "summary":     item.get("summary", "No summary")[:500],
                "url":         item.get("url", ""),
                "asset_type":  "crypto",
                "published":   datetime.fromtimestamp(item.get("datetime", 0)).isoformat(),
                "source_type": "finnhub_crypto",
                "fetched_at":  datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"Finnhub crypto news error: {e}")

    print(f"Finnhub crypto news: fetched {len(results)} articles")
    return results


def fetch_finnhub_news(
    include_stocks: bool = True,
    include_etfs:   bool = False,
    include_crypto: bool = False,
) -> list[dict]:
    """
    Main entry point. Fetches news based on which asset types are enabled.
    Now includes dedicated RSS feeds for ETF and crypto for higher volume.
    """
    from ingestion.crypto_news import fetch_crypto_rss
    from ingestion.etf_news    import fetch_etf_rss

    client  = get_client()
    results = []

    # General market news always included
    results += fetch_market_news(client)

    # Stock company news
    if include_stocks:
        results += fetch_company_news(client, asset_type="stocks")

    # ETF news — Finnhub company news + dedicated RSS
    if include_etfs:
        results += fetch_company_news(client, asset_type="etfs")
        results += fetch_etf_rss()

    # Crypto news — Finnhub general + dedicated RSS
    if include_crypto:
        results += fetch_crypto_news(client)
        results += fetch_crypto_rss()

    return results


if __name__ == "__main__":
    # Test with all asset types enabled
    items = fetch_finnhub_news(
        include_stocks=True,
        include_etfs=True,
        include_crypto=True,
    )
    print(f"\nTotal: {len(items)} items")
    for item in items[:3]:
        print(f"\n[{item['source_type']}] {item['title'][:70]}")