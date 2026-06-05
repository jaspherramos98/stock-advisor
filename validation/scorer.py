from datetime import datetime, timezone

# --- Score table ---
# These weights reflect how trustworthy each source type is.
# SEC is auto-verified by law. Reddit is retail noise.
# Adjust these over time as you learn which sources are most accurate.
SOURCE_WEIGHTS = {
    "sec":              1.0,
    "finnhub_company":  0.7,
    "finnhub_etf":      0.7,
    "robinhood_news":   0.65,
    "finnhub_general":  0.6,
    "finnhub_crypto":   0.5,
    "rss":              0.5,
    "etf_rss":          0.5,
    "crypto_rss":       0.45,
    "reddit_rss":       0.15,
}

# --- Confidence thresholds ---
# HIGH   → passed to Claude for analysis
# MEDIUM → passed to Claude but flagged with a warning for the user
# LOW    → discarded, not worth analyzing
HIGH_THRESHOLD   = 0.6
MEDIUM_THRESHOLD = 0.35

# --- Recency bonus ---
# Items published within the last 6 hours get a small boost.
# News that's 2 days old is less actionable than news from this morning.
RECENCY_BONUS      = 0.08
RECENCY_WINDOW_HRS = 6


def _parse_published(published_str: str) -> datetime | None:
    """
    Tries to parse a published date string into a datetime object.
    Our sources use different date formats so we try a few common ones.
    Returns None if it can't parse — the item won't get a recency bonus.
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S",        # ISO format (SEC, Finnhub)
        "%a, %d %b %Y %H:%M:%S %z",  # RSS standard format
        "%Y-%m-%d",                    # date only fallback
    ]
    for fmt in formats:
        try:
            return datetime.strptime(published_str[:25], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _recency_bonus(published_str: str) -> float:
    """
    Returns RECENCY_BONUS if the item was published within the
    last RECENCY_WINDOW_HRS hours, otherwise returns 0.
    """
    dt = _parse_published(published_str)
    if dt is None:
        return 0.0

    # Make both datetimes timezone-naive for safe comparison
    now = datetime.now()
    dt  = dt.replace(tzinfo=None)

    age_hours = (now - dt).total_seconds() / 3600
    return RECENCY_BONUS if age_hours <= RECENCY_WINDOW_HRS else 0.0


def score_item(item: dict) -> dict:
    """
    Takes a single news item and returns it with three new fields added:
      - confidence_score  : float between 0 and 1
      - confidence_label  : 'high', 'medium', or 'low'
      - flagged           : True if medium confidence (user will see a warning)
    """
    source_type = item.get("source_type", "unknown")
    base_score  = SOURCE_WEIGHTS.get(source_type, 0.1)
    bonus       = _recency_bonus(item.get("published", ""))
    score       = round(min(base_score + bonus, 1.0), 3)

    if score >= HIGH_THRESHOLD:
        label   = "high"
        flagged = False
    elif score >= MEDIUM_THRESHOLD:
        label   = "medium"
        flagged = True
    else:
        label   = "low"
        flagged = False

    return {**item, "confidence_score": score, "confidence_label": label, "flagged": flagged}


def run_scorer(items: list[dict]) -> dict:
    """
    Scores all items, then splits them into three buckets:
      - high    : ready for Claude analysis
      - medium  : will be analyzed but flagged for the user
      - low     : discarded

    Returns a dict with all three buckets plus a summary.
    """
    scored = [score_item(item) for item in items]

    high   = [i for i in scored if i["confidence_label"] == "high"]
    medium = [i for i in scored if i["confidence_label"] == "medium"]
    low    = [i for i in scored if i["confidence_label"] == "low"]

    print(f"\n--- Scorer results ---")
    print(f"High confidence   : {len(high)}  (sent to Claude)")
    print(f"Medium confidence : {len(medium)}  (sent to Claude, flagged)")
    print(f"Low confidence    : {len(low)}  (discarded)")

    # Show a few examples of what passed and what was cut
    if high:
        print(f"\n  Sample HIGH item:")
        h = high[0]
        print(f"  [{h['confidence_score']}] {h['source_type']} — {h['title'][:70]}")

    if low:
        print(f"\n  Sample LOW item (discarded):")
        l = low[0]
        print(f"  [{l['confidence_score']}] {l['source_type']} — {l['title'][:70]}")

    return {
        "high":   high,
        "medium": medium,
        "low":    low,
        "all_scored": scored,
    }


if __name__ == "__main__":
    # Quick test with fake items so you can run this file on its own
    test_items = [
        {"source_type": "sec",             "title": "Apple files 8-K",          "published": datetime.now().isoformat()},
        {"source_type": "finnhub_company",  "title": "NVDA earnings beat",        "published": datetime.now().isoformat()},
        {"source_type": "rss",             "title": "Fed holds rates steady",     "published": "2020-01-01"},
        {"source_type": "reddit_rss",      "title": "TSLA to the moon 🚀",        "published": "2020-01-01"},
    ]
    results = run_scorer(test_items)
    print(f"\nPassed to Claude: {len(results['high']) + len(results['medium'])} items")