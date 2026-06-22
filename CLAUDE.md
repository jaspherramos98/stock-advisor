# Argus Stock Advisor — Claude Context

## Project Overview
Personal AI-powered stock advisor named Argus. Runs locally on Windows via Streamlit.
- **Run command:** `streamlit run dashboard/app.py`
- **Local URL:** http://localhost:8501
- **Chatbot proxy:** Flask server on port 8502
- **GitHub:** https://github.com/jaspherramos98/stock-advisor
- **Local path:** D:\CS\Projects\stock-advisor\

## Documentation Maintenance (ALWAYS FOLLOW)
Keep `CLAUDE.md` and `TODO.md` current as part of every change — treat docs as
part of "done," not an afterthought.
- **CLAUDE.md** — update whenever a change alters anything described here:
  architecture/pipeline flow, key files, source weights, JSON schema, env keys,
  ports, commands, dashboard tabs, or known issues/constraints. If the code and
  CLAUDE.md disagree, fix CLAUDE.md in the same change.
- **TODO.md** — when you finish a backlog item, move it to `## Done` with a short
  summary of what changed and which files. When new work/follow-ups surface, add
  them to `## Backlog`.
- A change is not complete until these two files reflect it. Mention doc updates
  (or explicitly note "no doc change needed") in your summary.

## Tech Stack
- Python, Streamlit, Flask, Claude API (claude-sonnet-4-5)
- robin_stocks (unofficial Robinhood API)
- Finnhub API, SEC EDGAR, Reddit RSS, CoinGecko
- Google Sheets API for history export
- yfinance for portfolio trend graph

## Environment
- Windows, VS Code, virtualenv at `venv/`
- Dependencies in `requirements.txt`
- Secrets in `.env` (never commit)
- Mock mode: set `MOCK_MODE=true` and `MOCK_INGESTION=true` in `.env`

## Key Files
```
dashboard/app.py              Main Streamlit app + Flask proxy + chatbot
analysis/claude_analyst.py    Claude analysis prompt and JSON schema
calculator/portfolio.py       Budget allocation with HR 2x weighting
ingestion/
  prices.py                   Live prices + 14d trend + technical indicators (RSI/MACD/SMA/52w/vol)
                              + ETF relative-strength rotation vs SPY (RRG: RS-Ratio/RS-Momentum/quadrant)
  fundamentals.py             Fact-based company fundamentals (valuation/growth/margins) via yfinance
  etf_facts.py                Fact-based ETF facts (category/AUM/expense/yield/top holdings/sectors) via yfinance
  robinhood.py                Robinhood sync and news ingestion
  finnhub.py                  Finnhub news ingestion
  rss.py                      RSS feed ingestion
  sec.py                      SEC EDGAR 8-K filings (item codes → plain English + ticker + high-signal flag)
  coingecko.py                Crypto context (what each coin is) + market data (cap/rank/momentum/volume/ATH, R4)
  reddit.py                   Reddit RSS
validation/scorer.py          News scoring by source credibility
storage/
  positions.py                Open/closed position tracking
  watchlist.py                Ticker watchlist
  sheets.py                   Google Sheets export/read
alerts/snooze.py              Alert snooze/dismiss logic
main.py                       Pipeline orchestrator
pipeline_cache.json           Today's recommendations cache
budget.json                   User's current budget setting
```

## Architecture

### Pipeline Flow
1. `main.py` runs parallel ingestion via `ThreadPoolExecutor` (max_workers=5)
2. `validation/scorer.py` scores each item by source weight
3. Top 25 deduplicated stories sent to Claude, plus per-ticker TECHNICAL INDICATORS
   (RSI/MACD/SMA50-200/52w/volume from ~1y of prices) and FUNDAMENTALS (valuation,
   growth, margins, debt, FCF) as confirmation/quality context, and the user's OPEN
   POSITIONS to exclude. Technicals/fundamentals are context the analyst reasons over —
   they confirm or temper a news catalyst, they don't gate or invent one. For ETF
   tickers (R3) the analyst additionally gets ETF RELATIVE STRENGTH (rotation vs SPY)
   and ETF FACTS instead of company fundamentals — see "ETF rotation (R3)" below. For
   crypto tickers (R4) it also gets CRYPTO MARKET DATA — see "Crypto conviction (R4)".
4. Claude returns recommendations with `highly_recommended` field. **Watch floor:** on a
   normal news day it always returns ≥10 items (buys + shorts + watches) so the user sees a
   full read on the day; BUYS stay strict/few (usually 0-3, never padded), the rest are
   watches with concrete triggers. Empty array only if there's genuinely no relevant news.
4b. `_filter_recommendations()` enforces deterministically: drops any owned ticker, any
   rec with no ticker, and any vague/placeholder exit ("N/A", "watching for deal clarity",
   "await details"). Only NEW, fact-based ideas survive. The prompt also instructs this,
   but the code filter is the guarantee.
5. `calculator/portfolio.py` allocates budget (HR signals get 2x weight)
6. Results cached to `pipeline_cache.json`

### Source Confidence Weights
```python
SOURCE_WEIGHTS = {
    "sec": 1.0,
    "finnhub_company": 0.7,
    "finnhub_etf": 0.7,
    "robinhood_news": 0.65,
    "finnhub_general": 0.6,
    "finnhub_crypto": 0.5,
    "rss": 0.5,
    "etf_rss": 0.5,
    "crypto_rss": 0.45,
    "reddit_rss": 0.15,
}
```
Plus a +0.08 recency bonus for items < 6h old. Thresholds: score ≥ 0.6 → HIGH
(sent to Claude), ≥ 0.35 → MEDIUM (sent, flagged), below → LOW (discarded).

### Conviction vs Confidence (R2)
Two separate numbers per recommendation:
- **`confidence_score`** (0-1, set by scorer) = SOURCE CREDIBILITY (trust the report). Unchanged;
  still the dedup/scoring input.
- **`conviction`** (0-100, set by the analyst) = the EDGE (how strong/timely/un-priced-in the trade
  is). Scored *relative to the asset's own class* (so crypto/ETF can be high-conviction despite capped
  credibility). **Conviction drives position size** (`portfolio._compute_weight`) and the HR gate.
  Back-compat: recs without `conviction` fall back to `confidence_score × 100`.

### ETF rotation (R3)
ETFs are macro/thematic baskets, not single-catalyst trades, so for ETF tickers the analyst
gets two ETF-specific context blocks instead of (meaningless) company fundamentals:
- **ETF RELATIVE STRENGTH vs SPY** — a simplified JdK Relative Rotation Graph (RRG) computed in
  `ingestion/prices.py` (`_compute_rrg` / `fetch_etf_relative_strength`) from ~1y of yfinance
  history aligned to SPY. Outputs `rs_ratio` (>100 = outperforming the market trend),
  `rs_momentum` (>100 = that outperformance is accelerating), a `quadrant`
  (Leading → Weakening → Lagging → Improving), and `rel_perf_63d` (plain % vs SPY over ~3mo).
  The analyst favors `Leading`, avoids `Lagging`; a leading ETF on a real theme can be
  high-conviction even with no single news catalyst. Pure deterministic math (unit-tested).
- **ETF FACTS** — `ingestion/etf_facts.py` (`fetch_etf_facts`): category, sponsor, AUM, expense
  ratio, yield, top holdings, sector weights (top holdings/sectors via yfinance `funds_data`,
  wrapped so a missing API never costs the rest). Units normalized (`yield` is decimal→×100;
  expense ratio is already-percent on most versions; ytdReturn omitted as unreliable).

These are CONTEXT only — R3 added no new recommendation fields; the output schema is unchanged.
In `run_analysis`, news tickers are classified stock/etf/crypto: stocks get fundamentals, ETFs
get rotation+facts, both get technicals/price history, crypto keeps its own path.

### Crypto conviction (R4)
Crypto rides on R2's conviction/credibility split: crypto sources never reach SEC-level credibility,
so a capped `confidence_score` must NOT cap `conviction` — crypto ideas are scored RELATIVE TO CRYPTO.
To give the analyst fact-based crypto inputs (the analog of fundamentals / ETF facts):
- **CRYPTO MARKET DATA** — `ingestion/coingecko.py` (`fetch_coin_market_data`, `_extract_market_data`):
  one batched `/coins/markets` call → price, market cap + rank, 24h/7d/30d momentum, 24h volume,
  % from all-time high. Used like technicals (don't chase a coin already run-up or near ATH; oversold
  pullback in an uptrend = better entry). The existing `fetch_crypto_context` (what the coin IS) stays.
- A **CRYPTO prompt section** tells the analyst: take the genuinely high-credibility crypto catalysts
  seriously (spot-ETF approvals / SEC filings = 1.0, major exchange listings, shipped protocol upgrades,
  verifiable on-chain shifts, multi-source corroboration); require corroboration before high conviction
  from a lone low-credibility source; crypto is long/watch only (never short — shorts are stocks-only).
Context only — R4 added no new recommendation fields; output schema unchanged.

### Highly Recommended Criteria (all 4 must be met)
1. Catalyst is unambiguous AND recent (~last 1-2 trading days; earnings beat, M&A, FDA approval, major contract)
2. Conviction >= 75 (strong, un-priced-in edge) AND confidence_score >= 0.5 (credible source floor)
3. Edge still open — price has NOT already fully reflected the catalyst (not pinned at 14-day high on this same news, not a buyout target trading at offer price)
4. Price trend supports entry (not in a sharp downtrend unless a genuine reversal catalyst)

The analyst prompt is built to avoid buying already-priced-in moves: it treats
`confidence_score` as source credibility (not edge), runs a catalyst-timing /
"buy the rumor sell the news" check against the 14-day trend, handles M&A
target-vs-acquirer mechanics (announced cash-deal targets → `watch`, closed deals →
skip), and prefers `watch`/empty over forced buys on weak days.

### Budget Allocation
- Weight = `(conviction/100) × risk_multiplier × HR_multiplier` — sized by CONVICTION (edge),
  not credibility (R2). Back-compat: missing conviction falls back to `confidence_score × 100`.
- `HIGHLY_RECOMMENDED_MULTIPLIER = 2.0` — HR buys get 2x capital weight
- `MAX_SINGLE_ALLOCATION = 0.40` — no single stock gets more than 40%
- `MAX_SHORT_EXPOSURE = 0.30` — total short exposure capped at 30% of budget
- Sort order: HR buys → regular buys → shorts → watches
- Shorts (R1) are a **separate sleeve** (use margin, not the long cash budget) — buy
  allocation logic is untouched. Shorts are stocks-only, never highly_recommended.

### Shorts (R1)
- Analyst emits `direction: "short"` for unambiguous, recent, fact-based BEARISH
  catalysts (earnings miss + weak guidance, FDA rejection, fraud, dilution, death cross
  + weak fundamentals). Same priced-in check in reverse; hard squeeze-guard (never short
  heavily-shorted/low-float/squeeze setups). Stocks only — never crypto/ETFs.
- `exit_condition` uses the same "target X% gain, stop loss at Y%" wording; for a short,
  "gain" = price falling in your favor, "stop loss" = it rising against you.
- P&L inverts everywhere: `close_position` realized P&L, `exit_checker` (negates
  change_pct so the gain/stop parser works), and the dashboard live P&L for short
  positions. Portfolio money-graph excludes shorts (long-only value math).

### Chatbot (Argus Assistant)
- Injected directly into Streamlit parent DOM (bypasses iframe positioning issues)
- Assistant replies are HTML-escaped BEFORE the **bold**/newline formatting is applied,
  so model output (which can carry prompt-injected content from news/Reddit) can't inject
  active markup into the DOM (XSS guard). User messages render via `textContent`.
- Flask proxy on port 8502 keeps API key server-side; bound to 127.0.0.1, debug=False,
  CORS locked to localhost:8501
- `/context` endpoint builds live portfolio snapshot on every chat open
- System prompt includes: current US MARKET STATUS (Eastern-time session via `_market_status()`:
  open/pre-market/after-hours/weekend; ignores holidays), live Robinhood BUYING POWER
  (`fetch_buying_power()` read on every chat open — not the sidebar sync button), the budget
  setting, open positions with P&L, closed position stats, today's recommendations, watchlist
- Times advice to the session (CLOSED → "at the open"/limit order; thin pre/after-hours) and sizes
  every suggestion to the live buying power; crypto noted as 24/7. Tuned for the user's recurring
  "what moves should I make with my current buying power" question.
- Gives direct actionable advice; honest about weak signal days
- Same anti-priced-in discipline as the analyst: catalyst-timing check ("buy the
  rumor, sell the news"), M&A target-vs-acquirer mechanics, `confidence_score` =
  source credibility (not edge); prefers watch over chasing moves that already ran
- Aligned with the analyst's watch-floor + shorts (R1): knows the list always
  includes watches by design and walks the user through them on weak days instead
  of dismissing; understands `short` ideas (bearish, stocks-only, invert P&L)

## Claude Analysis JSON Schema
Each recommendation must have:
```json
{
  "ticker": "string or null",
  "company_name": "string",
  "asset_type": "stock|etf|crypto",
  "direction": "buy|short|watch|avoid",
  "entry_rationale": "string (max 2 sentences)",
  "entry_trigger": "string (watch: the buy condition/price that makes it actionable; buy/short: 'now')",
  "exit_condition": "string (e.g. 'target 12% gain, stop loss at 5%')",
  "risk_level": "low|medium|high",
  "confidence_score": "number (source credibility, passed through from scorer)",
  "conviction": "number 0-100 (analyst's edge score — drives sizing + HR; R2)",
  "flagged": "boolean",
  "source_title": "string",
  "highly_recommended": "boolean"
}
```

## Exit Targets by Signal Type
- Highly recommended: gain targets 12-20%, stops 4-6%
- Regular buy: gain targets 6-10%, stops 2-4%
- Upside must be at least 2x the stop distance

## Dashboard Tabs
1. **Today's Recommendations** — allocation table with HR gold highlighting, stock detail expanders, add to positions
2. **Portfolio** — invested money, P&L trend graph (yfinance), position breakdown
3. **My Positions** — open/closed positions, manual entry, price updates, snooze alerts
4. **Watch List** — Finnhub ticker watchlist editor per asset type
5. **History** — Google Sheets export history with charts

## Known Issues / Constraints
- `robin_stocks` is unofficial — if Robinhood changes their app it may break; only edit `ingestion/robinhood.py`
- Flask proxy must be on port 8502; guard against multiple threads with `st.session_state.proxy_started`
- Streamlit rerenders entire script on every interaction — all expensive operations should be cached
- Chatbot DOM injection uses `(function() { if already injected, return; })()` guard to prevent duplicates
- Pipeline cache date-checks against today — stale cache from yesterday is ignored, backup cache used if main fails mid-run
- **Console encoding (caused "0 recommendations"):** pipeline `print()`s contain non-ASCII
  symbols (→, —, ⭐, ⚠, ✓). On a Windows cp1252 console these raise `UnicodeEncodeError`
  and crash the pipeline mid-run (e.g. the dedup log in `claude_analyst.py`), before Claude
  is called → empty result. Guards in place: `argus.bat` sets `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`,
  and `main.py` + `dashboard/app.py` reconfigure stdout/stderr to UTF-8 (errors="replace") at
  startup. Keep all three; don't add bare non-ASCII to prints without them.

## .env Keys Required
```
ANTHROPIC_API_KEY=
FINNHUB_API_KEY=
GOOGLE_SHEET_ID=
GOOGLE_CREDENTIALS_FILE=google_credentials.json
ALERT_EMAIL_SENDER=
ALERT_EMAIL_PASSWORD=
ALERT_EMAIL_RECEIVER=
REDDIT_USER_AGENT=stock-advisor-bot/1.0
ROBINHOOD_USERNAME=
ROBINHOOD_PASSWORD=
MOCK_MODE=false
MOCK_INGESTION=false
```

## Common Commands
```bash
# Run the app
streamlit run dashboard/app.py

# Run in mock mode (no API calls)
# Set MOCK_MODE=true and MOCK_INGESTION=true in .env first

# Install dependencies
pip install -r requirements.txt

# Activate venv (Windows)
venv\Scripts\activate
```
