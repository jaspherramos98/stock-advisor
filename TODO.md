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

### 7. Time-based catalyst-timing column (recommendations + positions) ✅
Added a `catalyst_timing` field surfacing WHEN the news catalyst is expected to
play out (e.g. "Earnings Jul 15", "Merger closes ~Q3 2026"), so the user knows
the timeframe and when to reassess — complements the price-based exit_condition.
- `analysis/claude_analyst.py` — new `catalyst_timing` field in the JSON schema
  plus a "CATALYST TIMING FIELD" prompt block: use a date only when grounded in
  the news (no guessing), otherwise an honest hold-horizon estimate.
- `calculator/portfolio.py` — passes `catalyst_timing` through `_build_result`.
- `storage/positions.py` — `add_position()` stores it; new
  `update_catalyst_timing()` for editing; updated on existing positions.
- `dashboard/app.py` — "Catalyst by" column in the Recommendations table and the
  Open Positions table; shown in both detail expanders; editable input in
  Manage positions (beside Exit strategy) and in the manual-add form; included
  in the chatbot `/context` for both positions and today's recs.
- Display-only — no alert plumbing (per chosen scope).
- `CLAUDE.md` JSON schema updated to match.

### 8. Adopt TradingAgents ideas — bull/bear debate, reflection, risk gate ✅
Borrowed from TauricResearch/TradingAgents (multi-agent trading framework) but
kept inside Argus's single Claude call to stay cheap/fast. Three additions to
`analysis/claude_analyst.py`:
- **Bull/Bear debate** — new `bull_case` / `bear_case` schema fields; the prompt
  requires the strongest case for AND against every candidate, and the bear case
  is the gate for keeping a 'buy' (else downgrade to 'watch'). Passed through
  `calculator/portfolio.py`; shown as side-by-side green/red boxes in each
  recommendation detail expander (`dashboard/app.py`) and added to the chatbot
  `/context`.
- **Reflection memory** — `run_analysis()` now loads closed positions and
  `_build_prompt()` adds a "PAST TRADE OUTCOMES — LEARN FROM THESE" block
  (realized P&L %, win rate, source, close reason, capped at 12 recent trades) so
  the analyst adapts to what has actually worked/lost for this user.
- **Portfolio risk gate** — new prompt section makes the analyst review all buys
  as one book (plus existing open positions) and downgrade correlated/over-
  concentrated names to 'watch'. Heuristic/prompt-level (no sector data ingested).
- `CLAUDE.md` pipeline flow + JSON schema updated to match.

### 9. Self-contained exits for the one-shot model ✅
Argus runs once with no live monitoring, so exits must stand alone. The analyst was
emitting useless process placeholders like "await 8-K details review" (especially on
watch signals). Fixed in `analysis/claude_analyst.py`:
- New "THIS IS A ONE-SHOT TOOL" rule: every exit_condition must be a concrete,
  self-contained price rule (gain target + stop) the user can set right now; banned
  "await/review/reassess/monitor/check back/pending" placeholders — if no concrete
  exit is possible, mark 'avoid'/omit.
- EXIT CONDITIONS section now covers 'watch' too: must give a concrete if/then
  (buy trigger price + target/stop), not vague language.
- catalyst_timing is now purely factual ("Already filed Jun 15", "Earnings Jul 15"),
  never a "review within X days" instruction.
- `CLAUDE.md` Exit Targets section updated with the one-shot constraint.

---

## Backlog

### 10. Robinhood MCP sync
Official read-only position import via agent.robinhood.com MCP instead of
the unofficial robin_stocks library. More stable long-term.

### 11. Reactive loading screen
Show ingestion source icons in real-time during pipeline run so the user
can see progress. Currently just a spinner. Deferred — complex to implement
with Streamlit's execution model.
