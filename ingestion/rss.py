import feedparser
from datetime import datetime

# These are free RSS feeds from major financial outlets.
# You can add or remove URLs at any time.
RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/rss/topfinstories",
]

def fetch_rss_news() -> list[dict]:
    """
    Loops through each RSS feed URL, pulls the latest articles,
    and returns them as a list of clean dictionaries.
    """
    all_articles = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)

            for entry in feed.entries[:5]:  # only take the 5 newest per feed
                article = {
                    "source": feed.feed.get("title", "Unknown"),
                    "title": entry.get("title", "No title"),
                    "summary": entry.get("summary", "No summary"),
                    "url": entry.get("link", ""),
                    "published": entry.get("published", "Unknown date"),
                    "source_type": "rss",          # used later by the scorer
                    "fetched_at": datetime.now().isoformat(),
                }
                all_articles.append(article)

        except Exception as e:
            # If one feed fails, print the error and keep going.
            # We don't want one broken feed to crash everything.
            print(f"RSS fetch error for {url}: {e}")
            continue

    print(f"RSS: fetched {len(all_articles)} articles")
    return all_articles


# This block lets you test this file directly by running:
#   python ingestion/rss.py
if __name__ == "__main__":
    articles = fetch_rss_news()
    for a in articles[:3]:
        print(f"\n--- {a['source']} ---")
        print(f"Title   : {a['title']}")
        print(f"Summary : {a['summary'][:120]}...")
        print(f"URL     : {a['url']}")