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


def _extract_market_data(row: dict) -> dict:
    """
    Pulls a clean, fact-based subset from one CoinGecko /coins/markets row.
    Missing values are None. Separated from the network call so it can be unit-tested.
    """
    def _num(x, nd=2):
        try:
            return round(float(x), nd)
        except (TypeError, ValueError):
            return None

    return {
        "price":           _num(row.get("current_price")),
        "market_cap":      _num(row.get("market_cap"), 0),
        "market_cap_rank": row.get("market_cap_rank"),
        "volume_24h":      _num(row.get("total_volume"), 0),
        "change_24h_pct":  _num(row.get("price_change_percentage_24h")),
        "change_7d_pct":   _num(row.get("price_change_percentage_7d_in_currency")),
        "change_30d_pct":  _num(row.get("price_change_percentage_30d_in_currency")),
        "pct_from_ath":    _num(row.get("ath_change_percentage")),
    }


def fetch_coin_market_data(tickers: list[str]) -> dict[str, dict]:
    """
    Fetches fact-based market data (price, market cap + rank, 24h/7d/30d momentum,
    volume, distance from all-time high) for crypto tickers in ONE batched
    /coins/markets call. This is the crypto analog of company fundamentals / ETF
    facts — quantitative, reported numbers the analyst reasons over to judge a
    crypto catalyst's conviction RELATIVE TO CRYPTO. Tickers without a CoinGecko
    mapping are skipped; never raises.
    """
    ids, id_to_ticker = [], {}
    for t in tickers:
        cid = TICKER_TO_COINGECKO_ID.get(t.upper())
        if cid:
            ids.append(cid)
            id_to_ticker[cid] = t.upper()

    results: dict[str, dict] = {}
    if not ids:
        return results

    try:
        response = requests.get(
            f"{COINGECKO_BASE}/coins/markets",
            params={
                "vs_currency":             "usd",
                "ids":                     ",".join(ids),
                "price_change_percentage": "24h,7d,30d",
                "sparkline":               "false",
            },
            timeout=10,
        )
        if response.status_code == 429:
            print("CoinGecko market data: rate limited.")
            return results
        if response.status_code != 200:
            print(f"CoinGecko market data: error {response.status_code}")
            return results
        for row in response.json():
            t = id_to_ticker.get(row.get("id"))
            if t:
                results[t] = _extract_market_data(row)
    except Exception as e:
        print(f"CoinGecko market data error: {e}")

    print(f"CoinGecko: fetched market data for {len(results)}/{len(ids)} tickers")
    return results


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