import anthropic
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
from dotenv import load_dotenv

load_dotenv()

MAX_STORIES = 25


def _deduplicate(items: list[dict], max_stories: int = MAX_STORIES) -> list[dict]:
    """Removes near-duplicate stories and returns up to max_stories items."""
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

    return unique[:max_stories]


def _deduplicate_by_asset_type(
    items:          list[dict],
    include_stocks: bool = True,
    include_etfs:   bool = False,
    include_crypto: bool = False,
) -> list[dict]:
    """
    Deduplicates while reserving slots for each enabled asset type.
    Prevents high-scoring stock news from crowding out ETF/crypto news.

    Slot allocation out of MAX_STORIES (25):
    - Stocks only:              25 stock slots
    - Stocks + ETFs:            17 stock, 8 ETF
    - Stocks + Crypto:          17 stock, 8 crypto
    - Stocks + ETFs + Crypto:   13 stock, 6 ETF, 6 crypto
    """
    enabled = sum([include_stocks, include_etfs, include_crypto])

    if enabled == 1:
        slots = {
            "stocks": MAX_STORIES if include_stocks else 0,
            "etfs":   MAX_STORIES if include_etfs   else 0,
            "crypto": MAX_STORIES if include_crypto  else 0,
        }
    elif enabled == 2:
        major = 17
        minor = 8
        slots = {
            "stocks": major if include_stocks else 0,
            "etfs":   (minor if include_etfs else 0) if include_stocks else major,
            "crypto": (minor if include_crypto else 0) if include_stocks else major,
        }
    else:
        slots = {"stocks": 13, "etfs": 6, "crypto": 6}

    def get_asset_type(item: dict) -> str:
        source_type = item.get("source_type", "")
        asset_type  = item.get("asset_type", "")
        if asset_type == "crypto" or source_type in ("finnhub_crypto", "crypto_rss"):
            return "crypto"
        if asset_type == "etf" or source_type in ("finnhub_etf", "etf_rss"):
            return "etfs"
        return "stocks"

    # Separate items by asset type
    buckets: dict[str, list] = {"stocks": [], "etfs": [], "crypto": []}
    for item in sorted(items, key=lambda x: x.get("confidence_score", 0), reverse=True):
        atype = get_asset_type(item)
        buckets[atype].append(item)

    # Deduplicate within each bucket and take up to its slot limit
    final = []
    for atype, slot_count in slots.items():
        if slot_count == 0:
            continue
        deduped = _deduplicate(buckets[atype], max_stories=slot_count)
        final.extend(deduped)

    print(f"Deduplication: {len(items)} items → {len(final)} unique stories (slots: {slots})")
    return final


def _build_prompt(
    items:         list[dict],
    crypto_context: dict = None,
    price_history:  dict = None,
    open_positions:  list = None,
) -> str:
    """
    Formats the news items into a clean numbered list for Claude.
    Includes crypto context and 14-day price trend data when available
    so Claude can generate calibrated exit conditions and stop loss levels.
    """
    lines = []

    # Crypto asset context block
    if crypto_context:
        lines.append("=== CRYPTO ASSET CONTEXT ===")
        for ticker, ctx in crypto_context.items():
            lines.append(
                f"${ticker} — {ctx['name']} | Rank #{ctx['market_cap_rank']} | "
                f"Categories: {', '.join(ctx['categories'])}\n"
                f"What it is: {ctx['description']}"
            )
        lines.append("=== END CONTEXT ===\n")

    # 14-day price trend block
    if price_history:
        available = {k: v for k, v in price_history.items() if v is not None}
        if available:
            lines.append("=== 14-DAY PRICE TREND DATA ===")
            lines.append(
                "Use this data to calibrate exit targets and stop loss levels. "
                "High volatility = wider stops. Downtrend = more conservative targets. "
                "Uptrend near 14d high = watch for resistance."
            )
            for ticker, data in available.items():
                lines.append(
                    f"${ticker}: {data['trend_14d']} {data['pct_change_14d']:+.1f}% over 14d | "
                    f"volatility: {data['volatility']} (avg daily range: {data['avg_daily_range_pct']:.1f}%) | "
                    f"14d high: ${data['high_14d']} ({data['pct_from_high']:+.1f}% from now) | "
                    f"14d low: ${data['low_14d']} ({data['pct_from_low']:+.1f}% from now)"
                )
            lines.append("=== END TREND DATA ===\n")
            # Open positions block — tells Claude what the user already owns

    if open_positions:
        lines.append("=== YOUR CURRENT OPEN POSITIONS ===")
        lines.append(
            "The user already owns these assets. Do NOT recommend buying more of them. "
            "If there is strong news about an already-owned ticker, you may include it "
            "as direction='watch' with a note about the news impact on the existing position, "
            "but do not allocate new capital to it."
        )
        for p in open_positions:
            lines.append(
                f"${p['ticker']} — {p['company_name']} | "
                f"entry: ${p.get('reference_price', 0):.2f} | "
                f"exit when: {p.get('exit_condition', 'not set')}"
            )
        lines.append("=== END OPEN POSITIONS ===\n")

    # News items
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
            f"   Summary : {summary[:100]}\n"
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
    Main entry point. Takes scored news items, deduplicates them by asset type,
    fetches crypto context from CoinGecko if needed, sends to Claude,
    and returns a list of recommendations as clean dictionaries.
    """
    # ── MOCK MODE ──────────────────────────────────────────────────
    # When MOCK_MODE=true in .env, skip the Claude API entirely and
    # return pre-saved recommendations. Zero tokens consumed.
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        mock_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "mock_recommendations.json"
        )
        try:
            with open(mock_path, "r") as f:
                mock_recs = json.load(f)

            # Filter by asset type so checkboxes still work in mock mode
            filtered = []
            for rec in mock_recs:
                asset = rec.get("asset_type", "stock")
                if asset == "stock"  and include_stocks: filtered.append(rec)
                if asset == "etf"    and include_etfs:   filtered.append(rec)
                if asset == "crypto" and include_crypto:  filtered.append(rec)

            print(f"\n⚠️  MOCK MODE — Claude API skipped. Returning {len(filtered)} mock recommendations.")
            return filtered
        except Exception as e:
            print(f"Mock mode error: {e} — falling through to real Claude call.")
    # ── END MOCK MODE ──────────────────────────────────────────────

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Fetch open positions so Claude knows what the user already owns
    open_positions = []
    try:
        from storage.positions import get_open_positions
        open_positions = get_open_positions()
        if open_positions:
            tickers = [p["ticker"] for p in open_positions]
            print(f"Claude analyst: user has {len(open_positions)} open positions ({', '.join(tickers)}) — passing to Claude")
    except Exception as e:
        print(f"Claude analyst: could not load open positions: {e}")

    unique_items = _deduplicate_by_asset_type(
        items,
        include_stocks=include_stocks,
        include_etfs=include_etfs,
        include_crypto=include_crypto,
    )
    if not unique_items:
        print("No items to analyze.")
        return []

    # Fetch CoinGecko context for crypto tickers if crypto is enabled
    # Fetch CoinGecko context for crypto tickers if crypto is enabled
    crypto_context = {}
    if include_crypto:
        try:
            from ingestion.coingecko import fetch_crypto_context
            from storage.watchlist   import get_tickers
            crypto_tickers = get_tickers("crypto")
            crypto_context = fetch_crypto_context(crypto_tickers)
            print(f"Claude analyst: loaded context for {len(crypto_context)} crypto assets")
        except Exception as e:
            print(f"Claude analyst: crypto context fetch failed: {e}")

    # Fetch 14-day price history for all recommended tickers
    # Fetch 14-day price history — only for tickers that appear in the news
    price_history = {}
    try:
        from ingestion.prices import fetch_price_history

        # Only fetch history for tickers actually mentioned in the news
        news_tickers = list({
            item.get("ticker") for item in unique_items
            if item.get("ticker")
        })

        stock_news_tickers  = [t for t in news_tickers if not any(
            item.get("asset_type") == "crypto"
            for item in unique_items if item.get("ticker") == t
        )]
        crypto_news_tickers = [t for t in news_tickers if any(
            item.get("asset_type") == "crypto"
            for item in unique_items if item.get("ticker") == t
        )]

        if stock_news_tickers and (include_stocks or include_etfs):
            stock_history = fetch_price_history(stock_news_tickers, asset_type="stock")
            price_history.update(stock_history)

        if crypto_news_tickers and include_crypto:
            crypto_history = fetch_price_history(crypto_news_tickers, asset_type="crypto")
            price_history.update(crypto_history)

        fetched = sum(1 for v in price_history.values() if v is not None)
        print(f"Claude analyst: loaded 14-day price history for {fetched} tickers")
    except Exception as e:
        print(f"Claude analyst: price history fetch failed: {e}")

    try:
        news_block = _build_prompt(
            unique_items,
            crypto_context=crypto_context,
            price_history=price_history,
            open_positions=open_positions,
        )
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

    system_prompt = f"""You are a sharp, profit-driven investment banker running a
personal trading desk for a single client. Your one mandate is to GROW THE CLIENT'S
CAPITAL — hunt the news for the trades with the best risk-adjusted upside and put
money to work only where the edge is real. You read validated news items, identify
which assets the catalyst actually moves, and surface the highest-conviction
money-making opportunities. Think like a banker whose own bonus depends on the
client's returns: aggressive on real edges, disciplined about protecting capital.

IMPORTANT RULES:
- This is for informational purposes only, not formal financial advice.
- Only recommend assets in these categories: {asset_scope}.
- Do not invent tickers. If you are unsure of the ticker, use null.
- For flagged (unverified) sources, set risk_level to 'high' regardless.
- For crypto assets, use the standard symbol (BTC, ETH, SOL etc) as the ticker.
- For ETFs, use the standard ticker (SPY, QQQ etc) as the ticker.
- Check the OPEN POSITIONS block. Do not recommend 'buy' for tickers the user already owns.
  If strong news exists about an owned ticker, include it as 'watch' only to inform the user.
  Always try to find at least 5-10 other actionable opportunities from the news beyond owned tickers.
- Use the CRYPTO ASSET CONTEXT block to understand what each crypto asset does.
- Use the 14-DAY PRICE TREND DATA block to calibrate exit targets and stop loss levels.
  Always include a stop loss in the exit_condition field, e.g. "target 8% gain, stop loss at 4%".

SIGNAL QUALITY — BE RUTHLESSLY SELECTIVE:
- Only return a 'buy' signal if the catalyst is unambiguous and directly actionable.
  Strong catalysts: earnings beats, M&A announcements, FDA approvals, major contract wins,
  short squeeze setups, insider buying at scale, SEC filings showing material positive events.
  Weak catalysts (use 'watch' or skip): analyst upgrades, general sector optimism, vague macro tailwinds.
- Return 'watch' for interesting but unclear setups — where the thesis is sound but timing is uncertain.
- Return 'avoid' or omit entirely for anything with weak evidence or unverified sources.

HIGHLY RECOMMENDED — SET TO TRUE ONLY WHEN ALL 3 CONDITIONS ARE MET:
1. The catalyst is unambiguous — no "may", "could", "might". It happened or was officially announced.
2. The confidence score is 0.68 or above (Finnhub company news, SEC filing, or Robinhood news).
3. The price trend supports entry — not already at the 14-day high, not in a sharp downtrend
   (unless the catalyst is a reversal event like an earnings beat or buyout).
Set highly_recommended to false for everything else including all watch signals.

EXIT CONDITIONS — BE AGGRESSIVE FOR STRONG SIGNALS:
- highly_recommended buys: gain targets 12-20%, stops 4-6% (let winners run, stops wide enough to breathe)
- Regular buys: gain targets 6-10%, stops 2-4%
- Upside must be at least 2x the stop loss distance. If it isn't, widen the target not the stop.
- For high volatility assets (avg daily range >3%) use stops of at least 5% to avoid noise shakeouts.
- For downtrending assets be more conservative with targets unless the catalyst is a clear reversal.

You must respond with ONLY a valid JSON array. No preamble, no explanation,
no markdown code fences. Just the raw JSON array.

Each object in the array must have exactly these fields:
{{
  "ticker":             string or null,
  "company_name":       string,
  "asset_type":         "stock" or "etf" or "crypto",
  "direction":          "buy" or "watch" or "avoid",
  "entry_rationale":    string (max 2 sentences),
  "exit_condition":     string (e.g. '10% gain' or '2 weeks' or 'earnings release'),
  "risk_level":         "low" or "medium" or "high",
  "confidence_score":   number (pass through from the news item),
  "flagged":            boolean,
  "source_title":       string (the news headline this is based on),
  "highly_recommended": boolean
}}

If no assets are clearly actionable from the news provided, return an empty array: []"""

    user_prompt = f"""Here are today's validated news items. Analyze them and return
your recommendations as a JSON array.

NEWS ITEMS:
{news_block}"""

    print(f"\nSending {len(unique_items)} stories to Claude for analysis...")
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
        )

        raw = message.content[0].text.strip()

        if message.stop_reason == "max_tokens":
            print("Warning: Claude hit token limit — truncating to valid JSON.")
            last_brace = raw.rfind("},")
            if last_brace != -1:
                raw = raw[:last_brace + 1] + "]"
            else:
                print("Could not recover truncated JSON.")
                return []

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        recommendations = json.loads(raw)

        if not isinstance(recommendations, list):
            print(f"Claude returned unexpected type: {type(recommendations)}")
            return []

        # Deduplicate by ticker — keep the highest confidence score per ticker
        seen_tickers = {}
        for rec in recommendations:
            ticker = rec.get("ticker")
            if not ticker:
                continue
            if ticker not in seen_tickers:
                seen_tickers[ticker] = rec
            else:
                # Keep whichever has higher confidence
                if rec.get("confidence_score", 0) > seen_tickers[ticker].get("confidence_score", 0):
                    seen_tickers[ticker] = rec

        deduped = list(seen_tickers.values())
        if len(deduped) < len(recommendations):
            print(f"Removed {len(recommendations) - len(deduped)} duplicate ticker(s)")

        print(f"Claude returned {len(deduped)} recommendations")
        return deduped

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response was:\n{raw[:500]}")
        return []
    except Exception as e:
        import traceback
        print(f"Claude API error: {e}")
        print(traceback.format_exc())
        return []


if __name__ == "__main__":
    test_items = [
        {
            "title":            "Bitcoin faces identity crisis as institutional adoption grows",
            "summary":          "Bitcoin adoption by institutions is accelerating despite ongoing debates about its primary use case.",
            "source":           "CoinDesk",
            "source_type":      "crypto_rss",
            "asset_type":       "crypto",
            "confidence_score": 0.45,
            "flagged":          False,
            "ticker":           "BTC",
        },
        {
            "title":            "HPE skyrockets 30% on biggest earnings beat since 2018",
            "summary":          "Hewlett Packard Enterprise crushed earnings estimates driven by surging AI server demand.",
            "source":           "CNBC",
            "source_type":      "finnhub_company",
            "asset_type":       "stock",
            "confidence_score": 0.70,
            "flagged":          False,
            "ticker":           "HPE",
        },
    ]
    results = run_analysis(test_items, include_stocks=True, include_crypto=True)
    print(f"\n--- Recommendations ---")
    for r in results:
        print(f"\n{r.get('ticker')} — {r.get('company_name')}")
        print(f"  Direction : {r.get('direction')}")
        print(f"  Rationale : {r.get('entry_rationale')}")
        print(f"  Exit      : {r.get('exit_condition')}")
        print(f"  Risk      : {r.get('risk_level')}")