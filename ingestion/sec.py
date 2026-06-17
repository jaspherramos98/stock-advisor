import re
import requests
from datetime import datetime, timedelta

# SEC EDGAR full-text search API — no key needed, just a user-agent header.
# Each hit's _source carries the 8-K item codes and the company's ticker, which we
# translate into plain English so Claude knows what the filing actually says.

# Form types we care about:
# 8-K  = major company events (mergers, earnings surprises, exec changes)
# SC 13D = someone bought >5% of a company (activist investor signal)

HEADERS = {
    "User-Agent": "stock-advisor-bot contact@example.com"
    # SEC requires a user-agent. Use your real email if you want,
    # but any string works for personal use.
}

# 8-K item codes → plain-English meaning. This is what turns a contentless
# "Form 8-K filed" headline into an actionable, fact-checked signal.
ITEM_DESCRIPTIONS = {
    "1.01": "Entry into a Material Definitive Agreement (major contract/deal)",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition of Assets (M&A CLOSED)",
    "2.02": "Results of Operations and Financial Condition (EARNINGS release)",
    "2.03": "Creation of a Direct Financial Obligation (new debt)",
    "2.04": "Triggering Events Accelerating a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities (restructuring)",
    "2.06": "Material Impairments (write-down)",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities (dilution)",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant (auditor change)",
    "4.02": "Non-Reliance on Previously Issued Financial Statements (restatement)",
    "5.01": "Changes in Control of Registrant (takeover)",
    "5.02": "Departure/Appointment of Directors or Officers (exec change)",
    "5.03": "Amendments to Articles/Bylaws; Change in Fiscal Year",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure (general announcement)",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# Items that tend to actually move a stock — used to flag the high-signal filings.
HIGH_SIGNAL_ITEMS = {"1.01", "1.03", "2.01", "2.02", "2.05", "2.06", "4.02", "5.01", "5.02"}


def _parse_company_and_ticker(display_name: str) -> tuple[str, str | None]:
    """
    EDGAR display_names look like 'PROCTER & GAMBLE Co  (PG)  (CIK 0000080424)'.
    Returns (clean_company_name, ticker_or_None).
    """
    company = display_name.split("(")[0].strip()
    # Ticker is a short all-caps token in parens that isn't the CIK block.
    m = re.search(r"\(([A-Z][A-Z0-9.\-]{0,5})\)", display_name)
    ticker = m.group(1) if m else None
    return company or display_name, ticker


def _describe_items(item_codes: list[str]) -> tuple[str, bool]:
    """Turns ['2.02', '7.01'] into a readable summary and a high-signal flag."""
    if not item_codes:
        return "", False
    parts = []
    high_signal = False
    for code in item_codes:
        desc = ITEM_DESCRIPTIONS.get(code)
        if desc:
            parts.append(desc)
        if code in HIGH_SIGNAL_ITEMS:
            high_signal = True
    return "; ".join(parts), high_signal


def fetch_sec_filings(keywords: list[str] = None) -> list[dict]:
    """
    Searches SEC EDGAR for recent 8-K filings and enriches each with the
    plain-English meaning of its 8-K item codes plus the company's ticker.
    SEC filings automatically get a confidence score of 1.0 later —
    they are verified by definition.
    """
    if keywords is None:
        keywords = ["merger", "acquisition", "earnings", "bankruptcy"]

    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    seen_accessions = set()  # dedupe filings that match multiple keywords
    all_filings = []

    for keyword in keywords:
        try:
            url = (
                f"https://efts.sec.gov/LATEST/search-index?q={keyword}"
                f"&dateRange=custom&startdt={yesterday}&enddt={today_str}&forms=8-K"
            )
            response = requests.get(url, headers=HEADERS, timeout=10)
            data = response.json()

            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:5]:
                src = hit.get("_source", {})

                accession = src.get("adsh")
                if accession and accession in seen_accessions:
                    continue
                if accession:
                    seen_accessions.add(accession)

                display = (src.get("display_names") or ["Unknown company"])[0]
                company, ticker = _parse_company_and_ticker(display)

                items = src.get("items") or []
                item_summary, high_signal = _describe_items(items)

                file_date = src.get("file_date", today_str)

                # Build a title/summary that actually says what the filing is about.
                if item_summary:
                    title   = f"{company} 8-K: {item_summary}"
                    summary = f"8-K filed {file_date} — {item_summary}."
                else:
                    title   = f"{company} 8-K filing"
                    summary = f"8-K filed {file_date} (no itemized detail available)."

                all_filings.append({
                    "source":      "SEC EDGAR",
                    "title":       title,
                    "summary":     summary,
                    "ticker":      ticker,
                    "url": (
                        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                        f"&CIK={src.get('ciks', [''])[0]}&type=8-K"
                    ),
                    "published":   file_date,
                    "source_type": "sec",     # scorer gives this 1.0 automatically
                    "high_signal": high_signal,
                    "matched_keyword": keyword,
                    "fetched_at":  datetime.now().isoformat(),
                })

        except Exception as e:
            print(f"SEC fetch error for '{keyword}': {e}")
            continue

    high_count = sum(1 for f in all_filings if f["high_signal"])
    print(f"SEC: fetched {len(all_filings)} filings ({high_count} high-signal)")
    return all_filings


if __name__ == "__main__":
    filings = fetch_sec_filings()
    for f in filings[:5]:
        print(f"\n--- {f['source']} ({'HIGH SIGNAL' if f['high_signal'] else 'low'}) ---")
        print(f"Ticker  : {f['ticker']}")
        print(f"Title   : {f['title']}")
        print(f"Summary : {f['summary']}")
        print(f"URL     : {f['url']}")
