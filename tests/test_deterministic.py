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

def test_parse_exit_condition():
    from analysis.exit_rules import parse_exit_condition as p
    assert p("target 8% gain, stop loss at 4%") == {"target_pct": 8.0, "stop_pct": 4.0, "max_days": None}
    assert p("Target 12% gain, stop-loss 4.5%, exit in 2 weeks") == {"target_pct": 12.0, "stop_pct": 4.5, "max_days": 14}
    assert p("stop at 3%, 6% rise or 10 days")["target_pct"] == 6.0          # order-independent
    assert p("target 6% or 9% gain, stop loss at 3%")["target_pct"] == 6.0   # first level hit
    # Real analyst text: explanatory %s are not targets (this picked 2.2 before the fix)
    assert p("Target 8% gain, stop loss at 4% (ATR-sized: avg daily range is 2.2%)")["target_pct"] == 8.0
    assert p("merger close or 10% gain, stop loss at 4%")["target_pct"] == 10.0
    assert p("hold while RSI < 70%")["target_pct"] is None
    assert p("1 month or 20 days")["max_days"] == 20                        # earliest limit
    assert p("") == {"target_pct": None, "stop_pct": None, "max_days": None}
    assert p(None)["stop_pct"] is None


def test_exit_alert_no_longer_fires_gain_at_stop_distance(monkeypatch):
    # Regression: "target 8% gain, stop loss at 4%" used to alert "gain target reached" at +4%.
    from alerts import exit_checker as ec
    monkeypatch.setattr(ec, "get_effective_price", lambda p: 100.0)
    pos = {"ticker": "ABC", "exit_condition": "target 8% gain, stop loss at 4%", "direction": "buy"}
    assert ec._check_percentage_exit(pos, 104.0) is None
    assert ec._check_percentage_exit(pos, 107.6)["alert_type"] == "percentage_gain"   # 0.5 tolerance
    assert ec._check_percentage_exit(pos, 96.4)["alert_type"] == "stop_loss"
    short = dict(pos, direction="short")
    assert ec._check_percentage_exit(short, 96.0) is None                             # +4% for a short
    assert ec._check_percentage_exit(short, 104.0)["alert_type"] == "stop_loss"


def test_equity_exit_decision():
    from analysis.exit_rules import equity_exit_decision as d
    rule = {"target_pct": 10.0, "stop_pct": 4.0, "max_days": 20}
    assert d(100, 95.9, rule)[0] == "close"                        # hard stop
    assert d(100, 110.0, rule)[0] == "close"                       # target
    assert d(100, 104.0, rule, peak_price=104.0)[0] == "hold"      # up 1R, at peak
    assert d(100, 103.0, rule, peak_price=106.0)[0] == "hold"      # 2.8% off peak < 4%
    act, why = d(100, 101.5, rule, peak_price=106.0)               # 4.2% off a +6% peak → trail
    assert act == "close" and why.startswith("trail")
    assert d(100, 101.0, rule, peak_price=103.0)[0] == "hold"      # peak < 1R → no trail yet
    assert d(100, 101.0, rule, days_held=20)[0] == "close"         # time limit
    assert d(100, 101.0, {"target_pct": None, "stop_pct": None})[1] == "+1.0%"   # defaults fill in
    assert d(100, 95.0, {})[0] == "close"                          # default 4% stop
    assert d(0, 10.0, rule)[0] == "hold" and d(100, None, rule)[0] == "hold"


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


def test_guard_prices_whole_share_limit_buys():
    from trading_guards import buy_cost
    st = GuardState(start_equity=1000.0)
    limit_buy = OrderIntent(ticker="F", side="buy", quantity=3, order_type="limit", limit_price=12.5)
    assert buy_cost(limit_buy) == 37.5 and check_order(limit_buy, st, buying_power=100.0).allowed
    assert not check_order(limit_buy, st, buying_power=30.0).allowed              # 37.50 > BP
    big = OrderIntent(ticker="F", side="buy", quantity=40, order_type="limit", limit_price=12.5)
    assert "single-name cap" in check_order(big, st, buying_power=1000.0).reason  # 500 > 40%
    qty_market = OrderIntent(ticker="F", side="buy", quantity=3)                  # no price bound
    assert buy_cost(qty_market) is None and not check_order(qty_market, st, 1000.0).allowed


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


def _fake_equity_review(monkeypatch, alert_type=None):
    """DRY_RUN + a fake MCP whose review returns `alert_type` (None = clean). Records every call."""
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    calls = []
    checks = {"alertType": alert_type} if alert_type else {}
    def fake(name, args=None):
        calls.append(name)
        return {"data": {"order_checks": checks}}
    monkeypatch.setattr(mcp, "_call_tool", fake)
    return mcp, calls


def test_mcp_dry_run_order_logs_not_sends(monkeypatch):
    mcp, calls = _fake_equity_review(monkeypatch)
    st = GuardState(start_equity=1000.0)
    res = mcp.place_order(_buy(100.0, client_id="d1"), st, buying_power=500.0, account_number="A")
    assert res["status"] == "dry_run" and st.orders_today == 1
    assert calls == ["review_equity_order"]                          # previewed, never placed


def test_equity_review_alert_blocks_buy_not_sell(monkeypatch):
    mcp, calls = _fake_equity_review(monkeypatch, "EQUITY_EXTREMELY_UNMARKETABLE_LIMIT_PRICE")
    st = GuardState(start_equity=1000.0)
    buy = mcp.place_order(_buy(100.0, client_id="b"), st, buying_power=500.0, account_number="A")
    assert buy["status"] == "rejected" and "UNMARKETABLE" in buy["reason"] and st.orders_today == 0
    sell = mcp.place_order(_sell(1, client_id="s"), st, buying_power=500.0, account_number="A")
    assert sell["status"] == "dry_run"                              # exits are never trapped


def test_equity_review_failure_does_not_place(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", False, raising=False)   # even LIVE: nothing sent
    sent = []
    def fake(name, args=None):
        if name.startswith("review_"):
            raise mcp.MCPToolError("review down")
        sent.append(name)
    monkeypatch.setattr(mcp, "_call_tool", fake)
    res = mcp.place_order(_sell(1, client_id="rf"), GuardState(start_equity=1000.0),
                          buying_power=500.0, account_number="A")
    assert res["status"] == "review_failed" and sent == []


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


def test_options_strategies_select_and_size(monkeypatch):
    from analysis import options_strategies as os_
    from analysis.options_strategies import (
        select_strategy, size_contracts, catalyst_momentum, short_dte_momentum)
    hot = {"direction": "buy", "conviction": 85}
    # short_dte_momentum is DISABLED (user, until the judge is proven): a hot risk-on signal falls
    # through to the catalyst swing.
    assert "short_dte_momentum" in os_.DISABLED_STRATEGIES
    assert select_strategy(hot, {"risk": "risk_on"})["strategy"] == "catalyst_momentum"
    # Re-enabled, it needs conviction>=80 + risk_on and wins over catalyst when both qualify.
    monkeypatch.setattr(os_, "DISABLED_STRATEGIES", set())
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


def test_affordable_scout_lean():
    from ingestion.affordable_scout import _lean_from_rsi
    assert _lean_from_rsi(30) == ("buy", 65)      # oversold → mean_reversion buy
    assert _lean_from_rsi(72) == ("short", 65)    # overbought → mean_reversion short
    assert _lean_from_rsi(58) is None             # mid-range momentum removed (noise, no edge)
    assert _lean_from_rsi(45) is None             # chop → skip
    assert _lean_from_rsi(None) is None


def test_closed_underlyings_anti_churn():
    from alerts.agentic_options import _closed_underlyings
    exits = [
        {"close": "SNAP", "status": "placed"},
        {"close": "T", "status": "error"},        # live: didn't actually close → allow re-entry
        {"close": None, "status": "placed"},      # malformed → ignored
        {"close": "aal", "status": "dry_run"},
    ]
    # Only placed (live or paper fill) / dry_run closes block re-entry.
    assert _closed_underlyings(exits) == {"SNAP", "AAL"}


def test_paper_book_flow(tmp_path, monkeypatch):
    from storage import paper_book as pb
    monkeypatch.setattr(pb, "_FILE", str(tmp_path / "paper.json"))
    pb.reset(25.0)
    assert pb.cash() == 25.0
    # buy 1 F 15C @ 0.18 = $18
    assert pb.open_option({"option_id": "o1", "ticker": "F", "right": "call", "strike": 15,
                           "expiration": "2026-09-18", "qty": 1, "entry_price": 0.18}) is None
    assert pb.cash() == 7.0
    # can't afford a $18 second contract with $7 left; no duplicate option_id
    assert "exceeds" in pb.open_option({"option_id": "o2", "ticker": "NIO", "qty": 1, "entry_price": 0.18})
    assert pb.open_option({"option_id": "o1", "ticker": "F", "qty": 1, "entry_price": 0.10})
    # close at 0.30 → pnl (0.30-0.18)*100 = $12; cash back to 7 + 30 = 37
    rec = pb.close_option("o1", 0.30, "target")
    assert rec["pnl"] == 12.0 and rec["leg"] == "option" and pb.cash() == 37.0 and not pb.get_book()["options"]

    # shares: $10 of SPY @ $500, then sell it all at $510
    assert pb.buy_shares("spy", 10.0, 500.0) is None
    held = pb.get_book()["shares"]["SPY"]
    assert held["shares"] == 0.02 and held["avg_cost"] == 500.0 and pb.cash() == 27.0
    assert "exceeds" in pb.buy_shares("QQQ", 100.0, 400.0)
    s_open = pb.summarize(pb.get_book(), {}, {"SPY": 510.0})
    assert s_open["open_value"] == 10.2 and s_open["n_open"] == 1
    rec = pb.sell_shares("SPY", 5, 510.0, "target")                 # qty capped at what's held
    assert rec["pnl"] == 0.2 and rec["leg"] == "stock" and "SPY" not in pb.get_book()["shares"]
    assert pb.sell_shares("SPY", 1, 510.0) is None

    s = pb.summarize(pb.get_book(), {})
    assert s["realized"] == 12.2 and s["equity"] == 37.2 and s["win_rate"] == 100.0
    # PDT fills: open+close for both instruments
    assert [(f["key"], f["effect"]) for f in pb.get_book()["fills"]] == [
        ("opt:o1", "open"), ("opt:o1", "close"), ("eq:SPY", "open"), ("eq:SPY", "close")]


def test_paper_book_migrates_v1(tmp_path, monkeypatch):
    import json as _json
    from storage import paper_book as pb
    monkeypatch.setattr(pb, "_FILE", str(tmp_path / "paper.json"))
    (tmp_path / "paper.json").write_text(_json.dumps({"cash": 9.0, "start": 25.0, "open": [{"option_id": "x"}],
                                                      "closed": []}))
    b = pb.get_book()
    assert b["options"] == [{"option_id": "x"}] and b["shares"] == {} and b["fills"] == [] and b["cash"] == 9.0


def test_paper_scope_isolates_agent_state(tmp_path, monkeypatch):
    import agent_mode
    from storage import agent_stock_book as book, decision_log as dl, peak_tracker
    monkeypatch.setattr(book, "_FILE", str(tmp_path / "agent_stocks.json"))
    monkeypatch.setattr(peak_tracker, "_FILE", str(tmp_path / "peaks.json"))
    book.record_entry("LIVE", "target 8% gain")
    with agent_mode.paper_scope():
        assert agent_mode.in_paper() and book.all_entries() == {}          # paper never sees the live book
        book.record_entry("PAPR", "target 6% gain", dollars=5.0)
        peak_tracker.update_peak("eq:PAPR", 10.0)
        dl.record("entry", "PAPR", "placed")
    assert not agent_mode.in_paper()
    assert set(book.all_entries()) == {"LIVE"} and peak_tracker.get_peak("eq:PAPR") is None
    assert (tmp_path / "paper_agent_stocks.json").exists() and (tmp_path / "paper_peaks.json").exists()
    assert [r["mode"] for r in dl.read()] == ["paper"]
    assert dl.read(mode=dl.ACTING_MODES) == dl.read(mode="paper")


def test_paper_cycle_runs_the_live_entry_loop(monkeypatch, tmp_path):
    """A paper cycle goes through run_options_agent's real entry loop (routing, sizing, the stock cap, the
    decision log) with PaperBroker filling at live prices — only market data is stubbed."""
    import agent_mode
    from alerts import agentic_options as ao, agentic_stocks as stk, paper_broker
    from ingestion import robinhood_mcp as live
    from storage import agent_stock_book as book, decision_log as dl, paper_book as pb, peak_tracker
    monkeypatch.setattr(agent_mode, "MODE_FILE", str(tmp_path / "mode.txt"))
    monkeypatch.setattr(pb, "_FILE", str(tmp_path / "paper.json"))
    monkeypatch.setattr(book, "_FILE", str(tmp_path / "agent_stocks.json"))
    monkeypatch.setattr(peak_tracker, "_FILE", str(tmp_path / "peaks.json"))
    pb.reset(40.0)
    monkeypatch.setattr(live, "is_available", lambda: True)
    monkeypatch.setattr(live, "fetch_quotes", lambda tickers: {t: {"price": 50.0} for t in tickers})
    monkeypatch.setattr(ao, "_market_open", lambda: True)
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral", "detail": {}})
    monkeypatch.setattr(ao, "_signals", lambda: [
        {"ticker": "CORE", "direction": "buy", "conviction": 68, "risk_level": "medium",
         "exit_condition": "target 8% gain, stop loss at 4%"}])
    monkeypatch.setattr(stk, "budget_cap", lambda: 20.0)
    monkeypatch.setattr("market_hours.recent_trading_days", lambda n, today=None: [])

    out = ao.run_paper_agent(verbose=False)
    assert out["mode"] == "paper" and [(e["ticker"], e["leg"], e["status"]) for e in out["entries"]] == \
        [("CORE", "stock", "placed")]
    bk = pb.get_book()
    assert bk["cash"] == round(40.0 - out["entries"][0]["dollars"], 2) and "CORE" in bk["shares"]
    assert out["entries"][0]["dollars"] <= 16.0                        # 40% single-name cap of $40
    rec = dl.read()[-1]
    assert rec["mode"] == "paper" and rec["ticker"] == "CORE" and rec["action"] == "placed"
    assert book.all_entries() == {}                                   # the LIVE stock book is untouched
    with agent_mode.paper_scope():
        assert "CORE" in book.all_entries()                           # the paper one has the plan

    # Next cycle: price +10% → the stock exit rule's target fires and paper sells it.
    monkeypatch.setattr(live, "fetch_quotes", lambda tickers: {t: {"price": 55.0} for t in tickers})
    monkeypatch.setattr(ao, "_signals", lambda: [])
    out = ao.run_paper_agent(verbose=False)
    assert [(x["close"], x["status"]) for x in out["exits"]] == [("CORE", "placed")]
    assert pb.get_book()["shares"] == {} and pb.get_book()["closed"][-1]["pnl"] > 0


def test_paper_broker_guards_and_order_types(monkeypatch, tmp_path):
    from alerts.paper_broker import PaperBroker
    from ingestion import robinhood_mcp as live
    from storage import paper_book as pb
    from trading_guards import GuardState, OrderIntent
    monkeypatch.setattr(pb, "_FILE", str(tmp_path / "paper.json"))
    monkeypatch.setattr(live, "fetch_quotes", lambda tickers: {t: {"price": 10.0} for t in tickers})
    pb.reset(40.0)
    b, st = PaperBroker(), GuardState(start_equity=40.0)
    big = OrderIntent(ticker="F", side="buy", dollars=30.0, client_id="a")
    assert b.place_order(big, st, 40.0)["status"] == "rejected"          # 40% single-name cap, like live
    stop = OrderIntent(ticker="F", side="sell", quantity=1, order_type="stop", stop_price=9.0, client_id="b")
    assert b.place_order(stop, st, 40.0)["status"] == "rejected"         # no resting orders on paper
    ok = OrderIntent(ticker="F", side="buy", dollars=10.0, client_id="c")
    assert b.place_order(ok, st, 40.0)["status"] == "placed" and b.fetch_buying_power() == 30.0
    assert b.place_order(ok, st, 30.0)["status"] == "rejected"           # duplicate client_id
    assert [p["ticker"] for p in b.fetch_positions()] == ["F"] and b.fetch_positions()[0]["shares"] == 1.0


def test_paper_position_pnl():
    from storage.paper_book import position_pnl
    assert position_pnl(0.20, 0.30, 1) == (10.0, 50.0)
    assert position_pnl(0.20, 0.10, 2) == (-20.0, -50.0)
    assert position_pnl(0, 0.10, 1) == (0.0, 0.0)


def test_pin_ttl_expiry_and_renew(tmp_path, monkeypatch):
    from storage import entry_watch as ew
    from datetime import date, timedelta
    monkeypatch.setattr(ew, "ENTRY_WATCH_FILE", str(tmp_path / "entry_watch.json"))

    def _pin(ticker, days_ago):
        d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        data = ew.load_entry_watch()
        data.setdefault("pinned", []).append(
            {"ticker": ticker, "company_name": ticker, "trigger_text": "breakout $10",
             "exit_condition": "", "pinned_at": d})
        ew.save_entry_watch(data)

    _pin("FRESH", 0)                    # pinned today → full TTL left
    _pin("EDGE", ew.PIN_TTL_DAYS)       # age == TTL → still alive (0 left)
    _pin("STALE", ew.PIN_TTL_DAYS + 1)  # age > TTL → expired

    # load prunes the expired one lazily
    pins = {p["ticker"]: p for p in ew.get_pinned()}
    assert set(pins) == {"FRESH", "EDGE"}
    assert ew.pin_days_left(pins["FRESH"]) == ew.PIN_TTL_DAYS
    assert ew.pin_days_left(pins["EDGE"]) == 0

    # renew resets the clock for a ticker that reappeared in fresh recs
    assert ew.renew_pins(["edge"]) == 1               # case-insensitive
    edge = next(p for p in ew.get_pinned() if p["ticker"] == "EDGE")
    assert ew.pin_days_left(edge) == ew.PIN_TTL_DAYS  # back to full TTL
    assert ew.renew_pins(["EDGE"]) == 0               # already today → no-op
    # an unparseable / missing date is treated as expired (fail-safe)
    assert ew._is_expired({"ticker": "X"})


def test_clear_all_pinned(tmp_path, monkeypatch):
    from storage import entry_watch as ew
    monkeypatch.setattr(ew, "ENTRY_WATCH_FILE", str(tmp_path / "entry_watch.json"))
    assert ew.add_pinned("AAA", "AAA Co", "breaks above $10")
    assert ew.add_pinned("BBB", "BBB Co", "breaks above $20")
    ew.mark_notified("AAA", "pinned")
    assert ew.clear_all_pinned() == 2
    assert ew.get_pinned() == []
    # its pinned notify record is gone too, so a re-pin can alert again
    assert not ew.was_notified_today("AAA", "pinned")
    assert ew.clear_all_pinned() == 0          # idempotent on an empty list


def test_catalyst_staleness_gate():
    from alerts.entry_checker import _catalyst_is_stale, CATALYST_MAX_AGE_DAYS
    from datetime import date, timedelta
    today = date.today()
    fresh = (today - timedelta(days=CATALYST_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    stale = (today - timedelta(days=CATALYST_MAX_AGE_DAYS + 1)).strftime("%Y-%m-%d")
    assert _catalyst_is_stale(stale) is True
    assert _catalyst_is_stale(fresh) is False          # exactly at the boundary = kept
    assert _catalyst_is_stale(today.strftime("%Y-%m-%d")) is False
    # fail-open: no date / null / garbage is NOT stale (technical watches + back-compat recs)
    assert _catalyst_is_stale(None) is False
    assert _catalyst_is_stale("") is False
    assert _catalyst_is_stale("not-a-date") is False


def test_recommendation_entry_scope_and_staleness(tmp_path, monkeypatch):
    import json as _json
    from alerts import entry_checker as ec
    from storage import pipeline_cache as pc
    from datetime import date, timedelta
    monkeypatch.setattr(pc, "CACHE_FILE", str(tmp_path / "pipeline_cache.json"))
    monkeypatch.setattr(pc, "CACHE_BACKUP_FILE", str(tmp_path / "pipeline_cache_backup.json"))
    # Watchlist has only WATCHED; UNTRACKED is not curated.
    monkeypatch.setattr(ec, "_watchlist_tickers", lambda: {"WATCHED", "OLDNEWS"})
    today = date.today().strftime("%Y-%m-%d")
    old = (date.today() - timedelta(days=ec.CATALYST_MAX_AGE_DAYS + 2)).strftime("%Y-%m-%d")
    recs = [
        {"ticker": "WATCHED",  "direction": "watch", "entry_trigger": "breaks above $10",
         "catalyst_date": today},
        {"ticker": "UNTRACKED", "direction": "watch", "entry_trigger": "breaks above $5",
         "catalyst_date": today},                                   # dropped: not on watchlist
        {"ticker": "OLDNEWS",  "direction": "watch", "entry_trigger": "breaks above $7",
         "catalyst_date": old},                                     # dropped: stale catalyst
        {"ticker": "WATCHED",  "direction": "buy",   "entry_trigger": "now",
         "catalyst_date": today},                                   # dropped: 'now' has no trigger
    ]
    (tmp_path / "pipeline_cache.json").write_text(_json.dumps({"date": today, "recommendations": recs}))
    got = {c["ticker"] for c in ec._candidates_from_recommendations()}
    assert got == {"WATCHED"}
    # Yesterday's cache (the pipeline didn't run today) → no recommendation alerts at all.
    (tmp_path / "pipeline_cache.json").write_text(_json.dumps({"date": old, "recommendations": recs}))
    assert ec._candidates_from_recommendations() == []


def test_record_usage_updates_ledger(tmp_path, monkeypatch):
    import llm_budget as lb
    monkeypatch.setattr(lb, "_LEDGER", str(tmp_path / "ledger.json"))
    lb.set_balance(5.0)

    class U:   # the SDK's Usage object shape
        input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 10_000, 2_000, 0, None
    assert lb.record_usage("claude-sonnet-4-6", U()) == 0.06               # 10k×$3 + 2k×$15 per 1M
    assert lb.record_usage("claude-sonnet-4-6", {"input_tokens": 0, "cache_creation_input_tokens": 1_000_000}) == 3.75
    assert lb.get_state()["spent"] == 3.81
    assert lb.record_usage("claude-sonnet-4-6", object()) == 0.0          # nothing readable → no spend


def test_agent_signals_merge_chat_buys(tmp_path, monkeypatch):
    import json as _json
    from alerts import agentic_options as ao
    from storage import entry_watch as ew
    from datetime import date
    today = date.today().isoformat()   # stale (other-day) cache / chat are ignored — see test below
    # pipeline cache: RDDT only a watch, plus one real buy
    cache = tmp_path / "pipeline_cache.json"
    cache.write_text(_json.dumps({"date": today, "recommendations": [
        {"ticker": "RDDT", "direction": "watch", "conviction": 55},
        {"ticker": "NVDA", "direction": "buy", "conviction": 80}]}), encoding="utf-8")
    monkeypatch.setattr(ao, "_CACHE", str(cache))
    monkeypatch.setattr(ew, "get_chat_suggestions", lambda: [
        {"ticker": "RDDT", "action": "buy", "created_at": today},
        {"ticker": "MSFT", "action": "watch", "created_at": today}])
    sigs = {s["ticker"]: s for s in ao._signals()}
    assert sigs["NVDA"]["direction"] == "buy" and sigs["NVDA"]["conviction"] == 80   # pipeline buy
    assert sigs["RDDT"]["direction"] == "buy" and sigs["RDDT"]["source"] == "chat"   # chat upgraded the watch
    assert sigs["RDDT"]["conviction"] == ao.CHAT_BUY_CONVICTION
    assert "MSFT" not in sigs                                                         # chat 'watch' not an entry


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


def test_option_exit_trailing():
    from analysis.options_strategies import option_exit_decision
    rule = {"profit_pct": 80, "stop_pct": 50, "close_dte": 2,
            "trail_activate": 25, "trail_giveback": 20}
    # entry 0.10, peaked 0.18 (+80%, armed), now 0.16 → gave back (0.18-0.16)/0.18=11% < 20 → hold
    assert option_exit_decision(0.10, 0.16, 20, rule, peak_mark=0.18)[0] == "hold"
    # now 0.14 → gave back (0.18-0.14)/0.18=22% ≥ 20 → CLOSE (locks ~+40% off the +80% peak)
    act, why = option_exit_decision(0.10, 0.14, 20, rule, peak_mark=0.18)
    assert act == "close" and "trail" in why
    # peak only +15% (< trail_activate 25) → not armed → hold even on a pullback
    assert option_exit_decision(0.10, 0.105, 20, rule, peak_mark=0.115)[0] == "hold"
    # stop still wins over trailing
    assert option_exit_decision(0.10, 0.04, 20, rule, peak_mark=0.18)[0] == "close"


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
    # A CLOSE must be allowed even when `right` isn't call/put — real get_option_positions
    # reports type as long/short, and closing sells by option_id (right irrelevant to the order).
    close = OptionOrderIntent(underlying="F", option_id="z", right="long", side="sell",
                              position_effect="close", quantity=1, price=0.31)
    assert check_option_order(close, st, buying_power=0.0).allowed


def test_option_args_shape():
    from ingestion.robinhood_mcp import _option_args
    from trading_guards import OptionOrderIntent
    intent = OptionOrderIntent(underlying="F", option_id="opt-1", right="call", side="buy",
                               position_effect="open", quantity=2, price=0.12)
    args = _option_args(intent, "AGENTIC")
    assert args["legs"][0] == {"option_id": "opt-1", "side": "buy",
                               "position_effect": "open", "ratio_quantity": 1}
    assert args["quantity"] == "2" and args["price"] == "0.12" and args["direction"] == "debit"


def test_order_alert_reads_nested_order_checks():
    from ingestion.robinhood_mcp import _order_alert
    # Real review payload shape: alert nested under data.order_checks; {} = clean.
    assert _order_alert({"data": {"order_checks": {}}}) is None
    assert _order_alert({"data": {"order_checks": {"alertType": "OPTION_NOT_ENOUGH_BP_FOR_PREMIUM"}}}) \
        == "OPTION_NOT_ENOUGH_BP_FOR_PREMIUM"
    assert _order_alert({"order_checks": {}, "data": {}}) is None
    assert _order_alert(None) is None and _order_alert("text") is None


def _review_returning(monkeypatch, alert_type):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    checks = {"alertType": alert_type} if alert_type else {}
    monkeypatch.setattr(mcp, "_call_tool", lambda name, args=None: {"data": {"order_checks": checks}})
    return mcp


def test_option_review_alert_blocks_open(monkeypatch):
    from trading_guards import OptionOrderIntent
    mcp = _review_returning(monkeypatch, "OPTION_NOT_ENOUGH_BP_FOR_PREMIUM")
    st = GuardState(start_equity=1000.0)
    intent = OptionOrderIntent(underlying="F", option_id="o1", right="call", side="buy",
                               position_effect="open", quantity=1, price=0.10, client_id="t-open")
    res = mcp.place_option_order(intent, st, buying_power=500.0, account_number="AGENTIC")
    assert res["status"] == "rejected" and "OPTION_NOT_ENOUGH_BP_FOR_PREMIUM" in res["reason"]
    assert st.orders_today == 0                                    # blocked → not counted


def test_option_review_alert_does_not_block_close(monkeypatch):
    from trading_guards import OptionOrderIntent
    mcp = _review_returning(monkeypatch, "SOME_ALERT")
    st = GuardState(start_equity=1000.0)
    intent = OptionOrderIntent(underlying="F", option_id="o1", right="call", side="sell",
                               position_effect="close", quantity=1, price=0.10,
                               direction="credit", client_id="t-close")
    res = mcp.place_option_order(intent, st, buying_power=500.0, account_number="AGENTIC")
    assert res["status"] == "dry_run"                              # exits still go out


def test_option_review_clean_open_proceeds(monkeypatch):
    from trading_guards import OptionOrderIntent
    mcp = _review_returning(monkeypatch, None)
    st = GuardState(start_equity=1000.0)
    intent = OptionOrderIntent(underlying="F", option_id="o1", right="call", side="buy",
                               position_effect="open", quantity=1, price=0.10, client_id="t-clean")
    assert mcp.place_option_order(intent, st, buying_power=500.0, account_number="AGENTIC")["status"] == "dry_run"


def _fill(key, effect, day, ts, qty=1):
    import datetime as dt
    return {"key": key, "effect": effect, "qty": qty, "ts": ts, "date": dt.date(2026, 9, day)}


def test_count_day_trades():
    import datetime as dt
    from trading_guards import count_day_trades
    window = [dt.date(2026, 9, d) for d in (25, 24, 23, 22, 21)]
    today = dt.date(2026, 9, 25)
    fills = [
        _fill("A", "open", 22, "t1"), _fill("A", "close", 22, "t2"),                 # 1 day trade
        _fill("B", "open", 23, "t1"), _fill("B", "close", 23, "t2"),
        _fill("B", "open", 23, "t3"), _fill("B", "close", 23, "t4"),                 # +2 (round trips)
        _fill("C", "open", 23, "t1", 2), _fill("C", "close", 23, "t2", 2),           # +1 (one sell)
        _fill("D", "open", 22, "t1"), _fill("D", "close", 24, "t2"),                 # overnight → 0
        _fill("E", "close", 24, "t1"),                                               # close only → 0
        _fill("F", "open", 18, "t1"), _fill("F", "close", 18, "t2"),                 # outside window
        _fill("G", "open", 25, "t1"),                                                # opened today, held
        _fill("H", "open", 25, "t1"), _fill("H", "close", 25, "t2"),                 # +1, not reserved
    ]
    assert count_day_trades(fills, window, today) == (5, 1)


def test_day_trade_budget_reserves_same_day_exits():
    import datetime as dt
    from trading_guards import day_trade_budget
    window = [dt.date(2026, 9, d) for d in (25, 24, 23, 22, 21)]
    today = dt.date(2026, 9, 25)
    assert day_trade_budget([], window, today, 42.0) == 3
    one_used_one_held = [_fill("A", "open", 22, "t1"), _fill("A", "close", 22, "t2"),
                         _fill("G", "open", 25, "t1")]
    assert day_trade_budget(one_used_one_held, window, today, 42.0) == 1
    three_used = [f for k in "ABC" for f in (_fill(k, "open", 23, "t1"), _fill(k, "close", 23, "t2"))]
    assert day_trade_budget(three_used, window, today, 42.0) == 0
    assert day_trade_budget(three_used, window, today, 30_000.0) is None       # $25k exempt


def test_fills_from_broker_orders():
    import datetime as dt
    from ingestion.robinhood_mcp import _fills_from_option_orders, _fills_from_equity_orders
    today = dt.date(2026, 9, 25)
    opt = [
        {"state": "filled", "pending_quantity": "0", "legs": [{"option_id": "o1", "position_effect": "close",
         "executions": [{"quantity": "2.0", "trade_date": "2026-09-08", "timestamp": "2026-09-08T13:58:17Z"}]}]},
        {"state": "queued", "pending_quantity": "1.0", "legs": [{"option_id": "o2", "position_effect": "open",
         "executions": []}]},                                                        # working buy → reserve
        {"state": "cancelled", "pending_quantity": "0", "legs": [{"option_id": "o3", "position_effect": "open",
         "executions": []}]},                                                        # dead → nothing
    ]
    f = _fills_from_option_orders(opt, today)
    assert f[0] == {"key": "opt:o1", "effect": "close", "qty": 2.0, "ts": "2026-09-08T13:58:17Z",
                    "date": dt.date(2026, 9, 8)}
    assert f[1]["key"] == "opt:o2" and f[1]["effect"] == "open" and f[1]["date"] == today
    assert len(f) == 2

    eq = [
        # 01:30 UTC on the 15th = 21:30 ET on the 14th → dated the 14th (Eastern trading date)
        {"side": "sell", "state": "filled", "instrument_id": "i1",
         "executions": [{"quantity": "1.0", "timestamp": "2026-08-15T01:30:00Z"}]},
        {"side": "buy", "state": "queued", "instrument_id": "i2", "quantity": None,
         "cumulative_quantity": "0", "executions": []},                              # $-based working buy
    ]
    f = _fills_from_equity_orders(eq, today)
    assert f[0]["key"] == "eq:i1" and f[0]["effect"] == "close" and f[0]["date"] == dt.date(2026, 8, 14)
    assert f[1] == {"key": "eq:i2", "effect": "open", "qty": 1.0, "ts": "~pending", "date": today}


def test_recent_trading_days_skips_weekends_and_holidays():
    import datetime as dt
    from market_hours import recent_trading_days
    # Tue 2026-09-08 back 5: Labor Day Mon 09-07 + weekend skipped
    assert recent_trading_days(5, dt.date(2026, 9, 8)) == [
        dt.date(2026, 9, 8), dt.date(2026, 9, 4), dt.date(2026, 9, 3), dt.date(2026, 9, 2), dt.date(2026, 9, 1)]


def test_entries_stop_when_pdt_budget_exhausted(monkeypatch):
    from alerts import agentic_options as ao
    monkeypatch.setattr(ao, "_signals", lambda: [{"ticker": "AAA", "direction": "buy"},
                                                 {"ticker": "BBB", "direction": "buy"}])
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: set())
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral"})
    out = ao._run_entries(mcp=None, acct="A", buying_power=100.0, verbose=False, max_new=0)
    assert out == [{"ticker": "AAA", "status": "skipped", "reason": "PDT day-trade budget exhausted"}]


def test_agent_signals_ignore_stale_cache_and_chat(monkeypatch, tmp_path):
    import json as _json
    from datetime import date, timedelta
    from alerts import agentic_options as ao
    from storage import entry_watch
    today, old = date.today().isoformat(), (date.today() - timedelta(days=2)).isoformat()
    cache = tmp_path / "pipeline_cache.json"
    monkeypatch.setattr(ao, "_CACHE", str(cache))
    monkeypatch.setattr(entry_watch, "get_chat_suggestions", lambda: [
        {"ticker": "NEW", "action": "buy", "created_at": f"{today}T09:00:00"},
        {"ticker": "OLDC", "action": "buy", "created_at": f"{old}T09:00:00"},
        {"ticker": "NODATE", "action": "buy"}])
    rec = {"ticker": "REC", "direction": "buy", "conviction": 70}

    cache.write_text(_json.dumps({"date": old, "recommendations": [rec]}), encoding="utf-8")
    assert [s["ticker"] for s in ao._signals()] == ["NEW"]          # Friday's cache ignored on Monday
    cache.write_text(_json.dumps({"date": today, "recommendations": [rec]}), encoding="utf-8")
    assert [s["ticker"] for s in ao._signals()] == ["REC", "NEW"]
    assert ao._is_today(None) is False and ao._is_today("garbage") is False


def test_stock_routing():
    from alerts.agentic_stocks import route
    hr = {"ticker": "A", "direction": "buy", "highly_recommended": True, "conviction": 60}
    strong = {"ticker": "B", "direction": "buy", "conviction": 75}
    core = {"ticker": "C", "direction": "buy", "conviction": 68}
    short = {"ticker": "D", "direction": "short", "conviction": 50}
    coin = {"ticker": "BTC", "direction": "buy", "conviction": 60, "asset_type": "crypto"}
    watch = {"ticker": "E", "direction": "watch"}
    assert [route(s) for s in (hr, strong, core, short, coin, watch)] == \
        ["option", "option", "stock", "option", None, None]


def test_stock_rank_and_plan():
    from alerts.agentic_stocks import rank, plan_buy
    sigs = [{"ticker": "LOW", "conviction": 60}, {"ticker": "HI", "conviction": 90},
            {"ticker": "CHAT", "conviction": 72}, {"ticker": "OLD", "confidence_score": 0.8}]
    assert [s["ticker"] for s in rank(sigs)] == ["HI", "OLD", "CHAT", "LOW"]
    i = plan_buy({"ticker": "f", "conviction": 68}, dollars=12.349, avail=40.0)
    assert i.ticker == "F" and i.dollars == 12.34 and i.side == "buy" and i.order_type == "market"
    # Sized exactly at the 40% cap of $41.92 (16.768) must floor, not round up past the guard's cap.
    capped = plan_buy({"ticker": "F"}, dollars=0.40 * 41.92, avail=41.92)
    assert capped.dollars == 16.76
    assert check_order(capped, GuardState(start_equity=41.92), buying_power=41.92).allowed
    assert plan_buy({"ticker": "F"}, dollars=12.0, avail=5.0).dollars == 5.0     # capped to what's left
    assert plan_buy({"ticker": "F"}, dollars=0.8, avail=40.0) is None           # < $1 broker minimum


def test_stock_sizing_uses_pyramid_on_agentic_bp():
    from alerts.agentic_stocks import size_buys
    sigs = [{"ticker": "A", "direction": "buy", "conviction": 70, "risk_level": "medium"},
            {"ticker": "B", "direction": "buy", "conviction": 70, "risk_level": "low"},
            {"ticker": "BTC", "direction": "buy", "conviction": 90, "asset_type": "crypto"}]
    out = size_buys(sigs, 40.0)
    assert out["A"] == 16.0          # 55% core pool, capped at 40% of $40
    assert out["B"] == 10.0          # 25% base pool
    assert "BTC" not in out and size_buys([], 40.0) == {}
    # Real case: at BP $41.92 the allocator returns 16.77 for a capped name (16.768 rounded UP),
    # one cent over the order guard's 40% cap → must be clamped to 16.76.
    assert size_buys(sigs[:1], 41.92)["A"] == 16.76


def test_combined_entries_route_and_share_one_pool(monkeypatch):
    from alerts import agentic_options as ao
    from alerts import agentic_stocks as stk
    sigs = [{"ticker": "CORE", "direction": "buy", "conviction": 68, "risk_level": "medium"},
            {"ticker": "HOT", "direction": "buy", "conviction": 85, "risk_level": "medium"},
            {"ticker": "NOCON", "direction": "buy", "conviction": 80, "risk_level": "low"}]
    monkeypatch.setattr(ao, "_signals", lambda: sigs)
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: set())
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral"})
    monkeypatch.setattr(stk, "held_tickers", lambda mcp, acct: set())
    order = []

    def fake_option(mcp, acct, sig, regime, avail, state, verbose):
        order.append(("opt", sig["ticker"], avail))
        if sig["ticker"] == "HOT":
            return {"ticker": "HOT", "leg": "option", "status": "dry_run", "reason": ""}, 10.0
        return None, 0.0                                   # NOCON: no affordable contract
    monkeypatch.setattr(ao, "_try_option_entry", fake_option)

    class FakeMcp:
        def place_order(self, intent, state, buying_power, account_number):
            order.append(("stk", intent.ticker, intent.dollars, buying_power))
            return {"status": "dry_run", "reason": ""}

    from storage import agent_stock_book as book
    monkeypatch.setattr(book, "_FILE", str(__import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "b.json"))
    monkeypatch.setattr(stk, "budget_cap", lambda: None)            # uncapped for this routing test
    out = ao._run_entries(FakeMcp(), "A", 40.0, verbose=False, max_new=2)
    # best idea first: HOT (85, option) → NOCON (80, option → falls back to shares) → CORE blocked by PDT
    assert order[0] == ("opt", "HOT", 40.0)
    assert order[1] == ("opt", "NOCON", 30.0)              # pool shrank by HOT's premium
    assert order[2][:2] == ("stk", "NOCON") and order[2][3] == 30.0
    assert [r.get("leg") or r["status"] for r in out] == ["option", "stock", "skipped"]


def test_stock_budget_cap(monkeypatch, tmp_path):
    from alerts import agentic_options as ao
    from alerts import agentic_stocks as stk
    from storage import agent_stock_book as book
    assert stk.stock_room(0, None) == float("inf")
    assert stk.stock_room(12.5, 20.0) == 7.5 and stk.stock_room(25, 20.0) == 0.0
    monkeypatch.setattr(book, "_FILE", str(tmp_path / "b.json"))
    book.record_entry("OLD", "", dollars=6.0)
    assert book.invested() == 6.0

    sigs = [{"ticker": t, "direction": "buy", "conviction": 60, "risk_level": "medium"}
            for t in ("AAA", "BBB", "CCC", "DDD")]
    monkeypatch.setattr(ao, "_signals", lambda: sigs)
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: set())
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral"})
    monkeypatch.setattr(stk, "held_tickers", lambda mcp, acct: set())
    monkeypatch.setattr(stk, "budget_cap", lambda: 20.0)
    bought = []

    class FakeMcp:
        def place_order(self, intent, state, buying_power, account_number):
            bought.append((intent.dollars, buying_power))
            return {"status": "dry_run", "reason": ""}

    ao._run_entries(FakeMcp(), "A", 40.0, verbose=False, max_new=None)
    assert round(sum(d for d, _ in bought), 2) <= 14.0              # $20 cap − $6 already held
    assert all(bp >= 10 for _, bp in bought)                        # guard sees real BP, not cap room


def test_pipeline_cache_roundtrip_and_today_only(monkeypatch, tmp_path):
    import json as _json
    from storage import pipeline_cache as pc
    monkeypatch.setattr(pc, "CACHE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(pc, "CACHE_BACKUP_FILE", str(tmp_path / "b.json"))
    assert pc.load_today() == (None, False)
    pc.save([{"ticker": "A"}], {"A": {"price": 1}}, "now")
    data, backup = pc.load_today()
    assert data["recommendations"] == [{"ticker": "A"}] and backup is False
    pc.save([{"ticker": "B"}], {}, "later")                               # A's run becomes the backup
    assert _json.loads((tmp_path / "b.json").read_text())["recommendations"] == [{"ticker": "A"}]
    (tmp_path / "c.json").write_text(_json.dumps({"date": "2020-01-01", "recommendations": [1]}))
    data, backup = pc.load_today()                                        # stale main → today's backup
    assert backup is True and data["recommendations"] == [{"ticker": "A"}]
    # The agent reads the same file/format: a pc.save() cache is "today" to it.
    from alerts import agentic_options as ao
    pc.save([{"ticker": "C", "direction": "buy", "conviction": 70}], {}, "x")
    monkeypatch.setattr(ao, "_CACHE", pc.CACHE_FILE)
    monkeypatch.setattr("storage.entry_watch.get_chat_suggestions", lambda: [])
    assert [s["ticker"] for s in ao._signals()] == ["C"]


def test_pipeline_history_keeps_every_run(monkeypatch, tmp_path):
    from datetime import date, timedelta
    from storage import pipeline_cache as pc
    monkeypatch.setattr(pc, "CACHE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setattr(pc, "CACHE_BACKUP_FILE", str(tmp_path / "b.json"))
    pc.save([{"ticker": "A"}], {}, "06:31")
    pc.save([{"ticker": "B"}], {}, "09:10")                                 # overwrites the cache, not history
    runs = pc.read_history()
    assert [r["recommendations"][0]["ticker"] for r in runs] == ["A", "B"] and all("archived_at" in r for r in runs)
    (tmp_path / "pipeline_history" / "2020-01-02.jsonl").write_text('{"date": "2020-01-02"}\nnot json\n')
    assert [r["date"] for r in pc.read_history(until=date(2020, 12, 31))] == ["2020-01-02"]
    assert len(pc.read_history(since=date.today() - timedelta(days=1))) == 2
    monkeypatch.setattr(pc, "HISTORY_DIR", str(tmp_path / "c.json"))        # unwritable (a file) → cache still saved
    pc.save([{"ticker": "C"}], {}, "x")
    assert pc.load_today()[0]["recommendations"] == [{"ticker": "C"}]


def test_run_pipeline_skips_closed_days():
    import datetime as dt
    from scripts.run_pipeline import is_trading_day
    assert is_trading_day(dt.date(2026, 9, 28)) is True          # Monday
    assert is_trading_day(dt.date(2026, 9, 27)) is False         # Sunday
    assert is_trading_day(dt.date(2026, 11, 26)) is False        # Thanksgiving


def test_agent_mode_file(monkeypatch, tmp_path):
    import agent_mode
    import pytest
    monkeypatch.setattr(agent_mode, "MODE_FILE", str(tmp_path / "agent_mode.txt"))
    assert agent_mode.get_mode() == "paper"                    # no file → paper, never real money
    agent_mode.set_mode("live")
    assert agent_mode.get_mode() == "live"
    (tmp_path / "agent_mode.txt").write_text("LIVE!!\n")
    assert agent_mode.get_mode() == "off"                      # garbled → off (fail safe)
    with pytest.raises(ValueError):
        agent_mode.set_mode("armed")


def test_run_agent_follows_mode(monkeypatch, tmp_path):
    import agent_mode
    from scripts import run_agent as ra
    import config
    calls = []
    monkeypatch.setattr(config, "DRY_RUN", True)               # main() flips it; restore after the test
    monkeypatch.setattr(agent_mode, "MODE_FILE", str(tmp_path / "m.txt"))
    monkeypatch.setattr(ra, "_LOG", str(tmp_path / "log.txt"))
    monkeypatch.setattr(ra, "_market_open", lambda: True)
    monkeypatch.setattr(ra, "run_options_agent", lambda verbose, entries=True: calls.append(("live", entries)) or {})
    monkeypatch.setattr(ra, "run_paper_agent", lambda verbose: calls.append(("paper",)) or {})
    for mode in ("off", "paper", "live"):
        agent_mode.set_mode(mode)
        ra.main()
    # off → nothing; paper → live EXITS only + a paper cycle; live → a full live cycle
    assert calls == [("live", False), ("paper",), ("live", True)]


def test_stock_exit_helpers():
    from datetime import date
    from alerts.agentic_stocks import protective_stop_price, decide_exit, parse_stop_pct
    assert protective_stop_price(12.50, 4.0) == 12.0 and protective_stop_price(0, 4) is None
    assert parse_stop_pct({"exit_condition": "target 6% gain, stop loss at 3%"}) == 3.0
    assert parse_stop_pct(None) == 4.0                                   # STOCK_EXIT_DEFAULT
    entry = {"exit_condition": "target 8% gain, stop loss at 4%", "opened": "2026-09-01"}
    pos = {"avg_cost": 10.0, "current_price": 10.9}
    assert decide_exit(pos, entry, 10.9, date(2026, 9, 10))[0] == "close"          # target
    assert decide_exit(dict(pos, current_price=10.2), entry, 10.2, date(2026, 9, 10))[0] == "hold"
    assert decide_exit(dict(pos, current_price=10.2), entry, 10.2, date(2026, 10, 2))[0] == "close"  # 31d ≥ 30d


class _FakeStockMcp:
    """Broker double for the stock exit pass: positions, one resting stop, records every order."""
    def __init__(self, positions, stops=None):
        self.positions, self.stops, self.orders, self.cancelled = positions, stops or [], [], []

    def fetch_positions(self, acct):
        return self.positions

    def _call_tool(self, name, args=None):
        if name == "cancel_equity_order":
            self.cancelled.append(args["order_id"])
        return {"data": {"orders": self.stops}}

    def _data(self, payload):
        return payload["data"]

    def place_order(self, intent, state, buying_power, account_number):
        self.orders.append(intent)
        return {"status": "dry_run", "reason": ""}


def test_stock_exit_pass(monkeypatch, tmp_path):
    import config
    from alerts import agentic_stocks as stk
    from storage import agent_stock_book as book, peak_tracker
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    monkeypatch.setattr(book, "_FILE", str(tmp_path / "agent_stocks.json"))
    monkeypatch.setattr(peak_tracker, "_FILE", str(tmp_path / "peaks.json"))
    today = __import__("datetime").date.today()
    book.record_entry("HOLD", "target 8% gain, stop loss at 4%", opened=today)
    book.record_entry("STOP", "target 8% gain, stop loss at 4%", opened=today)
    fake = _FakeStockMcp(
        positions=[{"ticker": "HOLD", "shares": 2.5, "avg_cost": 10.0, "current_price": 10.3},
                   {"ticker": "STOP", "shares": 1.0, "avg_cost": 20.0, "current_price": 19.0},
                   {"ticker": "MINE", "shares": 5.0, "avg_cost": 5.0, "current_price": 1.0}],   # hand-bought
        stops=[{"id": "o-stop", "symbol": "STOP", "side": "sell", "type": "market",
                "stop_price": "19.20", "state": "queued", "time_in_force": "gtc"}])
    out = stk.run_exits(fake, "A", 40.0, verbose=False)

    by = {(o.ticker, o.order_type) for o in fake.orders}
    assert ("MINE", "market") not in by and all(o.ticker != "MINE" for o in fake.orders)   # never touched
    sell = next(o for o in fake.orders if o.ticker == "STOP")
    assert sell.side == "sell" and sell.order_type == "market" and sell.quantity == 1.0     # -5% ≤ -4% stop
    stop = next(o for o in fake.orders if o.ticker == "HOLD")
    assert (stop.order_type, stop.quantity, stop.stop_price, stop.time_in_force) == ("stop", 2, 9.6, "gtc")
    assert {r.get("close") or r.get("stop") for r in out} == {"STOP", "HOLD"}
    assert book.get_entry("STOP") is not None            # DRY_RUN changes nothing in the book

    # A failed/empty position read must never wipe the book.
    assert stk.run_exits(_FakeStockMcp(positions=[]), "A", 40.0, verbose=False) == []
    assert set(book.all_entries()) == {"HOLD", "STOP"}


def test_stock_exit_cancels_resting_stop_before_selling(monkeypatch, tmp_path):
    import config
    from alerts import agentic_stocks as stk
    from storage import agent_stock_book as book, peak_tracker
    monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(book, "_FILE", str(tmp_path / "agent_stocks.json"))
    monkeypatch.setattr(peak_tracker, "_FILE", str(tmp_path / "peaks.json"))
    monkeypatch.setattr(stk, "CANCEL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr("time.sleep", lambda s: None)
    book.record_entry("STOP", "target 8% gain, stop loss at 4%")
    resting = {"id": "o-stop", "symbol": "STOP", "side": "sell", "type": "market",
               "stop_price": "19.20", "state": "queued", "time_in_force": "gtc"}
    pos = [{"ticker": "STOP", "shares": 1.0, "avg_cost": 20.0, "current_price": 19.0}]

    class Live(_FakeStockMcp):
        def __init__(self, cancel_confirms):
            super().__init__(pos, [dict(resting)])
            self.confirms = cancel_confirms

        def _call_tool(self, name, args=None):
            if name == "cancel_equity_order":
                self.cancelled.append(args["order_id"])
                if self.confirms:
                    self.stops[0]["state"] = "cancelled"
            return {"data": {"orders": self.stops}}

        def place_order(self, intent, state, buying_power, account_number):
            self.orders.append(intent)
            return {"status": "placed", "reason": "sent"}

    ok = Live(cancel_confirms=True)
    out = stk.run_exits(ok, "A", 40.0, verbose=False)
    assert ok.cancelled == ["o-stop"] and ok.orders[0].order_type == "market"   # cancel THEN sell
    assert out[0]["status"] == "placed" and book.get_entry("STOP") is None      # live close forgets plan

    book.record_entry("STOP", "target 8% gain, stop loss at 4%")
    stuck = Live(cancel_confirms=False)
    out = stk.run_exits(stuck, "A", 40.0, verbose=False)
    assert stuck.orders == [] and out[0]["status"] == "deferred"                # never sell into a held stop


def test_decision_log_roundtrip_and_link():
    from storage import decision_log as dl
    e = dl.record("entry", "f", "placed", "stock entry", key="eq:F", inputs={"conviction": 68})
    dl.record("entry", "F", "skip", "already held", key="eq:F")
    x = dl.record("exit", "F", "placed", "+8% target", key="eq:F", entry_id=dl.last_entry_id("eq:F"),
                  pnl_pct=8.1)
    recs = dl.read()
    assert [r["kind"] for r in recs] == ["entry", "entry", "exit"]
    assert recs[0]["ticker"] == "F" and recs[0]["mode"] in ("dry", "live") and recs[0]["inputs"]["conviction"] == 68
    assert recs[2]["id"] == x and recs[2]["entry_id"] == e          # exit links to the TAKEN entry, not the skip
    assert dl.read(limit=1)[0]["id"] == x and dl.last_entry_id("eq:NONE") is None
    assert dl.signal_inputs({"conviction": 70, "entry_rationale": "long text", "source": None}) == {"conviction": 70}


def test_entry_loop_logs_every_candidate(monkeypatch):
    from alerts import agentic_options as ao
    from alerts import agentic_stocks as stk
    from storage import decision_log as dl
    sigs = [{"ticker": "HELD", "direction": "buy", "conviction": 80},
            {"ticker": "BTC", "direction": "buy", "conviction": 60, "asset_type": "crypto"},
            {"ticker": "NOPE", "direction": "buy", "conviction": 80},       # ≥75 → options first
            {"ticker": "LATE", "direction": "buy", "conviction": 50}]
    monkeypatch.setattr(ao, "_signals", lambda: sigs)
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: {"HELD"})
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral"})
    monkeypatch.setattr(stk, "held_tickers", lambda mcp, acct: set())
    monkeypatch.setattr(stk, "budget_cap", lambda: 0.0)                     # stock cap full → shares decline too
    monkeypatch.setattr(ao, "_try_option_entry", lambda *a, **k: (None, 0.0))
    ao._run_entries(mcp=None, acct="A", buying_power=40.0, verbose=False, max_new=None)
    by = {r["ticker"]: r for r in dl.read()}
    assert by["HELD"]["reason"].startswith("already held")
    assert by["BTC"]["action"] == "skip" and "not tradeable" in by["BTC"]["reason"]
    assert by["NOPE"]["reason"] == ("options: no applicable strategy or affordable/liquid contract; "
                                    "shares: size below the $1 minimum or stock cap full")  # fallback failed too
    assert by["LATE"]["reason"].startswith("shares: size below") and by["LATE"]["leg"] == "stock"
    assert all(r["kind"] == "entry" and r["inputs"]["buying_power"] == 40.0 for r in by.values())


def test_risk_bucket_matches_strategy_expectations():
    from alerts.agentic_options import _risk_bucket
    from analysis.options_strategies import short_dte_momentum
    assert _risk_bucket("risk-on (favorable for longs)") == "risk_on"      # was 'risk_on (favorable…)'
    assert _risk_bucket("risk-off (defensive)") == "risk_off"
    assert _risk_bucket("neutral / mixed") == "neutral" and _risk_bucket(None) == "neutral"
    sig = {"ticker": "X", "direction": "buy", "conviction": 85}
    assert short_dte_momentum(sig, {"risk": _risk_bucket("risk-on (favorable for longs)")}) is not None


def test_agent_context_helpers():
    from datetime import datetime
    from analysis.agent_context import pct_move, session_elapsed, volume_pace
    assert pct_move(105, 100) == 5.0 and pct_move(None, 100) is None and pct_move(1, 0) is None
    assert session_elapsed(datetime(2026, 9, 28, 9, 30)) == 0.0
    assert session_elapsed(datetime(2026, 9, 28, 12, 45)) == 0.5            # 195 of 390 min
    assert session_elapsed(datetime(2026, 9, 28, 17, 0)) == 1.0
    assert session_elapsed(datetime(2026, 11, 27, 11, 15), half_day=True) == 0.5
    assert volume_pace(0.5, 0.5) == 1.0          # half the avg volume by mid-day = a normal day's pace
    assert volume_pace(0.5, 0.01) is None and volume_pace(None, 0.5) is None


def test_agent_track_record_links_exits_to_entries():
    from analysis.agent_context import track_record
    recs = [
        {"id": "e1", "kind": "entry", "action": "placed", "inputs": {"source": "pipeline"},
         "order": {"strategy": "catalyst_momentum"}},
        {"id": "e2", "kind": "entry", "action": "placed", "inputs": {"source": "chat"}, "leg": "stock"},
        {"id": "x1", "kind": "exit", "action": "placed", "entry_id": "e1", "pnl_pct": 40.0},
        {"id": "x2", "kind": "exit", "action": "placed", "entry_id": "e2", "pnl_pct": -4.0},
        {"id": "x3", "kind": "exit", "action": "rejected", "entry_id": "e2", "pnl_pct": -9.0},   # not a close
        {"id": "x4", "kind": "exit", "action": "placed", "entry_id": "gone", "pnl_pct": 99.0},  # unlinked
    ]
    r = track_record(recs)
    assert r["overall"] == {"n": 2, "win_rate": 50, "avg_pnl": 18.0}
    assert r["by_source"]["chat"] == {"n": 1, "win_rate": 0, "avg_pnl": -4.0}
    assert set(r["by_strategy"]) == {"catalyst_momentum", "stock"}
    assert track_record([]) == {"overall": {"n": 0}, "by_source": {}, "by_strategy": {}}


def test_agent_context_build_and_render():
    from analysis.agent_context import build, render
    sig = {"ticker": "lly", "direction": "buy", "conviction": 52, "source": "watch",
           "entry_rationale": "GLP-1 demand", "trigger_fired": "broke above $1210.7 (now $1215)"}
    hist = {"current_price": 1215.0, "pct_change_14d": 5.3, "trend_14d": "uptrend", "rsi_14": 58.1,
            "macd_state": "bullish", "vol_vs_avg": 0.9, "avg_daily_range_pct": 2.1,
            "key_levels": {"nearest_support": 1150.0, "nearest_resistance": None, "atr_abs": 25.5,
                           "stop_pct_atr": 3.7, "target_pct_resist": None, "reward_risk": None}}
    ctx = build(sig, history=hist, rec_price=1190.0, news=[{"title": "Lilly beats", "source": "RTRS",
                                                           "published": "10:05"}],
                earnings={"days_to_earnings": 30}, sector="Healthcare",
                regime={"regime": "risk-on (favorable for longs)", "vix": 16.1, "vix_state": "normal"},
                holdings={"MRK": "Healthcare", "F": "Consumer Cyclical"}, elapsed=0.5)
    assert ctx["ticker"] == "LLY" and ctx["price"]["move_since_rec_pct"] == 2.1
    assert ctx["tape"]["volume_pace"] == 1.8 and ctx["book"]["same_sector"] == ["MRK"]
    text = render(ctx)
    assert "CANDIDATE LLY" in text and "(2.1% since)" in text and "[FIRED: broke above" in text
    assert "SAME SECTOR already held: MRK" in text and "Lilly beats" in text and "AGENT RECORD 0 closed" in text
    bare = render(build({"ticker": "X", "direction": "buy"}))            # every piece missing → n/a, no crash
    assert "PRICE now $n/a" in bare and "No company headlines" in bare and "source pipeline" in bare


def test_parse_verdict_clamps_and_rejects():
    from analysis.agent_judge import parse_verdict as pv
    ok = pv({"decision": "enter", "size_multiplier": 1.7, "instrument": "shares", "stop_pct": 3.456,
             "confidence": 140, "thesis": "t", "invalidation": "below $10", "risks": ["a", "b", "c", "d"]})
    assert ok["size_multiplier"] == 1.0 and ok["confidence"] == 100          # can never enlarge
    assert ok["stop_pct"] == 3.46 and ok["risks"] == ["a", "b", "c"]
    w = pv({"decision": "wait", "size_multiplier": 0.8, "instrument": "x", "confidence": 50})
    assert w["size_multiplier"] == 0.0 and w["instrument"] == "either"       # wait/skip carry no size
    assert pv({"decision": "enter", "size_multiplier": 0, "confidence": 60})["decision"] == "skip"
    assert pv({"decision": "enter", "size_multiplier": "big", "stop_pct": "wide"})["decision"] == "skip"
    bad = pv({"decision": "yolo"})
    assert bad["decision"] == "skip" and bad["error"] == "malformed verdict"
    assert pv(None)["decision"] == "skip"


def _real_ctx():
    from analysis.agent_context import build
    return build({"ticker": "LLY", "direction": "buy", "conviction": 60}, history={"current_price": 100})


def test_judge_entry_shadow_call_and_failure(monkeypatch):
    import config
    import llm_budget
    from analysis import agent_judge as aj
    spent = []
    monkeypatch.setattr(llm_budget, "can_spend", lambda: True)
    monkeypatch.setattr(llm_budget, "record_cost", spent.append)
    monkeypatch.setattr(config, "AGENT_JUDGE", "shadow", raising=False)
    seen = {}

    def fake(briefing, plan):
        seen.update(briefing=briefing, plan=plan)
        return ({"decision": "wait", "size_multiplier": 0, "instrument": "shares", "confidence": 40,
                 "thesis": "needs volume", "invalidation": "loses support"}, {"input_tokens": 1500,
                                                                               "output_tokens": 300})
    monkeypatch.setattr(aj, "_call_model", fake)
    v = aj.judge_entry(_real_ctx(), "stock", 8.0)
    assert v["decision"] == "wait" and v["mode"] == "shadow" and v["cost_usd"] == 0.0090
    assert spent == [0.009] and "CANDIDATE LLY" in seen["briefing"] and "$8.00 of shares" in seen["plan"]

    monkeypatch.setattr(aj, "_call_model", lambda *a: (_ for _ in ()).throw(RuntimeError("503")))
    v = aj.judge_entry(_real_ctx(), "option")
    assert v["decision"] == "skip" and "503" in v["error"]                   # never raises

    monkeypatch.setattr(llm_budget, "can_spend", lambda: False)
    assert "credit" in aj.judge_entry(_real_ctx(), "stock")["error"]
    monkeypatch.setattr(config, "AGENT_JUDGE", "off", raising=False)
    assert aj.judge_entry(_real_ctx(), "stock") is None
    monkeypatch.setattr(config, "AGENT_JUDGE", "binding", raising=False)     # not implemented until J5
    assert aj.judge_entry(_real_ctx(), "stock") is None and aj.mode() == "off"
    assert aj.judge_entry({"stub": True}, "stock") is None                  # no briefing → no call


def test_shadow_judge_never_changes_the_trade(monkeypatch):
    from alerts import agentic_options as ao
    from alerts import agentic_stocks as stk
    from analysis import agent_context, agent_judge
    from storage import decision_log as dl
    monkeypatch.setattr(ao, "_signals", lambda: [{"ticker": "CORE", "direction": "buy", "conviction": 60,
                                                   "risk_level": "medium"}])
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: set())
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral", "detail": {}})
    monkeypatch.setattr(stk, "held_tickers", lambda mcp, acct: set())
    monkeypatch.setattr(stk, "budget_cap", lambda: None)
    monkeypatch.setattr(agent_context, "gather", lambda *a, **k: _real_ctx())
    monkeypatch.setattr(agent_judge, "judge_entry", lambda *a, **k: {"decision": "skip", "size_multiplier": 0.0,
                                                                      "thesis": "priced in"})
    placed = []

    class FakeMcp:
        def place_order(self, intent, state, buying_power, account_number):
            placed.append(intent.ticker)
            return {"status": "dry_run", "reason": ""}

    ao._run_entries(FakeMcp(), "A", 40.0, verbose=False, max_new=None)
    assert placed == ["CORE"]                                               # judge said skip; rules still traded
    rec = dl.read()[-1]
    assert rec["action"] == "dry_run" and rec["judge"]["decision"] == "skip"
    assert rec["context"]["ticker"] == "LLY"                                # the briefing rides along


def test_watch_trigger_fired_rules():
    from alerts.agentic_watch import fired, needs_close
    pull = "Pullback to support ~$321.88 on volume before a long entry"
    assert fired(pull, 320.0, False).startswith("pulled back to $321.88")      # at/under support, within 3%
    assert fired(pull, 330.0, False) is None                                   # hasn't pulled back
    assert fired(pull, 300.0, False) is None                                   # crashed through (>3% below)
    brk = "Break above $344.03 resistance on volume"
    assert fired(brk, 346.0, False).startswith("broke above $344.03")
    assert fired(brk, 360.0, False) is None                                    # already ran >3% — don't chase
    close = "Confirmed bounce and close above $256.16 resistance"
    assert needs_close(close) and not needs_close(brk)
    assert fired(close, 257.0, near_close=False) is None                       # intraday ≠ a close
    assert fired(close, 257.0, near_close=True).startswith("broke above")
    assert fired("", 100, True) is None and fired(pull, None, True) is None


def test_watch_near_close_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from alerts.agentic_watch import is_near_close
    et = ZoneInfo("America/New_York")
    assert is_near_close(datetime(2026, 9, 28, 15, 40, tzinfo=et)) is True     # Monday 3:40 PM ET
    assert is_near_close(datetime(2026, 9, 28, 14, 0, tzinfo=et)) is False
    assert is_near_close(datetime(2026, 9, 27, 15, 45, tzinfo=et)) is False     # Sunday


def test_watch_candidates_and_shares_only_route():
    from alerts.agentic_watch import candidates
    from alerts.agentic_stocks import route
    recs = [{"ticker": "LLY", "direction": "watch", "conviction": 52, "entry_trigger": "breaks above $1,210.70"},
            {"ticker": "AMD", "direction": "watch", "conviction": 35, "entry_trigger": "pullback to $507"},
            {"ticker": "NVDA", "direction": "buy", "conviction": 80, "entry_trigger": "now"},
            {"ticker": "META", "direction": "watch", "conviction": 60, "entry_trigger": "now"}]
    pins = [{"ticker": "AMD", "trigger_text": "pullback to $500", "exit_condition": "target 6% gain"}]
    got = {c["ticker"]: c for c in candidates(recs, pins)}
    assert set(got) == {"LLY", "AMD"}                                          # low-conv AMD only via its pin
    assert got["AMD"]["source"] == "pinned-watch" and got["AMD"]["entry_trigger"] == "pullback to $500"
    sig = {"ticker": "LLY", "direction": "buy", "conviction": 90, "shares_only": True}
    assert route(sig) == "stock"                                               # even at conv ≥75: never options
    assert route(dict(sig, asset_type="crypto")) is None                       # crypto can't be held as shares


def test_fired_watch_is_judged_and_bought_as_shares(monkeypatch):
    from alerts import agentic_options as ao
    from alerts import agentic_stocks as stk
    from alerts import agentic_watch
    from analysis import agent_context, agent_judge
    from storage import decision_log as dl
    monkeypatch.setattr(ao, "_signals", lambda: [])
    monkeypatch.setattr(agentic_watch, "watch_signals", lambda **k: [
        {"ticker": "LLY", "direction": "buy", "conviction": 52, "shares_only": True, "source": "watch",
         "trigger_fired": "broke above $1210.7 (now $1215)", "exit_condition": "target 6% gain, stop loss at 3.8%"}])
    monkeypatch.setattr(ao, "_held_underlyings", lambda mcp, acct: set())
    monkeypatch.setattr(ao, "_regime", lambda: {"risk": "neutral", "detail": {}})
    monkeypatch.setattr(stk, "held_tickers", lambda mcp, acct: set())
    monkeypatch.setattr(stk, "budget_cap", lambda: None)
    monkeypatch.setattr(agent_context, "gather", lambda *a, **k: _real_ctx())
    monkeypatch.setattr(agent_judge, "judge_entry", lambda *a, **k: {"decision": "wait", "size_multiplier": 0.0})

    placed = []

    class FakeMcp:
        def place_order(self, intent, state, buying_power, account_number):
            placed.append((intent.ticker, intent.order_type))
            return {"status": "dry_run", "reason": ""}

    ao._run_entries(FakeMcp(), "A", 40.0, verbose=False, max_new=None)
    assert placed == [("LLY", "market")]                                       # a share buy, never an option
    rec = dl.read()[-1]                                                        # shadow judge: logged, not binding
    assert rec["ticker"] == "LLY" and rec["action"] == "dry_run" and rec["judge"]["decision"] == "wait"
    assert rec["inputs"]["source"] == "watch"


def test_holding_review_events():
    from analysis.holding_review import events, mark_reviewed, headline_id
    st = {"anchor_price": 100.0, "adr_pct": 2.0, "days_to_earnings": 5, "seen": [headline_id("Old news")]}
    h = [{"title": "Old news"}, {"title": "FDA rejects drug"}]
    assert events(st, 101.0, h, "2026-09-28") == ["news: FDA rejects drug"]          # seen headline ignored
    assert events(st, 97.0, [], "2026-09-28") == ["move -3.0% since $100 (≥1.5× its 2% daily range)"]
    assert events(st, 102.9, [], "2026-09-28") == []                                   # 2.9% < 3% (1.5×2%)
    st2 = dict(st, days_to_earnings=1)
    assert events(st2, 100.0, [], "2026-09-28") == ["earnings in 1 day(s)"]
    after = mark_reviewed(st2, 97.0, h, "2026-09-28", ["earnings in 1 day(s)"])
    assert after["anchor_price"] == 97.0 and after["earnings_flagged"] == "2026-09-28"
    assert events(after, 97.0, h, "2026-09-28") == []                                  # nothing re-fires today
    assert events({}, 50.0, [], "2026-09-28") == []                                    # no reference yet
    # News cooldown: within 2h of a review, new headlines wait (still unseen → batched later);
    # a big move still fires immediately.
    from datetime import datetime, timedelta
    now = datetime(2026, 9, 28, 11, 0)
    cool = mark_reviewed(st, 100.0, [], "2026-09-28", [], now=now - timedelta(minutes=30))
    assert events(cool, 100.0, h, "2026-09-28", now) == []
    assert events(cool, 96.0, h, "2026-09-28", now) == ["move -4.0% since $100 (≥1.5× its 2% daily range)"]
    assert events(cool, 100.0, h, "2026-09-28", now + timedelta(hours=2)) == ["news: FDA rejects drug"]


def test_parse_holding_verdict():
    from analysis.agent_judge import parse_holding_verdict as ph
    t = ph({"action": "tighten_stop", "new_stop_price": 98.456, "confidence": 70,
            "what_changed": "downgrade", "reasoning": "lock gains"}, price=105.0, current_stop=96.0)
    assert t["action"] == "tighten_stop" and t["new_stop_price"] == 98.46
    loosen = ph({"action": "tighten_stop", "new_stop_price": 95.0, "confidence": 70}, price=105.0, current_stop=96.0)
    assert loosen["action"] == "keep" and "invalid tighten" in loosen["error"]          # can't loosen
    above = ph({"action": "tighten_stop", "new_stop_price": 106.0}, price=105.0, current_stop=96.0)
    assert above["action"] == "keep"                                                   # stop above price
    opt = ph({"action": "tighten_stop", "new_stop_price": 1.0}, price=5.0, current_stop=None, allow_tighten=False)
    assert opt["action"] == "keep"                                                     # options: keep|sell only
    assert ph({"action": "sell", "confidence": 90})["action"] == "sell"
    assert ph({"action": "panic"})["action"] == "keep" and ph(None)["error"] == "malformed verdict"


def test_holding_review_fires_judges_and_logs(monkeypatch):
    from analysis import agent_context, agent_judge, holding_review as hr
    from storage import decision_log as dl
    review = hr._review_impl
    monkeypatch.setattr(hr, "_daily_reads", lambda t, st, today: {**st, "adr_pct": 2.0, "days_to_earnings": 20})
    monkeypatch.setattr(agent_context, "_news_since", lambda t, since: [{"title": "Company cuts guidance"}])
    monkeypatch.setattr(agent_context, "gather", lambda *a, **k: _real_ctx())
    seen = {}

    def fake_judge(ctx, text, **kw):
        seen.update(text=text, **kw)
        return {"action": "sell", "reasoning": "guidance cut breaks the growth thesis"}
    monkeypatch.setattr(agent_judge, "judge_holding", fake_judge)
    e = dl.record("entry", "F", "placed", "stock entry", key="eq:F",
                  judge={"thesis": "EV demand", "invalidation": "below $11"})
    pos = {"ticker": "F", "key": "eq:F", "kind": "stock", "price": 12.0, "entry": 12.5, "now": 12.0,
           "pnl_pct": -4.0, "days_held": 3, "plan": "target 8% gain, stop loss at 4%", "current_stop": 11.9}
    out = review(pos, holdings={"F", "T"}, regime={"regime": "risk-on"})
    assert out["events"] == ["news: Company cuts guidance"] and out["judge"]["action"] == "sell"
    assert "Original thesis: EV demand" in seen["text"] and "RULES' ACTION: hold" in seen["text"]
    assert seen["allow_tighten"] is True and seen["current_stop"] == 11.9
    rec = dl.read()[-1]
    assert rec["kind"] == "review" and rec["action"] == "hold" and rec["entry_id"] == e
    assert review(pos, holdings={"F"}, regime={}) is None                  # same headline → no second review


def test_judge_verdict_reuse():
    from datetime import datetime, timedelta
    from analysis.agent_judge import reusable_verdict
    now = datetime(2026, 9, 28, 11, 0)
    rec = {"id": "r1", "kind": "entry", "ticker": "LLY", "ts": (now - timedelta(minutes=40)).isoformat(),
           "judge": {"decision": "wait", "size_multiplier": 0.0, "cost_usd": 0.012},
           "context": {"price": {"now": 100.0}}}
    got = reusable_verdict([rec], "LLY", 101.0, now)
    assert got["decision"] == "wait" and got["reused_from"] == "r1" and got["cost_usd"] == 0.0
    assert reusable_verdict([rec], "LLY", 102.0, now) is None                  # moved 2% > 1.5% → re-judge
    assert reusable_verdict([rec], "LLY", 100.0, now + timedelta(hours=2)) is None   # too old
    assert reusable_verdict([rec], "AMD", 100.0, now) is None
    err = dict(rec, judge={"decision": "skip", "error": "judge call failed"})
    assert reusable_verdict([err], "LLY", 100.0, now) is None                  # never reuse a failure


def _closes(start_day, prices):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start_day)
    return [(d0 + timedelta(days=i), p) for i, p in enumerate(prices)]


def test_forward_return_and_dedupe():
    from datetime import date
    from analysis.judge_scorecard import forward_return, first_per_day
    closes = _closes("2026-09-28", [100, 102, 99, 105, 104, 110])
    assert forward_return(closes, date(2026, 9, 28), 100.0, 1) == 2.0
    assert forward_return(closes, date(2026, 9, 28), 100.0, 5) == 10.0
    assert forward_return(closes, date(2026, 9, 28), None, 1) == 2.0          # falls back to that day's close
    assert forward_return(closes, date(2026, 10, 2), 104.0, 5) is None         # not matured yet
    recs = [{"kind": "entry", "ticker": "A", "ts": "2026-09-28T09:00", "judge": {"decision": "skip"}},
            {"kind": "entry", "ticker": "A", "ts": "2026-09-28T09:20", "judge": {"decision": "enter"}},
            {"kind": "entry", "ticker": "A", "ts": "2026-09-28T09:40",
             "judge": {"decision": "skip", "reused_from": "x"}},
            {"kind": "entry", "ticker": "B", "ts": "2026-09-28T09:00", "judge": {"decision": "skip", "error": "e"}}]
    assert [r["ts"] for r in first_per_day(recs, "entry")] == ["2026-09-28T09:00"]


def test_judge_scorecard_scoring_and_conclusion():
    from analysis import judge_scorecard as js
    closes = {"UP": _closes("2026-09-01", [100 + i for i in range(40)]),     # rises every day
              "DN": _closes("2026-09-01", [100 - i for i in range(40)])}     # falls every day

    def rec(t, day, decision, direction="buy"):
        return {"kind": "entry", "ticker": t, "ts": f"2026-09-{day:02d}T10:00", "inputs": {"direction": direction},
                "judge": {"decision": decision, "size_multiplier": 1.0 if decision == "enter" else 0.0}}
    recs = [rec("UP", d, "enter") for d in range(1, 16)] + [rec("DN", d, "skip") for d in range(1, 16)]
    s = js.entry_scores(recs, lambda t: closes[t])
    assert s["enter"][5]["n"] == 15 and s["enter"][5]["avg"] > 0 and s["not_enter"][5]["avg"] < 0
    c = js.conclusion(s, {"judge_enter": {"n": 0}, "judge_wait_or_skip": {"n": 0}})
    assert c["ready"] and c["helps"] and c["edge_pp"] > js.HELPS_MARGIN_PP
    # a bearish idea the judge entered is scored on the DROP (sign flipped)
    short = js.entry_scores([rec("DN", 1, "enter", "short")], lambda t: closes[t])
    assert short["enter"][1]["avg"] > 0
    thin = js.conclusion(js.entry_scores(recs[:3], lambda t: closes[t]), {})
    assert not thin["ready"] and "Not enough data" in thin["text"]
    taken = js.taken_trade_scores([
        {"id": "e1", "kind": "entry", "action": "placed", "judge": {"decision": "skip"}},
        {"id": "x1", "kind": "exit", "action": "placed", "entry_id": "e1", "pnl_pct": -12.0}])
    assert taken["judge_wait_or_skip"] == {"n": 1, "avg": -12.0, "win_rate": 0}
    rv = js.review_scores([{"kind": "review", "ticker": "DN", "ts": "2026-09-02T10:00",
                            "judge": {"action": "sell"}}], lambda t: closes[t])
    assert rv["sell"]["n"] == 1 and rv["sell"]["avg"] < 0                      # a good sell precedes a drop


def test_entry_budget_fails_closed():
    from alerts.agentic_options import _entry_budget

    class Boom:
        def day_trade_budget(self, *a):
            raise RuntimeError("orders unreadable")
    assert _entry_budget(Boom(), "A", 42.0, verbose=False) == 0


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
    intent = OrderIntent(ticker="ABC", side="sell", quantity=2, order_type="stop",
                         stop_price=97.5, client_id="x1")
    args = _order_args(intent, "AGENTIC123")
    assert args["type"] == "stop_market" and args["stop_price"] == "97.50"
    assert args["account_number"] == "AGENTIC123" and args["quantity"] == "2"
    assert "ref_id" not in args and "dollar_amount" not in args   # ref_id is place-only; one of qty/$


def test_order_args_sends_exactly_one_of_qty_or_dollars():
    from ingestion.robinhood_mcp import _order_args
    buy = _order_args(OrderIntent(ticker="F", side="buy", dollars=5.0), "A")
    assert buy["dollar_amount"] == "5.00" and "quantity" not in buy and buy["type"] == "market"
    sell = _order_args(OrderIntent(ticker="F", side="sell", quantity=0.1 + 0.2), "A")
    assert sell["quantity"] == "0.3" and "dollar_amount" not in sell   # no float noise


def test_order_shape_error():
    from ingestion.robinhood_mcp import _order_shape_error as err
    ok = [
        OrderIntent(ticker="F", side="buy", dollars=5.0),                                    # $ market
        OrderIntent(ticker="F", side="sell", quantity=0.25),                                 # frac market
        OrderIntent(ticker="F", side="buy", quantity=2, order_type="limit", limit_price=12.7),
        OrderIntent(ticker="F", side="sell", quantity=3, order_type="stop", stop_price=11.0),
    ]
    for i in ok:
        assert err(i) is None, i
    assert "exactly one" in err(OrderIntent(ticker="F", side="buy", dollars=5.0, quantity=1))
    assert "exactly one" in err(OrderIntent(ticker="F", side="buy"))
    assert "market" in err(OrderIntent(ticker="F", side="buy", dollars=5.0, order_type="limit", limit_price=1))
    assert "minimum" in err(OrderIntent(ticker="F", side="buy", dollars=0.5))
    assert "fractional" in err(OrderIntent(ticker="F", side="sell", quantity=0.5, order_type="stop", stop_price=1))
    assert "fractional" in err(OrderIntent(ticker="F", side="buy", quantity=1.5, order_type="limit", limit_price=1))
    assert "positive" in err(OrderIntent(ticker="F", side="sell", quantity=0))
    assert "limit_price" in err(OrderIntent(ticker="F", side="buy", quantity=1, order_type="limit"))
    assert "stop_price" in err(OrderIntent(ticker="F", side="sell", quantity=1, order_type="stop"))
    assert "unknown" in err(OrderIntent(ticker="F", side="buy", dollars=5.0, order_type="bogus"))


def test_place_order_rejects_bad_shape_even_in_dry_run(monkeypatch):
    import config
    from ingestion import robinhood_mcp as mcp
    monkeypatch.setattr(config, "DRY_RUN", True, raising=False)
    st = GuardState(start_equity=1000.0)
    res = mcp.place_order(OrderIntent(ticker="F", side="buy", dollars=0.5, client_id="s1"), st, buying_power=500.0)
    assert res["status"] == "rejected" and "minimum" in res["reason"] and st.orders_today == 0


def test_live_placements_send_fresh_uuid_ref_id(monkeypatch):
    # Server dedups by ref_id → each placement gets its own UUID (never the reusable client_id),
    # and it goes ONLY to place_* (the review tools' schemas have no ref_id).
    import uuid
    import config
    from ingestion import robinhood_mcp as mcp
    from trading_guards import OptionOrderIntent
    monkeypatch.setattr(config, "DRY_RUN", False, raising=False)
    calls = []
    def fake(name, args=None):
        calls.append((name, dict(args or {})))
        return {"data": {"order_checks": {}}}
    monkeypatch.setattr(mcp, "_call_tool", fake)

    st = GuardState(start_equity=1000.0)
    for cid in ("same-a", "same-b"):   # distinct client_ids pass the local dup guard
        mcp.place_order(OrderIntent(ticker="F", side="sell", quantity=1, order_type="stop",
                                    stop_price=11.0, client_id=cid), st, buying_power=500.0,
                        account_number="AGENTIC")
    opt = OptionOrderIntent(underlying="F", option_id="o1", right="call", side="buy",
                            position_effect="open", quantity=1, price=0.10, client_id="o-1")
    mcp.place_option_order(opt, st, buying_power=500.0, account_number="AGENTIC")

    placed = [a for n, a in calls if n.startswith("place_")]
    reviews = [a for n, a in calls if n.startswith("review_")]
    refs = [a["ref_id"] for a in placed]
    assert len(placed) == 3 and len(set(refs)) == 3
    assert all(str(uuid.UUID(r)) == r for r in refs)                 # real UUIDs, not client_ids
    assert reviews and all("ref_id" not in a for a in reviews)


def test_delete_position(monkeypatch):
    from storage import positions as sp
    store = [{"ticker": "F", "opened_at": "t1", "status": "closed"},
             {"ticker": "AAPL", "opened_at": "t2", "status": "closed"}]
    saved = {}
    monkeypatch.setattr(sp, "load_positions", lambda: [dict(p) for p in store])
    monkeypatch.setattr(sp, "save_positions", lambda ps: saved.update(ps=ps))
    assert sp.delete_position("t1") is True
    assert [p["ticker"] for p in saved["ps"]] == ["AAPL"]      # only the matching record removed
    assert sp.delete_position("missing") is False              # no match → no-op
    assert sp.delete_position("") is False                     # empty guard


def test_chat_direction_resolution():
    from alerts.agentic_options import _chat_direction
    assert _chat_direction({"action": "buy", "trigger_text": "buy above $50"}) == "buy"
    assert _chat_direction({"action": "short", "trigger_text": "weak"}) == "short"
    # the TLT bug: 'Buy — TLT (short)' → the text says short → treat as short (puts), not calls
    assert _chat_direction({"action": "buy", "trigger_text": "(short), exit 3% gain"}) == "short"
    assert _chat_direction({"action": "buy", "trigger_text": "bearish setup"}) == "short"
    assert _chat_direction({"action": "watch", "trigger_text": "buy above $x"}) is None   # not an entry
    assert _chat_direction({"action": "sell", "trigger_text": "take profit"}) is None      # exit, not short


# ── Persistent MCP session (ingestion/mcp_session.py) ─────────────────────────
def _fake_opener(log, behave, fail_open_on=()):
    """Async-context-manager opener yielding a fake session. `behave(name, session_idx, args)` returns
    a result or raises; `fail_open_on` = session indexes whose OPEN raises."""
    import contextlib

    @contextlib.asynccontextmanager
    async def opener():
        log["opens"] += 1
        idx = log["opens"]
        if idx in fail_open_on:
            raise ValueError(f"open {idx} failed")

        class _S:
            async def call_tool(self, name, arguments=None):
                log["calls"].append((idx, name))
                return behave(name, idx, arguments or {})
        yield _S()
    return opener


def test_mcp_session_is_retryable():
    from ingestion.mcp_session import is_retryable
    assert is_retryable("get_portfolio") and is_retryable("review_option_order")
    assert not is_retryable("place_option_order") and not is_retryable("place_equity_order")
    assert not is_retryable("cancel_order")


def test_mcp_session_reuses_one_connection():
    from ingestion.mcp_session import PersistentSession
    log = {"opens": 0, "calls": []}
    ps = PersistentSession(_fake_opener(log, lambda n, i, a: f"{n}:{a.get('k')}"))
    assert [ps.call("get_x", {"k": k}) for k in range(5)] == [f"get_x:{k}" for k in range(5)]
    assert log["opens"] == 1                                   # handshake paid once


def test_mcp_session_retries_a_read_once_on_a_fresh_session():
    from ingestion.mcp_session import PersistentSession
    import pytest
    log = {"opens": 0, "calls": []}

    def behave(name, idx, args):
        if idx == 1:
            raise RuntimeError("connection dropped")
        return "ok"
    ps = PersistentSession(_fake_opener(log, behave))
    assert ps.call("get_quotes") == "ok"
    assert log["calls"] == [(1, "get_quotes"), (2, "get_quotes")] and log["opens"] == 2

    log2 = {"opens": 0, "calls": []}                          # retry fails too → error surfaces
    ps2 = PersistentSession(_fake_opener(log2, lambda n, i, a: (_ for _ in ()).throw(RuntimeError("down"))))
    with pytest.raises(RuntimeError):
        ps2.call("get_quotes")
    assert len(log2["calls"]) == 2                             # exactly one retry, no loop


def test_mcp_session_never_retries_an_order():
    from ingestion.mcp_session import PersistentSession
    import pytest
    log = {"opens": 0, "calls": []}

    def behave(name, idx, args):
        if name.startswith("place_") and idx == 1:
            raise RuntimeError("response lost")
        return "ok"
    ps = PersistentSession(_fake_opener(log, behave))
    with pytest.raises(RuntimeError):
        ps.call("place_option_order", {"qty": 1})
    assert log["calls"] == [(1, "place_option_order")]          # attempted ONCE — no double-place
    assert ps.call("get_x") == "ok" and log["opens"] == 2       # broken session was closed + reopened


def test_mcp_session_open_failure_propagates_then_recovers():
    from ingestion.mcp_session import PersistentSession
    import pytest
    log = {"opens": 0, "calls": []}
    ps = PersistentSession(_fake_opener(log, lambda n, i, a: "ok", fail_open_on={1}))
    with pytest.raises(ValueError):
        ps.call("get_x")
    assert ps.call("get_x") == "ok" and log["opens"] == 2


def test_mcp_session_idle_close_and_reset():
    import time as _t
    from ingestion.mcp_session import PersistentSession
    log = {"opens": 0, "calls": []}
    ps = PersistentSession(_fake_opener(log, lambda n, i, a: "ok"), idle_seconds=0.05)
    ps.call("get_x"); _t.sleep(0.3); ps.call("get_x")
    assert log["opens"] == 2                                   # idle session closed, then reopened

    log2 = {"opens": 0, "calls": []}
    ps2 = PersistentSession(_fake_opener(log2, lambda n, i, a: "ok"))
    ps2.call("get_x"); ps2.reset(); ps2.call("get_x")
    assert log2["opens"] == 2                                  # reset forces a fresh session


def test_mcp_session_thread_safe():
    import threading
    from ingestion.mcp_session import PersistentSession
    log = {"opens": 0, "calls": []}
    ps = PersistentSession(_fake_opener(log, lambda n, i, a: a["i"]))
    out = {}
    threads = [threading.Thread(target=lambda i=i: out.__setitem__(i, ps.call("get_x", {"i": i})))
               for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert out == {i: i for i in range(10)} and log["opens"] == 1
