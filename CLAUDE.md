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
  prices.py                   Live price fetching (Robinhood primary, yfinance fallback)
  robinhood.py                Robinhood sync and news ingestion
  finnhub.py                  Finnhub news ingestion
  rss.py                      RSS feed ingestion
  sec.py                      SEC EDGAR filings
  coingecko.py                Crypto context
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
3. Top 25 deduplicated stories sent to Claude, along with the user's OPEN POSITIONS
   and a PAST TRADE OUTCOMES block (closed positions w/ realized P&L) for reflection
4. Claude returns up to 20 recommendations. Per candidate it argues a `bull_case`
   vs `bear_case` (the bear case is the gate for keeping a 'buy'), runs a portfolio
   risk gate over the whole basket (concentration/correlation), and sets
   `highly_recommended`
5. `calculator/portfolio.py` allocates budget (HR signals get 2x weight)
6. Results cached to `pipeline_cache.json`

Analyst design ideas adapted from TauricResearch/TradingAgents (multi-agent
trading framework), kept within Argus's single-call model: bull/bear debate,
reflection from the user's own closed-trade outcomes, and a portfolio-level
risk review.

### Source Confidence Weights
```python
SOURCE_WEIGHTS = {
    "sec": 1.0,
    "finnhub_company": 0.68,
    "finnhub_etf": 0.68,  
    "finnhub_general": 0.68,
    "robinhood_news": 0.65,
    "finnhub_crypto": 0.5,
    "rss": 0.5,
    "etf_rss": 0.5,
    "crypto_rss": 0.45,
    "reddit_rss": 0.15,
}
```

### Highly Recommended Criteria (all 4 must be met)
1. Catalyst is unambiguous AND recent (~last 1-2 trading days; earnings beat, M&A, FDA approval, major contract)
2. Confidence score >= 0.68 (source credibility — NOT a measure of trade edge)
3. Edge still open — price has NOT already fully reflected the catalyst (not pinned at 14-day high on this same news, not a buyout target trading at offer price)
4. Price trend supports entry (not in a sharp downtrend unless a genuine reversal catalyst)

The analyst prompt is built to avoid buying already-priced-in moves: it treats
`confidence_score` as source credibility (not edge), runs a catalyst-timing /
"buy the rumor sell the news" check against the 14-day trend, handles M&A
target-vs-acquirer mechanics (announced cash-deal targets → `watch`, closed deals →
skip), and prefers `watch`/empty over forced buys on weak days.

### Budget Allocation
- `HIGHLY_RECOMMENDED_MULTIPLIER = 2.0` — HR buys get 2x capital weight
- `MAX_SINGLE_ALLOCATION = 0.40` — no single stock gets more than 40%
- Sort order: HR buys → regular buys → watches

### Chatbot (Argus Assistant)
- Injected directly into Streamlit parent DOM (bypasses iframe positioning issues)
- Flask proxy on port 8502 keeps API key server-side
- `/context` endpoint builds live portfolio snapshot on every chat open
- System prompt includes: budget, open positions with P&L, closed position stats, today's recommendations, watchlist
- Gives direct actionable advice; honest about weak signal days
- Same anti-priced-in discipline as the analyst: catalyst-timing check ("buy the
  rumor, sell the news"), M&A target-vs-acquirer mechanics, `confidence_score` =
  source credibility (not edge); prefers watch over chasing moves that already ran

## Claude Analysis JSON Schema
Each recommendation must have:
```json
{
  "ticker": "string or null",
  "company_name": "string",
  "asset_type": "stock|etf|crypto",
  "direction": "buy|watch|avoid",
  "entry_rationale": "string (max 2 sentences)",
  "bull_case": "string (strongest reason it works)",
  "bear_case": "string (strongest reason it loses money — the gate for keeping a 'buy')",
  "exit_condition": "string (e.g. 'target 12% gain, stop loss at 5%')",
  "catalyst_timing": "string — when the catalyst is expected to play out (e.g. 'Earnings Jul 15', 'Merger ~Q3 2026'); honest horizon estimate if no date is in the news",
  "risk_level": "low|medium|high",
  "confidence_score": "number (passed through from scorer)",
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
