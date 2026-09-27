"""
History — Google Sheets export history with charts.
Extracted verbatim from dashboard/app.py (tab 5); rendered via render().
"""
import streamlit as st
import pandas as pd
import plotly.express as px
from dashboard.common import _cached_history


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
    else:
        df_hist = pd.DataFrame(history)

        # Clean up date column — take just the date part
        df_hist["date"] = df_hist["date"].str[:10]
        df_hist         = df_hist[df_hist["ticker"] != ""]

        # --- Summary metrics ---
        total_runs    = df_hist["date"].nunique()
        total_tickers = df_hist["ticker"].nunique()
        total_buys    = len(df_hist[df_hist["direction"] == "buy"])
        total_watches = len(df_hist[df_hist["direction"] == "watch"])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total runs exported", total_runs)
        c2.metric("Unique tickers seen", total_tickers)
        c3.metric("Total buy signals",   total_buys)
        c4.metric("Total watch signals", total_watches)

        st.divider()

        # --- Chart 1: Most frequently recommended tickers ---
        st.subheader("Most recommended tickers")
        ticker_counts = (
            df_hist.groupby("ticker")
            .size()
            .reset_index(name="appearances")
            .sort_values("appearances", ascending=False)
            .head(15)
        )

        fig1 = px.bar(
            ticker_counts,
            x="ticker",
            y="appearances",
            color="appearances",
            color_continuous_scale="Teal",
            labels={"ticker": "Ticker", "appearances": "Times recommended"},
        )
        fig1.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#ffffff",
            showlegend=False,
            coloraxis_showscale=False,
            margin=dict(t=20, b=20),
        )
        fig1.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
        fig1.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
        st.plotly_chart(fig1, use_container_width=True)

        st.divider()

        # --- Chart 2: Direction breakdown per ticker ---
        st.subheader("Buy vs watch breakdown")
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

        fig2 = px.bar(
            direction_counts,
            x="ticker",
            y="count",
            color="direction",
            color_discrete_map=color_map,
            labels={"ticker": "Ticker", "count": "Count", "direction": "Direction"},
            barmode="stack",
        )
        fig2.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#ffffff",
            margin=dict(t=20, b=20),
        )
        fig2.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
        fig2.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
        st.plotly_chart(fig2, use_container_width=True)

        st.divider()

        # --- Chart 3: Average confidence score per ticker ---
        st.subheader("Average confidence score by ticker")
        avg_confidence = (
            df_hist.groupby("ticker")["confidence"]
            .mean()
            .reset_index()
            .sort_values("confidence", ascending=False)
            .head(15)
        )

        fig3 = px.bar(
            avg_confidence,
            x="ticker",
            y="confidence",
            color="confidence",
            color_continuous_scale="Greens",
            range_y=[0, 1],
            labels={"ticker": "Ticker", "confidence": "Avg confidence"},
        )
        fig3.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#ffffff",
            showlegend=False,
            coloraxis_showscale=False,
            margin=dict(t=20, b=20),
        )
        fig3.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
        fig3.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
        st.plotly_chart(fig3, use_container_width=True)

        st.divider()

        # --- Chart 4: Recommended amount over time per ticker ---
        st.subheader("Allocation over time")
        st.caption("How much was suggested to invest in each ticker across runs.")

        # Ticker filter
        all_tickers = sorted(df_hist["ticker"].unique().tolist())
        selected    = st.multiselect(
            "Filter by ticker",
            options=all_tickers,
            default=all_tickers[:5],
        )

        df_filtered = df_hist[df_hist["ticker"].isin(selected)] if selected else df_hist

        fig4 = px.line(
            df_filtered,
            x="date",
            y="amount",
            color="ticker",
            markers=True,
            labels={"date": "Date", "amount": "Amount ($)", "ticker": "Ticker"},
        )
        fig4.update_layout(
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#ffffff",
            margin=dict(t=20, b=20),
        )
        fig4.update_xaxes(gridcolor="rgba(255,255,255,0.1)")
        fig4.update_yaxes(gridcolor="rgba(255,255,255,0.1)")
        st.plotly_chart(fig4, use_container_width=True)

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
