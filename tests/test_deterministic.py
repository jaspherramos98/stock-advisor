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
