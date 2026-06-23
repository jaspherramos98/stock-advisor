# Argus — To Do

## Done

### 13. CI pipeline + first committed test suite ✅
First real automated tests (was zero). `tests/test_deterministic.py` — 19 pytest cases over the
pure formula/data layer (technicals, RRG, key levels, fund/ETF/crypto extractors + unit normalization,
NYSE market-session/holidays, portfolio allocation + budget floor, track-record + filter guards). No
network/secrets/LLM — only the parts that CAN be validated. `pytest.ini` sets `pythonpath=.`.
`.github/workflows/ci.yml` runs on every push + PRs to main: setup-python 3.12 → install a minimal
explicit dep set (pandas/anthropic/requests/python-dotenv/pytest — NOT the UTF-16 requirements.txt,
which breaks pip on Linux) → `compileall` (catches syntax errors in dashboard/app.py which pytest
can't import) → `pytest`. Also re-saved `requirements.txt` as UTF-8 (was UTF-16, broke `pip -r` on
Linux/clean installs); CI still uses the minimal explicit set for speed.

### 1. Fix Finnhub depletion — switch prices to Robinhood ✅
`fetch_prices()` now uses Robinhood as the primary quote source (no free-tier
cap, prices match the trading platform exactly) and falls back to Yahoo Finance
for anything Robinhood can't resolve (most crypto, occasional ETFs).
- `ingestion/robinhood.py` — added `fetch_quotes()`; all robin_stocks access
  stays isolated here. Computes change/% from last trade vs previous close.
- `ingestion/prices.py` — `fetch_prices()` rewritten: Robinhood first, yfinance
  fallback via new `_yfinance_quote()`. Finnhub client removed. `CRYPTO_YAHOO_MAP`
  promoted to module level and shared with `fetch_price_history()`.
- Return shape unchanged, so all consumers (Tabs 1/2/3, chatbot context,
  exit_checker) work without edits. Robinhood quotes don't expose intraday
  high/low, so those return 0.0 (not consumed by the app).

### 2. Less invasive first-time / empty state screen ✅
The blocking "Get started" `else:` screen is gone. The dashboard always renders
the tabs and sidebar on launch (`dashboard/app.py`). When there are no
recommendations yet, a dismissible welcome banner appears above the tabs
("No recommendations yet — run the pipeline...") with an ✕ to dismiss
(`welcome_dismissed` in session state). Tab 1 shows a friendly info message
instead of forcing a pipeline run, so My Positions / Watch List / History are
reachable immediately without burning tokens.

### 3. Sync budget to Robinhood buying power ✅
New "💰 Sync budget to buying power" button in the sidebar pulls real
available cash from Robinhood and sets it as the investment budget.
- `ingestion/robinhood.py` — added `fetch_buying_power()` (reads
  `buying_power`/`cash_available_for_withdrawal`/`cash` from the account
  profile; all robin_stocks access stays isolated here).
- `dashboard/app.py` — sidebar button; guards against a sub-$10 (e.g. $0,
  fully invested) value crashing `st.number_input` via new `MIN_BUDGET`
  constant. `load_budget()` clamps to `MIN_BUDGET`.

### 4. Editable exit strategy for synced positions ✅
Robinhood-synced positions now get a default "target 10% gain, stop loss at
5%" exit instead of a placeholder string, and can be edited in the UI.
- `storage/positions.py` — added `update_exit_condition()`.
- `dashboard/app.py` — exit-strategy text input + save button under
  My Positions; synced positions seed a real default exit condition.

### 5. Fix chatbot listeners dying on Streamlit rerun ✅
The floating Argus chat used to stop responding to clicks until a page
refresh, because Streamlit tears down and recreates the component iframe on
every interaction, killing the old event listeners.
- `dashboard/app.py` — chat state (`open`/`history`/`context`) moved onto
  `window.parent.argusState` so it survives reruns; widget DOM built once;
  listeners re-bound every run via `cloneNode` + `replaceChild`. Also
  reworded both system prompts (chat + analyst) to a profit-driven
  investment-banker persona.

### 6. Reassess analyst prompt to stop chasing priced-in moves ✅
Reworked the system prompt in `analysis/claude_analyst.py` to protect capital,
not just chase catalysts. Changes:
- Persona reframed: keeping the job means NOT losing money; prefer `watch` over
  `buy` when in doubt; capital preserved = capital for the next real setup.
- New "CATALYST TIMING" section — the #1 loss driver is buying news already in
  the price. Cross-checks each buy against the 14-day trend; if the move already
  happened (at/near 14d high on this same news, or old news), it's a `watch`.
- New "M&A / BUYOUTS" section — distinguishes target vs acquirer; announced
  all-cash targets trade at the offer price (arb spread only) → `watch`, never
  highly_recommended; closed deals → skip; flags regulatory/financing risk.
- Clarified `confidence_score` = SOURCE CREDIBILITY, not trade edge.
- Earnings "beat" is not automatically bullish (guidance / already expected).
- Removed the "always find 5-10 opportunities" quota that forced buys; mostly
  `watch` or empty array on weak days is now explicitly correct.
- HIGHLY RECOMMENDED upgraded from 3 → 4 conditions: catalyst must be recent,
  edge must still be open (not priced in), plus the prior trend/confidence gates.
- Applied the same discipline to the Argus chatbot prompt (`dashboard/app.py`):
  catalyst-timing check, M&A mechanics, confidence-is-not-edge, prefer watch.
- `CLAUDE.md` HR criteria + analyst/chatbot notes updated to match.

### 7. Fix "0 recommendations" — UnicodeEncodeError crash on cp1252 console ✅
The real cause of the persistent empty pipeline runs (NOT the analyst logic). The dedup
log in `analysis/claude_analyst.py` prints a "→" character; on a Windows cp1252 console
(what `argus.bat`/cmd.exe uses) that raises `UnicodeEncodeError` and crashes the pipeline
BEFORE Claude is called → the app shows 0. Proven: same code returns 10+ recs with UTF-8
output, crashes to 0 on cp1252. Latent bug, surfaced by the launch console's code page.
- `argus.bat` — sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` before launching streamlit.
- `main.py` + `dashboard/app.py` — reconfigure stdout/stderr to UTF-8 (errors="replace")
  at startup so no print can ever crash the run (covers non-argus.bat launches too).
- Verified: 3 runs on the default cp1252 console returned 10/8/9 recs, no crash.
- `CLAUDE.md` Known Issues updated. Pure bug fix — no analyst/feature behavior changed.

### 8. Exclude owned tickers + drop guesses (fact-checked recs only) ✅
After the crash fix, runs surfaced owned tickers (ISBA/PAYO/ROKU) as 'watch' and
contentless SEC 8-Ks as vague guesses ("N/A - watching for deal clarity"). Per
request: only NEW, fact-based, informed ideas. Changes in `analysis/claude_analyst.py`:
- Prompt: EXCLUDE owned tickers entirely (not even 'watch'); recommend only when the
  news has concrete verifiable detail; never guess on bare "8-K filed" items; banned
  placeholder exits ("N/A", "watching for deal clarity", "await details").
- Open-positions block reworded to "EXCLUDE THESE".
- New `_filter_recommendations()` — deterministic guard run after Claude: drops any
  owned ticker, any rec with no ticker, and any vague/placeholder exit_condition.
  Unit-tested with synthetic data (no API): drops owned+vague, keeps real ideas.
- Prefer surfacing the best fact-based NEW ideas over an empty list (empty reserved
  for genuinely nothing credible+actionable).
- `CLAUDE.md` updated. Verified by unit test + mock-mode boot (no live tokens spent).

### 9. Fact-based analyst inputs — technicals + fundamentals ✅
Added two deterministic, fact-based context streams for the analyst (adapted from
TradingAgents' analyst roles, but as CONTEXT the model reasons over — never hard
gates that drop output, and no social-sentiment guesswork).
- **Technicals** (`ingestion/prices.py`): `fetch_price_history()` now pulls ~1y and
  computes RSI(14, Wilder), MACD(12/26/9) state + crossover, SMA50/200 + price-vs-MA
  + golden/death cross, 52-week range, and volume-vs-30d-avg via a unit-tested
  `_compute_technicals()` helper. New "TECHNICAL INDICATORS" prompt block with timing
  guidance (RSI>70 don't chase, MACD/SMA confirm entry, etc.).
- **Fundamentals** (`ingestion/fundamentals.py`, new): `fetch_fundamentals()` pulls
  valuation (P/E, P/B), growth, margins, debt/equity, FCF, market cap, sector from
  yfinance `.info`; cached per process; None-safe. Fetched for stock news tickers in
  `run_analysis`; new "FUNDAMENTALS" prompt block as a quality check.
- Verified with synthetic unit tests (uptrend→RSI100/golden cross, downtrend→RSI0/
  death cross, short series→None; fundamentals parse + None handling) — zero API tokens.
- `CLAUDE.md` key files + pipeline flow updated.

### 10. SEC 8-K enrichment (re-applied) ✅
Re-applied the SEC enrichment (previously reverted with the TradingAgents rollback).
Bare "Company — keyword / Form 8-K filed" headlines gave the analyst nothing to act on;
now each filing carries real, fact-checked content.
- `ingestion/sec.py` — `fetch_sec_filings()` translates 8-K `items` codes to plain English
  via `ITEM_DESCRIPTIONS` (e.g. 2.01 → "Completion of Acquisition (M&A CLOSED)", 2.02 →
  "EARNINGS release"), extracts the real ticker from `display_names`, flags `high_signal`
  items (earnings/M&A/exec/bankruptcy/etc), dedupes across keywords by accession number.
- `analysis/claude_analyst.py` — news list shows a "⭐ high-signal filing" hint; summary
  truncation raised 100→200 to keep itemized detail.
- Directly strengthens M&A-timing: `2.01 Completion of Acquisition` is now a structured
  "deal already closed" signal, not just an inference from price.
- Verified: helper unit tests (no network) + one free live SEC fetch (no LLM tokens) —
  enriched titles + tickers + high-signal flags confirmed. `CLAUDE.md` updated.

### 11. Market-hours & buying-power awareness (chatbot + dashboard + alerts) ✅
Shared `market_hours.py` (NYSE session: regular/pre/after/weekend + holidays + half-day early
closes; pandas holiday primitives, no new dep, cached per year; `market_session()` returns
status/is_open/badge/line/action_note and degrades gracefully). Wired into four places:
- **Chatbot** — context already carried live market status + live Robinhood buying power (read on
  every chat open) and prompt guidance to size to buying power + time to the session.
- **Dashboard header badge** — 🟢/🟡/🔴 session badge + timestamp and a live buying-power readout.
- **Budget guardrail** — sidebar warns when the allocation budget exceeds real buying power.
- **Session-aware exit alerts** — `exit_checker` tags each alert `actionable_now`/`market_status`
  and appends an "act at next open / extended-hours only" caveat when the market isn't open.
- **TTL cache** — `_live_buying_power()` caches Robinhood buying power for 60s (sidebar Sync forces
  refresh) so the badge + chatbot don't re-hit Robinhood on every rerun.
Verified: `market_session` unit checks across regular/pre/after/weekend/holiday/half-day, exit-alert
annotation (open vs closed vs after-hours), headless mock-mode dashboard boot, py_compile. CLAUDE.md updated.

### 12. Analyst trading-logic improvements (R6) ✅
Five additive, deterministic context streams (no new recommendation fields; schema unchanged), all
guarded so a fetch failure degrades silently:
1. **Market regime** — `prices.fetch_market_regime()` (SPY vs 50/200-SMA + cross + %-from-52w-high +
   RSI, VIX level/bucket → risk-on/neutral/risk-off). MARKET REGIME prompt block: don't fight the tape.
2. **Earnings proximity** — `fundamentals._next_earnings()` adds next earnings date + days; ⚠ flag in
   FUNDAMENTALS when within ~5 days (binary gap risk → don't open a fresh swing long right before a report).
3. **ATR-based stops** — EXIT CONDITIONS prompt now sizes stops to ~1.5-2× avg daily range (already in
   the trend block), not arbitrary round numbers.
4. **Concentration** — owned STOCK positions tagged with sector + a sector tally; analyst avoids piling
   new buys onto a heavy sector or stacking correlated (same-theme) buys.
5. **Calibration** — `_summarize_track_record()` from closed positions (win rate / avg P&L overall + by
   direction) → YOUR REALIZED TRACK RECORD block; calibrate to what has worked without overfitting.
Verified: track-record + prompt-block render unit tests, free live SPY/VIX regime + AAPL earnings-date
fetch (no LLM tokens), mock boot. `CLAUDE.md` updated.

---

### 14. Exit-band backtester (the tractable slice of validation) ✅
`backtest/exit_backtest.py` — `simulate_trade()` walks forward on real OHLC from an entry and records
target-hit / stop-hit / time-exit + P&L (conservative: stop wins a same-bar tie; no lookahead);
`backtest_exit_bands()` samples entries every N days over ~2y yfinance history and aggregates win
rate / avg P&L / outcome mix per band. Validates the (previously arbitrary) target/stop %s.
**Scope (honest):** entries are SAMPLED, not Argus news signals, and the LLM is NOT replayed — so it
measures whether a stop/target band is sane vs alternatives, NOT whether Argus is profitable. Full
point-in-time news + LLM replay remains deferred (research effort). First real finding: tight stops
(3% / 1.5%) get whipsawed on higher-vol names — confirms the R6 ATR-stop rationale with data. 6 unit
tests added (target/stop/time/short/same-bar-tie/summarize). No LLM tokens; free price data.

## Roadmap (sequenced — from session brainstorm)
Dependency-ordered so we don't build something we have to tear up. Discipline for
every phase: additive CONTEXT, never hard gates that drop output; verify with unit
tests + mock-mode boot (no token-wasting live runs); update CLAUDE.md + TODO as part
of "done". R3 and R4 depend only on R2 and can swap/parallelize.

### R1. Shorts (stocks) — express bearish theses  ✅ DONE — merged to main (tag `stable-post-r1`)
Shipped together with R1: the **watch floor** (analyst always returns ~10+ items so the
user sees the full read; buys stay strict/few, rest are watches) and **chatbot alignment**
(knows the list includes watches + shorts; walks the user through watches on weak days).
Implemented: `short` direction (schema/prompt) with bearish-catalyst rules, squeeze
guard, stocks-only, never highly_recommended; `portfolio.py` short sleeve capped at
`MAX_SHORT_EXPOSURE=0.30` (buys untouched); `positions.py` + `exit_checker` invert P&L
and target/stop; dashboard shows shorts distinctly (count, 🔻, red, "Why short", Side
column, inverted live P&L) + manual short entry; portfolio money-graph excludes shorts.
Verified: unit tests (buys unchanged, short cap, realized + exit-checker inversion) +
mock boot — no live tokens. Original design notes below.

Add `short` as a direction. Route strong bearish catalysts (earnings miss, guidance
cut, dilution, fraud, death-cross + weak fundamentals) to `short` instead of passive
`avoid`. Exit = cover target (price falls X%) + stop (price rises Y%); stops matter
more here.
- Touches: schema/prompt (`analysis/claude_analyst.py`), `calculator/portfolio.py`
  (short sizing + separate short-exposure cap), `storage/positions.py` + exit_checker
  (add `side`, invert P&L), dashboard display.
- Must check short-interest / squeeze risk before recommending (Argus treats squeezes
  as a bullish catalyst — the short side has to guard against it).
- Orthogonal to R2 (touches `direction`, not `confidence`) — safe to do first.
- Done when: analyst emits `short` w/ cover+stop; positions track inverted P&L;
  short-exposure cap + squeeze check in place.

### UX. "Buy when" column (split entry trigger from exit)  ✅ on `buy-when-trigger` (pending merge)
A watch's `exit_condition` used to cram the buy trigger AND the target/stop together under a
"Sell when" label. Split into a new `entry_trigger` field → "Buy when" column. Watches show their
buy condition there; buy/short show "now"; `exit_condition` is now target/stop only. Schema + prompt +
`portfolio._build_result` + recs table/detail + chatbot context; back-compat (old recs → blank).

### R2. Split conviction from credibility (FOUNDATION)  ✅ DONE — merged to main (tag `stable-post-r2`)
Done additively (kept `confidence_score` as-is = credibility; ADDED `conviction` 0-100):
new schema field + "CREDIBILITY vs CONVICTION" prompt block (conviction = edge, scored
per asset class); HR gate now `conviction>=75 AND confidence_score>=0.5`;
`portfolio._compute_weight` sizes by conviction with back-compat fallback to
`confidence_score×100`; dashboard shows Conviction beside Confidence (detail + table +
caption, None→"—"); chatbot context + prompt explain conviction vs confidence; sheets
export appends a Conviction column (no index shift). Verified: unit tests (conviction
drives weight+allocation, back-compat) + mock boot. Original design notes below.

`confidence_score` is overloaded (source credibility + HR gate + dedup sort key) —
the root of the crypto ceiling. Split into: `source_credibility` (0-1 scorer weight,
stays the dedup input) and `conviction` (0-100, analyst-set, per asset class, à la
ai-hedge-fund). HR gate becomes "conviction ≥ threshold AND credibility ≥ floor".
- Touches: `validation/scorer.py`, schema, `calculator/portfolio.py`, HR block,
  dashboard display, `storage/sheets.py` export, cache (keep back-compat).
- Riskiest single change (schema refactor) — do AFTER R1, BEFORE R3/R4.
- Done when: two fields exist; HR uses conviction+floor; dedup unchanged; cached data
  still loads.

### R3. ETF relative-strength / rotation (RRA)  ✅ on `r3-etf-rrs` (pending live test + merge)
Done additively (CONTEXT only — no new recommendation fields, output schema unchanged):
- `ingestion/prices.py` — `_compute_rrg()` (simplified JdK RRG) + `fetch_etf_relative_strength()`:
  from ~1y yfinance history aligned to SPY, computes RS-Ratio (>100 = outperforming market trend),
  RS-Momentum (>100 = accelerating), quadrant (Leading/Weakening/Lagging/Improving), and rel-perf
  vs SPY over ~3mo. Pure deterministic math.
- `ingestion/etf_facts.py` (new) — `fetch_etf_facts()`: category, sponsor, AUM, expense ratio,
  yield, top holdings, sector weights (holdings/sectors via yfinance `funds_data`, wrapped). Unit
  scales normalized (expense already-percent vs yield decimal; ytdReturn dropped as unreliable).
- `analysis/claude_analyst.py` — news tickers classified stock/etf/crypto; ETFs get rotation+facts
  INSTEAD of company fundamentals; two new prompt blocks (ETF RELATIVE STRENGTH, ETF FACTS) + a
  rules line telling the analyst to judge ETFs on rotation (favor Leading, avoid Lagging), not by
  forcing a news catalyst onto them.
- Verified: RRG math unit tests (outperform→Leading, accel-down→Lagging, short→None), ETF-facts
  unit-scale tests, prompt-block render, mock boot, + one free live yfinance fetch (XLK/XLE/XLU
  rotation + facts, no LLM tokens). `CLAUDE.md` updated.

Original design notes: Compute RS-Ratio (ETF strength vs SPY) and RS-Momentum from the ~1y history
we already fetch; rank into Leading/Weakening/Lagging/Improving. Swap the (meaningless-for-funds)
company-fundamentals block for ETF facts. Reuses the technicals engine + R2's conviction field.

### R4. Crypto per-asset-class conviction  ✅ on `r4-crypto-conviction` (pending live test + merge)
Done additively (CONTEXT only — no new recommendation fields, output schema unchanged):
- `ingestion/coingecko.py` — `fetch_coin_market_data()` + `_extract_market_data()`: one batched
  `/coins/markets` call → price, market cap + rank, 24h/7d/30d momentum, 24h volume, % from ATH.
  The crypto analog of fundamentals/ETF-facts; existing `fetch_crypto_context` (what the coin is) kept.
- `analysis/claude_analyst.py` — new CRYPTO MARKET DATA prompt block (used like technicals: don't chase
  a coin already run-up / near ATH) + a CRYPTO system-prompt section: score conviction RELATIVE TO CRYPTO
  so capped source credibility doesn't cap conviction; take real high-credibility crypto catalysts
  seriously (spot-ETF/SEC filings = 1.0, major exchange listings, shipped protocol upgrades, on-chain
  shifts, multi-source corroboration); require corroboration before high conviction from a lone
  low-credibility source; crypto is long/watch only (never short). Wired into run_analysis for crypto
  news tickers when crypto is enabled.
- Verified: extractor unit test (synthetic + missing-field), prompt-block render, mock boot, + one free
  live CoinGecko fetch (BTC/ETH/SOL market data, no LLM tokens). `CLAUDE.md` updated.

Original design notes: Rides on R2 — judge crypto against the best crypto sources (not the SEC), so a
strong catalyst can earn high conviction despite capped credibility. Done when: crypto eligible for high
conviction via class-relative scoring.

### R5. Options — DEFERRED (evidence-gated, income-only if ever)
Research conclusion: AI/LLM picking option DIRECTION has no credible out-of-sample
evidence (overfitting / "profit mirage", arxiv 2510.07920). BUT covered-call / CSP /
"wheel" income strategies have decades of independent evidence (CBOE BXM & PUT indices;
Whaley 2002, Ibbotson 2004, Callan 2006 — ~S&P returns at ~2/3 volatility). So if
options are ever added, do the RULE-BASED wheel/CC/CSP slice tied to held positions
(model after ThetaGang), NOT an LLM directional bet. Needs an option-chain/IV module.
Lowest core-fit; do last or not at all.

---

## Backlog

### B1. Robinhood MCP sync
Official read-only position import via agent.robinhood.com MCP instead of
the unofficial robin_stocks library. More stable long-term.

### B2. Reactive loading screen
Show ingestion source icons in real-time during pipeline run so the user
can see progress. Currently just a spinner. Deferred — complex to implement
with Streamlit's execution model.
