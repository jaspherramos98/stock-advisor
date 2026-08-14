"""
Deterministic unit tests for Argus — pure logic, no network, no secrets, no LLM.

Covers the formula/data layer the analyst reasons over: technicals, ETF rotation,
key price levels, market regime helpers, fund/crypto extractors, market-session
calendar, portfolio allocation, and the analyst's deterministic guards. These are the
parts that CAN be validated (the LLM judgment layer cannot be unit-tested).

Run: pytest
"""
import datetime as dt

from ingestion.prices import _compute_technicals, _compute_rrg, _compute_key_levels
from ingestion.fundamentals import _pct, _extract_fundamentals
from ingestion.etf_facts import _expense_pct, _extract_etf_facts
from ingestion.coingecko import _extract_market_data
import market_hours as mh
from calculator.portfolio import calculate_allocations, _compute_weight, MIN_ALLOCATION_BUDGET
from analysis.claude_analyst import _summarize_track_record, _filter_recommendations
from backtest.exit_backtest import simulate_trade, summarize
from analysis.scorecard import parse_band, classify_exit, compute_scorecard
from alerts.entry_checker import _parse_triggers, _is_hit
from alerts.exit_checker import _relevant_new_headlines
from chat_budget import trim_history, SYSTEM_BASE_MIN_CHARS
from trading_guards import (
    OrderIntent, GuardState, check_order, record_placed,
    MAX_ORDERS_PER_DAY, DAILY_LOSS_LIMIT_PCT, MIN_BUYING_POWER, MAX_SINGLE_ORDER_FRACTION,
)


# ── technicals ──────────────────────────────────────────────────────────────
def test_technicals_uptrend():
    closes = [float(i) for i in range(1, 211)]          # 210 pts → SMA50 + SMA200 both exist
    t = _compute_technicals(closes, closes, closes, [1000] * len(closes))
    assert t["rsi_14"] == 100.0                          # no losses → max
    assert t["price_vs_sma50"] == "above"
    assert "golden cross" in t["ma_trend"]               # rising → SMA50 > SMA200

def test_technicals_insufficient_history():
    t = _compute_technicals([1, 2, 3], [3, 4, 5], [0, 1, 2], [10, 10, 10])
    assert t["rsi_14"] is None and t["macd_state"] is None


# ── ETF relative rotation (RRG) ───────────────────────────────────────────────
def test_rrg_leading_vs_lagging():
    n = 160
    bench = [100.0] * n
    up = []; v = 100.0
    for i in range(n):
        v *= 1.001 if i < 100 else 1.004; up.append(v)
    r = _compute_rrg(up, bench)
    assert r["rs_ratio"] > 100 and r["rel_perf_63d"] > 0 and r["quadrant"] == "Leading"

    down = []; v = 100.0
    for i in range(n):
        v *= (1 - (0.0005 + i * 0.00004)); down.append(v)
    r2 = _compute_rrg(down, bench)
    assert r2["rs_ratio"] < 100 and r2["rs_momentum"] < 100 and r2["quadrant"] == "Lagging"

def test_rrg_insufficient_history():
    assert _compute_rrg([1, 2, 3] * 5, [1] * 15)["quadrant"] is None


# ── key price levels (R7 entry-trigger anchoring) ─────────────────────────────
def test_key_levels():
    k = _compute_key_levels(100, 105, 95, 120, 80, 98, 90, 2.0)
    assert k["atr_abs"] == 2.0
    assert k["nearest_resistance"] == 105 and k["nearest_support"] == 98
    assert k["breakout_buy"] == 106.0 and k["pullback_buy"] == 98

def test_key_levels_no_resistance_above():
    k = _compute_key_levels(200, 105, 95, 120, 80, 98, 90, 1.0)
    assert k["nearest_resistance"] is None and k["breakout_buy"] is None
    # No overhead resistance → no resistance-anchored target (blue sky); stop still sized.
    assert k["target_pct_resist"] is None and k["reward_risk"] is None
    assert k["stop_pct_atr"] == 2.0  # max(2.0, 1.75*1.0)

def test_key_levels_exit_anchoring():
    # last=100, nearest resistance 105 (+5%), avg daily range 2% → ATR stop 1.75*2=3.5%
    k = _compute_key_levels(100, 105, 95, 120, 80, 98, 90, 2.0)
    assert k["target_pct_resist"] == 5.0        # (105-100)/100
    assert k["stop_pct_atr"] == 3.5             # 1.75 * 2.0
    assert k["reward_risk"] == round(5.0 / 3.5, 2)  # ~1.43 → weak setup (< 2)

def test_key_levels_stop_scales_with_volatility():
    # a calm name (0.8% ADR) floors at 2%; a volatile one (5% ADR) gets a wide stop
    calm = _compute_key_levels(100, 110, 90, 120, 80, 95, 90, 0.8)
    vol  = _compute_key_levels(100, 110, 90, 120, 80, 95, 90, 5.0)
    assert calm["stop_pct_atr"] == 2.0          # floored (1.75*0.8=1.4 < 2)
    assert vol["stop_pct_atr"] == 8.8           # 1.75 * 5.0 — much wider, per volatility
    assert vol["stop_pct_atr"] > calm["stop_pct_atr"]


# ── fundamentals extractor ────────────────────────────────────────────────────
def test_pct_helper():
    assert _pct(0.23) == 23.0
    assert _pct(None) is None and _pct("x") is None

def test_extract_fundamentals():
    f = _extract_fundamentals({"sector": "Technology", "profitMargins": 0.25, "marketCap": 3e12})
    assert f["sector"] == "Technology" and f["profit_margin_pct"] == 25.0
    assert _extract_fundamentals({}) == {}


# ── ETF facts (unit normalization) ────────────────────────────────────────────
def test_expense_pct_normalization():
    assert _expense_pct(0.08) == 0.08      # already-percent
    assert _expense_pct(0.0008) == 0.08    # decimal fraction → ×100
    assert _expense_pct(None) is None

def test_extract_etf_facts_units_and_dropped_ytd():
    d = _extract_etf_facts({"category": "Technology", "yield": 0.004, "annualReportExpenseRatio": 0.0009})
    assert d["yield_pct"] == 0.4 and d["expense_ratio_pct"] == 0.09
    assert "ytd_return_pct" not in d


# ── crypto market data extractor ──────────────────────────────────────────────
def test_extract_market_data():
    row = {"current_price": 67000.1, "market_cap_rank": 1,
           "price_change_percentage_7d_in_currency": 5.4, "ath_change_percentage": -8.5}
    m = _extract_market_data(row)
    assert m["market_cap_rank"] == 1 and m["change_7d_pct"] == 5.4 and m["pct_from_ath"] == -8.5
    assert _extract_market_data({})["price"] is None


# ── market session / NYSE calendar ────────────────────────────────────────────
def test_market_session_states():
    assert mh.market_session(dt.datetime(2026, 6, 22, 10, 0))["status"] == "open"      # Mon 10am
    assert mh.market_session(dt.datetime(2026, 6, 22, 7, 0))["status"] == "pre_market"  # Mon 7am
    assert mh.market_session(dt.datetime(2026, 6, 20, 12, 0))["status"] == "closed_weekend"  # Sat
    assert mh.market_session(dt.datetime(2026, 7, 3, 11, 0))["status"] == "closed_holiday"   # Jul3 observed
    # half day: Friday after Thanksgiving 2026 = Nov 27
    assert mh.market_session(dt.datetime(2026, 11, 27, 11, 0))["status"] == "open_half_day"

def test_nyse_holidays_2026():
    h = set(mh.nyse_holidays(2026))
    expected = {dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16), dt.date(2026, 4, 3),
                dt.date(2026, 5, 25), dt.date(2026, 6, 19), dt.date(2026, 7, 3), dt.date(2026, 9, 7),
                dt.date(2026, 11, 26), dt.date(2026, 12, 25)}
    assert h == expected


# ── portfolio allocation ──────────────────────────────────────────────────────
_RECS = [
    {"ticker": "AAPL", "company_name": "Apple", "direction": "buy", "conviction": 80,
     "risk_level": "low", "highly_recommended": True, "exit_condition": "target 15% gain, stop loss at 5%"},
    {"ticker": "NVDA", "company_name": "Nvidia", "direction": "buy", "conviction": 60,
     "risk_level": "medium", "exit_condition": "target 8% gain, stop loss at 3%"},
    {"ticker": "XYZ", "company_name": "Xyz", "direction": "watch", "conviction": 40,
     "risk_level": "medium", "exit_condition": "target 6% gain, stop loss at 3%"},
]

def test_allocation_floor_below_ten_is_zero():
    out = calculate_allocations(_RECS, 5)
    assert len(out) == 3 and sum(r["dollar_amount"] for r in out) == 0  # shown, not allocated

def test_allocation_runs_at_floor():
    out = calculate_allocations(_RECS, 1000)
    assert sum(r["dollar_amount"] for r in out) > 0

def test_allocation_zero_budget_shows_recs_at_zero():
    # Budget 0 = buying power unreadable (Robinhood down). The day's analysis must still
    # show at $0, not vanish — same as any sub-floor budget.
    out = calculate_allocations(_RECS, 0)
    assert len(out) == 3 and sum(r["dollar_amount"] for r in out) == 0

def test_allocation_no_recs_empty():
    assert calculate_allocations([], 1000) == []

def test_compute_weight_conviction_drives_size():
    hi = _compute_weight({"conviction": 90, "risk_level": "low"})
    lo = _compute_weight({"conviction": 30, "risk_level": "low"})
    assert hi > lo
    # back-compat: missing conviction falls back to confidence_score×100
    assert _compute_weight({"confidence_score": 0.5, "risk_level": "low"}) == 0.5


# ── pyramid tier allocation (20/55/25, empty tier = cash) ─────────────────────
_PYR = [
    {"ticker": "H1", "direction": "buy", "risk_level": "high",   "conviction": 80,
     "exit_condition": "target 15% gain, stop loss at 6%"},
    {"ticker": "M1", "direction": "buy", "risk_level": "medium", "conviction": 70,
     "exit_condition": "target 8% gain, stop loss at 4%"},
    {"ticker": "M2", "direction": "buy", "risk_level": "medium", "conviction": 30,
     "exit_condition": "target 8% gain, stop loss at 4%"},
    {"ticker": "L1", "direction": "buy", "risk_level": "low",    "conviction": 60,
     "exit_condition": "target 5% gain, stop loss at 2%"},
]

def test_pyramid_pools_split_20_55_25():
    by = {r["ticker"]: r["dollar_amount"] for r in calculate_allocations(_PYR, 1000)}
    assert by["H1"] == 200.0                       # high pool 20%, sole name
    assert by["M1"] == 385.0 and by["M2"] == 165.0  # core 55% split 70/30 by conviction
    assert by["L1"] == 250.0                       # base pool 25%, sole name
    assert round(sum(by.values()), 2) == 1000.0    # all tiers populated → fully invested

def test_pyramid_empty_tier_held_as_cash():
    # only core (medium) ideas → only the 55% pool is invested, tails held as cash
    core_only = [r for r in _PYR if r["risk_level"] == "medium"]
    out = calculate_allocations(core_only, 1000)
    assert round(sum(r["dollar_amount"] for r in out), 2) == 550.0

def test_pyramid_single_name_cap_within_core():
    # one dominant core name → capped at MAX_SINGLE_ALLOCATION (40% of budget = $400),
    # the excess spills to the other core name (not across tiers).
    recs = [
        {"ticker": "BIG",   "direction": "buy", "risk_level": "medium", "conviction": 90,
         "exit_condition": "target 8% gain, stop loss at 4%"},
        {"ticker": "SMALL", "direction": "buy", "risk_level": "medium", "conviction": 10,
         "exit_condition": "target 8% gain, stop loss at 4%"},
    ]
    by = {r["ticker"]: r["dollar_amount"] for r in calculate_allocations(recs, 1000)}
    assert by["BIG"] == 400.0 and by["SMALL"] == 150.0   # 40% cap; rest of core pool to SMALL


# ── analyst deterministic guards ──────────────────────────────────────────────
def test_summarize_track_record():
    closed = [{"direction": "buy", "pnl_pct": 12.0}, {"direction": "buy", "pnl_pct": -4.0},
              {"direction": "short", "pnl_pct": 6.0}, {"direction": "buy", "pnl_pct": None}]
    tr = _summarize_track_record(closed)
    assert tr["total"] == 3                      # None excluded
    assert tr["by_direction"]["buy"]["count"] == 2
    assert _summarize_track_record([])["total"] == 0

def test_filter_recommendations_drops_owned_and_vague():
    recs = [
        {"ticker": "AAPL", "exit_condition": "target 8% gain, stop loss at 3%"},   # keep
        {"ticker": "MSFT", "exit_condition": "target 8% gain, stop loss at 3%"},   # owned → drop
        {"ticker": "TSLA", "exit_condition": "watching for deal clarity"},          # vague → drop
        {"ticker": "",     "exit_condition": "target 5% gain, stop loss at 2%"},    # no ticker → drop
    ]
    out = _filter_recommendations(recs, [{"ticker": "MSFT"}])
    assert [r["ticker"] for r in out] == ["AAPL"]


# ── exit-band backtester ──────────────────────────────────────────────────────
def test_simulate_long_target_hit():
    # day 2 high reaches +8% on a 100 entry
    r = simulate_trade([102, 109, 103], [99, 105, 101], [101, 108, 102],
                       100, target_pct=8, stop_pct=3, max_days=3)
    assert r["outcome"] == "target" and r["pnl_pct"] == 8 and r["days"] == 2

def test_simulate_long_stop_hit():
    r = simulate_trade([101, 100], [99, 96], [100, 97],
                       100, target_pct=8, stop_pct=3, max_days=5)
    assert r["outcome"] == "stop" and r["pnl_pct"] == -3 and r["days"] == 2

def test_simulate_same_bar_stop_wins_tie():
    # one bar that reaches BOTH +8% and -3% → conservative stop
    r = simulate_trade([108], [97], [100], 100, target_pct=8, stop_pct=3, max_days=1)
    assert r["outcome"] == "stop"

def test_simulate_time_exit():
    r = simulate_trade([101, 102, 103], [99, 100, 101], [100.5, 101.5, 104.0],
                       100, target_pct=20, stop_pct=20, max_days=3)
    assert r["outcome"] == "time" and r["days"] == 3 and r["pnl_pct"] == 4.0

def test_simulate_short_target_on_fall():
    # short from 100, price falls → low hits -8% target
    r = simulate_trade([101, 99], [98, 91], [99, 92],
                       100, target_pct=8, stop_pct=3, max_days=5, direction="short")
    assert r["outcome"] == "target" and r["pnl_pct"] == 8

def test_summarize_counts():
    trades = [{"outcome": "target", "pnl_pct": 8, "days": 3},
              {"outcome": "stop", "pnl_pct": -3, "days": 1},
              {"outcome": "time", "pnl_pct": 1, "days": 20}]
    s = summarize(trades)
    assert s["trades"] == 3 and s["target_hits"] == 1 and s["stop_hits"] == 1
    assert s["win_rate"] == round(2 / 3 * 100, 1)
    assert summarize([]) == {"trades": 0}


# ── performance scorecard ─────────────────────────────────────────────────────
def test_parse_band():
    assert parse_band("target 8% gain, stop loss at 4%") == (8.0, 4.0)
    assert parse_band("target 12% gain, stop loss at 4.5%") == (12.0, 4.5)
    assert parse_band("merger close or 10% gain, stop loss at 4%") == (10.0, 4.0)
    assert parse_band("no exit condition set") == (None, None)

def test_classify_exit():
    # reached ~target → let run
    assert classify_exit({"pnl_pct": 7.3, "exit_condition": "target 8% gain, stop loss at 4%"}) == "target"
    # hit the stop → taken as designed
    assert classify_exit({"pnl_pct": -3.9, "exit_condition": "target 8% gain, stop loss at 4%"}) == "stop"
    # closed near zero, well inside both bands → early scratch (the leak)
    assert classify_exit({"pnl_pct": -1.1, "exit_condition": "target 8% gain, stop loss at 4%"}) == "early"
    assert classify_exit({"pnl_pct": None, "exit_condition": "target 8% gain"}) is None

def test_parse_triggers_two_sided():
    # Real R7-style trigger: carries BOTH a pullback and a breakout price.
    t = _parse_triggers(
        "Pulls back to support near $314.91 and stabilizes with a reversal candle, "
        "or breaks above $328.04 on volume if AI concerns resolve"
    )
    assert ("below", 314.91) in t and ("above", 328.04) in t and len(t) == 2

def test_parse_triggers_directions_and_edges():
    assert _parse_triggers("Breaks above $29.25 on volume") == [("above", 29.25)]
    assert _parse_triggers("pulls back to ~$190") == [("below", 190.0)]
    assert _parse_triggers("buy above $1,250.50") == [("above", 1250.50)]
    assert _parse_triggers("") == [] and _parse_triggers("no price here") == []

def test_is_hit():
    assert _is_hit("above", 100, 101) and not _is_hit("above", 100, 99)
    assert _is_hit("below", 100, 99) and not _is_hit("below", 100, 101)
    assert _is_hit("above", 100, 100) and _is_hit("below", 100, 100)  # touching counts


# ── event-checker token gate ──────────────────────────────────────────────────
def test_relevant_new_headlines_gate():
    positions = [{"ticker": "AAPL", "company_name": "Apple Inc."}]
    news = [
        {"title": "Apple beats earnings, guidance raised", "ticker": "AAPL"},
        {"title": "Fed holds rates steady", "ticker": ""},          # irrelevant → dropped
        {"title": "Random biotech soars on trial data", "ticker": "XYZ"},  # not owned → dropped
    ]
    new, hashes = _relevant_new_headlines(news, positions, seen=set())
    assert len(new) == 1 and new[0]["ticker"] == "AAPL" and len(hashes) == 1
    # already-seen headline is skipped (no Claude call would fire)
    new2, _ = _relevant_new_headlines(news, positions, seen=hashes)
    assert new2 == []
    # no positions → nothing relevant
    assert _relevant_new_headlines(news, [], set())[0] == []


def test_compute_scorecard_discipline_and_concentration():
    closed = [
        # two let-run winners
        {"ticker": "MU",  "pnl_pct": 10.8, "pnl_dollars": 116.5, "exit_condition": "target 10% gain, stop loss at 5%"},
        {"ticker": "AMD", "pnl_pct": 10.9, "pnl_dollars": 50.6,  "exit_condition": "target 10% gain, stop loss at 6%"},
        # three early scratches inside the bands
        {"ticker": "GLD", "pnl_pct": -1.6, "pnl_dollars": -6.3,  "exit_condition": "target 8% gain, stop loss at 2.5%"},
        {"ticker": "AMZN","pnl_pct": -0.4, "pnl_dollars": -1.0,  "exit_condition": "target 6% gain, stop loss at 3%"},
        {"ticker": "LEN", "pnl_pct": -0.3, "pnl_dollars": -0.3,  "exit_condition": "target 8% gain, stop loss at 4%"},
        {"ticker": "X",   "pnl_pct": None},   # excluded (no P&L)
    ]
    sc = compute_scorecard(closed)
    assert sc["trades"] == 5
    assert sc["discipline"]["let_run_count"] == 2 and sc["discipline"]["early_count"] == 3
    assert sc["discipline"]["let_run_avg"] > sc["discipline"]["early_avg"]
    # payoff ratio > 1 (winners far bigger than the early scratches)
    assert sc["payoff_ratio"] > 1
    # MU is the dominant winner → high concentration share
    assert sc["top_trade"]["ticker"] == "MU" and sc["top_trade_profit_share"] >= 60
    assert compute_scorecard([])["trades"] == 0


# ── chat history trimming (token budget) ────────────────────────────────────
def _convo(n):
    """n messages alternating user/assistant, starting with user."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def test_trim_history_short_conversations_pass_through():
    assert trim_history([], limit=4) == []
    short = _convo(3)
    assert trim_history(short, limit=4) == short
    exact = _convo(4)
    assert trim_history(exact, limit=4) == exact


def test_trim_history_keeps_tail_and_does_not_mutate():
    convo = _convo(10)
    original = list(convo)
    out = trim_history(convo, limit=4)
    assert convo == original                      # input untouched
    assert out[-1]["content"] == "m9"             # newest message survives
    assert len(out) <= 4


def test_trim_history_always_starts_on_user():
    # A naive tail slice here lands on an assistant turn, which the API rejects with
    # a 400. The window must snap forward to the next user message.
    convo = _convo(10)                            # even idx = user, odd = assistant
    out = trim_history(convo, limit=5)            # raw tail would start at m5 (assistant)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "m6"
    assert out[-1]["content"] == "m9"

    for limit in range(1, 12):
        trimmed = trim_history(_convo(11), limit=limit)
        assert not trimmed or trimmed[0]["role"] == "user"


def test_trim_history_all_assistant_window_sends_nothing():
    # Degenerate input: better to send nothing than a guaranteed 400.
    convo = [{"role": "user", "content": "hi"}] + [
        {"role": "assistant", "content": f"a{i}"} for i in range(5)
    ]
    assert trim_history(convo, limit=3) == []


def test_argus_system_base_clears_cacheable_floor():
    # Prompt caching needs the static system block over the model's minimum prefix
    # (1024 tokens for Sonnet 4.6). ARGUS_SYSTEM_BASE measured 1,222 tokens at 4,626
    # chars — only ~16% of headroom. If someone trims the prompt below the floor,
    # caching stops working silently, so guard it here with a char-count proxy.
    import os
    import re

    app_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "app.py"
    )
    with open(app_py, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"const ARGUS_SYSTEM_BASE = `(.*?)`;", src, re.S)
    assert m, "ARGUS_SYSTEM_BASE not found — did the chat widget move?"
    assert len(m.group(1)) >= SYSTEM_BASE_MIN_CHARS, (
        f"ARGUS_SYSTEM_BASE is {len(m.group(1))} chars, below the "
        f"{SYSTEM_BASE_MIN_CHARS} floor — prompt caching will silently stop working"
    )


# --- trading_guards: order-safety layer for MCP execution (Path B) --------------------

def _buy(dollars, client_id="", ticker="ABC"):
    return OrderIntent(ticker=ticker, side="buy", dollars=dollars, client_id=client_id)


def _sell(qty, client_id="", ticker="ABC"):
    return OrderIntent(ticker=ticker, side="sell", quantity=qty, client_id=client_id)


def test_guard_allows_normal_buy():
    st = GuardState(start_equity=1000.0)
    assert check_order(_buy(100.0), st, buying_power=500.0).allowed


def test_guard_blocks_buy_over_buying_power():
    st = GuardState(start_equity=1000.0)
    r = check_order(_buy(600.0), st, buying_power=500.0)
    assert not r.allowed and "buying power" in r.reason


def test_guard_blocks_buy_below_min_buying_power():
    st = GuardState(start_equity=1000.0)
    r = check_order(_buy(5.0), st, buying_power=MIN_BUYING_POWER - 1)
    assert not r.allowed


def test_guard_enforces_single_name_cap():
    st = GuardState(start_equity=1000.0)
    # 45% of equity exceeds the 40% single-name cap even though buying power covers it.
    over = MAX_SINGLE_ORDER_FRACTION * 1000.0 + 50.0
    r = check_order(_buy(over), st, buying_power=1000.0)
    assert not r.allowed and "single-name cap" in r.reason


def test_guard_daily_order_cap():
    st = GuardState(start_equity=1000.0, orders_today=MAX_ORDERS_PER_DAY)
    r = check_order(_buy(50.0), st, buying_power=1000.0)
    assert not r.allowed and "cap" in r.reason


def test_guard_daily_loss_kill_switch():
    st = GuardState(start_equity=1000.0)
    st.day_pnl = -(DAILY_LOSS_LIMIT_PCT * 1000.0) - 1  # just past the limit
    r = check_order(_buy(50.0), st, buying_power=1000.0)
    assert not r.allowed and "loss limit" in r.reason


def test_guard_idempotency_blocks_duplicate_client_id():
    st = GuardState(start_equity=1000.0)
    intent = _buy(50.0, client_id="trade-xyz")
    assert check_order(intent, st, buying_power=1000.0).allowed
    record_placed(intent, st)
    r = check_order(_buy(50.0, client_id="trade-xyz"), st, buying_power=1000.0)
    assert not r.allowed and "duplicate" in r.reason


def test_guard_rejects_malformed_intents():
    st = GuardState(start_equity=1000.0)
    assert not check_order(_buy(0.0), st, 1000.0).allowed          # non-positive buy
    assert not check_order(_sell(0.0), st, 1000.0).allowed         # non-positive sell
    bad = OrderIntent(ticker="ABC", side="hold")
    assert not check_order(bad, st, 1000.0).allowed                # unknown side


def test_guard_sell_allowed_regardless_of_buying_power():
    st = GuardState(start_equity=1000.0)
    assert check_order(_sell(1.5), st, buying_power=0.0).allowed


def test_record_placed_increments_and_tracks():
    st = GuardState(start_equity=1000.0)
    record_placed(_buy(10.0, client_id="a"), st)
    assert st.orders_today == 1 and "a" in st.placed_client_ids


# --- robinhood_mcp: reads degrade safely, orders honor DRY_RUN + guards ---------------

def test_mcp_reads_degrade_to_empty_when_disabled(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "USE_MCP", False, raising=False)
    assert mcp.fetch_positions() == []
    assert mcp.fetch_buying_power() is None
    assert mcp.fetch_quotes(["AAPL"]) == {}


def test_mcp_reads_degrade_when_enabled_but_no_session(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "USE_MCP", True, raising=False)
    # Force the not-authenticated path deterministically (independent of ambient login):
    # every tool call raises MCPNotWired; reads must swallow it and degrade, not crash.
    def _boom(*a, **k):
        raise mcp.MCPNotWired("no session (test)")
    monkeypatch.setattr(mcp, "_call_tool", _boom)
    mcp._accounts_cache = None
    assert mcp.fetch_positions() == []
    assert mcp.fetch_buying_power() is None
    assert mcp.fetch_quotes(["AAPL"]) == {}


def test_mcp_dry_run_order_logs_not_sends(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    st = GuardState(start_equity=1000.0)
    res = mcp.place_order(_buy(100.0, client_id="d1"), st, buying_power=500.0)
    assert res["status"] == "dry_run" and st.orders_today == 1


def test_mcp_rejected_order_not_counted(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    st = GuardState(start_equity=1000.0)
    res = mcp.place_order(_buy(9999.0, client_id="d2"), st, buying_power=100.0)
    assert res["status"] == "rejected" and st.orders_today == 0


def test_mcp_normalize_positions_shape():
    # Real MCP get_equity_positions fields (average_buy_price) + a quotes map (pure, no network).
    from ingestion.robinhood_mcp import _normalize_positions
    raw = [{"symbol": "ABC", "quantity": "2", "average_buy_price": "10", "type": "long"}]
    quotes = {"ABC": {"price": 12.0}}
    out = _normalize_positions(raw, quotes)
    assert out[0]["ticker"] == "ABC" and out[0]["equity"] == 24.0 and out[0]["pnl_pct"] == 20.0


def test_mcp_normalize_positions_falls_back_without_quote():
    from ingestion.robinhood_mcp import _normalize_positions
    raw = [{"symbol": "ABC", "quantity": "2", "average_buy_price": "10", "type": "long"}]
    out = _normalize_positions(raw, {})           # no quote → current falls back to avg_cost
    assert out[0]["current_price"] == 10.0 and out[0]["equity"] == 20.0 and out[0]["pnl_pct"] == 0.0


def test_mcp_normalize_buying_power_nested():
    from ingestion.robinhood_mcp import _normalize_buying_power
    data = {"buying_power": {"buying_power": "25.0000", "display_currency": "USD"}, "cash": "25"}
    assert _normalize_buying_power(data) == 25.0
    assert _normalize_buying_power({"cash": "13.5"}) == 13.5   # fallback to cash
    assert _normalize_buying_power({}) is None


def test_mcp_normalize_quotes_shape():
    from ingestion.robinhood_mcp import _normalize_quotes
    data = {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "305.31",
                                    "previous_close": "305.26"}}]}
    out = _normalize_quotes(["AAPL", "MSFT"], data)
    assert out["AAPL"]["price"] == 305.31 and out["MSFT"] is None


def test_llm_budget_ledger(tmp_path, monkeypatch):
    import llm_budget as lb
    monkeypatch.setattr(lb, "_LEDGER", str(tmp_path / "ledger.json"))
    # unset → can_spend True (opt-in), remaining 0
    assert lb.can_spend() is True
    lb.set_balance(5.0, reserve=0.5)
    assert lb.get_state()["remaining"] == 5.0 and lb.can_spend()
    lb.record_cost(4.0)
    assert lb.get_state()["remaining"] == 1.0 and lb.can_spend()
    lb.record_cost(0.6)                       # remaining 0.4 ≤ reserve 0.5
    assert lb.get_state()["remaining"] == 0.4 and lb.can_spend() is False
    # cost math: 1M in @ $3 + 1M out @ $15 = $18; cache read billed 0.1×
    assert lb.cost_of("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    assert lb.cost_of("claude-sonnet-4-6", 1_000_000, 0, cache_read_tokens=1_000_000) == 3.3


def test_options_strategies_select_and_size():
    from analysis.options_strategies import (
        select_strategy, size_contracts, catalyst_momentum, short_dte_momentum)
    # short_dte needs conviction>=80 + risk_on; wins over catalyst when both qualify.
    hot = {"direction": "buy", "conviction": 85}
    plan = select_strategy(hot, {"risk": "risk_on"})
    assert plan["strategy"] == "short_dte_momentum" and plan["right"] == "call"
    # risk-off downgrades to catalyst_momentum (still fires on conviction>=70).
    plan2 = select_strategy(hot, {"risk": "risk_off"})
    assert plan2["strategy"] == "catalyst_momentum"
    # bearish → put
    assert select_strategy({"direction": "short", "conviction": 75}, {"risk": "neutral"})["right"] == "put"
    # too weak / non-directional → nothing
    assert select_strategy({"direction": "buy", "conviction": 60}, {"risk": "risk_on"}) is None
    assert select_strategy({"direction": "watch", "conviction": 99}, {"risk": "risk_on"}) is None

    # sizing: floor(alloc*bp/cost), min 1 if affordable, capped by bp.
    assert size_contracts(1.0, 100.0, 18.0) == 5     # floor(100/18)=5
    assert size_contracts(0.5, 100.0, 18.0) == 2     # floor(50/18)=2
    assert size_contracts(0.1, 100.0, 18.0) == 1     # floor(10/18)=0 → min 1 (affordable)
    assert size_contracts(1.0, 10.0, 18.0) == 0      # unaffordable


def test_signal_context_parsers():
    import datetime as _dt
    from ingestion.signal_context import _parse_rsi, _parse_earnings
    rsi_payload = {"data": {"indicators": [{"type": "rsi", "series": [
        {"begins_at": "2026-08-12T00:00:00Z", "value": 55.5},
        {"begins_at": "2026-08-13T00:00:00Z", "value": 28.9}]}]}}
    assert _parse_rsi(rsi_payload) == 28.9
    assert _parse_rsi({"data": {"indicators": []}}) is None

    today = _dt.date(2026, 8, 14)
    earn = {"data": {"results": [
        {"report": {"date": "2026-07-31"}, "eps": {"estimate": "1.42", "actual": "1.57"}},  # past, beat
        {"report": {"date": "2026-08-18"}, "eps": {"estimate": "1.60", "actual": ""}},       # upcoming
    ]}}
    ctx = _parse_earnings(earn, today)
    assert ctx["days_to_earnings"] == 4 and ctx["days_since_earnings"] == 14 and ctx["last_beat"] is True


def test_new_option_strategies():
    from analysis.options_strategies import (
        pre_earnings_iv, post_earnings_momentum, mean_reversion, applicable_plans)
    # pre-earnings gamble: report in 3 days, high conviction bull → call lottery
    p = pre_earnings_iv({"direction": "buy", "conviction": 80, "days_to_earnings": 3}, {})
    assert p and p["strategy"] == "pre_earnings_iv" and p["right"] == "call" and p["alloc_pct"] == 1.0
    assert pre_earnings_iv({"direction": "buy", "conviction": 80, "days_to_earnings": 20}, {}) is None
    # post-earnings drift: reported yesterday
    assert post_earnings_momentum({"direction": "short", "conviction": 75, "days_since_earnings": 1}, {})["right"] == "put"
    assert post_earnings_momentum({"direction": "buy", "conviction": 75, "days_since_earnings": 9}, {}) is None
    # mean reversion: oversold bull → call; overbought bull → nothing
    assert mean_reversion({"direction": "buy", "conviction": 65, "rsi": 30}, {})["right"] == "call"
    assert mean_reversion({"direction": "buy", "conviction": 65, "rsi": 55}, {}) is None
    assert mean_reversion({"direction": "buy", "conviction": 65, "rsi": None}, {}) is None
    # priority: earnings gamble beats plain momentum when both qualify
    sig = {"direction": "buy", "conviction": 85, "days_to_earnings": 2, "rsi": 30}
    assert applicable_plans(sig, {"risk": "risk_on"})[0]["strategy"] == "pre_earnings_iv"


def test_option_exit_decision():
    from analysis.options_strategies import option_exit_decision
    rule = {"profit_pct": 60, "stop_pct": 50, "close_dte": 2}
    assert option_exit_decision(0.20, 0.34, 20, rule)[0] == "close"   # +70% ≥ target
    assert option_exit_decision(0.20, 0.09, 20, rule)[0] == "close"   # -55% ≤ stop
    assert option_exit_decision(0.20, 0.22, 1, rule)[0] == "close"    # 1 DTE ≤ 2
    assert option_exit_decision(0.20, 0.24, 20, rule)[0] == "hold"    # +20%, healthy
    assert option_exit_decision(0.0, 0.10, 20, rule)[0] == "hold"     # no basis, DTE ok


def test_options_data_pure_helpers():
    import datetime as _dt
    from ingestion.options_data import (
        _dte, pick_expiration, pick_contract_by_moneyness, liquidity_ok, contract_cost)
    today = _dt.date(2026, 8, 14)
    dates = ["2026-08-15", "2026-08-21", "2026-09-04", "2026-09-18", "2026-11-20"]
    assert _dte("2026-08-21", today) == 7
    # window 21-40 DTE → 2026-09-04 (21d) and 2026-09-18 (35d); mid=30.5 → 09-04 closer? |21-30.5|=9.5 vs |35-30.5|=4.5 → 09-18
    assert pick_expiration(dates, 21, 40, today) == "2026-09-18"
    assert pick_expiration(dates, 200, 400, today) is None

    contracts = [{"strike_price": str(s), "state": "active", "tradability": "tradable"}
                 for s in (90, 95, 100, 105, 110)]
    call = pick_contract_by_moneyness(contracts, spot=100, right="call", otm_pct=4)  # target 104 → 105
    assert call["strike"] == 105.0
    put = pick_contract_by_moneyness(contracts, spot=100, right="put", otm_pct=4)    # target 96 → 95
    assert put["strike"] == 95.0

    assert liquidity_ok({"bid_price": "1.00", "ask_price": "1.10"})           # 9.5% spread ok
    assert not liquidity_ok({"bid_price": "0", "ask_price": "0.50"})          # no bid
    assert not liquidity_ok({"bid_price": "1.00", "ask_price": "2.00"})       # 66% spread
    assert contract_cost({"ask_price": "3.95"}, 1) == 395.0
    assert contract_cost(0.20, 2) == 40.0


def test_check_option_order():
    from trading_guards import OptionOrderIntent, GuardState, check_option_order
    st = GuardState(start_equity=25.0)
    ok = OptionOrderIntent(underlying="F", option_id="x", right="call", side="buy",
                           position_effect="open", quantity=1, price=0.10)  # $10 premium
    assert check_option_order(ok, st, buying_power=25.0).allowed
    too_dear = OptionOrderIntent(underlying="AAPL", option_id="y", right="call", side="buy",
                                 position_effect="open", quantity=1, price=3.95)  # $395
    r = check_option_order(too_dear, st, buying_power=25.0)
    assert not r.allowed and "exceeds buying power" in r.reason
    bad = OptionOrderIntent(underlying="F", option_id="z", right="stock", side="buy",
                            position_effect="open")
    assert not check_option_order(bad, st, 25.0).allowed
    assert ok.premium == 10.0


def test_option_args_shape():
    from ingestion.robinhood_mcp import _option_args
    from trading_guards import OptionOrderIntent
    intent = OptionOrderIntent(underlying="F", option_id="opt-1", right="call", side="buy",
                               position_effect="open", quantity=2, price=0.12)
    args = _option_args(intent, "AGENTIC")
    assert args["legs"][0] == {"option_id": "opt-1", "side": "buy",
                               "position_effect": "open", "ratio_quantity": 1}
    assert args["quantity"] == "2" and args["price"] == "0.12" and args["direction"] == "debit"


def test_plan_protective_stops():
    from alerts.agentic_stops import plan_protective_stops, _stop_price
    # stop price = current × (1 − pct/100)
    assert _stop_price(100.0, 4.0) == 96.0
    assert _stop_price(0, 4.0) is None and _stop_price(100.0, 0) is None

    positions = [
        {"ticker": "ABC", "shares": 3, "current_price": 100.0},     # whole → stop planned
        {"ticker": "FRAC", "shares": 0.4, "current_price": 50.0},   # fractional-only → skipped
        {"ticker": "PART", "shares": 2.7, "current_price": 20.0},   # floor to 2 whole shares
        {"ticker": "NOPCT", "shares": 5, "current_price": 10.0},    # no stop% → skipped
    ]
    stops = {"ABC": 4.0, "FRAC": 5.0, "PART": 10.0}
    out = plan_protective_stops(positions, stops)
    tickers = {i.ticker: i for i in out}
    assert set(tickers) == {"ABC", "PART"}                          # FRAC + NOPCT dropped
    abc = tickers["ABC"]
    assert abc.side == "sell" and abc.order_type == "stop" and abc.quantity == 3
    assert abc.stop_price == 96.0 and abc.client_id == "stop-ABC-96.0"
    assert abc.time_in_force == "gtc"          # protective stops MUST be GTC, not gfd
    assert tickers["PART"].quantity == 2 and tickers["PART"].stop_price == 18.0


def test_open_stops_parsing():
    from alerts.agentic_stops import _open_stops
    payload = {"data": {"orders": [
        # Real RH shape: a stop is type='market' WITH a stop_price, not type='stop_market'.
        {"id": "o1", "symbol": "ABC", "side": "sell", "type": "market", "stop_price": "96.00",
         "state": "queued", "time_in_force": "gtc"},
        {"id": "o2", "symbol": "GFD", "side": "sell", "type": "market", "stop_price": "40.00",
         "state": "queued", "time_in_force": "gfd"},                    # stale → replace
        {"id": "o3", "symbol": "XYZ", "side": "sell", "type": "limit", "state": "queued"},   # no stop_price
        {"id": "o4", "symbol": "OLD", "side": "sell", "type": "market", "stop_price": "5.00",
         "state": "cancelled"},                                          # not open
    ]}}
    stops = _open_stops(payload)
    assert set(stops) == {"ABC", "GFD"}
    assert stops["ABC"]["tif"] == "gtc" and stops["GFD"]["tif"] == "gfd"
    assert stops["GFD"]["order_id"] == "o2"


def test_mcp_order_args_includes_tif():
    from ingestion.robinhood_mcp import _order_args
    intent = OrderIntent(ticker="ABC", side="sell", quantity=1, order_type="stop",
                         stop_price=96.0, time_in_force="gtc")
    args = _order_args(intent, "AGENTIC")
    assert args["time_in_force"] == "gtc" and args["type"] == "stop_market"


def test_account_reads_dispatch(monkeypatch):
    # The dispatcher routes to robin_stocks or the MCP purely on config.USE_MCP, with no
    # cross-fallback (so USE_MCP=on can't secretly re-trigger the robin_stocks 429).
    import config
    from ingestion import account_reads
    from ingestion import robinhood_mcp, robinhood as rh

    monkeypatch.setattr(config, "USE_MCP", False, raising=False)
    monkeypatch.setattr(rh, "fetch_buying_power", lambda: 111.0)
    monkeypatch.setattr(robinhood_mcp, "fetch_buying_power", lambda *a, **k: 999.0)
    assert account_reads.buying_power() == 111.0     # robin_stocks path

    monkeypatch.setattr(config, "USE_MCP", True, raising=False)
    assert account_reads.buying_power() == 999.0     # MCP path


def test_mcp_order_args_native_stop():
    # OrderIntent 'stop' maps to the MCP's native 'stop_market' with a string stop_price.
    from ingestion.robinhood_mcp import _order_args
    intent = OrderIntent(ticker="ABC", side="sell", quantity=1.5, order_type="stop",
                         stop_price=97.5, client_id="x1")
    args = _order_args(intent, "AGENTIC123")
    assert args["type"] == "stop_market" and args["stop_price"] == "97.50"
    assert args["account_number"] == "AGENTIC123" and args["ref_id"] == "x1"
