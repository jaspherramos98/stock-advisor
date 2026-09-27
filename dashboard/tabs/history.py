"""
History — Google Sheets export history with charts.

Everything derived only from the exported rows (cleaned frame, summary metrics, the three static
figures) is built once and cached, keyed on the rows themselves — Plotly Express costs ~0.1s per
figure, and because every click reruns the whole script that was ~0.4s of EVERY click even with
the Sheets read cached. A new export changes the rows → new cache key → charts rebuild on their own.
Figures are cached with cache_resource (shared, not copied) — they are never mutated after build.
"""
import streamlit as st
import pandas as pd
import plotly.express as px
from dashboard.common import _cached_history

_GRID = "rgba(255,255,255,0.1)"


def _style(fig, **layout):
    """Shared dark/transparent chart styling (was repeated verbatim on all four charts)."""
    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="#ffffff",
        margin=dict(t=20, b=20),
        **layout,
    )
    fig.update_xaxes(gridcolor=_GRID)
    fig.update_yaxes(gridcolor=_GRID)
    return fig


@st.cache_resource(ttl=600, show_spinner=False, max_entries=4)
def _history_views(history: list) -> dict:
    df_hist = pd.DataFrame(history)

    # Clean up date column — take just the date part
    df_hist["date"] = df_hist["date"].str[:10]
    df_hist         = df_hist[df_hist["ticker"] != ""]

    # --- Chart 1: Most frequently recommended tickers ---
    ticker_counts = (
        df_hist.groupby("ticker")
        .size()
        .reset_index(name="appearances")
        .sort_values("appearances", ascending=False)
        .head(15)
    )
    fig1 = _style(px.bar(
        ticker_counts,
        x="ticker",
        y="appearances",
        color="appearances",
        color_continuous_scale="Teal",
        labels={"ticker": "Ticker", "appearances": "Times recommended"},
    ), showlegend=False, coloraxis_showscale=False)

    # --- Chart 2: Direction breakdown per ticker ---
    direction_counts = (
        df_hist.groupby(["ticker", "direction"])
        .size()
        .reset_index(name="count")
    )
    color_map = {
        "buy":   "#2ecc71",
        "watch": "#f39c12",
        "avoid": "#e74c3c",
    }
    fig2 = _style(px.bar(
        direction_counts,
        x="ticker",
        y="count",
        color="direction",
        color_discrete_map=color_map,
        labels={"ticker": "Ticker", "count": "Count", "direction": "Direction"},
        barmode="stack",
    ))

    # --- Chart 3: Average confidence score per ticker ---
    avg_confidence = (
        df_hist.groupby("ticker")["confidence"]
        .mean()
        .reset_index()
        .sort_values("confidence", ascending=False)
        .head(15)
    )
    fig3 = _style(px.bar(
        avg_confidence,
        x="ticker",
        y="confidence",
        color="confidence",
        color_continuous_scale="Greens",
        range_y=[0, 1],
        labels={"ticker": "Ticker", "confidence": "Avg confidence"},
    ), showlegend=False, coloraxis_showscale=False)

    return {
        "df": df_hist,
        "total_runs":    df_hist["date"].nunique(),
        "total_tickers": df_hist["ticker"].nunique(),
        "total_buys":    len(df_hist[df_hist["direction"] == "buy"]),
        "total_watches": len(df_hist[df_hist["direction"] == "watch"]),
        "all_tickers":   sorted(df_hist["ticker"].unique().tolist()),
        "fig1": fig1, "fig2": fig2, "fig3": fig3,
    }


@st.cache_resource(ttl=600, show_spinner=False, max_entries=32)
def _allocation_fig(history: list, selected: tuple):
    """Chart 4 depends on the ticker filter, so it's cached per (rows, selection)."""
    df_hist = _history_views(history)["df"]
    df_filtered = df_hist[df_hist["ticker"].isin(list(selected))] if selected else df_hist
    return _style(px.line(
        df_filtered,
        x="date",
        y="amount",
        color="ticker",
        markers=True,
        labels={"date": "Date", "amount": "Amount ($)", "ticker": "Ticker"},
    ))


def render() -> None:
    st.subheader("Historical performance")
    st.caption("Based on your exported runs in Google Sheets.")

    with st.spinner("Loading history from Google Sheets..."):
        history = _cached_history()

    if not history:
        st.info(
            "No history found. Export at least one pipeline run to Google Sheets "
                "using the Export button in the Recommendations tab."
        )
        return

    v = _history_views(history)
    df_hist = v["df"]

    # --- Summary metrics ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total runs exported", v["total_runs"])
    c2.metric("Unique tickers seen", v["total_tickers"])
    c3.metric("Total buy signals",   v["total_buys"])
    c4.metric("Total watch signals", v["total_watches"])

    st.divider()

    st.subheader("Most recommended tickers")
    st.plotly_chart(v["fig1"], use_container_width=True)

    st.divider()

    st.subheader("Buy vs watch breakdown")
    st.plotly_chart(v["fig2"], use_container_width=True)

    st.divider()

    st.subheader("Average confidence score by ticker")
    st.plotly_chart(v["fig3"], use_container_width=True)

    st.divider()

    # --- Chart 4: Recommended amount over time per ticker ---
    st.subheader("Allocation over time")
    st.caption("How much was suggested to invest in each ticker across runs.")

    # Ticker filter
    all_tickers = v["all_tickers"]
    selected    = st.multiselect(
        "Filter by ticker",
        options=all_tickers,
        default=all_tickers[:5],
    )
    st.plotly_chart(_allocation_fig(history, tuple(selected)), use_container_width=True)

    st.divider()

    # --- Raw data table ---
    st.subheader("Raw history")
    st.dataframe(
        df_hist[[
            "date", "ticker", "company", "direction",
            "amount", "allocation_pct", "risk", "confidence"
        ]].rename(columns={
            "date":           "Date",
            "ticker":         "Ticker",
            "company":        "Company",
            "direction":      "Direction",
            "amount":         "Amount ($)",
            "allocation_pct": "Allocation (%)",
            "risk":           "Risk",
            "confidence":     "Confidence",
        }),
        use_container_width=True,
        hide_index=True,
    )
