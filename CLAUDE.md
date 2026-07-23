# Argus Stock Advisor — Claude Context

## Working Rules (ALWAYS FOLLOW)
1. **Uncomfortable truth first.** Lead with the blunt assessment — no soft validation
   ("great question", "you're right to..."). If the user is wrong, say so directly,
   then give the correct answer and why. Disagree when warranted; don't hedge to please.
2. **Scalable, maintainable, readable code.** Follow professional engineering practice:
   clear names, single responsibility, no needless complexity, match existing patterns,
   guard edge cases, no dead/duplicated code. Leave the codebase better than found.
3. **Update `CLAUDE.md` + `TODO.md` every session/change** — see Documentation
   Maintenance below. Not done until docs reflect the change.

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
- Python, Streamlit, Flask, Claude API (model in `config.py` `CLAUDE_MODEL`, currently claude-sonnet-4-6)
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
analysis/scorecard.py         Closed-trade performance scorecard — payoff/profit-factor/expectancy,
                              concentration, let-run-vs-closed-early discipline leak, SPY benchmark
calculator/portfolio.py       Budget allocation with HR 2x weighting
ingestion/
  prices.py                   Live prices + 14d trend + technical indicators (RSI/MACD/SMA/52w/vol)
                              + ETF relative-strength rotation vs SPY (RRG) + market regime (SPY/VIX, R6)
                              + key price levels (support/resistance) anchoring watch entry triggers (R7)
  fundamentals.py             Fact-based company fundamentals (valuation/growth/margins) + next earnings date (R6) via yfinance
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
alerts/exit_checker.py        Stop/gain/time/event exit alerts — now session-aware (tags actionable_now)
alerts/entry_checker.py       "Buy when" entry alerts (R11) — fires when a watch trigger price is hit;
                              sources = today's recommendations + Argus chat's last suggestion
alerts/notifier.py            Gmail SMTP HTML alert email (exit + entry; subject/header adapt)
alerts/run_checks.py          Scheduled runner — market-hours gated, runs exit + entry, one email
storage/entry_watch.py        Persists PINNED watches + Argus chat's last buy/watch suggestions
                              + per-day notify record (entry_watch.json, gitignored)
argus.bat                     Launch Argus (reuses a running instance; opens one browser tab)
argus_silent.vbs              Launch Argus with NO terminal window (background/always-on).
                              Shortcut it into shell:startup to auto-run at login.
argus_stop.bat                Stop the background instance (kills whatever holds 8501/8502)
market_hours.py               Shared NYSE session logic (holidays/half-days/status) — used by dashboard
                              header badge, chatbot context, and exit_checker
config.py                     Shared constants (CLAUDE_MODEL) — single source of truth
backtest/exit_backtest.py     Exit-band backtester (target/stop % on real price paths) — validates
                              exit bands only; does NOT replay news/LLM (sampled entries)
main.py                       Pipeline orchestrator
pipeline_cache.json           Today's recommendations cache
budget.json                   DEPRECATED — no longer read/written. Budget is now live Robinhood
                              buying power (dashboard `_effective_budget`); the manual budget
                              number_input + save_budget/load_budget were removed (R9).
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
   crypto tickers (R4) it also gets CRYPTO MARKET DATA — see "Crypto conviction (R4)". Every run also
   includes MARKET REGIME (SPY/VIX), earnings-date flags, sector-concentration of owned positions, and
   the user's realized TRACK RECORD — see "Analyst risk/context improvements (R6)".
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

### Analyst risk/context improvements (R6)
Five additive, deterministic context streams (no new recommendation fields; output schema unchanged):
- **Market regime** (`ingestion/prices.py` `fetch_market_regime`): SPY vs 50/200-day SMA + golden/death
  cross + % from 52w high + RSI, and VIX level/bucket → an overall risk-on / neutral / risk-off read.
  MARKET REGIME prompt block tells the analyst to be defensive in risk-off, press catalysts in risk-on
  ("don't fight the tape"). Fetched once per run.
- **Earnings proximity** (`ingestion/fundamentals.py` `_next_earnings`): next earnings date +
  `days_to_earnings` per stock; flagged ⚠ in the FUNDAMENTALS block when within ~5 days (binary gap
  risk → don't open a fresh swing long right before a report unless the thesis IS the earnings).
- **ATR-based stops** (prompt, EXIT CONDITIONS): stops sized to ~1.5-2× the asset's avg daily range
  (ATR proxy already in the trend block), not arbitrary round numbers, so noise doesn't shake you out.
- **Concentration** (open-positions block): owned STOCK positions tagged with sector + a sector tally;
  analyst avoids piling new buys onto an already-heavy sector or stacking correlated (same-theme) buys.
- **Calibration** (`_summarize_track_record` from closed positions): realized win rate / avg P&L overall
  and by direction → YOUR REALIZED TRACK RECORD block; analyst calibrates to what has actually worked
  for the user (without overfitting a small sample). All guarded so any fetch failure degrades silently.

### Data-anchored entry triggers (R7)
Watch `entry_trigger` ("Buy when") used to be an LLM-eyeballed price — the worst unbacked output.
Now anchored to real price structure: `ingestion/prices.py` `_compute_key_levels` derives, per ticker,
nearest support/resistance (from recent + 52w highs/lows + SMA50/200), the ATR in dollars (avg daily
range), and two reference entries — `breakout_buy` (resistance + 0.5×ATR) and `pullback_buy` (support).
Surfaced in a KEY PRICE LEVELS prompt block; the analyst MUST anchor a watch's entry_trigger to one of
these computed levels (breakout vs pullback chosen per the catalyst), not invent a number. Formula owns
the level, Claude owns which one fits — context only, no schema change. (Full pipeline backtesting was
weighed and deferred: it needs point-in-time historical news + LLM replay, a research effort.)

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

### Budget Allocation — PYRAMID by risk tier (R8)
The long book is allocated as a **risk pyramid**: the risk tier picks the budget POOL,
conviction sizes WITHIN the pool. Most of the money sits in the medium-risk core.
- `PYRAMID_TIERS = {high: 0.20, medium: 0.55, low: 0.25}` — top/core/base fractions of
  the long budget. `_tier_of(rec)` maps `risk_level` → tier (unknown → medium/core).
  "Reward" is implicit: the analyst already scales exit targets with risk (HR 12-20%,
  regular 6-10%), so medium risk ≈ medium-high reward.
- Within a tier, `_pool_weight = (conviction/100) × HR_multiplier` (risk is NOT a factor
  here — the tier already handled it). `_allocate_pool` splits the pool by pool weight and
  applies the global single-name cap; excess from a capped name spills to uncapped names
  **in the same tier** only.
- **Empty tier → held as CASH, not redistributed** (design choice): on a day with no
  high-risk ideas, that 20% simply isn't deployed. So total invested < budget is normal
  and intentional (ballast). `print_allocation_table` shows a HELD AS CASH line.
- `HIGHLY_RECOMMENDED_MULTIPLIER = 2.0` — HR sizes you 2x WITHIN its pool (doesn't jump tiers).
- `MAX_SINGLE_ALLOCATION = 0.40` — global cap; no single name > 40% of budget (can bind
  inside the 55% core pool → the remainder is held as cash if no other core name absorbs it).
- `RISK_MULTIPLIERS` is now **shorts-only** (the shorts sleeve keeps the older
  conviction × risk × HR weighting via `_compute_weight`; the long book ignores it).
- `MAX_SHORT_EXPOSURE = 0.30` — total short exposure capped at 30% of budget.
- **Budget = live Robinhood buying power (R9), single source of truth.** The dashboard
  `_effective_budget()` returns live buying power (`_live_buying_power`, 60s TTL) or 0.0 when
  it can't be read; there is NO manual budget input anymore (removed to stop it disagreeing
  with real cash / confusing Argus chat). Chat context labels buying power as THE budget.
- `MIN_ALLOCATION_BUDGET = 10.0` — dollar allocation only runs when the budget (buying power)
  is ≥ $10; below that `calculate_allocations` returns every rec at $0 (analysis still shown).
- Sort order: HR buys → regular buys → shorts → watches
- Shorts (R1) are a **separate sleeve** (use margin, not the long cash budget) — pyramid
  applies to the long book only. Shorts are stocks-only, never highly_recommended.

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
- System prompt includes: current US MARKET STATUS (Eastern-time session from the shared
  `market_hours.market_session()`: open/pre-market/after-hours/weekend + NYSE holidays + half-day
  early closes; pandas holiday primitives, no extra dep, cached per year), live Robinhood BUYING POWER
  labeled as THE budget (R9 — no separate manual budget; `_live_buying_power()` read on every chat
  open), open positions with P&L, closed position stats, today's recommendations, watchlist
- Times advice to the session (CLOSED → "at the open"/limit order; thin pre/after-hours) and sizes
  every suggestion to the live buying power; crypto noted as 24/7. Tuned for the user's recurring
  "what moves should I make with my current buying power" question.
- Knows the **pyramid sizing** (R8): when advising buys it keeps the 20/55/25 shape — most fresh
  capital into medium-risk core ideas, only a small slice into high-risk shots, ≤40% in one name.
- Reinforces the **discipline leak** finding (scorecard): pushes the user to respect the exit plan
  and not hand-close at breakeven (a −1% to −3% wobble is not a stop).
- **Action-summary-first (R10):** for "what moves should I make / what should I do / review my
  portfolio" asks, Argus MUST open with a compact one-line-per-ticker action list
  (`Buy/Sell/Hold/Watch — TICKER, exit rule or reason`) covering every open position + any new buy,
  then an `Explanation:` block. Other questions stay 2-3 sentences, no list. `/chat` `max_tokens = 450`
  (room for the list). Goal: user can act without asking follow-ups.
- **Order-type awareness (R10):** the chat context tags each open position with its share count;
  a FRACTIONAL (<1 share) holding can only be exited by a MARKET order — Argus never advises a
  limit/stop-limit on it (Robinhood only allows limit/stop on whole-share positions).
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

## Dashboard Header & Alerts
- **Header badge** (under the title): live market-session badge (`market_hours.market_session()` →
  🟢/🟡/🔴 + timestamp) and live Robinhood buying power. Buying power IS the allocation budget (R9),
  shown read-only in the sidebar (`_effective_budget`). Read via `_live_buying_power()` with a 60s TTL
  cache (the sidebar "💵 Refresh buying power" button forces a refresh).
- **Session-aware exit alerts** (`alerts/exit_checker.py`): each alert is tagged `actionable_now`
  and `market_status`; when the market is closed/extended-hours the alert message appends a caveat
  ("act at the next open" / "extended-hours only, use a limit order") so the user never acts on an
  unexecutable signal.
- **Entry ("buy when") alerts (R11)** (`alerts/entry_checker.py`): the bullish counterpart, so a
  watch idea can't pass its trigger unnoticed. Candidates come from **three sources**, deduped by
  ticker with priority **pinned > chat > recommendation** (`_dedupe_by_ticker`) so one ticker never
  double-alerts:
  1. **Pinned watches** — the 👁 **Watch this trigger** button on a watch rec's expander copies its
     `entry_trigger` into `storage/entry_watch.py`. This is the ONLY source that survives a pipeline
     rerun; the level is stored exactly as pinned and is NOT refreshed if the ticker reappears later.
  2. **Argus chat's last suggestion** — the `/chat` proxy parses `Buy —`/`Watch — TICKER` action
     lines via `_capture_chat_suggestions`; each capture replaces the prior set.
  3. **Today's recommendations** — watch recs in `pipeline_cache.json` (their R7 `entry_trigger`).
     **Ephemeral:** `save_cache()` overwrites the cache each run, so an unpinned watch silently
     disappears on the next pipeline run — that's exactly what pinning solves.
  `_parse_triggers` extracts EVERY price + direction from a trigger string — a two-sided trigger
  ("pulls back to $314.91 ... or breaks above $328.04") yields both a `below` and an `above`
  condition, either of which fires. Owned tickers are skipped; one alert per ticker/source per day;
  session-aware like exits. Pure price math, no LLM tokens.
- **Email** (`alerts/notifier.py`): Gmail SMTP (`ALERT_EMAIL_*` in `.env`), HTML table. Subject and
  header adapt to what fired (exit-only / buy-trigger-only / mixed); `entry_trigger` renders as a
  teal "🎯 Buy Trigger" row. Verified working.
- **Runner** (`alerts/run_checks.py`): market-hours gated; runs exit checks then entry checks and
  sends ONE combined email. The entry block is separately guarded so an entry failure can't lose
  exit alerts. Crashes email themselves.

## Dashboard Tabs
1. **Today's Recommendations** — allocation table with HR gold highlighting, stock detail expanders, add to positions
2. **Portfolio** — invested money, P&L trend graph (yfinance), position breakdown
3. **My Positions** — open/closed positions, manual entry, price updates, snooze alerts. Open-positions
   table + each expander show a computed **Stop @** price (`_stop_loss_price` parses "stop loss at X%"
   from exit_condition × reference price; long = ref×(1−X), short = ref×(1+X)). Closed-positions header
   shows the **performance scorecard** (`analysis/scorecard.py`): Net $/win rate/profit factor/payoff/
   expectancy, a red **discipline-leak** callout when trades let-run-to-band avg ≫ trades closed-early
   (the core finding: hand-closing at breakeven flattens P&L, not the picks), a concentration warning
   when one trade is ≥50% of gross profit, and a SPY opportunity-cost line
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
