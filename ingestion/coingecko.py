import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import time
from dotenv import load_dotenv

load_dotenv()

# CoinGecko free API — no key needed
# Rate limit: 30 calls per minute
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Mapping from our ticker symbols to CoinGecko coin IDs
# Add more here as you expand your crypto watch list
TICKER_TO_COINGECKO_ID = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "SOL":  "solana",
    "BNB":  "binancecoin",
    "XRP":  "ripple",
    "ADA":  "cardano",
    "AVAX": "avalanche-2",
    "DOT":  "polkadot",
    "MATIC": "matic-network",
    "LINK": "chainlink",
}


def fetch_coin_context(ticker: str) -> dict | None:
    """
    Fetches a short description, white paper link, and key stats
    for a single crypto ticker from CoinGecko.

    Returns a dict with:
      - ticker:       the ticker symbol
      - name:         full name
      - description:  2-3 sentence summary of what the coin does
      - whitepaper_url: link to official white paper if available
      - website:      official website
      - market_cap_rank: how large the coin is (1 = Bitcoin)
      - categories:   what type of crypto it is
    """
    coin_id = TICKER_TO_COINGECKO_ID.get(ticker.upper())
    if not coin_id:
        print(f"CoinGecko: no mapping for {ticker}, skipping.")
        return None

    try:
        response = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}",
            params={
                "localization":    "false",
                "tickers":         "false",
                "market_data":     "false",
                "community_data":  "false",
                "developer_data":  "false",
                "sparkline":       "false",
            },
            timeout=10,
        )

        if response.status_code == 429:
            print(f"CoinGecko: rate limited, waiting 10 seconds...")
            time.sleep(10)
            return None

        if response.status_code != 200:
            print(f"CoinGecko: error {response.status_code} for {ticker}")
            return None

        data = response.json()

        # Extract description — CoinGecko descriptions can be very long
        # We take only the first 300 characters to keep prompts lean
        raw_desc  = data.get("description", {}).get("en", "")
        # Strip HTML tags that sometimes appear in descriptions
        import re
        clean_desc = re.sub(r"<[^>]+>", "", raw_desc)
        clean_desc = clean_desc[:300].strip()
        if len(raw_desc) > 300:
            clean_desc += "..."

        # Extract white paper and website links
        links        = data.get("links", {})
        whitepaper   = links.get("whitepaper", "") or ""
        websites     = links.get("homepage", [])
        website      = websites[0] if websites else ""

        # Market cap rank — gives Claude a sense of how established this coin is
        market_cap_rank = data.get("market_cap_rank", "unknown")

        # Categories — e.g. "Smart Contract Platform", "DeFi", "Layer 2"
        categories = data.get("categories", [])[:3]

        result = {
            "ticker":          ticker.upper(),
            "name":            data.get("name", ticker),
            "description":     clean_desc,
            "whitepaper_url":  whitepaper,
            "website":         website,
            "market_cap_rank": market_cap_rank,
            "categories":      categories,
        }

        print(f"CoinGecko: fetched context for {ticker} ({result['name']})")
        return result

    except Exception as e:
        print(f"CoinGecko error for {ticker}: {e}")
        return None


def fetch_crypto_context(tickers: list[str]) -> dict[str, dict]:
    """
    Fetches CoinGecko context for a list of crypto tickers.
    Returns a dict keyed by ticker.
    Adds a small delay between requests to respect rate limits.
    """
    results = {}
    for ticker in tickers:
        context = fetch_coin_context(ticker)
        if context:
            results[ticker] = context
        time.sleep(1.5)  # stay well within 30 calls/min limit

    print(f"CoinGecko: fetched context for {len(results)}/{len(tickers)} tickers")
    return results


if __name__ == "__main__":
    test_tickers = ["BTC", "ETH", "SOL"]
    contexts = fetch_crypto_context(test_tickers)
    for ticker, ctx in contexts.items():
        print(f"\n{'='*50}")
        print(f"  {ctx['name']} ({ticker})")
        print(f"  Rank: #{ctx['market_cap_rank']}")
        print(f"  Categories: {', '.join(ctx['categories'])}")
        print(f"  Description: {ctx['description']}")
        print(f"  White paper: {ctx['whitepaper_url']}")
        print(f"  Website: {ctx['website']}")