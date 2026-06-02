import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser
from datetime import datetime
from storage.watchlist import get_tickers

# Free crypto news RSS feeds — no API key needed
CRYPTO_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# Keywords to filter articles — only keep crypto-relevant news
CRYPTO_KEYWORDS = [
    "bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft",
    "solana", "bnb", "xrp", "web3", "token", "coin", "wallet",
    "exchange", "binance", "coinbase", "mining", "staking",
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _is_relevant(title: str, summary: str) -> bool:
    """
    Filters out non-crypto articles that sometimes appear in crypto feeds.
    Returns True if the article contains at least one crypto keyword.
    """
    text = (title + " " + summary).lower()
    return any(kw in text for kw in CRYPTO_KEYWORDS)


def _get_ticker_hint(title: str, summary: str) -> str | None:
    """
    Checks if the article mentions a specific crypto from the watch list.
    Returns the ticker if found, None if it's a general crypto story.
    """
    watchlist = get_tickers("crypto")
    text = (title + " " + summary).upper()

    ticker_names = {
        "BTC": ["BITCOIN", "BTC"],
        "ETH": ["ETHEREUM", "ETH", "ETHER"],
        "SOL": ["SOLANA", "SOL"],
        "BNB": ["BNB", "BINANCE COIN"],
        "XRP": ["XRP", "RIPPLE"],
    }

    for ticker in watchlist:
        names = ticker_names.get(ticker, [ticker])
        if any(name in text for name in names):
            return ticker
    return None


def fetch_crypto_rss() -> list[dict]:
    """
    Fetches crypto news from dedicated RSS feeds.
    Filters for relevance and tags articles with ticker hints
    when a specific coin is mentioned.
    """
    all_articles = []

    for url in CRYPTO_RSS_FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)

            for entry in feed.entries[:15]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")[:500]

                if not _is_relevant(title, summary):
                    continue

                ticker = _get_ticker_hint(title, summary)

                article = {
                    "source":      feed.feed.get("title", "Crypto News"),
                    "title":       title,
                    "summary":     summary,
                    "url":         entry.get("link", ""),
                    "published":   entry.get("published", datetime.now().isoformat()),
                    "asset_type":  "crypto",
                    "source_type": "crypto_rss",
                    "fetched_at":  datetime.now().isoformat(),
                }

                if ticker:
                    article["ticker"] = ticker

                all_articles.append(article)

        except Exception as e:
            print(f"Crypto RSS error for {url}: {e}")
            continue

    print(f"Crypto RSS: fetched {len(all_articles)} articles")
    return all_articles


if __name__ == "__main__":
    articles = fetch_crypto_rss()
    for a in articles[:5]:
        ticker_label = f" [${a['ticker']}]" if a.get("ticker") else ""
        print(f"\n[{a['source']}]{ticker_label}")
        print(f"  {a['title'][:80]}")