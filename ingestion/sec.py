import requests
from datetime import datetime, timedelta

# SEC EDGAR full-text search API — no key needed, just a user-agent header.
SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt={start}&enddt={end}&forms=8-K,SC+13D"

# Form types we care about:
# 8-K  = major company events (mergers, earnings surprises, exec changes)
# SC 13D = someone bought >5% of a company (activist investor signal)

HEADERS = {
    "User-Agent": "stock-advisor-bot contact@example.com"
    # SEC requires a user-agent. Use your real email if you want,
    # but any string works for personal use.
}

def fetch_sec_filings(keywords: list[str] = None) -> list[dict]:
    """
    Searches SEC EDGAR for recent 8-K and 13D filings.
    These are the most market-moving filing types.
    SEC filings automatically get a confidence score of 1.0 later —
    they are verified by definition.
    """
    if keywords is None:
        keywords = ["merger", "acquisition", "earnings", "bankruptcy"]

    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    all_filings = []

    for keyword in keywords:
        try:
            url = f"https://efts.sec.gov/LATEST/search-index?q={keyword}&dateRange=custom&startdt={yesterday}&enddt={today_str}&forms=8-K"
            response = requests.get(url, headers=HEADERS, timeout=10)
            data = response.json()

            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:5]:
                src = hit.get("_source", {})
                all_filings.append({
                    "source": "SEC EDGAR",
                    "title": src.get("display_names", ["Unknown company"])[0] + f" — {keyword}",
                    "summary": f"Form {src.get('form_type', '8-K')} filed on {src.get('file_date', 'unknown date')}",
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={src.get('entity_id','')}&type=8-K",
                    "published": src.get("file_date", today_str),
                    "source_type": "sec",     # scorer gives this 1.0 automatically
                    "fetched_at": datetime.now().isoformat(),
                })

        except Exception as e:
            print(f"SEC fetch error for '{keyword}': {e}")
            continue

    print(f"SEC: fetched {len(all_filings)} filings")
    return all_filings


if __name__ == "__main__":
    filings = fetch_sec_filings()
    for f in filings[:3]:
        print(f"\n--- {f['source']} ---")
        print(f"Title   : {f['title']}")
        print(f"Summary : {f['summary']}")
        print(f"URL     : {f['url']}")