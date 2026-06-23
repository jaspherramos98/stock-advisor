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

def test_allocation_zero_budget_empty():
    assert calculate_allocations(_RECS, 0) == []

def test_compute_weight_conviction_drives_size():
    hi = _compute_weight({"conviction": 90, "risk_level": "low"})
    lo = _compute_weight({"conviction": 30, "risk_level": "low"})
    assert hi > lo
    # back-compat: missing conviction falls back to confidence_score×100
    assert _compute_weight({"confidence_score": 0.5, "risk_level": "low"}) == 0.5


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
