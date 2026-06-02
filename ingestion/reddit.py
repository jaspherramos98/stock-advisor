import feedparser
from datetime import datetime

# Reddit exposes every subreddit as a free RSS feed.
# No API key, no approval, no library — just feedparser.
SUBREDDIT_FEEDS = [
    "https://www.reddit.com/r/stocks/.rss",
    "https://www.reddit.com/r/investing/.rss",
    "https://www.reddit.com/r/wallstreetbets/.rss",
    "https://www.reddit.com/r/SecurityAnalysis/.rss",
]

# feedparser needs a browser-like user-agent or Reddit returns a 429 error
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_reddit_news() -> list[dict]:
    """
    Pulls top posts from financial subreddits via their public RSS feeds.
    No API key or approval required.
    """
    all_posts = []

    for url in SUBREDDIT_FEEDS:
        try:
            # feedparser accepts custom headers via the request_headers param
            feed = feedparser.parse(url, agent=USER_AGENT)

            for entry in feed.entries[:8]:  # top 8 posts per subreddit
                # Extract subreddit name from the URL for the source label
                sub_name = url.split("/r/")[1].split("/")[0]

                all_posts.append({
                    "source": f"r/{sub_name}",
                    "title": entry.get("title", "No title"),
                    "summary": entry.get("summary", "[no text]")[:500],
                    "url": entry.get("link", ""),
                    "published": entry.get("published", "Unknown date"),
                    "source_type": "reddit_rss",  # scorer will give this 0.15
                    "fetched_at": datetime.now().isoformat(),
                })

        except Exception as e:
            print(f"Reddit RSS error for {url}: {e}")
            continue

    print(f"Reddit RSS: fetched {len(all_posts)} posts")
    return all_posts


if __name__ == "__main__":
    posts = fetch_reddit_news()
    for p in posts[:3]:
        print(f"\n--- {p['source']} ---")
        print(f"Title   : {p['title']}")
        print(f"URL     : {p['url']}")