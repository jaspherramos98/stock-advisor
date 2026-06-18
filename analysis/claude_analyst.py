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
    fundamentals:   dict = None,
    etf_strength:   dict = None,
    etf_facts:      dict = None,
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

            # Technical indicators block — deterministic math on real prices.
            # Use as CONTEXT to confirm/temper a thesis, not as a hard filter.
            tech_lines = []
            for ticker, data in available.items():
                if data.get("rsi_14") is None and data.get("sma_50") is None:
                    continue  # not enough history for this ticker
                rsi = data.get("rsi_14")
                rsi_tag = ""
                if rsi is not None:
                    rsi_tag = " (overbought)" if rsi >= 70 else " (oversold)" if rsi <= 30 else ""
                tech_lines.append(
                    f"${ticker}: RSI14 {rsi}{rsi_tag} | MACD {data.get('macd_state')}"
                    f" ({data.get('macd_cross')}) | price {data.get('price_vs_sma50')} 50d-SMA,"
                    f" {data.get('price_vs_sma200')} 200d-SMA | {data.get('ma_trend')} | "
                    f"{data.get('pct_from_52w_high')}% from 52w high | "
                    f"volume {data.get('vol_vs_avg')}x 30d avg"
                )
            if tech_lines:
                lines.append("=== TECHNICAL INDICATORS (confirmation/timing context) ===")
                lines.append(
                    "Deterministic indicators from ~1y of real prices. Use them to TIME and CONFIRM "
                    "a news thesis, not to invent one: RSI>70 = overbought (don't chase a buy; supports "
                    "'already ran' / watch), RSI<30 = oversold. MACD bullish + a recent bullish crossover "
                    "supports entry timing; bearish does the opposite. Price above both SMAs with a golden "
                    "cross = healthy uptrend; below with a death cross = weak. Near the 52w high = stretched; "
                    "high volume vs average = the move has real participation. These confirm or temper the "
                    "catalyst — they do NOT override a strong fact-based catalyst."
                )
                lines.extend(tech_lines)
                lines.append("=== END TECHNICAL INDICATORS ===\n")
            # Open positions block — tells Claude what the user already owns

    # Fundamentals block — reported financials as a quality check.
    if fundamentals:
        fund_avail = {k: v for k, v in fundamentals.items() if v}
        if fund_avail:
            lines.append("=== FUNDAMENTALS (quality check — reported financials) ===")
            lines.append(
                "Use these reported numbers to judge company QUALITY and temper conviction — not to "
                "invent a thesis. Rich valuation (high P/E) + slowing growth → be cautious / prefer watch. "
                "Strong margins, positive earnings growth, manageable debt, positive free cash flow → "
                "quality name. Tiny market cap + thin fundamentals → treat as higher risk (pump-prone). "
                "Missing values just mean Yahoo had no data; don't penalize for that alone."
            )
            for ticker, f in fund_avail.items():
                mc = f.get("market_cap")
                mc_str = f"${mc/1e9:.1f}B" if isinstance(mc, (int, float)) and mc else "n/a"
                lines.append(
                    f"${ticker}: {f.get('sector') or 'n/a'} / {f.get('industry') or 'n/a'} | "
                    f"mkt cap {mc_str} | P/E trail {f.get('trailing_pe')} fwd {f.get('forward_pe')} | "
                    f"P/B {f.get('price_to_book')} | margin {f.get('profit_margin_pct')}% | "
                    f"rev growth {f.get('revenue_growth_pct')}% | earnings growth {f.get('earnings_growth_pct')}% | "
                    f"debt/equity {f.get('debt_to_equity')} | FCF {f.get('free_cash_flow')}"
                )
            lines.append("=== END FUNDAMENTALS ===\n")

    # ETF relative strength (rotation vs SPY) — the right lens for macro/thematic funds.
    if etf_strength:
        rs_avail = {k: v for k, v in etf_strength.items() if v}
        if rs_avail:
            lines.append("=== ETF RELATIVE STRENGTH vs SPY (rotation) ===")
            lines.append(
                "ETFs are macro/thematic baskets, not single-catalyst trades — judge them by ROTATION "
                "vs the market (SPY), not by hunting a news catalyst. RS-Ratio >100 = the ETF is "
                "outperforming the market's trend; RS-Momentum >100 = that outperformance is still "
                "accelerating. Quadrant read: Leading (strong + accelerating → favor longs / higher "
                "conviction) → Weakening (strong but fading → take profits / watch) → Lagging (weak + "
                "still falling → avoid longs, short-bias) → Improving (weak but turning up → early watch "
                "for a long). Use this to set ETF direction and conviction; a leading ETF on a real "
                "thematic tailwind can be high-conviction even without a single news catalyst."
            )
            for ticker, rs in rs_avail.items():
                lines.append(
                    f"${ticker}: {rs.get('quadrant')} | RS-Ratio {rs.get('rs_ratio')} | "
                    f"RS-Momentum {rs.get('rs_momentum')} | {rs.get('rel_perf_63d'):+}% vs SPY (3mo)"
                )
            lines.append("=== END ETF RELATIVE STRENGTH ===\n")

    # ETF facts — fund quality (use INSTEAD of company fundamentals for ETFs).
    if etf_facts:
        ef_avail = {k: v for k, v in etf_facts.items() if v}
        if ef_avail:
            lines.append("=== ETF FACTS (fund quality — use instead of company fundamentals for ETFs) ===")
            lines.append(
                "Reported fund facts. Lower expense ratio = less drag; larger AUM = more liquid/established. "
                "Top holdings and sector weights tell you what the fund is REALLY exposed to — make sure the "
                "thesis matches the actual basket (e.g. don't buy a 'tech' ETF for an AI catalyst if its top "
                "weights are utilities). Missing values just mean Yahoo had no data; don't penalize for that."
            )
            for ticker, f in ef_avail.items():
                aum = f.get("aum")
                aum_str = f"${aum/1e9:.1f}B" if isinstance(aum, (int, float)) and aum else "n/a"
                holdings = ", ".join(f["top_holdings"]) if f.get("top_holdings") else "n/a"
                sectors = (
                    ", ".join(f"{k} {v}%" for k, v in f["sector_weights"].items())
                    if f.get("sector_weights") else "n/a"
                )
                lines.append(
                    f"${ticker}: {f.get('category') or 'n/a'} | {f.get('fund_family') or 'n/a'} | "
                    f"AUM {aum_str} | expense {f.get('expense_ratio_pct')}% | yield {f.get('yield_pct')}% | "
                    f"beta3y {f.get('beta_3y')}\n"
                    f"   top: {holdings}\n"
                    f"   sectors: {sectors}"
                )
            lines.append("=== END ETF FACTS ===\n")

    if open_positions:
        lines.append("=== YOUR CURRENT OPEN POSITIONS (EXCLUDE THESE) ===")
        lines.append(
            "The user ALREADY OWNS these assets and tracks them elsewhere. Do NOT include any of these "
            "tickers in your output — not as 'buy', not as 'watch', not at all. Only surface NEW ideas "
            "the user doesn't already hold."
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
        signal_hint = " ⭐ high-signal filing" if item.get("high_signal") else ""
        summary     = item.get("summary") or "No summary available"
        source      = item.get("source") or "Unknown"
        title       = item.get("title") or "No title"
        score       = item.get("confidence_score", 0)
        lines.append(
            f"{i}. [{score} confidence{flag_hint}{signal_hint}]{ticker_hint}\n"
            f"   Title   : {title}\n"
            f"   Summary : {summary[:200]}\n"
            f"   Source  : {source}"
        )
    return "\n\n".join(lines)


# Phrases that signal a non-actionable guess rather than a real, evidenced plan.
_VAGUE_EXIT_MARKERS = (
    "n/a", "await", "watching for", "deal clarity", "tbd", "to be determined",
    "pending", "unclear", "wait and see", "more info", "monitor for",
)


def _filter_recommendations(recs: list[dict], open_positions: list[dict]) -> list[dict]:
    """
    Enforces two rules deterministically, regardless of what the model returned:
      1. Drop anything the user already owns (open positions are tracked elsewhere).
      2. Drop guesses — recs with no ticker or a placeholder/vague exit_condition
         ("N/A", "watching for deal clarity", "await details", etc.).
    Keeps only NEW, fact-based, actionable ideas.
    """
    owned = {(p.get("ticker") or "").upper() for p in (open_positions or [])}
    cleaned, dropped_owned, dropped_vague = [], 0, 0

    for rec in recs:
        ticker = (rec.get("ticker") or "").strip().upper()
        if not ticker:
            dropped_vague += 1
            continue
        if ticker in owned:
            dropped_owned += 1
            continue
        exit_text = (rec.get("exit_condition") or "").lower().strip()
        if not exit_text or any(m in exit_text for m in _VAGUE_EXIT_MARKERS):
            dropped_vague += 1
            continue
        cleaned.append(rec)

    if dropped_owned:
        print(f"Filtered out {dropped_owned} already-owned ticker(s)")
    if dropped_vague:
        print(f"Filtered out {dropped_vague} vague/non-actionable recommendation(s)")
    return cleaned


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

    # Classify each news ticker by asset type so we fetch the right context for it:
    # stocks/ETFs get price history; stocks get company fundamentals; ETFs get
    # relative-strength rotation + fund facts (R3); crypto gets its own history path.
    def _item_asset_type(item: dict) -> str:
        st = item.get("source_type", "")
        at = item.get("asset_type", "")
        if at == "crypto" or st in ("finnhub_crypto", "crypto_rss"):
            return "crypto"
        if at == "etf" or st in ("finnhub_etf", "etf_rss"):
            return "etf"
        return "stock"

    ticker_types: dict[str, str] = {}
    for item in unique_items:
        t = item.get("ticker")
        if not t:
            continue
        ty = _item_asset_type(item)
        # If any item tags the ticker as etf/crypto, prefer that over plain "stock".
        if t not in ticker_types or ticker_types[t] == "stock":
            ticker_types[t] = ty

    stock_news_tickers  = [t for t, ty in ticker_types.items() if ty == "stock"]
    etf_news_tickers    = [t for t, ty in ticker_types.items() if ty == "etf"]
    crypto_news_tickers = [t for t, ty in ticker_types.items() if ty == "crypto"]

    # Fetch ~1y price history (+ technicals) — only for tickers that appear in the news.
    price_history = {}
    try:
        from ingestion.prices import fetch_price_history

        equity_tickers = stock_news_tickers + etf_news_tickers  # both use the yfinance stock path
        if equity_tickers and (include_stocks or include_etfs):
            price_history.update(fetch_price_history(equity_tickers, asset_type="stock"))

        if crypto_news_tickers and include_crypto:
            price_history.update(fetch_price_history(crypto_news_tickers, asset_type="crypto"))

        fetched = sum(1 for v in price_history.values() if v is not None)
        print(f"Claude analyst: loaded 14-day price history for {fetched} tickers")
    except Exception as e:
        print(f"Claude analyst: price history fetch failed: {e}")

    # Fetch fundamentals (quality check) for STOCK tickers only — meaningless for ETFs.
    fundamentals = {}
    try:
        if stock_news_tickers and include_stocks:
            from ingestion.fundamentals import fetch_fundamentals
            fundamentals = fetch_fundamentals(stock_news_tickers)
            loaded = sum(1 for v in fundamentals.values() if v)
            print(f"Claude analyst: loaded fundamentals for {loaded} tickers")
    except Exception as e:
        print(f"Claude analyst: fundamentals fetch failed: {e}")

    # R3: ETF relative-strength rotation (vs SPY) + ETF fund facts — for ETF tickers.
    etf_strength, etf_facts = {}, {}
    try:
        if etf_news_tickers and include_etfs:
            from ingestion.prices    import fetch_etf_relative_strength
            from ingestion.etf_facts import fetch_etf_facts
            etf_strength = fetch_etf_relative_strength(etf_news_tickers)
            etf_facts    = fetch_etf_facts(etf_news_tickers)
            print(f"Claude analyst: loaded ETF rotation for {sum(1 for v in etf_strength.values() if v)} "
                  f"and facts for {sum(1 for v in etf_facts.values() if v)} ETFs")
    except Exception as e:
        print(f"Claude analyst: ETF context fetch failed: {e}")

    try:
        news_block = _build_prompt(
            unique_items,
            crypto_context=crypto_context,
            price_history=price_history,
            open_positions=open_positions,
            fundamentals=fundamentals,
            etf_strength=etf_strength,
            etf_facts=etf_facts,
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
- For ETFs, use the standard ticker (SPY, QQQ etc) as the ticker. ETFs are macro/thematic, not
  single-catalyst — judge them on ROTATION using the ETF RELATIVE STRENGTH block (favor 'Leading',
  avoid 'Lagging') and the ETF FACTS block, not by forcing a news catalyst onto them.
- Check the OPEN POSITIONS block and EXCLUDE every ticker listed there — do not output it at all,
  not as 'buy', not as 'watch'. The user already holds and tracks those; only surface NEW ideas.
- FACT-CHECKED ONLY — NO GUESSING. Recommend an asset only when the news item gives concrete,
  verifiable detail (actual earnings numbers, a named deal with terms, a stated approval/contract).
  If a headline or filing has no real substance — e.g. a bare "8-K filed" or "Form 8-K" with no
  description of WHAT it says — you cannot fact-check it, so SKIP it. Do NOT speculate, and never
  emit placeholder plans like "watching for deal clarity", "await details", or "N/A". Base every
  recommendation on an informed, evidenced movement — quality of evidence over quantity.
- Never force BUYS to hit a quota — only genuinely actionable, un-priced-in catalysts are buys
  (usually just 0-3). But DO give the user a full read on the day via watches — see the WATCH FLOOR below.
- Use the CRYPTO ASSET CONTEXT block to understand what each crypto asset does.
- Use the 14-DAY PRICE TREND DATA block to calibrate exit targets and stop loss levels.
  Always include a stop loss in the exit_condition field, e.g. "target 8% gain, stop loss at 4%".

TWO SEPARATE NUMBERS — CREDIBILITY vs CONVICTION:
- confidence_score (given to you, do not change it) measures SOURCE CREDIBILITY — how much to TRUST
  the report (1.0 SEC filing … 0.15 Reddit). It does NOT measure how good or timely the trade is.
- conviction (YOU set it, 0-100) measures the EDGE — how strong, timely, and still-open the money-making
  opportunity is. This is your call on trade quality, and it drives ranking and position size.
- Judge them SEPARATELY. A highly credible source reporting an already-priced-in event = high
  confidence_score but LOW conviction (the edge is gone). A strong fresh catalyst from a decent source =
  high conviction even if confidence_score is moderate.
- Score conviction RELATIVE TO THE ASSET'S OWN CLASS — a great crypto/ETF setup can be high-conviction
  even though crypto/ETF news sources never reach SEC-level credibility. Don't let a capped
  confidence_score drag down conviction for a genuinely strong non-stock setup.
- conviction guide: 80-100 = strong, recent, clearly un-priced-in edge; 50-79 = real but partial
  (timing/size uncertain → usually 'watch'); below 50 = weak/priced-in (watch or skip).

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
- Skip (omit) anything with weak evidence, unverified sources, or no concrete detail to fact-check.
WATCH FLOOR — ALWAYS SHOW YOUR WORK (do not return a near-empty list):
- ALWAYS return at least 10 items total (buys + shorts + watches), drawn from the strongest, most
  relevant, NON-owned stories you were given. The point is to show the user your read on the WHOLE day,
  not just trades. A 'watch' = "notable, but I'd wait for X before acting" — it commits NO capital, so
  surfacing it is low-risk and informative. A blank/near-blank result is NOT acceptable on a normal news
  day; it just leaves the user blind.
- BUYS stay strict and few: only genuinely actionable, un-priced-in, fact-based catalysts qualify
  (usually 0-3). NEVER pad the buy list to reach the floor — fill the rest with watches.
- A WATCH must be grounded in a REAL news item from the list (never invented). Put its BUY TRIGGER in
  the `entry_trigger` field (the concrete price level/condition that would make it actionable, e.g.
  "breaks above $52 on volume" or "pulls back to ~$190"), and put ONLY the target/stop in
  `exit_condition`. Keep the two separate — do not cram the entry condition into exit_condition. No
  vague placeholders ("monitor", "await details", "N/A").
- Skipping is only for true noise (no ticker, unverified, zero concrete detail). Among everything that
  is NOT noise, surface the top ~10+ as buy/watch. Return fewer than 10 ONLY if there genuinely aren't
  that many relevant, non-owned stories to discuss (rare — you are given ~25).

SHORTS — BEARISH IDEAS (stocks only, same fact-based discipline):
- Use direction='short' to profit from a stock you expect to FALL. This is the bearish counterpart
  to 'buy'; the old passive 'avoid' is for ideas you simply skip, 'short' is for ones worth acting on.
- Only short on an unambiguous, recent, FACT-BASED bearish catalyst: earnings miss WITH weak guidance,
  guidance cut, FDA rejection, lost major customer/contract, accounting/fraud red flags, large dilution
  or debt distress, or a clear breakdown (death cross + deteriorating fundamentals). No guessing.
- Same priced-in check applies in reverse: if the stock already collapsed on this news, the easy money
  is gone → 'watch' or skip, not 'short'.
- SQUEEZE GUARD (critical): do NOT short stocks that are heavily shorted, low-float, or already a short
  squeeze setup — those can rip upward violently. (Note: a short squeeze is a BULLISH catalyst elsewhere
  in these rules; never short into one.) Prefer liquid, large-cap names for shorts.
- Shorts are stocks only — never short crypto or ETFs here.
- exit_condition for a short uses the SAME wording as a buy — "target X% gain, stop loss at Y%" —
  where for a short "gain" means YOUR PROFIT as the stock FALLS X%, and the stop triggers if it RISES
  Y% against you. Keep stops tight; losses on shorts are theoretically unbounded.

HIGHLY RECOMMENDED — SET TO TRUE ONLY WHEN ALL 4 CONDITIONS ARE MET:
1. The catalyst is unambiguous AND recent — it happened or was officially announced within roughly the
   last 1-2 trading days, not old news. No "may", "could", "might".
2. Your conviction is 75 or above (a strong, clearly un-priced-in edge) AND confidence_score is at
   least 0.5 (a credible source, not a Reddit rumor). Conviction drives this gate; the credibility
   floor just keeps out unverified noise.
3. The edge is still open — the price has NOT already fully reflected the catalyst: not pinned at the
   14-day high on this same news, and not a buyout target already trading at its offer price.
4. The price trend supports entry — not in a sharp downtrend (unless the catalyst is a genuine reversal
   event such as a fresh earnings beat with room to run).
Set highly_recommended to false for everything else, including all watch signals, all M&A targets,
and all shorts (shorts are higher-risk — never give them the 2x highly-recommended capital weight).

EXIT CONDITIONS — REWARD MUST JUSTIFY RISK:
- highly_recommended buys: gain targets 12-20%, stops 4-6% (let winners run, stops wide enough to breathe)
- Regular buys: gain targets 6-10%, stops 2-4%
- Upside must be at least 2x the stop loss distance. If it isn't, widen the target not the stop —
  and if a realistic target can't clear that 2x bar, it's a 'watch', not a 'buy'.
- For high volatility assets (avg daily range >3%) use stops of at least 5% to avoid noise shakeouts.
- For downtrending assets be more conservative with targets unless the catalyst is a clear reversal.
- For 'short': use the same "target X% gain, stop loss at Y%" phrasing — "gain" = the stock dropping
  in your favor, "stop loss" = it rising against you. Keep stops tight; reward must still be ≥2x stop.
- exit_condition must be a real price-based rule (gain/cover target + stop) for EVERY item. If you can't
  state one from concrete info, you don't have a thesis — skip the item. Never output "N/A", "watching
  for deal clarity", "await details", or any placeholder.
- entry_trigger: for a 'watch', the concrete buy condition (price level/event that makes it actionable).
  For a 'buy' or 'short' you act now, so set entry_trigger to "now" (or "" ). Never put the entry
  condition inside exit_condition.

You must respond with ONLY a valid JSON array. No preamble, no explanation,
no markdown code fences. Just the raw JSON array.

Each object in the array must have exactly these fields:
{{
  "ticker":             string or null,
  "company_name":       string,
  "asset_type":         "stock" or "etf" or "crypto",
  "direction":          "buy" or "short" or "watch" or "avoid",
  "entry_rationale":    string (max 2 sentences),
  "entry_trigger":      string (for 'watch': the buy condition/price that makes it actionable; for buy/short: "now"),
  "exit_condition":     string (target + stop only, e.g. 'target 10% gain, stop loss at 4%'),
  "risk_level":         "low" or "medium" or "high",
  "confidence_score":   number (SOURCE CREDIBILITY — pass through from the news item, do not change),
  "conviction":         number 0-100 (YOUR edge/quality score — see CREDIBILITY vs CONVICTION above),
  "flagged":            boolean,
  "source_title":       string (the news headline this is based on),
  "highly_recommended": boolean
}}

Per the WATCH FLOOR, return at least 10 items (buys + shorts + watches) on a normal news day. Only
return an empty array if there is genuinely no relevant, fact-based market news at all — which is very rare."""

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

        # Enforce: no owned tickers, no vague guesses — fact-based new ideas only.
        cleaned = _filter_recommendations(deduped, open_positions)

        print(f"Claude returned {len(cleaned)} actionable recommendations")
        return cleaned

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