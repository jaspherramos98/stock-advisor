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
    items:           list[dict],
    crypto_context:  dict = None,
    price_history:   dict = None,
    open_positions:  list = None,
    closed_positions: list = None,
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

    # Past trade outcomes — let Claude learn from the user's own realized results
    if closed_positions:
        scored = [p for p in closed_positions if p.get("pnl_pct") is not None]
        if scored:
            wins   = [p for p in scored if p["pnl_pct"] > 0]
            win_rate = round(len(wins) / len(scored) * 100)
            avg_pnl  = round(sum(p["pnl_pct"] for p in scored) / len(scored), 1)
            lines.append("=== PAST TRADE OUTCOMES — LEARN FROM THESE ===")
            lines.append(
                f"The user's realized track record so far: {len(scored)} closed trades, "
                f"{win_rate}% win rate, average P&L {avg_pnl:+.1f}%. Study what actually made or "
                "lost money for THIS user and adjust your conviction accordingly — if a certain kind "
                "of setup (source, catalyst type, risk level) has repeatedly lost, be more skeptical "
                "of similar ones today; lean into the kinds that have worked."
            )
            # Show the most recent trades (cap to keep tokens down)
            for p in scored[:12]:
                outcome = "WIN " if p["pnl_pct"] > 0 else "LOSS"
                lines.append(
                    f"[{outcome} {p['pnl_pct']:+.1f}%] ${p.get('ticker','?')} — "
                    f"{p.get('direction','?')} | conf {p.get('confidence', 0):.2f} | "
                    f"source: {p.get('source_title','?')} | closed because: {p.get('close_reason','?')}"
                )
            lines.append("=== END PAST TRADE OUTCOMES ===\n")

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
    closed_positions = []
    try:
        from storage.positions import get_open_positions, get_closed_positions
        open_positions = get_open_positions()
        if open_positions:
            tickers = [p["ticker"] for p in open_positions]
            print(f"Claude analyst: user has {len(open_positions)} open positions ({', '.join(tickers)}) — passing to Claude")
        closed_positions = get_closed_positions()
        if closed_positions:
            print(f"Claude analyst: feeding {len(closed_positions)} past trade outcomes back for reflection")
    except Exception as e:
        print(f"Claude analyst: could not load positions: {e}")

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
            closed_positions=closed_positions,
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

    system_prompt = f"""You are a sharp, disciplined investment banker running a
personal trading desk for a single client. Your mandate is to GROW THE CLIENT'S
CAPITAL — but you keep this job by NOT losing money, and the fastest way to lose
money is buying a move that has already happened. So you are equally ruthless in
two directions: you put capital to work when there is a real, still-open edge, and
you refuse to chase catalysts the market has already priced in. A trade you skip
costs nothing; a top you buy costs real money. When in doubt, prefer 'watch' over
'buy'. Capital preserved is capital ready for the next genuine setup.

IMPORTANT RULES:
- This is for informational purposes only, not formal financial advice.
- Only recommend assets in these categories: {asset_scope}.
- Do not invent tickers. If you are unsure of the ticker, use null.
- For flagged (unverified) sources, set risk_level to 'high' regardless.
- For crypto assets, use the standard symbol (BTC, ETH, SOL etc) as the ticker.
- For ETFs, use the standard ticker (SPY, QQQ etc) as the ticker.
- Check the OPEN POSITIONS block. Do not recommend 'buy' for tickers the user already owns.
  If strong news exists about an owned ticker, include it as 'watch' only to inform the user.
  You may surface other opportunities from the news as 'buy' OR 'watch', but never force
  buys to hit a quota — on a weak day, returning only watches (or an empty array) is correct.
- Use the CRYPTO ASSET CONTEXT block to understand what each crypto asset does.
- Use the 14-DAY PRICE TREND DATA block to calibrate exit targets and stop loss levels.
  Always include a stop loss in the exit_condition field, e.g. "target 8% gain, stop loss at 4%".

THIS IS A ONE-SHOT TOOL — EVERY RECOMMENDATION MUST STAND ALONE:
- This tool runs ONCE per session. It does NOT monitor positions live, send updates, or get a
  second pass. The user reads this snapshot and acts on it directly. There is no "later" where you
  or the tool revisit anything.
- Therefore exit_condition must ALWAYS be a concrete, self-contained instruction the user can set and
  follow on their own RIGHT NOW. It must be a real price/level-based rule — a gain target AND a stop
  loss (e.g. "target 9% gain, stop loss at 4%"), or a clear price level to act on.
- NEVER write process/deferral placeholders. Banned exit_condition phrasings include "await details",
  "review the filing", "reassess later", "monitor", "check back", "wait and see", "pending review",
  or anything that depends on someone re-evaluating after this run. If you can't state a concrete
  price-based exit, the item is not actionable — mark it 'avoid' or omit it.

CONFIDENCE_SCORE IS NOT EDGE:
- confidence_score measures SOURCE CREDIBILITY (how much to trust the report) — it does NOT
  measure how good the trade is or how much money it will make. A highly credible source
  reporting an already-priced-in event is still a losing buy. Judge the edge and the timing
  SEPARATELY from how trustworthy the source is.

CATALYST TIMING — IS THE EDGE STILL THERE? (most important check)
- The #1 way to lose money here is buying news that is already in the price. Before any 'buy',
  ask: has the market already reacted to this catalyst? If the stock already gapped/ran on this
  exact news, the easy money is gone — that is a 'watch' (you missed the entry), not a 'buy'.
- Cross-check every buy candidate against the 14-DAY PRICE TREND DATA:
  - At/near the 14-day high AND that run was driven by this same news → likely priced in → 'watch'.
  - Only call a fresh 'buy' when there is still room to run: the catalyst is recent and the price
    has NOT already fully reflected it.
- Old news = no edge. If the headline describes something from days ago and the price already
  moved, skip it. "Buy the rumor, sell the news" — by the time it's a headline, much is priced in.

M&A / BUYOUTS — HANDLE WITH CARE (this is where naive buyers get trapped):
- Distinguish the TARGET from the ACQUIRER — they move very differently.
- All-cash deal already announced: the TARGET snaps to just below the offer price and then trades
  flat until close. The only remaining upside is the small arbitrage spread. Treat as 'watch',
  not 'buy', unless a clearly material spread remains — and NEVER mark it highly_recommended.
- If the deal has already CLOSED/COMPLETED, the target is being delisted — do NOT recommend it.
- The ACQUIRER often FALLS on announcement (paying a premium, taking on debt) — don't reflexively buy it.
- Always flag unresolved regulatory/antitrust/financing risk: deals break, and the target craters when they do.

EARNINGS & OTHER CATALYSTS:
- An earnings "beat" is NOT automatically bullish. Stocks routinely fall on a beat when guidance is
  weak or the beat was already expected. Only buy if the price reaction still has room and the trend agrees.
- Treat the same way for FDA approvals, contract wins, etc.: the question is always whether the move is
  still ahead of us or already behind us.

SIGNAL QUALITY — BE RUTHLESSLY SELECTIVE:
- Only return a 'buy' signal if the catalyst is unambiguous, directly actionable, AND the edge is
  still open (not already priced in per the timing check above).
  Strong catalysts: earnings beats with room to run, fresh M&A with material spread, FDA approvals,
  major contract wins, short squeeze setups, insider buying at scale, SEC filings showing material events.
  Weak catalysts (use 'watch' or skip): analyst upgrades, general sector optimism, vague macro tailwinds.
- Return 'watch' for sound theses with uncertain timing OR catalysts that already moved the price.
- Return 'avoid' or omit entirely for anything with weak evidence or unverified sources.
- It is completely fine — and often the right call — to return mostly 'watch' or an empty array on a
  weak day. Forcing buys when nothing has real, un-priced-in edge is exactly how the desk loses money.

BULL vs BEAR DEBATE — ARGUE BOTH SIDES BEFORE YOU COMMIT:
- For EVERY candidate you return (buy or watch), you must fill bull_case and bear_case.
  - bull_case: the single strongest, most specific reason the trade works (the catalyst, the edge).
  - bear_case: the single strongest, most specific reason it LOSES money — what a smart short-seller
    would say. Be concrete: "already priced in after the 9% pop", "deal faces antitrust review",
    "beat was low-quality, guidance cut", "thin volume, easily reversed". Never write "no risks".
- Then decide HONESTLY: only keep direction='buy' if the bull case clearly outweighs the bear case
  after that scrutiny. If the bear case is comparably strong or the edge is already gone, downgrade
  to 'watch'. A 'watch' costs nothing; a bad 'buy' costs real money.
- This debate is the gate for 'buy' — do not wave a candidate through just because the catalyst exists.

HIGHLY RECOMMENDED — SET TO TRUE ONLY WHEN ALL 4 CONDITIONS ARE MET:
1. The catalyst is unambiguous AND recent — it happened or was officially announced within roughly the
   last 1-2 trading days, not old news. No "may", "could", "might".
2. The confidence score is 0.68 or above (Finnhub company news, SEC filing, or Robinhood news).
3. The edge is still open — the price has NOT already fully reflected the catalyst: not pinned at the
   14-day high on this same news, and not a buyout target already trading at its offer price.
4. The price trend supports entry — not in a sharp downtrend (unless the catalyst is a genuine reversal
   event such as a fresh earnings beat with room to run).
Set highly_recommended to false for everything else, including all watch signals and all M&A targets.

EXIT CONDITIONS — CONCRETE, SELF-CONTAINED, REWARD JUSTIFIES RISK:
- For 'buy': exit_condition is a price-based sell rule the user sets immediately — a gain target AND a
  stop loss. highly_recommended buys: gain targets 12-20%, stops 4-6% (let winners run, wide enough to
  breathe). Regular buys: gain targets 6-10%, stops 2-4%.
- For 'watch': still give a concrete, self-contained rule — the specific price level or condition that
  would make it a buy, PLUS the target/stop to use if entered. E.g. "Buy only on a pullback to ~$190;
  then target 8% gain, stop loss at 4%" or "Skip unless it breaks above $52 on volume; then target 10%,
  stop 5%". A watch is NOT permission to be vague — it's a clear if/then the user can act on alone.
- Upside must be at least 2x the stop loss distance. If it isn't, widen the target not the stop —
  and if a realistic target can't clear that 2x bar, it's a 'watch', not a 'buy'.
- For high volatility assets (avg daily range >3%) use stops of at least 5% to avoid noise shakeouts.
- For downtrending assets be more conservative with targets unless the catalyst is a clear reversal.
- Reminder: NO "await/review/reassess/monitor" placeholders — see the one-shot rule above.

PORTFOLIO RISK GATE — REVIEW YOUR BUYS AS ONE BOOK (final check before you answer):
- Don't just rate candidates in isolation. Step back and look at all your 'buy' signals together,
  PLUS what the user already holds in OPEN POSITIONS — that is the real portfolio.
- Avoid concentration: if several buys are driven by the same theme, sector, or single macro driver
  (e.g. all semiconductors, all rate-cut plays, all one customer's supply chain), they will crash
  together. Keep only the strongest 1-2 as 'buy' and downgrade the rest to 'watch'.
- If a buy would pile more exposure onto a sector/theme the user already owns heavily, prefer 'watch'
  and say so in the bear_case.
- Diversify the buy list across uncorrelated catalysts where possible. Quality and spread beat quantity.
- (Budgeting note: the allocator caps any single name at 40% and double-weights highly_recommended,
  so don't over-stuff the list — a few well-chosen, uncorrelated buys is the goal.)

CATALYST TIMING FIELD — purely factual "when", not an instruction:
- "catalyst_timing" is a short factual note about WHEN the catalyst happens/happened, so the user knows
  the timeframe. It is informational only — never a process step (no "review within X days").
- Future event with a date grounded in the news: state it — e.g. "Earnings Jul 15", "Merger expected
  to close ~Q3 2026", "FDA PDUFA decision Mar 1".
- Event already occurred (e.g. an 8-K already filed, earnings already reported): say so plainly —
  e.g. "Already filed Jun 15" or "Reported Jun 14" — so the user knows the move may be done.
- No date in the news and none implied: give a short honest horizon — e.g. "momentum, ~1-2 weeks" or
  "open-ended". Do NOT invent dates.
- Keep it to a few words. Price-based sell rules live in exit_condition, not here.

You must respond with ONLY a valid JSON array. No preamble, no explanation,
no markdown code fences. Just the raw JSON array.

Each object in the array must have exactly these fields:
{{
  "ticker":             string or null,
  "company_name":       string,
  "asset_type":         "stock" or "etf" or "crypto",
  "direction":          "buy" or "watch" or "avoid",
  "entry_rationale":    string (max 2 sentences),
  "bull_case":          string (1-2 sentences — the strongest reason this works, see DEBATE below),
  "bear_case":          string (1-2 sentences — the strongest reason this loses money, see DEBATE below),
  "exit_condition":     string (e.g. '10% gain' or '2 weeks' or 'earnings release'),
  "catalyst_timing":    string — WHEN the catalyst is expected to play out (see CATALYST TIMING FIELD below),
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