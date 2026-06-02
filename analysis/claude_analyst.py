import anthropic
import os
import json
from dotenv import load_dotenv

load_dotenv()

# How many deduplicated stories we send to Claude per run.
# 25 is enough signal without burning through API tokens.
MAX_STORIES = 25


def _deduplicate(items: list[dict]) -> list[dict]:
    seen_word_sets = []
    unique = []

    for item in sorted(items, key=lambda x: x.get("confidence_score", 0), reverse=True):
        title       = item.get("title") or ""
        title_words = set(title.lower().split())

        is_duplicate = False
        for seen in seen_word_sets:
            if len(title_words & seen) >= 4:
                is_duplicate = True
                break

        if not is_duplicate:
            unique.append(item)
            seen_word_sets.append(title_words)

    print(f"Deduplication: {len(items)} items → {len(unique)} unique stories")
    return unique[:MAX_STORIES]


def _build_prompt(items: list[dict]) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        ticker_hint = f" [ticker: ${item['ticker']}]" if item.get("ticker") else ""
        flag_hint   = " ⚠ unverified source" if item.get("flagged") else ""
        summary     = item.get("summary") or "No summary available"
        source      = item.get("source") or "Unknown"
        title       = item.get("title") or "No title"
        score       = item.get("confidence_score", 0)
        lines.append(
            f"{i}. [{score} confidence{flag_hint}]{ticker_hint}\n"
            f"   Title   : {title}\n"
            f"   Summary : {summary[:200]}\n"
            f"   Source  : {source}"
        )
    return "\n\n".join(lines)


def run_analysis(
    items:          list[dict],
    include_stocks: bool = True,
    include_etfs:   bool = False,
    include_crypto: bool = False,
) -> list[dict]:
    """
    Main entry point for Week 3.
    Takes scored news items, deduplicates them, sends them to Claude,
    and returns a list of stock recommendations as clean dictionaries.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    unique_items = _deduplicate(items)
    if not unique_items:
        print("No items to analyze.")
        return []

    try:
        news_block = _build_prompt(unique_items)
    except Exception as e:
        import traceback
        print(f"Prompt build error: {e}")
        print(traceback.format_exc())
        return []

    # Build asset type instruction dynamically
    asset_instructions = []
    if include_stocks:
        asset_instructions.append("US stocks (NYSE, NASDAQ)")
    if include_etfs:
        asset_instructions.append("US ETFs")
    if include_crypto:
        asset_instructions.append("major cryptocurrencies (BTC, ETH, SOL, etc)")

    asset_scope = " and ".join(asset_instructions) if asset_instructions else "US stocks"

    system_prompt = f"""You are a financial analysis assistant for a personal,
informational stock advisor tool. Your job is to read validated news items
and identify which assets are likely affected.

IMPORTANT RULES:
- This is for informational purposes only, not financial advice.
- Only recommend assets in these categories: {asset_scope}.
- Be conservative — only flag an asset if the news has a clear, direct impact.
- Do not invent tickers. If you are unsure of the ticker, use null.
- For flagged (unverified) sources, set risk_level to 'high' regardless.
- For crypto assets, use the standard symbol (BTC, ETH, SOL etc) as the ticker.
- For ETFs, use the standard ticker (SPY, QQQ etc) as the ticker.

You must respond with ONLY a valid JSON array. No preamble, no explanation,
no markdown code fences. Just the raw JSON array.

Each object in the array must have exactly these fields:
{{
  "ticker":          string or null,
  "company_name":    string,
  "asset_type":      "stock" or "etf" or "crypto",
  "direction":       "buy" or "watch" or "avoid",
  "entry_rationale": string (max 2 sentences),
  "exit_condition":  string (e.g. '10% gain' or '2 weeks' or 'earnings release'),
  "risk_level":      "low" or "medium" or "high",
  "confidence_score": number (pass through from the news item),
  "flagged":         boolean,
  "source_title":    string (the news headline this is based on)
}}

If no assets are clearly actionable from the news provided, return an empty array: []"""

    user_prompt = f"""Here are today's validated news items. Analyze them and return 
your stock recommendations as a JSON array.

NEWS ITEMS:
{news_block}"""

    # Step 3 — call Claude
    print(f"\nSending {len(unique_items)} stories to Claude for analysis...")
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )

        raw = message.content[0].text.strip()

        # Check if Claude hit the token limit mid-response
        if message.stop_reason == "max_tokens":
            print("Warning: Claude hit token limit — truncating to valid JSON.")
            # Find the last complete object in the array
            last_brace = raw.rfind("},")
            if last_brace != -1:
                raw = raw[:last_brace + 1] + "]"
            else:
                print("Could not recover truncated JSON.")
                return []

        # Strip accidental markdown fences just in case
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        recommendations = json.loads(raw)

        print(f"Claude returned {len(recommendations)} recommendations")
        return recommendations

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response was:\n{raw[:500]}")
        return []
    except Exception as e:
        print(f"Claude API error: {e}")
        return []


if __name__ == "__main__":
    # Quick test with two fake items so you can run this file alone
    test_items = [
        {
            "title":            "Alphabet plans to raise $80 billion from stock sales to fund AI buildout",
            "summary":          "Google parent Alphabet announced a massive capital raise to accelerate AI infrastructure spending.",
            "source":           "Reuters",
            "source_type":      "rss",
            "confidence_score": 0.58,
            "flagged":          False,
            "ticker":           "GOOGL",
        },
        {
            "title":            "HPE skyrockets 30% on biggest earnings beat since 2018",
            "summary":          "Hewlett Packard Enterprise crushed earnings estimates driven by surging AI server demand.",
            "source":           "CNBC",
            "source_type":      "finnhub_company",
            "confidence_score": 0.70,
            "flagged":          False,
            "ticker":           "HPE",
        },
    ]
    results = run_analysis(test_items)
    print(f"\n--- Recommendations ---")
    for r in results:
        print(f"\n{r.get('ticker')} — {r.get('company_name')}")
        print(f"  Direction : {r.get('direction')}")
        print(f"  Rationale : {r.get('entry_rationale')}")
        print(f"  Exit      : {r.get('exit_condition')}")
        print(f"  Risk      : {r.get('risk_level')}")