"""
Shared dashboard helpers: cached network readers (st.cache_data — process-wide, survives
reruns and is safe from the chat-proxy thread) and small pure/render helpers used by several tabs.
Never cache with a module-level dict in app.py: the script re-executes on every click.
"""
import streamlit as st
import pandas as pd
from ingestion.prices import fetch_prices


# Live buying power is read on every chat open AND for the header badge; cache it for a
# short TTL so reopening the chat / reruns don't re-hit Robinhood each time. Must be
# st.cache_data (process-wide), NOT a module-level dict: Streamlit re-executes this script on
# every click, which re-created the dict and silently defeated the cache (~1.3s per click).
_BP_TTL_SECONDS = 60


@st.cache_data(ttl=_BP_TTL_SECONDS, show_spinner=False)
def _cached_buying_power():
    try:
        from ingestion.account_reads import buying_power, is_available
        return buying_power() if is_available() else None
    except Exception:
        return None


def _live_buying_power(force: bool = False):
    """
    Returns live Robinhood buying power (float) or None, cached for _BP_TTL_SECONDS.
    `force=True` bypasses the cache (used by the sidebar 'Refresh buying power' button).
    Safe to call from the chat-proxy thread (st.cache_data is process-wide).
    """
    if force:
        _cached_buying_power.clear()
    return _cached_buying_power()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_prices(tickers: tuple) -> dict:
    """Live quotes cached 30s. Every click reruns the whole script and both the Portfolio and
    My Positions tabs quote the same holdings, so uncached this re-hit the broker on every click.
    Pass a sorted tuple so equal ticker sets share one cache entry."""
    return fetch_prices(list(tickers))


@st.cache_data(ttl=900, show_spinner=False)
def _cached_spy_benchmark(closed_positions: list):
    """SPY opportunity-cost line for the scorecard — a yfinance SPY download, so cache it (the
    closed-trade list only changes when a position is closed/removed, which changes the key)."""
    from analysis.scorecard import spy_benchmark
    return spy_benchmark(closed_positions)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_history():
    """Google Sheets export history, cached 10 min (it only changes on export, which clears it)."""
    from storage.sheets import read_history
    return read_history()


def _stop_loss_price(exit_condition: str, ref_price: float, direction: str = "buy"):
    """
    Parse "stop loss at X%" from an exit_condition and return the trigger PRICE, or
    None if there's no parseable stop or no reference price.
    - Long:  ref × (1 − X/100)  (you exit when price falls X% below entry)
    - Short: ref × (1 + X/100)  (you exit when price rises X% against you)
    """
    from analysis.exit_rules import parse_exit_condition
    stop_pct = parse_exit_condition(exit_condition)["stop_pct"] if ref_price else None
    if stop_pct is None:
        return None
    pct = stop_pct / 100.0
    return round(ref_price * (1 + pct), 2) if direction == "short" else round(ref_price * (1 - pct), 2)


@st.cache_data(ttl=900, show_spinner=False)
def _suggested_exit(ticker: str, asset_type: str = "stock"):
    """
    Structure-anchored exit for an EXISTING position — the same R24 math the analyst uses
    for new ideas (target = % to nearest resistance, stop = ATR-sized), applied to a ticker
    you already hold. Returns the key_levels dict or None. Cached 15 min so opening My
    Positions doesn't re-pull ~1y of history every Streamlit rerun.
    """
    try:
        from ingestion.prices import fetch_price_history
        data = fetch_price_history([ticker], asset_type=asset_type) or {}
        kl = ((data.get(ticker) or {}).get("key_levels")) or {}
        if kl.get("stop_pct_atr") is None and kl.get("target_pct_resist") is None:
            return None
        return kl
    except Exception as e:
        print(f"Suggested exit failed for {ticker}: {e}")
        return None


# --- Cached agentic reads (network-heavy; Streamlit reruns the whole script on every click, so
# uncaching these made every checkbox/button wait on ~10-15 MCP round-trips). 30-60s TTL keeps
# the Agent tab snappy; actions (run cycle / close) still use fresh data via the agent functions. ---
@st.cache_data(ttl=30, show_spinner=False)
def _c_agentic_bp(acct):
    from ingestion import robinhood_mcp as _m
    return _m.fetch_buying_power(acct)


@st.cache_data(ttl=30, show_spinner=False)
def _c_option_positions(acct):
    from ingestion import robinhood_mcp as _m, mcp_auth as _a
    d = _m._data(_m._unwrap_tool_result(_a.call_tool("get_option_positions", {"account_number": acct, "nonzero": True})))
    return d.get("positions", []) if isinstance(d, dict) else []


@st.cache_data(ttl=30, show_spinner=False)
def _c_option_quote(option_id):
    from ingestion import options_data as _od
    return _od.fetch_quote(option_id) or {}


@st.cache_data(ttl=30, show_spinner=False)
def _c_agentic_equity(acct):
    from ingestion import robinhood_mcp as _m
    return _m.fetch_positions(acct)


@st.cache_data(ttl=60, show_spinner=False)
def _c_day_trade_budget(acct, equity):
    """PDT: new positions the agent may open now ('exempt' ≥$25k, None = unreadable)."""
    from ingestion import robinhood_mcp as _m
    try:
        b = _m.day_trade_budget(acct, equity or 0.0)
    except Exception:  # noqa: BLE001 — display only; the agent itself fails closed
        return None
    return "exempt" if b is None else b


@st.cache_data(ttl=60, show_spinner=False)
def _c_option_orders(acct):
    from ingestion import robinhood_mcp as _m, mcp_auth as _a
    d = _m._data(_m._unwrap_tool_result(_a.call_tool("get_option_orders", {"account_number": acct})))
    return d.get("orders", []) if isinstance(d, dict) else []


def _render_alloc_table(allocations):
    """Render ONE allocation table (called once per enabled asset class, R28). Builds the
    DataFrame, applies the direction/risk/HR styling, and renders st.dataframe with the tuned
    column widths + 2-line row height so it fits without horizontal scroll."""
    if not allocations:
        return
    df = pd.DataFrame(allocations)
    df["flags"] = [
        ("⭐" if a.get("highly_recommended") else "") + ("⚠" if a.get("flagged") else "")
        for a in allocations
    ]

    def _short(text, limit=72):
        t = " ".join(str(text or "").split())
        return t if len(t) <= limit else t[:limit - 1].rstrip(" ,.;") + "…"

    df["entry_trigger"] = df["entry_trigger"].map(_short)
    df["exit_condition"] = df["exit_condition"].map(_short)
    df = df[[
        "ticker", "direction", "current_price", "change_pct",
        "dollar_amount", "percentage", "risk_level", "conviction", "confidence_score",
        "entry_trigger", "exit_condition", "flags",
    ]].rename(columns={
        "ticker": "Ticker", "direction": "Direction", "current_price": "Price",
        "change_pct": "Today", "dollar_amount": "Amount ($)", "percentage": "Alloc %",
        "risk_level": "Risk", "conviction": "Conviction", "confidence_score": "Confidence",
        "entry_trigger": "Buy when", "exit_condition": "Sell when", "flags": "Flags",
    })

    def color_direction(val):
        if val == "buy":   return "color: #2ecc71; font-weight: bold"
        if val == "short": return "color: #e74c3c; font-weight: bold"
        if val == "watch": return "color: #f39c12"
        return "color: #e74c3c"

    def color_risk(val):
        if val == "low":    return "color: #2ecc71"
        if val == "medium": return "color: #f39c12"
        return "color: #e74c3c; font-weight: bold"

    def color_change(val):
        if val == "N/A":        return ""
        if val.startswith("+"): return "color: #2ecc71"
        if val.startswith("-"): return "color: #e74c3c"
        return ""

    def highlight_hr(row):
        if "⭐" in str(row.get("Flags", "")):
            return ["background-color: rgba(255, 215, 0, 0.08); border-left: 3px solid #FFD700"] * len(row)
        return [""] * len(row)

    styled_df = (
        df.style
        .apply(highlight_hr, axis=1)
        .map(color_direction, subset=["Direction"])
        .map(color_risk,      subset=["Risk"])
        .map(color_change,    subset=["Today"])
        .format({
            "Amount ($)": "${:.2f}", "Alloc %": "{:.1f}%", "Confidence": "{:.2f}",
            "Conviction": lambda v: f"{int(v)}" if pd.notna(v) else "—",
        })
    )
    _narrow = {"Direction": 74, "Price": 74, "Today": 72, "Amount ($)": 84,
               "Alloc %": 72, "Risk": 74, "Conviction": 84, "Confidence": 84}
    _table_height = min(len(df) * 70 + 45, 800)
    st.dataframe(
        styled_df, use_container_width=True, hide_index=True,
        row_height=70, height=_table_height,
        column_config={
            "Ticker":    st.column_config.TextColumn("Ticker", width=64, pinned=True),
            "Flags":     st.column_config.TextColumn("Flags", width=48),
            "Buy when":  st.column_config.TextColumn("Buy when",  width=215),
            "Sell when": st.column_config.TextColumn("Sell when", width=215),
            **{c: st.column_config.Column(c, width=w) for c, w in _narrow.items()},
        },
    )
