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

---

## Backlog

### 3. Robinhood MCP sync
Official read-only position import via agent.robinhood.com MCP instead of
the unofficial robin_stocks library. More stable long-term.

### 4. Reactive loading screen
Show ingestion source icons in real-time during pipeline run so the user
can see progress. Currently just a spinner. Deferred — complex to implement
with Streamlit's execution model.
