# Argus — To Do

## Done

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

---

## Backlog

### 9. Robinhood MCP sync
Official read-only position import via agent.robinhood.com MCP instead of
the unofficial robin_stocks library. More stable long-term.

### 10. Reactive loading screen
Show ingestion source icons in real-time during pipeline run so the user
can see progress. Currently just a spinner. Deferred — complex to implement
with Streamlit's execution model.
