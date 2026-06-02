import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser
from datetime import datetime
from storage.watchlist import get_tickers

# Free ETF news RSS feeds — no API key needed
ETF_RSS_FEEDS = [
    "https://www.etftrends.com/feed/",
    "https://etfdb.com/etfdb-category/news/feed/",
    "https://finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,VTI&region=US&lang=en-US",
]

ETF_KEYWORDS = [
    "etf", "fund", "index", "s&p", "nasdaq", "dow", "vanguard",
    "ishares", "spdr", "invesco", "expense ratio", "dividend",
    "sector", "bond", "treasury", "equity", "portfolio",
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw in text for kw in ETF_KEYWORDS)


def _get_ticker_hint(title: str, summary: str) -> str | None:
    """
    Checks if the article mentions a specific ETF from the watch list.
    """
    watchlist = get_tickers("etfs")
    text      = (title + " " + summary).upper()
    for ticker in watchlist:
        if ticker in text:
            return ticker
    return None


def fetch_etf_rss() -> list[dict]:
    """
    Fetches ETF news from dedicated RSS feeds.
    Tags articles with specific ETF tickers when mentioned.
    """
    all_articles = []

    for url in ETF_RSS_FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)

            for entry in feed.entries[:15]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")[:500]

                if not _is_relevant(title, summary):
                    continue

                ticker = _get_ticker_hint(title, summary)

                article = {
                    "source":      feed.feed.get("title", "ETF News"),
                    "title":       title,
                    "summary":     summary,
                    "url":         entry.get("link", ""),
                    "published":   entry.get("published", datetime.now().isoformat()),
                    "asset_type":  "etf",
                    "source_type": "etf_rss",
                    "fetched_at":  datetime.now().isoformat(),
                }

                if ticker:
                    article["ticker"] = ticker

                all_articles.append(article)

        except Exception as e:
            print(f"ETF RSS error for {url}: {e}")
            continue

    print(f"ETF RSS: fetched {len(all_articles)} articles")
    return all_articles


if __name__ == "__main__":
    articles = fetch_etf_rss()
    for a in articles[:5]:
        ticker_label = f" [${a['ticker']}]" if a.get("ticker") else ""
        print(f"\n[{a['source']}]{ticker_label}")
        print(f"  {a['title'][:80]}")