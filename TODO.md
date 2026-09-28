# Argus — To Do

## Done

### 63. short_dte_momentum disabled until the judge is proven (2026-09-28) ✅
The regime-bucket fix (#58) made `short_dte_momentum` reachable for the first time: weekly OTM options at 100% of
available buying power on conviction ≥80 in a risk-on tape. User chose to keep it OFF until the shadow judge is
proven (J4 → J5). New `options_strategies.DISABLED_STRATEGIES = {"short_dte_momentum"}`; `applicable_plans` skips
disabled playbooks, so a hot risk-on signal falls through to `catalyst_momentum`. The playbook code is untouched —
re-enabling = deleting the name. Test covers both disabled and re-enabled behavior (144). Files:
analysis/options_strategies.py, tests.

### 62. Judge scorecard + verdict reuse (judgment plan J4 tooling, 2026-09-28) ✅
J4 can't conclude yet (no shadow data — the judge went live today on a no-candidate day), so this builds the
scoring so the J5 call rests on numbers. New `analysis/judge_scorecard.py`: entry verdicts → the underlying's return
1/5 trading days after the verdict (sign-flipped for bearish), grouped enter vs wait/skip — counts every judged
candidate, traded or not, so data accrues faster than round-trips; taken trades → realized P&L split by the
judge's call; holding reviews → the move after sell/tighten vs keep. One sample per ticker per day; reused verdicts
excluded. `conclusion()`: needs ≥15 per group; "helps" = enter beats wait/skip by ≥1 pp at 5 days and isn't worse
on taken trades (the user decides J5). Caveat printed: the underlying's return is a proxy, not an option's P&L.
`scripts/judge_review.py` (CLI) + Agent tab "⚖ Judge scorecard" (computed on click). Real run: 0 samples →
"Not enough data … keep the judge in shadow".
**Cost leak fixed (J2/J2b):** a candidate that stays eligible (a fired watch with the stock leg off, a buy with no
affordable contract) was re-judged EVERY 20-min cycle (~$0.23/day per ticker, and 19 duplicate samples). New
`agent_judge.reusable_verdict`: reuse the ticker's last good verdict for 2h unless price moved >1.5%.
+4 tests (144). Files: analysis/judge_scorecard.py (new), scripts/judge_review.py (new), analysis/agent_judge.py,
dashboard/tabs/agent.py, tests.

### 61. Event-driven holding review, shadow (judgment plan J3, 2026-09-28) ✅
New `analysis/holding_review.py`: on the HOLD path of both exit passes (option `_run_exits`, stock `run_exits`), a
held position is reviewed only when an event fires — an unseen Finnhub headline, a move ≥1.5× its average daily
range since the last reference price (reference resets after each review), or earnings ≤2 days (once a day). News
has a 2-hour per-ticker cooldown: live testing showed Finnhub tags market-roundup pieces to big names (NVDA got 5
"news" hits, 4 of them roundups), which would otherwise buy a ~$0.01 review per roundup; headlines arriving in the
cooldown stay unseen and batch into the next review. New `agent_judge.judge_holding` (+ `HOLD_SYSTEM`,
`record_holding_verdict`, `parse_holding_verdict`): keep | sell | tighten_stop — tighten only for shares and only to a
stop above the current one and below price (else → keep with an error); failures/no credit → keep. The judge sees the
J1 briefing plus entry→now, the plan, and the ORIGINAL thesis + invalidation from the linked entry record. Logged as a
`review` record; under shadow nothing changes. State in holding_review.json (gitignored). Agent tab decision log
shows review verdicts (keep / sell / tighten @ $X). New `decision_log.get(id)`. Live simulation (synthetic NVDA
holding, real data): picked the one relevant headline ($150B buyback) out of 5, verdict KEEP, $0.009.
+3 tests (141). Files: analysis/holding_review.py (new), analysis/agent_judge.py, alerts/agentic_options.py,
alerts/agentic_stocks.py, storage/decision_log.py, dashboard/tabs/agent.py, tests/conftest.py, .gitignore, tests.

### 60. Watch triggers become agent candidates (judgment plan J2b, 2026-09-28) ✅
New `alerts/agentic_watch.py`: today's watch recs with conviction ≥50 plus ALL pinned watches (a pin wins for its
ticker) are checked every cycle; a trigger FIRES when entry_checker's parser says the level is hit AND the price is
still within 3% of it (a breakout that already ran, or a pullback that crashed through support, doesn't count); a
trigger that says "close" only counts in the last 30 minutes of the session. Fired watches join the entry loop as
`shares_only` signals — `agentic_stocks.route` sends them to shares, never options; while `AGENT_TRADE_STOCKS` is
off the rules skip them, but they're still briefed + judged so the shadow record covers watch triggers too. With the
stock leg on they're dollar share buys inside the $20 cap + PDT guard, exit plan = the watch's exit_condition.
Live scan: the user's pinned watches (GOOGL/LLY/META/MSFT/NVDA…) evaluated, none fired. tests/conftest.py stubs the
live scan. +4 tests (138). Files: alerts/agentic_watch.py (new), alerts/agentic_stocks.py, alerts/agentic_options.py,
tests/conftest.py, tests.

### 59. Sonnet entry judge, shadow mode (judgment plan J2, 2026-09-28) ✅
New `analysis/agent_judge.py`: for each candidate that reaches a decision, one Sonnet call reads the J1 briefing +
the rules' plan and answers through a FORCED tool call (`record_verdict`): enter|wait|skip, size_multiplier,
instrument, stop, confidence, thesis, invalidation, ≤3 risks. `parse_verdict` clamps size to [0,1] (the judge can
only SHRINK, never enlarge) and turns anything malformed into an explicit skip; an API failure / credit at reserve
also → a tagged skip, never an exception. System prompt: data-only (no facts from memory), skeptical by default,
weighs priced-in move, volume confirmation, reward:risk, earnings, headlines, regime, sector overlap, instrument,
own record. `config.AGENT_JUDGE = "shadow"` → verdict logged as `judge` on the decision record and changes NOTHING
(test proves a "skip" verdict still lets the rules trade); "off" disables. Cost → llm_budget. Agent tab decision log
gained Judge / Judge-why columns. Live check (real Sonnet, ~$0.012 + ~10s each): LLY → skip (trigger unmet, volume
0.64×, R:R 0.2); AMZN → skip (~$10 under its close-above trigger, R:R 1.25, cited today's UK antitrust headline).
Also: the judge loads .env itself (the scheduler only had the key via a side-effect import). tests/conftest.py stubs
`_call_model`. +3 tests (134). Files: analysis/agent_judge.py (new), alerts/agentic_options.py, config.py,
dashboard/tabs/agent.py, tests/conftest.py, tests.

### 58. Per-candidate briefing (judgment plan J1) + regime-bucket fix (2026-09-28) ✅
New `analysis/agent_context.py`: `gather()` (live, never raises, ~7s per candidate) → `build()` (pure) a briefing
of what's knowable RIGHT NOW: price vs the pipeline's (move since the rec), RSI/MACD/SMA/52w, volume PACE (today's
partial volume scaled by session elapsed — raw vol_vs_avg makes every stock look quiet before the close), R24
structure, days to earnings, Finnhub headlines published since the pipeline ran, regime, the agent's holdings +
same-sector overlap, and its own realized record by source/strategy (`track_record` over the decision log).
`render()` = the text block the J2 judge will read. The entry loop stores the briefing as `context` on every
candidate that reaches a decision. Live check (LLY): +1.0% since the rec, still under its $1,210.70 breakout
trigger, volume pace 0.65×, reward:risk 0.1 — the "on volume" half of the trigger visibly unmet.
**Bug fixed:** `agentic_options._regime` turned "risk-on (favorable for longs)" into "risk_on (favorable for
longs)", but `short_dte_momentum` checks `== "risk_on"` — that playbook could NEVER fire. New `_risk_bucket` maps to
risk_on/risk_off/neutral. ⚠ Behavior change: in risk-on markets (today) short_dte_momentum is now reachable for
conviction ≥80 signals — the most aggressive playbook (weekly OTM, alloc 100% of available BP).
tests/conftest.py now also stubs `gather` (no network in tests). +4 tests (131). Files: analysis/agent_context.py
(new), alerts/agentic_options.py, tests/conftest.py, tests.

### 57. Agent decision log (judgment plan J0, 2026-09-28) ✅
New `storage/decision_log.py` → append-only `agent_decisions.jsonl` (gitignored). The entry loop logs EVERY
candidate it considers — including the skips that were silent before (already held, not tradeable, PDT budget,
no applicable strategy/affordable contract, below $1 / stock cap full, guard or broker-review rejection) — with a
signal + context snapshot (conviction, source, exit plan, BP, stock room, regime). Option + share exits log the
entry → exit prices, pnl_pct and `entry_id` (linked by key = option_id / "eq:TICKER"); protective stops and the
LLM-credit halt are logged too. Agent tab: "🧾 Decision log" panel (live-only toggle). `tests/conftest.py`
(new) redirects the log to a temp file for every test. Fixed on the way in `_run_exits` (options): the trailing
peak was cleared BEFORE the sell was attempted, so a rejected close or a dry preview reset the trailing stop — now
cleared only once the close is placed; and the exit reason was overwritten by the order-status text (now `detail`).
Verified: headless dashboard renders the panel (0 exceptions); real DRY cycle clean. +2 tests (127).
Files: storage/decision_log.py (new), tests/conftest.py (new), alerts/agentic_options.py, alerts/agentic_stocks.py,
dashboard/tabs/agent.py, .gitignore, tests.

### 56. Market-open routine actually works: headless pipeline + shared cache writer (2026-09-28) ✅
**Bug:** `market_open.bat` step 2 ran `main.py`, which (a) prompts `input("How much are you willing to invest
today?")` — in the hidden scheduled window it hung forever, so step 3 (arm) never ran — and (b) never writes
`pipeline_cache.json`, so even a finished run gave the agent nothing (only the dashboard button wrote the cache).
Fix: new `storage/pipeline_cache.py` = the ONE cache writer/reader (moved out of dashboard/app.py; `load_today()`
never returns another day's cache — chat context used to label a stale cache "TODAY'S RECOMMENDATIONS"). New
`scripts/run_pipeline.py` = the headless "Run pipeline" (dashboard's stock-only defaults; skips NYSE
holidays/weekends unless --force; exit 1 on failure). `market_open.bat` calls it and appends to `market_open.log`.
Verified: real run wrote today's cache (7 recs) that the agent's `_signals` reads; dashboard headless 0 exceptions.
Task registration = user action (weekdays 06:30). +2 tests (125). Files: storage/pipeline_cache.py (new),
scripts/run_pipeline.py (new), dashboard/app.py, market_open.bat, .gitignore, tests.

### 55. Stock budget cap + S5 live test script (2026-09-28) ✅
`config.AGENT_STOCK_BUDGET_CAP = 20.0` — max total entry cost the agent may hold in shares (None = no cap, the
one line to remove the training wheels). Counted from the stock book (new `dollars` field + `invested()`), not the
broker, so a just-placed order counts before its fill shows. `_run_entries` sizes the pyramid on min(BP, cap) and
stops the stock leg at the remaining room (`stock_room`); the order guard still sees real BP (a room under $10
would otherwise trip its $10 min-BP rule). New `scripts/stock_leg_test.py`: 2-3 positions (today's share signals,
padded with SPY/QQQ/IWM), the cap split equally, PDT-slot-aware, through the agent's own path; `--live`, `--verify`.
`scripts/run_agent.py` runs it ONCE, LIVE on the first armed in-hours cycle when `stock_leg_test.pending` exists
(marker deleted before buying). DRY run vs live reviews: SPY/QQQ/IWM × $6.66 clean. `AGENT_TRADE_STOCKS` stays
False — flipping it (ongoing stock entries) is the user's call; the test and the exit pass work without it.
+2 tests (123). Files: config.py, storage/agent_stock_book.py, alerts/agentic_stocks.py, alerts/agentic_options.py,
scripts/stock_leg_test.py (new), scripts/run_agent.py, .gitignore, tests.

### 54. Agent tab — share positions, decisions, Sell now, stock-leg preview (S4, 2026-09-27) ✅
Agentic share positions now render one row each: shares / avg → price / P&L / equity, and either the agent's plan
(exit_condition, opened date) + live hold/close decision (`agentic_stocks.decide_exit`, peak from peak_tracker) or
"not agent-managed — never touched". **Sell now** per row reuses the extracted `agentic_stocks.close_position`
(cancel resting stop → wait for confirm → market sell; live close forgets the plan) — the exit pass uses the same
function, so there's one sell path. Preview cycle gained an **Include the stock leg** toggle (dry only; LIVE
follows `config.AGENT_TRADE_STOCKS`, shown in the LIVE expander). Verified headless (AppTest) with synthetic
positions: both row kinds render, 0 exceptions. Files: dashboard/tabs/agent.py, alerts/agentic_stocks.py.

### 53. Agent stock leg — exits (S3, 2026-09-27) ✅
`agentic_stocks.run_exits`, called every cycle from `run_options_agent` right after the options exits (even with
`AGENT_TRADE_STOCKS` off, so turning it off can't strand a position; zero reads when the agent owns no shares).
Manages ONLY positions in the agent's stock book — hand-bought shares in the agentic account are never touched.
Each is judged by `decide_exit` = `equity_exit_decision` against the plan it was opened on (recorded
exit_condition, days held) with a trailing peak (`peak_tracker` key `eq:TICKER`). Close: cancel the resting stop
first and WAIT until the broker confirms it's no longer working (`_cancel_and_wait`, 8s) — otherwise the row is
`deferred` and nothing is sold (the stop still reserves the shares); then a market sell of the whole position; a
live close forgets the plan + peak. Holding: the whole-share part rests on a GTC `stop_market` at entry × (1 −
plan stop%) for PC-off protection (fractional remainder = poll only, broker rule). An empty position read never
wipes the book. Exit rows use the options `{"close": T}` shape so anti-churn covers both legs. +3 tests incl. the
live cancel→sell order and the deferred path (121). Files: alerts/agentic_stocks.py, storage/agent_stock_book.py,
alerts/agentic_options.py, tests.

### 52. Agent ignores stale signals (2026-09-27) ✅
`agentic_options._signals` read `pipeline_cache.json` and chat suggestions with NO age check — the cache is only
overwritten when the pipeline runs, so with no scheduled morning run a Friday recommendation (e.g. today's NKE short
from 2026-09-25) would still drive Monday entries, as would a week-old chat "Buy". New `_is_today` keeps only a
cache whose `date` and chat suggestions whose `created_at` are from today (local date, the clock both are stamped
with); missing/garbled stamps fail closed. Matches the dashboard's existing today-only cache rule. +1 test (118).
Files: alerts/agentic_options.py, tests.

### 51. Agent stock leg — routing + entries, DRY_RUN (S2, 2026-09-27) ✅
New `alerts/agentic_stocks.py`: `route` (HR / conviction ≥75 → options; other buys → shares; shorts → puts;
crypto never shares), `rank` (best idea first by conviction across both legs), `size_buys` (Argus pyramid over all
the cycle's buy signals on AGENTIC BP), `plan_buy` / `enter` (dollar-based market buy via review-first
`place_order`; a live fill records the exit plan in new `storage/agent_stock_book.py` → agent_stocks.json,
gitignored). `agentic_options._run_entries` refactored into one best-first loop over a SHARED BP pool: options leg
extracted verbatim into `_try_option_entry`; an options-routed buy with no affordable contract (common at ~$42)
falls back to shares; held = option underlyings + share tickers + this cycle's closes; one PDT budget for both.
Gated by new `config.AGENT_TRADE_STOCKS` (False → options-only exactly as before, except signals are now ranked by
conviction instead of pipeline-before-chat); `run_options_agent(stocks=True)` previews it. Real-run catch: the
pyramid rounds a name sized AT the 40% cap up a cent (16.768 → 16.77) and the order guard rejected it — sizes are
now clamped to the floored cap and order dollars floor to the cent. Verified DRY vs live reviews: F $16.76 + T
$10.48 share buys review clean. +4 tests (117). Files: alerts/agentic_stocks.py, storage/agent_stock_book.py (new),
alerts/agentic_options.py, config.py, .gitignore, tests.

### 50. Shared exit parser — fixes premature "gain target reached" alerts + equity_exit_decision (S1, 2026-09-27) ✅
**Bug (user-facing, exit emails):** `exit_checker._check_percentage_exit` matched EVERY percentage as a gain
target, and its stop-exclusion looked up `f"{8.0}%"` in text that says "8%" (never found) — so for
"target 8% gain, stop loss at 4%" it emailed **"Gain target reached" at +4%**, and for analyst text like
"…(ATR-sized: avg daily range is 2.2%)" at ~+1.7%. That pushed exits at a fraction of the plan — plausibly
feeding the scorecard's discipline leak (early closes). Fix: new `analysis/exit_rules.parse_exit_condition` is
the ONE parser (target = explicit "target X%" else "X% gain/rise/…", never a bare %; stop phrase blanked first;
earliest week/day/month limit), now used by exit_checker (percentage + time exits), `scorecard.parse_band` and
dashboard `_stop_loss_price` (3 duplicate regex sets removed). Verified: agrees with the old scorecard parser on
all 53 real exit conditions (positions + today's recs); the alert path is what changed. Plus pure
`equity_exit_decision` for the stock leg: hard stop → ATR trail (after +1R, 1 stop-distance off peak) → target →
time limit, `STOCK_EXIT_DEFAULT` 8%/4%/30d. +3 tests (113). Files: analysis/exit_rules.py (new),
alerts/exit_checker.py, analysis/scorecard.py, dashboard/common.py, tests.

### 49. Equity orders review-first + whole-share limit buys (S1, 2026-09-27) ✅
`robinhood_mcp.place_order` (equity) now previews every order with `review_equity_order` after the shape +
guard checks, same policy as options: a broker alert BLOCKS a buy (`rejected`), a sell proceeds with the alert
logged, a failed review → `review_failed` and nothing is sent (even live). `agentic_stops` dropped its own
duplicate preview (+ the now-dead `verbose` param). `trading_guards.buy_cost` prices a buy as dollars OR shares ×
limit price, so a whole-share limit buy passes the BP/single-name caps (it was always rejected); a market buy by
share count has no price bound → rejected (size market buys in dollars). Verified live (DRY_RUN): $5 market and
1-share $12.50 limit F buys review clean → dry_run; a $1 limit → `EQUITY_EXTREMELY_UNMARKETABLE_LIMIT_PRICE` →
rejected. +3 tests (110). Files: ingestion/robinhood_mcp.py, trading_guards.py, alerts/agentic_stops.py,
scripts/agentic_stops.py, tests.

### 48. PDT day-trade guard for the agent (S1, 2026-09-27) ✅
The live options agent had NO pattern-day-trader protection. Replaying the real fill history shows it made **7 day
trades on 2026-08-24/25** (5 + 2 — agent entries closed same day, mostly by hand during the churn bug), past the
3-per-5-days limit. Whether Robinhood flagged the account isn't readable via the MCP (check the app's day-trade
counter). New: pure `trading_guards.count_day_trades` / `day_trade_budget` (≤3 per 5 NYSE business days, options +
stocks together, exempt ≥$25k; conservative pairing — may over-count, never under-count). Each position opened
today RESERVES its same-day exit, so the guard only ever limits ENTRIES — exits are never blocked. Live input
`robinhood_mcp.day_trade_budget` reads the broker's own fills (`get_option_orders`/`get_equity_orders`, created
≥ window start − 10 days for GTC fills) so manual trades + restarts count; still-working opening orders reserve a
slot too. `agentic_options._entry_budget` reads it AFTER exits and fails CLOSED (unreadable → 0 entries).
`market_hours.recent_trading_days` + `et_date`. Agent tab: **New entries (PDT)** metric. Verified: counter matches a
hand count of the real Aug 24–25 fills (7); live budget now = 3; DRY cycle + headless dashboard clean. +7 tests (107).
Files: trading_guards.py, market_hours.py, ingestion/robinhood_mcp.py, alerts/agentic_options.py,
dashboard/common.py, dashboard/tabs/agent.py, tests.

### 47. Order shape validation + per-placement ref_id (S1 bugs 2 + 3, 2026-09-27) ✅
**Bug 2:** `_order_args` could send both `dollar_amount` and `quantity` (broker wants exactly one) and silently
turned an unknown order_type into `market`. New pure `robinhood_mcp._order_shape_error(intent)` enforces the S0
rules — exactly one of qty/$, $ ⇒ market and ≥ `MIN_DOLLAR_ORDER` ($1), fractional ⇒ market, limit/stop need
their price, known order_type — and `place_order` rejects on it BEFORE the DRY_RUN branch (previews catch bad
shapes too). `_order_args` now maps a valid intent 1:1; quantities formatted ≤6 dp with no float noise.
**Bug 3:** `ref_id` was the deterministic `client_id` (server dedups by ref_id → a re-placed stop on a later day
could be swallowed) and option orders sent none. `_new_ref_id()` = fresh UUID per placement, added only to the
`place_*` call (review schemas reject it); returned as `res["ref_id"]` + printed for tracing. Safe because
placement is never auto-retried. Local dedup unchanged (`client_id` in trading_guards). Verified the new args
live against `review_equity_order` (clean). +4 tests (101). Files: ingestion/robinhood_mcp.py, tests.

### 46. Option review alerts now block entries (S1 bug 1, 2026-09-27) ✅
`place_option_order` read `order_checks` at the top level, but the review payload nests it under `data` (verified
live), so every broker alert (insufficient BP, PDT, halt…) logged as "none" and nothing was ever blocked. New
`robinhood_mcp._order_alert(preview)` reads `data.order_checks.alertType`. Policy: an alert BLOCKS an opening
order (`status: "rejected"`, reason `broker pre-trade alert: <TYPE>` — existing callers already skip non-placed);
a CLOSING order proceeds with the alert logged (a blocked exit could trap a loser; the broker hard-rejects truly
illegal orders). `agentic_stops` reuses the helper. Verified live under DRY_RUN: 50× F call →
`OPTION_NOT_ENOUGH_BP_FOR_PREMIUM` → rejected; 1× → clean. +4 tests (97). Files: ingestion/robinhood_mcp.py,
alerts/agentic_stops.py, tests/test_deterministic.py.

### 45. Stock-trading S0 verification (read-only, 2026-09-27) ✅
Probed tool schemas + `review_equity_order` on the agentic account (nothing placed). Findings (fractional/dollar
rules, advisory review, PDT applies, 3 existing order-path bugs) recorded under Backlog "Stock trading for the
agent" S0 and folded into S1. CLAUDE.md: equity order rules + corrected the stale "MCP has no crypto" note.

### 44. Docs refresh (pre-compact, 2026-09-27) ✅
README: 6 tabs + Agent tab, autonomous-agent section, buy-trigger alerts, Robinhood official MCP as the
brokerage (robin_stocks = fallback), project tree rewritten for dashboard/tabs + common, MCP/agent modules,
scripts, tests. CLAUDE.md: scheduled tasks marked deleted-by-user with the re-register command; equity
trading points to the approved plan; buying-power cache location (dashboard/common.py).

### 43. History tab — cache charts (click 0.45s → 0.25s) ✅
Warm-rerun profile showed the History tab was the largest per-click cost: `_cached_history` cached the
Sheets read, but every rerun rebuilt the DataFrame and 4 Plotly Express figures (~0.1s fixed overhead each —
NOT row-driven: only 27 rows; my earlier "grows with exports" note was wrong).
- `dashboard/tabs/history.py`: `_history_views(history)` (frame + metrics + 3 static figures) and
  `_allocation_fig(history, selected)` cached with `st.cache_resource` keyed on the rows (a new export changes
  the key → auto-refresh; no clear-on-export coupling). Figures are never mutated after build, so sharing is safe.
  Folded the 4× duplicated chart styling into `_style()`.
- Verified: old vs new tab code run on the real 27 rows with a recording `st` → **all 18 outputs byte-identical**
  (4/4 figure JSON, 4/4 metrics, filter widget, raw table); app fingerprint identical; 93 tests; 0 lint issues.
- Click median 0.45s → **0.25s** (`scripts/bench_dashboard.py`).

### 42. Refactor step E — persistent MCP session (one connection per process) ✅
Measured: every MCP tool call opened a fresh OAuth+HTTP+initialize session (~1.2–1.5s). A dashboard cold
load made 7 calls (10.7s) and an agent cycle 7 (8.8s of 11.4s).
- New `ingestion/mcp_session.py` `PersistentSession` (no mcp import → CI-testable): one session on a daemon
  event-loop thread; a single owner task opens/serves/closes it (anyio cancel scopes must exit in the task
  that entered them); calls serialized; idle 5 min → close. A failed call closes the session and is retried
  ONCE on a fresh one **only for `get_/review_/list_` tools — order placement is never auto-retried**
  (a lost response could mean the order filled; a retry could double-place).
- `mcp_auth`: `_with_session` → reusable `_open_session` context manager; `call_tool` routes through
  `_PERSISTENT` (NotAuthenticated still unwrapped); `login()` resets it (old tokens). Kill switch:
  `config.MCP_PERSISTENT_SESSION = False` → per-call sessions.
- **After:** dashboard MCP time 10.7s → **3.6s**; agent cycle 11.4s → **6.0s** (identical decisions);
  scheduled-agent process still exits cleanly (daemon thread). 7 new tests (reuse, read retry-once,
  never-retry orders, open-failure recovery, idle close, reset, 10-thread safety). 93 green.
- A/B (flag off vs on): no warm-click difference (0.45 vs 0.46–0.49s, 0 MCP calls per click). The click
  went 0.25s → 0.45s day-over-day with the flag OFF too — traced to the History tab (see Backlog).

### 41. Refactor step C — split dashboard/app.py into per-tab modules ✅
`dashboard/app.py` 2,739 → ~900 lines. New `dashboard/common.py` (all cached readers + shared render/pure
helpers) and `dashboard/tabs/{recommendations,portfolio,positions,watchlist,history,agent}.py`, each a
`render()`; app.py wires them into `st.tabs`. Also dropped the dead `if True:` wrapper and 20 unused imports.
- Done as a scripted mechanical move (verbatim code; re-indent never touches multi-line string interiors)
  after an AST coupling scan: no cross-tab variable leaks, only Recommendations needs script state
  (`allocations, budget, prices, recs` → render args). Removed a redundant function-local
  `import pandas as pd` (Portfolio) that would have made `pd` function-local → UnboundLocalError risk.
- Verified: 0 undefined names / 0 unused imports (stdlib `symtable` check on all 8 files); 86 tests;
  headless before/after widget fingerprint IDENTICAL for all 6 tabs + sidebar, 0 exceptions — including
  seeded runs (mock recs → Recommendations 66 widgets; synthetic invested positions → Portfolio chart path +
  My Positions 45 widgets; real positions.json untouched).
- Perf side effect: click 0.52s → **0.25s** (cache decorators now applied once at import, not every rerun).

### 40. Refactor step B — dashboard click lag 4.7s → 0.5s ✅
Measured with the new `scripts/bench_dashboard.py` (headless AppTest; warm rerun = click cost).
**Before:** click median 4.73s, cold 17.6s. **After:** click median 0.52s (−89%), cold ~15.8–17.6s, 0 exceptions.
- **Cache bug:** `_BP_CACHE` was a module-level dict in `app.py`; Streamlit re-executes the script every
  click, so it was re-created and never hit (~1.3s/click). Now `_cached_buying_power` (`st.cache_data`
  ttl 60, process-wide so the chat-proxy thread shares it); Refresh button calls `.clear()`.
- **Uncached reads on every click → cached:** `_cached_prices` (ttl 30, sorted-tuple key) now serves the
  Portfolio, My Positions, Watch List (open positions + pins) and chat context — equal ticker sets share one
  fetch; `_cached_history` (Sheets, ttl 600, cleared after a successful export); `_cached_spy_benchmark`
  (yfinance SPY, ttl 900). Removed the now-dead `spy_benchmark` import. Run-pipeline + add-position quotes
  stay uncached on purpose (on-demand actions).
- Docs: CLAUDE.md Key Files (bench script) + Known Issues (never module-level caches in app.py).

### 39. Refactor step A — remove dead code + outdated files/docs ✅
First step of the refactor/perf plan (see Backlog "Refactor plan B–E"). Evidence-based: import-graph +
unreferenced-symbol scan, then a headless AppTest load.
- **Deleted (local, untracked):** `collect_for_review.py`, `argus_review.txt`, `argus_review_*.zip` (June
  review tooling), `argus-header-verify.png`, `budget.json` (deprecated since R9), a stray `.lnk`
  shortcut, `positions_backup_20260721_*.json` (superseded July backup).
- **Deleted (tracked):** `scripts/mcp_spike.py` (pre-auth probe, job done); dead functions
  `market_hours.is_market_open`, `paper_book.open_option_ids`, `paper_book.get_closed`,
  `llm_budget.set_reserve` (redundant — the UI sets reserve via `set_balance(balance, reserve)`).
- **Kept on purpose:** `alerts/agentic_stops.py` + `scripts/agentic_stops.py` (planned stock side of the
  agent), `robin_stocks` path (USE_MCP=False fallback + only crypto reader), backtest tool + mock data.
- **Dedup:** `agentic_stops` now uses `robinhood_mcp._TOOL_REVIEW_ORDER` instead of a hardcoded tool string.
- **Docs:** rewrote the stale R25 status ("scaffolding only / DRY_RUN / on branch …"), the "_TOOL_* are
  placeholders" note, the entry_checker sources line, and the mcp_login line; removed mcp_spike/budget.json.
- Verified: 86 tests, compileall, headless app load (0 exceptions, 6 tabs). Dead-symbol scan now shows only
  2 intentional test-guarded names.

### 38. MCP OAuth: silent refresh across restarts + reads never pop a browser ✅
Recurring pain: every ~3 days the MCP demanded a full browser re-login and concurrent Streamlit reruns
raced the OAuth flows into `State parameter mismatch`, killing buying-power reads. Root cause found in the
`mcp` SDK: `_initialize` restores the stored token but NOT `token_expiry_time`, so `is_token_valid()`
short-circuits True for an expired access token and the refresh branch never runs — it uses the dead
token, 401s, then does a full authorization_code (browser) grant. Fixed in `ingestion/mcp_auth.py`
(no SDK patch):
- `_RefreshingProvider(OAuthClientProvider)` overrides `_initialize` to restore an absolute expiry so an
  expired access token takes the REFRESH path (silent renewal from the refresh token; SDK persists the
  rotated token). `_FileTokenStorage.set_tokens` now stamps `expires_at`; `read_expires_at()` reads it
  (unknown → treat as expired = prefer refresh over a stale token).
- Non-interactive `call_tool`/`list_tools` pass redirect/callback handlers that RAISE `NotAuthenticated`
  (never open a browser); `_run_sync`/`_unwrap` surface it out of the SDK's anyio ExceptionGroup so reads
  degrade clean instead of an opaque TaskGroup error + no browser race.
- NOTE: these fixes were written earlier this session but LOST in PR #13's squash (main's mcp_auth had
  neither) — re-applied here as one clean auth PR. Re-login is now needed only when the refresh token
  itself expires (rare), not every few days. mcp_auth is import-gated out of CI (mcp SDK not installed);
  verified locally. Docs: CLAUDE.md Key Files + Known Issues.

### 37. Entry-alert scoping + freshness + 1-day pin TTL ✅
User: entry emails fired for tickers they didn't want, and buy triggers kept firing on old news.
Diagnosis: the recommendations source was ungated (fired for EVERY watch rec, ignoring the curated
watchlist) and had no catalyst-age check; pins lived 7 days. (IWM/PLTR turned out to BE in the etfs
watchlist + IWM was batch-pinned that morning — the alerts were legitimate, but the gates were still
missing.) Fixes:
- **Watchlist scope** (`entry_checker._candidates_from_recommendations` + `_watchlist_tickers`): a
  recommendation entry trigger only fires if its ticker is in the Finnhub watchlist. Pinned + chat
  sources bypass (explicit opt-ins). Watchlist read failure fails-open on pinned/chat.
- **Catalyst freshness** (`_catalyst_is_stale`, `CATALYST_MAX_AGE_DAYS=3`): added `catalyst_date` to the
  analyst JSON schema + prompt (copied from each news item's new `Date` line, itself from
  `scorer._parse_published`); entry_checker drops a rec trigger whose catalyst is older than 3 days.
  Missing/null/unparseable = fail-open (technical watches + pre-change cached recs still work).
- **Pin TTL 1 day** (`entry_watch.PIN_TTL_DAYS` 7→1): a pin auto-unpins the day after it's pinned unless
  re-surfaced in a fresh run (`renew_pins` resets the clock). Lazy prune on load does the auto-unpin.
- Tests: `_catalyst_is_stale` boundary + fail-open, watchlist+staleness scope on the rec source, pin TTL
  updated to be TTL-relative. 85 green. Docs: CLAUDE.md schema + entry-alert sources + pin TTL + Watch tab.
- NOTE: this only gates the RECOMMENDATIONS source. Existing pins (e.g. the 8 batch-added) still fire
  until they expire (now ~1 day) or are unpinned in the Watch List tab.
- **Clear-all pins button** (`clear_all_pinned` + Watch List two-step-confirm button) to wipe every pin
  at once instead of ✕ each. While adding it, found + fixed a REAL bug: `load_entry_watch` returned
  `dict(_EMPTY)` — a SHALLOW copy sharing the module-level constant's mutable `pinned`/`notified` lists,
  so when the file didn't exist yet, callers mutated the shared constant and state leaked across calls.
  Now `_empty()` returns a fresh structure each time. Unit-tested (`test_clear_all_pinned`). 86 green.

### 35. Agent bleed fixes — churn + scout noise ✅  (branch feat/robinhood-mcp-agentic)
Diagnosed why the live agent was losing beyond its baseline -EV. Two real defects fixed:
- **Churn (sell-then-rebuy):** exits run before entries, so a just-closed underlying no longer read
  as "held" and the same signal re-bought it the SAME cycle (log 08-25 11:59: SNAP placed + SNAP
  closed together) — paying the round-trip spread and undoing the exit. Fix: `_closed_underlyings`
  (pure, unit-tested) excludes this-cycle closes from re-entry; threaded into `_run_entries` +
  `_run_paper_entries` via a new `exclude` arg. Live counts only placed/dry_run closes; paper counts all.
- **Scout noise:** removed the mid-range RSI momentum buy (52-67 → call) from `affordable_scout._lean_from_rsi`
  — buying a call on a mid-range RSI is noise that can't clear premium+theta+spread. Scout now fires
  ONLY on statistical extremes (≤35 buy / ≥68 short, mean-reversion basis). Test updated.
- **Scout OFF by default:** `SCOUT_AFFORDABLE=False` — agent trades ONLY real catalyst signals
  (pipeline + chat) and stays idle when nothing affordable qualifies. Idle beats -EV noise. Flip True
  to re-enable.
- NOTE: the core "agent loses" truth is unchanged — it's an aggressive -EV pilot by design. These fixes
  stop it losing FASTER than baseline; they don't manufacture edge. 82 tests green.

### 36. Pinned-watch TTL — expire stale entry triggers ✅  (branch feat/robinhood-mcp-agentic)
A pinned "buy when" trigger used to live forever, so an orphaned price level kept firing after its
catalyst/thesis was long gone (and the level itself was stale). Now (design B):
- **7-day TTL** (`PIN_TTL_DAYS`) from `pinned_at`; expired pins are lazily pruned on `load_entry_watch`
  (persists only when it actually drops one → normal load stays read-only). Missing/unparseable date =
  treated as expired (fail-safe).
- **Renew-on-reappear:** `renew_pins()` resets the clock when a pinned ticker resurfaces as a live watch
  in a fresh pipeline run (`entry_checker` calls it before checking) — a still-valid thesis persists, a
  true orphan ages out. So expiry means "no recent thesis", not merely "old".
- **UI:** Watch List pins show an expires-in-N-days countdown (`pin_days_left`) + the TTL is explained in
  the section caption. Unit-tested (`test_pin_ttl_expiry_and_renew`). 83 tests green.
- Chat suggestions were already self-limiting (each set replaces the last); this is pinned-only.

### 34. Phase 2 — autonomous options agent, LIVE (R25→R27) ✅  (branch feat/robinhood-mcp-agentic)
Built + running the whole agentic options pilot this session. Highlights (see CLAUDE.md for module map):
- **MCP live (R25):** reads swapped to the official Trading MCP via `ingestion/account_reads.py`
  (USE_MCP=True) → 429 gone; news skipped under MCP. Auth in `ingestion/mcp_auth.py` (DCR+PKCE+refresh).
- **Options stack (R26):** `ingestion/options_data.py` (chain→contract, liquidity+affordability),
  `analysis/options_strategies.py` (5 playbooks + trailing exit), `trading_guards.OptionOrderIntent`/
  `check_option_order`, `robinhood_mcp.place_option_order` (review-first, is_error-honored, agentic-only).
- **Agent + scheduler (R27):** `alerts/agentic_options.py` (`run_paper_agent`/`run_options_agent`),
  `scripts/run_agent.py` + tasks "Argus Options Agent" (every 20 min) + "Argus Market Open" (6:30 AM PT:
  launch+pipeline+arm). DRY→PAPER default, LIVE only when `agent_live.arm` exists; kill switch
  `agentic_halt.flag`; LLM-credit halt (`llm_budget.py`).
- **Signal breadth:** pipeline buys/shorts + chat suggestions (direction-correct via `_chat_direction` —
  fixed the TLT short→call bug) + `ingestion/affordable_scout.py` (RSI screen over a broad cheap/liquid
  universe so it isn't idle at low BP).
- **Exits:** trailing take-profit (`storage/peak_tracker.py`) — stop −50% → trail (+25% arm / −20% giveback)
  → +80% target → ≤2 DTE. Auto-sells each cycle (poll-based).
- **UI (tab 🤖 Agent):** paper book + live candlestick (paper=teal/red, LIVE=gold markers), credit ledger,
  kill switch, preview/LIVE run, agentic positions + Close-now. Cached MCP reads (30-60s) to fix click lag.
- **Verified LIVE:** first real cycle F call bought $0.19 → auto-sold $0.34 (+79%). Agent funded to ~$100,
  holds real TLT/AAL/SNAP calls, exits managing correctly.
- **Also this session:** reverted R28 per-class recs (padded low-conviction) back to ≥10-total single list;
  remove individual closed trades from scorecard (`storage.positions.delete_position`); batch watchlist-add
  form; MRNA + biotech watchlist; crypto exit-strategy research (DOGE/XRP — MCP can't trade crypto).
Remaining/next: tune trailing (`trail_activate`) if it gives back small rips; validate edge over more
trades before trusting; the open TLT call is the pre-fix wrong-direction leftover (close manually if wanted).

### 33. Robinhood agentic execution — Path B scaffolding (R25) ✅
Branch `feat/robinhood-mcp-agentic`. Groundwork for using Robinhood's official Trading MCP as the
sanctioned execution path (ends the `robin_stocks` 429; auto-places exits so no 24/7 watch). Design A
(Argus computes orders, MCP executes literally — no LLM per trade), confirm-first, DRY_RUN default ON.
Shipped **additive + flag-gated** so native Argus is untouched (`config.USE_MCP=False`, `DRY_RUN=True`):
- `config.py` — `USE_MCP`/`DRY_RUN`/`ROBINHOOD_MCP_URL`.
- `trading_guards.py` — OrderIntent/GuardState + `check_order` (idempotency, daily order cap,
  daily-loss kill switch, buying-power + 40% single-name cap). Pure logic, 11 new tests.
- `ingestion/robinhood_mcp.py` — MCP client: mirrors robinhood.py read shapes + guarded DRY_RUN-safe
  `place_order`. Live tool calls stubbed at `_call_tool` (`MCPNotWired`) until the spike; reads degrade
  to empty. 4 new tests.
- `scripts/mcp_spike.py` — one-shot OAuth-handshake probe (tools/list, places nothing).
- Docs: CLAUDE.md Key Files + new "Robinhood agentic execution (R25)" section.
**NOT live** — blocked on the spike + a funded Agentic account (see Backlog + plan file). 58 tests green.

### 32. Apply structure exits to EXISTING positions (R24b) ✅
R24 only fed the analyst for new ideas; owned tickers are excluded from recommendations, so there was
no way to use the new exit math on positions you already hold. Now each open-position expander in My
Positions shows a **📐 Suggested exit (structure)** — `_suggested_exit(ticker)` (st.cache_data, 15m TTL)
runs the same R24 `key_levels` math on the holding's live chart: target% to nearest resistance, ATR
stop%, R:R — with an **Apply** button that writes `target X% gain, stop loss at Y%` into the position's
exit_condition. Longs only (the target-to-resistance math is long-oriented; shorts skipped). R:R<2 is
flagged weak; blue-sky (no overhead resistance) shows the ATR stop + a measured-move target hint.
Verified live: AVGO → target 2.8% (to resistance $432.73), stop 6.1%, R:R 0.46 (weak) — exposing that
its old ~10% target had only 2.8% of real room. 43 tests still pass (UI/dashboard, no unit change).

### 31. Structure-anchored exits — kill the flat ~10% (R24) ✅
User: exits were always ~10%, unrealistic per position. They actually ranged 6-12% but clustered
8-10% and weren't tied to each stock's chart. Root cause: we computed ATR + support/resistance (R7)
but only used them for ENTRY triggers, never for exits — exits were free-floating %.
- `ingestion/prices.py` `_compute_key_levels`: added `stop_pct_atr` (~1.75× avg daily range, floored
  2% — scales with volatility), `target_pct_resist` (% to nearest resistance = real upside; None in
  blue sky), and `reward_risk` (target÷stop; <2 = weak).
- `analysis/claude_analyst.py`: KEY PRICE LEVELS block now prints a `SUGGESTED EXIT` per ticker;
  EXIT CONDITIONS prompt rewritten to anchor exits to those numbers, never default to a single band,
  mark R:R<2 as watch, size blue-sky breakouts to a measured move (reward ≥ 2× ATR stop). Exits should
  visibly vary across the list.
- Verified: three chart shapes → 2%/2% (weak, R:R 1.0 → watch), 12%/4.4% (R:R 2.73), 18%/8.8%
  (R:R 2.05). +3 tests (43 passing). Also answered the lookback question: news = ~1-3 days (fresh
  catalysts, not 1-2 months), price = ~1yr technicals (confirmation/timing context, not prediction).

### 30. Chat token budget — fix the broken cache, bound the history (R23) ✅
Measured the whole chat path instead of estimating it. The chat, not the pipeline, is the dominant
Claude cost: one analyst run is ~$0.05–0.09 (~$2/mo at one run/day), while a long chat session runs
$0.50–0.70 on its own. Two distinct defects, both in `dashboard/app.py`:
- **R22's chat caching never actually worked.** The `cache_control` block was right, but the cached
  prefix was `ARGUS_SYSTEM_BASE + live portfolio snapshot`, and the snapshot carries fresh prices,
  live buying power and P&L, rebuilt by `loadContext()` on every panel open. Prompt caching is an
  exact-prefix byte match — one changed price digit invalidates it, so every message paid the ~1.25×
  write premium and never got a read. Now split at the stability boundary: the client sends
  `system_base` and `system_context` as two fields, and the proxy puts the single cache breakpoint on
  the static base only. Content after a breakpoint isn't part of the cached prefix, so live numbers
  can churn freely. Measured `ARGUS_SYSTEM_BASE` at **1,222 tokens / 4,626 chars** — over Sonnet 4.6's
  1024-token minimum cacheable prefix, but only by ~16%, so a test now guards the char floor
  (`SYSTEM_BASE_MIN_CHARS`); trimming the prompt past it would turn caching off with no visible symptom.
- **Chat history was never trimmed.** `state.history` was pushed to on every turn, sent whole on every
  request, and never evicted — and the widget DOM survives Streamlit reruns, so a tab left open grew
  forever. Input cost grew quadratically across a session. Now trimmed server-side in the proxy
  (`chat_budget.trim_history`, `CHAT_HISTORY_LIMIT = 12`), snapped forward to start on a `user` turn
  since the API 400s if `messages[0]` is an assistant reply. Server-side so a stale browser tab can't
  bypass it; the browser still shows the full transcript, only the tail is billed. Safe to keep small
  here because the facts Argus reasons over live in the *system* prompt and refresh on every chat open —
  history only carries conversational thread.
- **Instrumentation first:** there was zero token visibility anywhere in the codebase (no `usage` reads,
  no counting, no cost log) — the `usage` object was already in the response and thrown away. The proxy
  now prints `in / out / cache_read / cache_write / msgs_sent` per call. `cache_read` is the number to
  watch: 0 on message 1, non-zero after. Zero throughout means the prefix is being invalidated again.
- **Clear-chat button** (⟲ in the chat header) — resets `state.history`, the only reset short of a hard
  page reload. Cheapest saving available and fully under the user's control.
- New root module `chat_budget.py` (extracted so it's unit-testable without booting Streamlit, same
  reason `market_hours.py` is its own module). +5 tests (41 total), added to CI's `compileall` list.
- Expect ~35% off a mid-length session and more as sessions run long. Confirm against the new log line
  before believing it.

### 29. Token cuts B/C/D — chat caching, trim pipeline, cost nudge (R22) ✅
> ⚠️ Superseded by R23: item B below (chat prompt caching) was correct in intent but never took effect —
> the volatile portfolio snapshot sat inside the cached prefix and invalidated it on every message.
- **B — chat prompt caching:** the `/chat` proxy now sends `system` as a `cache_control: ephemeral`
  block. The system prompt (big static rules + the portfolio snapshot loaded once per chat open) is
  identical across every message in a session, so the first message writes the cache and every
  follow-up reads it at ~0.1×. Biggest per-message chat saving. (Pipeline caching deliberately
  skipped — runs are hours apart, so nothing repeats within the cache TTL; it'd be wasted effort.)
- **C — trim pipeline input:** `MAX_STORIES` 25 → 15. Fewer stories = shorter prompt AND fewer
  tickers needing the fat technicals/fundamentals/levels context. Still a full read for a personal tool.
- **D — cost nudge:** the Run-pipeline caption now says once/day is usually enough (news rarely shifts
  intraday; re-running mostly re-spends for the same read). Behavioral, free.
- Note: A2 ("run event check 2×/day") was intentionally NOT done — the R21 new-news gate is strictly
  better (fires when real news breaks, $0 otherwise) and a fixed schedule would miss between-window news.

### 28. Token cut A — gate + Haiku the event checker (R21) ✅
Biggest recurring token drain: the news-driven exit check called Claude every 15-min scheduled run
(~26/day, mostly finding nothing). Two cuts:
- **Gate on new position-relevant news** (`_relevant_new_headlines` + `event_seen.json`, reset daily):
  only call Claude when a NEW headline mentions a ticker the user actually holds. Most windows have
  none → skip → $0. Replaces the rigid "2×/day" idea — strictly better (fires exactly when real news
  breaks, free otherwise).
- **Model → Haiku** (`config.CLAUDE_CHEAP_MODEL = claude-haiku-4-5`) for this mechanical "did news
  trigger the exit?" classification — ~1/3 the Sonnet cost. Also now sends only the new relevant
  headlines, not 30 generic ones.
- Est. this line ~$6/mo → well under $1. `event_seen.json` gitignored. +1 test (37... 36 total).

### 27. Chat affordability / order-executability rule (R20) ✅
Argus chat recommended shorting Apple when buying power was below one AAPL share — unexecutable
(shorts/limit/stop/options all need ≥1 whole share, and shorts need margin). Added an
AFFORDABILITY/EXECUTABILITY block to `ARGUS_SYSTEM_BASE`: if live buying power < one share's price,
never recommend shorting or a limit/stop on that name — say it's "not actionable at your current
buying power" and offer a fractional market buy as the only executable long on an expensive stock.
Chat already has buying power + recommendation prices in context, so it can sanity-check per name.
Fixed a `\$` → `$` slip (this is the prompt sent to Claude, not Streamlit markdown — no LaTeX escaping).

### 26. Login circuit breaker — stop the 429 self-DoS (R19) ✅
User's 429 loop wouldn't clear even after "an hour." Root cause found in their log: a SINGLE
argus.bat launch fired THREE device-approval challenges in seconds (three UUIDs) — the dashboard
calls `_login` from header + sidebar + prices + chat on every Streamlit rerun, and each attempt
starts a fresh challenge that resets Robinhood's throttle. Worse: the **"Argus Alert Checks"
scheduled task fires a login every 15 min in the background**, so the throttle never decays — the
"wait an hour" was never quiet.
- `ingestion/robinhood.py`: added a per-process circuit breaker — `_LOGIN_COOLDOWN_UNTIL` /
  `_LOGIN_FAIL_COOLDOWN_SECONDS = 900`. After a failed login, `_login` returns False immediately for
  15 min without calling `rh.login` (no new challenge), so many call sites + reruns can't spam.
  Cleared on success. Verified: with cooldown set, `_login` short-circuits with no network call.
- Ops (done live, reversible): **disabled the scheduled task** (`schtasks /Change ... /DISABLE`) and
  stopped the running Argus so nothing keeps hammering during recovery. Re-enable after re-auth.
- Docs: Known Issues now covers the breaker + the scheduler-trap recovery procedure.

### 25. 7-day Robinhood session (R18) ✅
Confirmed the account offers NO authenticator/SMS 2FA — only device approval + passkey — so the
R17 TOTP path can't be activated (kept anyway, harmless no-op). Only remaining lever to reduce the
daily re-approval pain: bump `_login` `expiresIn` 86400 → 604800 (1 day → 7 days) so the phone-tap
approval is ~weekly instead of daily. Robinhood may cap actual token life below this — best-effort.
Account itself is healthy (app works fine); the 429s were self-inflicted by retrying (each attempt
starts a fresh challenge). Docs updated.

### 24. Optional TOTP 2FA login — escape the device-approval 429 loop (R17) ✅
The Robinhood session expired and re-login was stuck in a 429 loop: it uses **device-approval** MFA,
which polls `get_prompts_status` (rate-limits hard), and **each attempt starts a fresh challenge that
resets the 429** — so retrying kept it hot (user waited 15 min, still 429, because the retries were
the problem). Also brittle: robin_stocks crashes on the 429 (`'NoneType' object is not subscriptable`).
- `ingestion/robinhood.py` `_login`: if `ROBINHOOD_MFA_SECRET` is set, generate a `pyotp` TOTP
  `mfa_code` and pass it to `rh.login` — the authenticator-app path, which is silent (no push) and
  never touches the 429-prone `get_prompts_status` endpoint. Guarded: no secret → unchanged
  device-approval behavior. Secret is a credential — never printed/logged. `pyotp==2.9.0` already
  pinned; `login()` already accepts `mfa_code` (robin_stocks 3.4.0).
- Bonus: TOTP re-auth is silent (no phone tap), so the daily session expiry stops killing the 15-min
  alert scheduler.
- Docs: `.env` keys (`ROBINHOOD_MFA_SECRET` optional) + a Known Issues entry on the auth/429 loop.
- User action to activate: enable Authenticator-app 2FA in Robinhood, put the base32 secret in `.env`.
  Immediate unblock is behavioral: STOP retrying, delete the stale pickle, wait an hour with zero
  attempts.

### 23. "No actionable recommendations" when Robinhood is down (R16) ✅
User saw "No actionable recommendations after filtering" twice. Root cause: it's a symptom of the
expired Robinhood session. `_effective_budget()` returns 0.0 when buying power can't be read, and
`calculate_allocations` treated budget ≤ 0 as "bail entirely" (`return []`) — so the day's analysis
vanished. Inconsistent too: budget 1-9 already showed recs at $0.
- `calculator/portfolio.py`: split the guard — only an empty `recommendations` list returns `[]`;
  budget is floored at 0 and falls through to the $0-display branch, so budget 0 now shows every rec
  at $0 (same as any sub-floor budget). Verified: 11 cached watches now render at budget 0.
- `dashboard/app.py`: when `budget <= 0` the Recommendations tab shows a warning that buying power is
  unavailable (likely an expired Robinhood session) and the table is analysis-only — so an all-$0
  table isn't misread as a dead market.
- Tests: `test_allocation_zero_budget_empty` (encoded the old wrong behavior) replaced with
  `test_allocation_zero_budget_shows_recs_at_zero` + `test_allocation_no_recs_empty`. 35 passing.
- Separately diagnosed the underlying RH failure: session pickle expired (~1-day `expiresIn`),
  re-login needs phone device-approval (can't be automated), and repeated attempts hit a 429. Fix is
  a one-time interactive `python ingestion/robinhood.py` + approve on the app. Not code — user action.

### 22. "What Argus is monitoring" panel — manage pinned watches (R15) ✅
Gap from R12: pins could only be removed from the recommendation expander, which disappears once the
pipeline reruns and drops that ticker — leaving an **orphaned pin** still alerting every 15 minutes
with no UI to delete it. The Watch List tab now opens with a single monitoring panel.
- **📍 Open positions — exit alerts** (auto-included per user request; nothing to pin): entry/live/
  P&L, computed stop price, exit condition. Read-only, managed under My Positions. Note: owned
  tickers stay excluded from ENTRY alerts by design — you're already in, so a "buy when" is moot.
- **📌 Pinned buy triggers — entry alerts**: each pin with parsed breakout/pullback levels, live
  price, pin date and a ✕ remove button. Chat suggestions listed in an expander with a clear button.
- **Two bugs the UI exposed:**
  - A pinned trigger with no `$` price (e.g. BTC's "CLARITY Act passes committee vote…") can NEVER
    fire — it was silent dead weight. Now flagged with an explicit warning to remove or re-pin.
  - The `$...$`-as-LaTeX bug again, this time mangling raw trigger text into monospace. Added an
    `_esc()` helper applied to every analyst-written string rendered in this tab.
- Verified with Playwright against real pins; removed the two test pins seeded during verification.

### 21. Recommendations table fits without horizontal scroll (R14) ✅
Table needed scrolling right to read Buy/Sell when, and the text was clipped to one line.
- `dashboard/app.py`: explicit pixel widths on all 12 columns so the total lands inside a normal
  desktop content area (no horizontal scroll); `row_height=70` (double) so Buy/Sell wrap to 2 lines;
  `height` derived from row count (capped 800) so all rows show without an inner vertical scrollbar.
- Bought the needed width by dropping **Company** (already the expander title) and merging ⭐ + ⚠
  into a single narrow **Flags** column. Buy/Sell text truncated to 72 chars via `_short()` — full
  wording still in the Stock details expander (noted in the caption).
- **Bug fixed:** the allocation caption rendered as `**Amount ( )** — 0.00 means...` — paired `$...$`
  was being parsed as LaTeX and eating the dollar signs. Escaped as `\$`; documented in CLAUDE.md.
- Verified visually with Playwright at 1600px across four iterations: first pass still overflowed
  (`width="large"` ≈300px each pushed Sell when + Flags off-screen), second fit but sat exactly at
  the limit so a page scrollbar re-clipped Flags — final widths leave margin. Screenshot artifacts
  deleted.

### 20. Alerts actually scheduled (R13) ✅
The last gap: everything was wired but nothing ran it, so no alert could ever fire on its own.
- Registered Windows scheduled task **"Argus Alert Checks"** → `wscript.exe run_checks_silent.vbs`,
  **every 15 min, 24/7, hidden**. Safe around the clock because `run_checks.py` self-gates on US
  market hours (ET, DST-aware), which also makes it correct regardless of the PC's local timezone.
- `run_checks.bat` (UTF-8 env + venv python) + `run_checks_silent.vbs` (hidden wrapper) added.
- `run_checks.py`: closed-market skips no longer written to the log (would be ~96 junk lines/day at
  a 15-min cadence — added a `to_file` flag to `log()`); log writes pinned to `encoding="utf-8"`
  (em-dashes were producing mojibake under the cp1252 locale default).
- **Bug found + fixed:** the new `.bat` files were written with LF endings, so cmd.exe split `REM`
  lines (`'M' is not recognized as an internal or external command`). Converted argus.bat /
  argus_stop.bat / run_checks.bat to CRLF; noted the constraint in CLAUDE.md.
- Verified end-to-end: manual run exit 0; task triggered via `schtasks /Run` → **Last Result 0**,
  log updated, no window shown. A **real alert fired and emailed** during testing (AVGO hit its
  gain target, +5.5%), and the same-day dedup correctly suppressed the repeat on the next run.

### 19. Pinned watch button + silent (no-terminal) launcher (R12) ✅
Two gaps from R11: recommendation triggers were ephemeral (a watch you were waiting on vanished
when the pipeline reran), and running Argus meant leaving a terminal window open.
- **Watch button:** each watch rec's expander now has 👁 **Watch this trigger** / ✕ Stop watching.
  Pinning copies the `entry_trigger` into `storage/entry_watch.py` (`add_pinned`/`remove_pinned`/
  `is_pinned`/`get_pinned`), where it survives pipeline reruns. The level is stored exactly as
  pinned — deliberately NOT refreshed if the ticker reappears in a later run (it's the level the
  user chose). Unpinning also clears its notify record so re-pinning can alert again.
- `alerts/entry_checker.py`: `_candidates_from_pinned()` + `_dedupe_by_ticker()` (priority
  **pinned > chat > recommendation**) so a pinned ticker that's also in today's fresh recs fires
  once, not twice. Alert message names the source ("your pinned watch list").
- **Silent launcher:** `argus_silent.vbs` runs `argus.bat` with the window hidden (background /
  always-on; shortcut it into `shell:startup` to auto-start at login). `argus_stop.bat` kills
  whatever holds 8501/8502 since there's no window to close. `argus.bat` unchanged — its
  reuse-if-running guard means launching twice is still safe.
- Verified: pin → dedupe (pinned wins, candidate count stays 11, no double alert) → unpin.
  34 tests passing. Docs: CLAUDE.md key files + alerts section.
- **Still open:** nothing schedules `alerts/run_checks.py` yet, so alerts only fire if run manually.

### 18. Entry ("buy when") alerts + email verification (R11) ✅
Email notification **verified working** (Gmail SMTP, test alert delivered). Added the missing
bullish half of alerting: until now Argus only told you when to get OUT.
- `alerts/entry_checker.py` (new): `_parse_triggers` pulls every (direction, price) out of a trigger
  string — a two-sided R7 trigger ("pulls back to support near $314.91 ... or breaks above $328.04")
  yields both a `below` and an `above` condition; direction is decided by whichever keyword sits
  closest before each `$`. `_is_hit` fires breakouts on `>=` and pullbacks on `<=`. `run_entry_checks`
  pulls candidates from **today's recommendations** (`pipeline_cache.json` watch `entry_trigger`) and
  **Argus chat's last suggestion**, skips owned tickers, one alert per ticker/source per day,
  session-aware. No LLM tokens.
- `storage/entry_watch.py` (new): persists chat's last buy/watch set (each capture REPLACES the
  previous — alerts track the *last* suggestion) + the per-day notify record.
- `dashboard/app.py`: `_capture_chat_suggestions` parses `Buy —`/`Watch — TICKER` lines out of the
  chat reply (uppercase-ticker only, so prose like "Buy the dip" never registers) and stores them;
  hooked into the `/chat` proxy on a 200, fully guarded so capture can never break a reply.
- `alerts/notifier.py`: `entry_trigger` styled as teal "🎯 Buy Trigger"; subject + header now adapt
  (exit-only / buy-trigger-only / mixed) instead of hardcoded "Exit Alert".
- `alerts/run_checks.py`: runs exit + entry, one combined email; entry block separately guarded so
  its failure can't drop exit alerts. Log header renamed to ALERT CHECK.
- Tests: +3 (two-sided parse, direction/edge cases, hit logic). 34 passing (was 31).
- Verified live: 11 recommendation triggers parsed + priced via Robinhood, 0 fired (all prices sat
  inside their bands — correct). Mixed exit+entry test email delivered.
- `.gitignore`: `entry_watch.json` (local data).
- **Follow-up — alert clarity:** a two-sided trigger has two valid entries, and an alert quoting
  only one looked like an invented price (user saw $328.04 in a test email but remembered $314.91
  from the recommendation — both are real, the pullback and breakout legs of the same GOOGL trigger).
  The alert message now names which leg fired (`BREAKOUT`/`PULLBACK`), lists the other level and
  that it wasn't hit, and labels the full trigger text; `leg` + `all_levels` added to the alert dict.

### 17. Scorecard reset + chat action-list & order-type awareness (R10) ✅
Earlier non-final Argus runs polluted `positions.json` — a $150 deposit was recorded as trade P&L,
so the scorecard read as +$174 when the account was actually ~$7 down. Reset to a clean baseline and
sharpened chat's output.
- **Data reset:** backed up `positions.json` → `positions_backup_<ts>.json`, then dropped all 29
  closed positions, keeping only the 5 real open ones (GOOG, PAYO, ACA, IWM, AVGO) as the fresh
  scorecard baseline. Scorecard now shows nothing until these close (dashboard already guards
  `if closed_positions`). No code change to `analysis/scorecard.py` — it just has clean input now.
- **Chat action-list-first:** `dashboard/app.py` `ARGUS_SYSTEM_BASE` — "what moves / what should I
  do / review my portfolio" asks now open with a one-line-per-ticker `Buy/Sell/Hold/Watch — TICKER,
  rule` list (every open position + any new buy), then `Explanation:`. `/chat` `max_tokens` 300 → 450.
- **Fractional order rule:** `_build_argus_context` tags each position with share count
  (`amount_invested / ref_price`); `ARGUS_SYSTEM_BASE` tells Argus a FRACTIONAL (<1 share) holding
  is market-order-only (Robinhood blocks limit/stop on fractional) — never advise a limit/stop on it.
- Docs: CLAUDE.md chatbot section + this entry.

### 16. Budget = live buying power, manual budget removed (R9) ✅
The manual "Investment budget ($)" number_input could disagree with the user's real Robinhood
cash and confused Argus chat (two competing "budget" numbers). Removed it entirely; live buying
power is now the single source of truth for sizing.
- `dashboard/app.py`: deleted `BUDGET_FILE`/`MIN_BUDGET`/`save_budget`/`load_budget` and the
  number_input + experimental caption. Added `_effective_budget()` → live buying power (60s TTL)
  or 0.0 when unreadable. Sidebar now shows the budget read-only ("Budget = live buying power").
  Replaced "Sync budget to buying power" with "💵 Refresh buying power" (forces the cache refresh).
  Chat context dropped the `CURRENT BUDGET` line and labels buying power as THE budget.
- Fallback: buying power unreadable (not connected / read fail) → budget 0.0, recs show at $0
  (analysis still visible), sidebar hints to connect Robinhood. No phantom number ever sizes trades.
- `budget.json` is now orphaned/deprecated (left on disk, harmless; no longer read or written).
- Docs: CLAUDE.md (key files, Budget Allocation, Header, Chatbot) + this entry. Tests unaffected
  (31 passing — allocation math is budget-agnostic; only the budget SOURCE changed).
- Tradeoff noted to user: lose the ability to deploy only PART of buying power. Offered an optional
  "deploy X%" cap as a later add if wanted — not built.

### 15. Pyramid risk-tier allocation + chat tune (R8) ✅
Replaced the monotonic risk-multiplier sizing (more risk = less money) with a **risk pyramid**:
the risk tier picks the budget POOL, conviction sizes within it. `PYRAMID_TIERS = {high:0.20,
medium:0.55, low:0.25}` — top satellite / core / base, most of the money in the medium core.
Empty tier is **held as cash** (user's choice), not redistributed, so invested < budget is normal.
- `calculator/portfolio.py`: `PYRAMID_TIERS`, `_tier_of`, `_pool_weight` (conviction × HR, risk
  handled by the tier), `_allocate_pool` (splits a pool, in-tier single-name-cap spill), rewrote the
  buys block to iterate tiers. `_compute_weight`/`RISK_MULTIPLIERS` now shorts-only. Shorts sleeve
  and the $10 floor unchanged. `print_allocation_table` shows a HELD AS CASH line.
- `dashboard/app.py` chatbot: system prompt trimmed + given the pyramid sizing rule and the
  discipline-leak nudge; brevity enforced (2-3 sentences, lead with the call); `/chat` max_tokens
  512 → 300. Both an explicit ask (integrate pyramid into chat + shorter/cheaper replies).
- `tests/test_deterministic.py`: +3 pyramid cases (20/55/25 split, empty-tier cash, in-core cap).
  31 passing (was 28).
- Split (20/55/25) and empty-tier=cash chosen by the user. NOTE: allocation shape ≠ the P&L fix —
  the real leak is behavioral (early closing, see #14); pyramid is risk management, not a profit lever.

### 14. Performance scorecard — diagnose "breaking even" ✅
User reported ~1 month of trading netting flat. Computed real closed-trade stats and found the
truth: NOT a bad-picks problem — a **discipline leak**. 17 closed trades, 41% win rate, but
payoff ratio 6.35 (avg win +6.98% vs avg loss −1.10%) and net **+$173.92**. Two trades (MU +$116,
AMD +$50) = ~96% of profit. The smoking gun: the **4 trades let run to target/stop averaged +8.9%;
the 13 manually closed *before* either band averaged +0.26%** (scratch). User is cutting winners
*and* losers early at breakeven — that's what flattens P&L, not the signal.
- `analysis/scorecard.py` (new, pure logic): `parse_band` (target/stop % out of exit_condition,
  handles "target X%" and "X% gain" forms), `classify_exit` (target/stop/early vs the trade's own
  band, 90% tolerance), `compute_scorecard` (win rate, payoff ratio, profit factor, expectancy,
  net $, top-trade profit share = concentration, and the let-run-vs-closed-early discipline split),
  `spy_benchmark` (yfinance, best-effort: same dollars in SPY over same dates → opportunity cost).
- `dashboard/app.py` My Positions → Closed positions: replaced the 5 vanity metrics with the
  scorecard — Net/Win rate/Profit factor/Payoff/Expectancy, a red **discipline-leak** callout when
  let-run avg ≫ early avg, a concentration warning when one trade is ≥50% of gross profit, and a
  SPY opportunity-cost line (Argus beat SPY by $179 over these dates).
- `tests/test_deterministic.py`: +3 cases (parse_band, classify_exit, scorecard discipline +
  concentration). 28 passing (was 25).
- Did NOT tighten the buy funnel (offered earlier) — the data shows the buy list isn't the leak
  (avg loss −1.1%, nowhere near stops); hand-closing is. Adding prompt tweaks would be cargo-cult.

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

### Agent judgment — PLAN (approved 2026-09-28, not built)
**Problem (audit):** the agent makes ZERO LLM calls. Every decision is a fixed rule over the pipeline's 6:30 AM
batch: buy/short recs → route by conviction (≥75 options, else shares, short → puts) → first matching playbook
(options_strategies) → pyramid/caps/PDT sizing; exits = fixed option_exit_decision / equity_exit_decision. Watches
are ignored; news on held positions is never read; the broker review only blocks broker-level problems. Versus the
user following the recs by hand, it adds execution discipline (20-min exits, no early closes), option expression
and sizing math — NOT judgment. Pipeline track record: 41 closed, 51% win, avg +0.8%.
**Principle:** hard safety stays mechanical (trading_guards, caps, PDT, broker review). The judge may only SKIP,
SHRINK (≤1× planned size), TIGHTEN a stop or EXIT — never upsize or loosen. Judgment must be FRESH (at decision
time), POSITION-AWARE and DATA-GROUNDED (every input from a live read, never model memory).
**Decisions (user, 2026-09-28):** authority = skip/shrink/tighten/exit only; model = Sonnet (`config.CLAUDE_MODEL`);
shadow 2–4 weeks before binding; agent-originated ideas IN scope (J6).
- ~~**J0 decision log**~~ — done #57 (outcomes are linked, not back-filled: an exit record carries pnl_pct +
  `entry_id`, keeping the file append-only).
- ~~**J1 context builder**~~ — done #58.
- ~~**J2 entry judge**~~ — done #59 (shadow).
- ~~**J2b watch triggers → judge candidates**~~ — done #60.
- **Pipeline LLM cost isn't in the credit ledger** — `claude_analyst.run_analysis` never calls
  `llm_budget.record_cost` (only chat + the judge do), so "LLM credit left" overstates what's left.
- ~~**J3 holding review**~~ — done #61 (the "invalidation level hit" trigger was dropped: the entry judge's
  invalidation is free text, not a parseable level — the review instead SHOWS it to the judge on every event).
- **J4 shadow (2–4 weeks from 2026-09-28)** — scoring tools BUILT (#62: `analysis/judge_scorecard.py`,
  `scripts/judge_review.py`, Agent tab scorecard). Remaining: accumulate ≥15 judged 'enter' + ≥15 'wait/skip' with
  5-day outcomes, then read the conclusion and decide J5. Data only accrues on days with candidates.
- **J5 binding** — only if J4 shows the judge helps; its skips/shrinks/exits become real. Kill switch = config flag.
  Also then decide whether to re-enable `short_dte_momentum` (disabled #63 until the judge is proven).
- **J6 own ideas** — scanner candidates (`create_scan`/`run_scan`/`get_scanner_*`, real data) through the SAME judge
  + shadow, tagged `source: agent-scan` in the log so their results are measured separately from pipeline ideas.
**Cost:** a few Sonnet calls/day, only when a candidate/event exists (~$0.02–0.05 each); credit ledger halts entries.
**Honest bar:** expect the judge to mostly SKIP. If shadow data shows its skips were no better than its takes,
turn it off.

### Stock trading for the agent — PLAN (approved 2026-09-27, not built)
Goal: the autonomous agent also trades SHARES on the agentic account, alongside options.
**Decisions (user):** route by conviction — highly_recommended or conviction ≥75 → options (aggressive leg),
other pipeline buys → stocks (core); **shared buying-power pool**, entries processed **best idea first across
both legs** each cycle; stock sizing = Argus pyramid (R8) + 40% single-name cap via `trading_guards.check_order`;
**no paper stage** — DRY_RUN then one tiny live trade (S6) as a hard gate before scheduling.
**Facts that shape it:** account `limited_margin` + Robinhood has no retail short selling → stocks LONG-ONLY
(bearish stays puts). BP ~$42 → mostly FRACTIONAL (`dollar_amount` market orders); Robinhood allows stop/limit
only on whole shares → fractional exits are poll-based (PC must be on), whole shares also get a resting GTC stop
(`alerts/agentic_stops.py`, kept for this). `robinhood_mcp.place_order` (equity) is NOT review-first yet.
Pipeline-cache allocations are sized on the MAIN account's BP → recompute the pyramid on AGENTIC BP.
Honest bar: Argus stock picks measured ~net-flat; this automates discipline + 24/7 exits, not edge.
- **S0 verify (no money) — DONE 2026-09-27** (tool schemas + `review_equity_order` probes on F; nothing placed):
  - **Fractional/dollar:** only `type=market` + `regular_hours`. `dollar_amount` min **$1.00** (review returns
    `EQUITY_DOLLAR_BASED_MINIMUM_AMOUNT_ERROR`); `dollar_amount` + `extended_hours` = HARD error ("fractional and
    dollar-based orders are only allowed in regular_hours"). ⇒ fractional stop/limit is NOT allowed (tool docs);
    fractional exits are poll-based market sells, regular hours only.
  - **Review is ADVISORY and INCOMPLETE:** problems come back as a single soft `data.order_checks.alertType`, not
    an error — and it is NOT a full validator (a fractional LIMIT buy reviewed fine except an unmarketable-price
    alert, though the rules forbid it). ⇒ S1 must (a) enforce order-shape rules locally and (b) BLOCK on alert
    types, not just print them.
  - **Stop + limit on same shares:** can't probe with no holdings; the sell alert
    `EQUITY_MAX_SELL_SHARES_EXCEEDED` reports `sharesPendingSell`, so open sell orders reserve shares → assume
    NO (cancel the resting stop before any other sell). Confirm in S5 with a real whole share.
  - **PDT:** no API field (`get_accounts` has none; `limited_margin` counts as a margin account, equity $41.92
    < $25k) ⇒ treat PDT as APPLYING: ≤3 day trades per rolling 5 business days, shared by options + stocks.
    Review lists PDT among its pre-trade alerts — block on it as a backstop, don't rely on it.
  - **Existing bugs found (fix in S1):** (1) ✅ FIXED (#46) `place_option_order` read `preview.get("order_checks")`
    but the payload is `{"data": {"order_checks": …}}` → option review alerts ALWAYS logged "none", never blocked.
    (2) ✅ FIXED (#47) `_order_args` could send BOTH `dollar_amount` and `quantity`. (3) ✅ FIXED (#47) `ref_id` was
    `client_id` (deterministic, e.g. `stop-F-11.5`) and options sent none — now a fresh UUID per placement.
- **S1 foundations:** ~~review-first equity `place_order` + whole-share limit buys in check_order~~ (done #49);
  ~~order-shape validation + UUID ref_id~~ (done #47); ~~pure `equity_exit_decision()`~~ (done #50 — S3 feeds it
  the rec's exit_condition + a `peak_tracker` key like `eq:TICKER`); ~~shared day-trade counter guard~~ (done #48 — stock entries in S2 must share `_entry_budget`). Unit tests.
- ~~**S2 routing + entries (DRY_RUN)**~~ — done #51 (all share buys = dollar market orders, not whole-share limits:
  an unfilled resting limit would read as un-held next cycle and invite a duplicate buy).
- ~~**S3 exits**~~ — done #53 (agent-opened positions only; resting stop at the PLAN's stop from entry, not the
  ATR-from-current stop that `agentic_stops.sync_protective_stops` computes — that script stays manual-only).
- ~~**S4 dashboard**~~ — done #54.
- **S5 tiny live (user runs it):** one ~$5 fractional buy → confirm fill → confirm the agent's exit sells it in
  the app. Only then add the stock leg to the scheduled cycle.

### Perf follow-ups (refactor plan A–E finished: #39–#42; D skipped — st.tabs switching is already client-side)
- Redundant MCP calls per agent cycle: `get_option_positions` ×2 (exits + `_held_underlyings`) and
  `get_portfolio` ×2 — now ~0.3s each on the shared session; dedup within a cycle is optional.
- Not worth it (measured): `_compute_technicals` (0.9ms warm; 469ms was the one-time pandas import); lazy
  `anthropic` import (1.1s, scheduler cold start only).

### R27. Autonomous options agent — Phase 2 (BUILT, DRY_RUN; live pending)
Agentic account approved for option_level_2 (2026-08-14). Built + verified in DRY_RUN:
- `ingestion/options_data.py` (contract selection), `trading_guards.OptionOrderIntent`/
  `check_option_order`, `robinhood_mcp.place_option_order` (leg schema, review-first, is_error, agentic).
- `analysis/options_strategies.py` (short_dte_momentum + catalyst_momentum, exit policy).
- `alerts/agentic_options.py` `run_options_agent()` (entries+exits, kill switch `agentic_halt.flag`,
  market-hours gate) + `scripts/agentic_options.py`.
Uncapped position size (disposable pilot); guards are correctness-only. 71 tests. Verified DRY_RUN end
to end (F 15C via strategy fallback, review clean, logged not sent).
**Remaining:** (1) run a few DRY_RUN cycles during market hours + inspect; (2) verify exit-path field
mapping against a REAL option position (avg_open_price/expiration shapes unconfirmed — no positions
existed at build); (3) tiny live cycle (DRY_RUN off, market hours); (4) scheduler (Task Scheduler,
market-hours gated, like run_checks) + document the kill switch. Blunt: uncapped short-DTE options on an
unproven signal is high-variance / likely -EV — pilot can go to zero by design.

### R26. Agentic control tab (PARKED — future feature)
A dashboard tab to control the agentic account directly: account view (buying power/positions/open
orders), a deterministic order ticket (ticker/side/type/$ or shares/price → review_equity_order →
confirm → place, DRY_RUN-aware), and a "🛡️ Protect positions" button (runs
`alerts.agentic_stops.sync_protective_stops`). Design A / confirm-first — NOT a free-text prompt agent
(that's Design B: ~20x tokens + LLM order-misfire risk). Optional later layer: NL → drafts an order
ticket the user confirms (LLM suggests, never auto-executes). Stocks first; options deferred (R5).
Parked 2026-08-14 — revisit after the pilot proves the stop flow with a real agentic position.


### R25b. Robinhood MCP — authenticated, reads wired + proven; dashboard swap + exits pending
**DONE (2026-08-14):** funded a dedicated Agentic account + authenticated via `scripts/mcp_login.py`
(DCR + PKCE + refresh token → 429 dead). All gates resolved (see CLAUDE.md R25): native stop orders
exist (#1), reads span the MAIN account, orders agentic-only (#4). `ingestion/robinhood_mcp.py` reads
(`fetch_positions`/`fetch_buying_power`/`fetch_quotes`) wired to the real tools and **verified live**
(read main buying power $150 + 6 positions with P&L). Orders wired (`place_equity_order`, native stop
mapping) but DRY_RUN. 62 tests green. Nothing in the app calls it yet (`USE_MCP=False`).
Remaining:
1. ✅ **DONE — dashboard reads swapped to MCP (429 gone in-app).** New `ingestion/account_reads.py`
   dispatcher (MCP when `USE_MCP`, else robin_stocks, no cross-fallback) backs buying power / positions /
   quotes in `dashboard/app.py` + `ingestion/prices.py`; `main.py` skips robin_stocks news under USE_MCP so
   no login fires. `config.USE_MCP=True` on this branch (reads MAIN account = the real book; agentic =
   pilot). Verified live. `git checkout main` / `USE_MCP=False` reverts.
2. ✅ **DONE (capability) — auto-exit native stops.** `alerts/agentic_stops.py`
   (`sync_protective_stops`) places a standing GTC `stop_market` per AGENTIC-account position:
   ATR stop % from R24 structure, confirm-first via `review_equity_order`, DRY_RUN-safe +
   trading_guards, whole-share only. `scripts/agentic_stops.py` runs it. Verified end-to-end
   (simulated 1-sh F holding → real structure stop @ $13.07, live review, DRY_RUN place). Main
   book stays manual (MCP can't order it). **Remaining:** hook it to a dashboard button / the
   15-min runner, and flip DRY_RUN=False after a live pilot position exists.
3. **DRY_RUN diff** several clean days → ONE tiny live confirm-first order → then enable.
   Confirm-first + tiny size until the edge beats SPY across >2 trades.
4. ✅ **DONE** — silenced the SDK's benign `Session termination failed: 400` teardown warning
   (`mcp_auth.py` lowers that logger to ERROR).
5. **Sync positions button** ✅ — synced holdings get R24 structure exits (not flat 10/5).
Full detail in the plan file `~/.claude/plans/crystalline-sniffing-kurzweil.md`.

### B1. Robinhood MCP sync
Official read-only position import via agent.robinhood.com MCP instead of
the unofficial robin_stocks library. More stable long-term.

### B2. Reactive loading screen
Show ingestion source icons in real-time during pipeline run so the user
can see progress. Currently just a spinner. Deferred — complex to implement
with Streamlit's execution model.
