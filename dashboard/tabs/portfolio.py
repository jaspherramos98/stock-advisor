"""
Portfolio — invested money, P&L trend graph (yfinance), position breakdown.
Extracted verbatim from dashboard/app.py (tab 2); rendered via render().
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
from storage.positions import get_open_positions
from dashboard.common import _cached_prices


def render() -> None:
    st.subheader("Portfolio overview")
    st.caption("Your real invested money across all open positions.")

    open_positions = get_open_positions()
    # Long-only money graph: shorts use a different P&L model (cash in, owe shares),
    # so exclude them here to keep the invested/current-value math correct. Shorts are
    # still shown in My Positions with inverted P&L.
    invested_positions = [
        p for p in open_positions
        if p.get("amount_invested", 0) > 0 and p.get("direction") != "short"
    ]

    if not invested_positions:
        st.info(
            "No investment amounts recorded yet. "
                "Go to **My Positions** → expand a position → set **Amount invested ($)**."
        )
    else:
        # Fetch live prices for all invested positions
        inv_tickers = [p["ticker"] for p in invested_positions]
        with st.spinner("Fetching live prices..."):
            inv_prices = _cached_prices(tuple(sorted(set(inv_tickers))))

        # Calculate portfolio summary
        total_invested    = sum(p.get("amount_invested", 0) for p in invested_positions)
        total_current     = 0.0
        position_data     = []

        for p in invested_positions:
            ticker         = p["ticker"]
            amount_inv     = p.get("amount_invested", 0)
            entry_price    = p.get("manual_price") or p.get("reference_price", 1)
            shares         = amount_inv / entry_price if entry_price > 0 else 0
            live           = inv_prices.get(ticker)
            live_price     = live["price"] if live else entry_price
            current_value  = shares * live_price
            pnl_dollars    = current_value - amount_inv
            pnl_pct        = ((live_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

            total_current += current_value
            position_data.append({
                "ticker":        ticker,
                "company":       p["company_name"],
                "entry_date":    p.get("entry_date", "—"),
                "amount_inv":    amount_inv,
                "shares":        shares,
                "entry_price":   entry_price,
                "live_price":    live_price,
                "current_value": current_value,
                "pnl_dollars":   pnl_dollars,
                "pnl_pct":       pnl_pct,
            })

        total_pnl_dollars = total_current - total_invested
        total_pnl_pct     = ((total_current - total_invested) / total_invested * 100) if total_invested > 0 else 0

        # Summary metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total invested",  f"${total_invested:,.2f}")
        m2.metric("Current value",   f"${total_current:,.2f}")
        m3.metric("Total P&L",       f"${total_pnl_dollars:+,.2f}", f"{total_pnl_pct:+.1f}%")
        m4.metric("Positions",       len(invested_positions))

        st.divider()

        # --- Portfolio trend graph ---
        st.subheader("Portfolio value over time")

        import yfinance as yf

        # Build combined daily portfolio value from each position's entry date
        all_dates    = pd.Series(dtype=float)
        earliest_date = None

        for pd_pos in position_data:
            try:
                entry_date_str = pd_pos["entry_date"]
                if entry_date_str == "—":
                    continue

                # Find earliest date across all positions
                entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d")
                if earliest_date is None or entry_dt < earliest_date:
                    earliest_date = entry_dt

                # Fetch daily history from entry date to today
                hist = yf.Ticker(pd_pos["ticker"]).history(start=entry_date_str, interval="1d")
                if hist.empty:
                    continue

                # Value of this position on each day = shares × daily close
                daily_value = hist["Close"] * pd_pos["shares"]
                # Strip timezone info cleanly
                if hasattr(daily_value.index, 'tz') and daily_value.index.tz is not None:
                    daily_value.index = daily_value.index.tz_convert("UTC").tz_localize(None)
                daily_value.index = daily_value.index.normalize()

                if all_dates.empty:
                    all_dates = daily_value
                else:
                    all_dates = all_dates.add(daily_value, fill_value=0)

            except Exception as e:
                print(f"Portfolio graph error for {pd_pos['ticker']}: {e}")
                continue

        if not all_dates.empty:
            portfolio_df = all_dates.reset_index()
            portfolio_df.columns = ["Date", "Value ($)"]
            portfolio_df["Date"] = pd.to_datetime(portfolio_df["Date"]).dt.date

            # Add total invested line as reference
            fig = go.Figure()

            fig.add_trace(go.Scatter(
                x=portfolio_df["Date"],
                y=portfolio_df["Value ($)"],
                mode="lines",
                name="Portfolio value",
                line=dict(color="#2ecc71", width=2),
                fill="tozeroy",
                fillcolor="rgba(46, 204, 113, 0.1)",
            ))

            fig.add_hline(
                y=total_invested,
                line_dash="dash",
                line_color="#f39c12",
                annotation_text=f"Invested: ${total_invested:,.0f}",
                annotation_position="bottom right",
            )

            fig.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#ffffff",
                margin=dict(t=20, b=20),
                xaxis=dict(gridcolor="rgba(255,255,255,0.1)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.1)", tickprefix="$"),
                hovermode="x unified",
                showlegend=False,
            )

            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Not enough price history to build the chart yet.")

        st.divider()

        # --- Individual position breakdown ---
        st.subheader("Position breakdown")

        breakdown_rows = []
        for pd_pos in position_data:
            breakdown_rows.append({
                "Ticker":          pd_pos["ticker"],
                "Company":         pd_pos["company"],
                "Invested ($)":    f"${pd_pos['amount_inv']:,.2f}",
                "Shares":          f"{pd_pos['shares']:.4f}",
                "Entry price":     f"${pd_pos['entry_price']:.2f}",
                "Live price":      f"${pd_pos['live_price']:.2f}",
                "Current value":   f"${pd_pos['current_value']:,.2f}",
                "P&L ($)":         f"${pd_pos['pnl_dollars']:+,.2f}",
                "P&L %":           f"{pd_pos['pnl_pct']:+.1f}%",
            })

        breakdown_df = pd.DataFrame(breakdown_rows)

        def color_portfolio_pnl(val):
            if val == "—":          return ""
            if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
            if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
            return ""

        styled_breakdown = (
            breakdown_df.style
            .map(color_portfolio_pnl, subset=["P&L ($)", "P&L %"])
        )
        st.dataframe(styled_breakdown, use_container_width=True, hide_index=True)
