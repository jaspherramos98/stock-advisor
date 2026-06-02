import streamlit as st
import sys
import os
import pandas as pd
from datetime import datetime

# Add the project root to the path so Streamlit can find all modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.prices import fetch_prices
from main import run_ingestion_and_analysis
from calculator.portfolio import calculate_allocations
from storage.positions import add_position, get_open_positions, close_position, update_manual_price
from storage.watchlist import load_watchlist, save_watchlist, add_ticker, remove_ticker, reset_to_defaults
import plotly.express as px
import plotly.graph_objects as go

# --- File paths ---
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache_backup.json")
BUDGET_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "budget.json")


# --- Helper functions (must be defined before any UI code) ---
def save_budget(amount: float):
    import json
    with open(BUDGET_FILE, "w") as f:
        json.dump({"budget": amount}, f)


def load_budget() -> float:
    import json
    if not os.path.exists(BUDGET_FILE):
        return 1000.0
    try:
        with open(BUDGET_FILE, "r") as f:
            return json.load(f).get("budget", 1000.0)
    except Exception:
        return 1000.0


def save_cache(recommendations, prices, last_run):
    import json
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                existing = json.load(f)
            if existing.get("recommendations"):
                with open(CACHE_BACKUP_FILE, "w") as f:
                    json.dump(existing, f)
        except Exception:
            pass
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "date":            datetime.now().strftime("%Y-%m-%d"),
            "last_run":        last_run,
            "recommendations": recommendations,
            "prices":          prices,
        }, f)


def load_cache() -> dict | None:
    import json

    def try_load(path) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return None

    data = try_load(CACHE_FILE)
    if data and data.get("date") == datetime.now().strftime("%Y-%m-%d"):
        return data

    backup = try_load(CACHE_BACKUP_FILE)
    if backup and backup.get("date") == datetime.now().strftime("%Y-%m-%d"):
        st.toast("Loaded from backup cache — today's pipeline may have failed mid-run.")
        return backup

    return None


# --- Page config ---
st.set_page_config(
    page_title="Stock Advisor",
    page_icon="📈",
    layout="wide",
)

st.title("📈 Stock Advisor")
st.caption("Informed suggestions based on today's validated financial news. Not financial advice.")

# --- Sidebar ---
with st.sidebar:
    st.header("Settings")

    budget = st.number_input(
        "Investment budget ($)",
        min_value=10.0,
        max_value=1_000_000.0,
        value=load_budget(),
        step=50.0,
        help="How much you are willing to invest across all suggestions today."
    )
    save_budget(budget)

    st.divider()

    st.subheader("Asset types")
    show_stocks = st.checkbox("US Stocks",  value=True)
    show_etfs   = st.checkbox("ETFs",       value=False)
    show_crypto = st.checkbox("Crypto",     value=False)

    if show_etfs or show_crypto:
        st.info("ETF and crypto news will be included in the next pipeline run.")

    st.divider()

    run_button = st.button("🔄 Run pipeline", use_container_width=True, type="primary")
    st.caption("Fetches fresh news, scores it, and runs Claude analysis. Takes ~30 seconds.")

# --- Session state ---
if "recommendations" not in st.session_state:
    cache = load_cache()
    if cache:
        st.session_state.recommendations = cache["recommendations"]
        st.session_state.prices          = cache["prices"]
        st.session_state.last_run        = cache["last_run"]
        st.session_state._from_cache     = True
    else:
        st.session_state.recommendations = None
        st.session_state.prices          = {}
        st.session_state.last_run        = None
        st.session_state._from_cache     = False

if "last_run" not in st.session_state:
    st.session_state.last_run = None
if "prices" not in st.session_state:
    st.session_state.prices = {}

# --- Run pipeline ---
if run_button:
    with st.spinner("Fetching news and running analysis... this takes about 30 seconds."):
        try:
            recs = run_ingestion_and_analysis(
                include_stocks=show_stocks,
                include_etfs=show_etfs,
                include_crypto=show_crypto,
            )
            st.session_state.recommendations = recs

            tickers = [r["ticker"] for r in recs if r.get("ticker")]
            st.session_state.prices = fetch_prices(tickers)

            last_run = datetime.now().strftime("%B %d, %Y at %I:%M %p")
            st.session_state.last_run    = last_run
            st.session_state._from_cache = False

            save_cache(recs, st.session_state.prices, last_run)
            st.success(f"Pipeline complete — {len(recs)} recommendations found.")
        except Exception as e:
            st.error(f"Pipeline error: {e}")

# --- Display results ---
if st.session_state.recommendations:
    recs        = st.session_state.recommendations
    allocations = calculate_allocations(recs, budget)
    prices      = st.session_state.prices

    for idx, a in enumerate(allocations):
        ticker     = a.get("ticker")
        price_data = prices.get(ticker)
        if price_data:
            a["current_price"] = f"${price_data['price']:.2f}"
            a["change_pct"]    = f"{price_data['change_pct']:+.2f}%"
        else:
            a["current_price"] = "N/A"
            a["change_pct"]    = "N/A"

    if st.session_state.last_run:
        if st.session_state.get("_from_cache"):
            st.caption(f"Loaded from today's cache — last run: {st.session_state.last_run}")
        else:
            st.caption(f"Last run: {st.session_state.last_run}")

    tab1, tab2, tab3, tab4 = st.tabs(["📈 Today's Recommendations", "📌 My Positions", "🔭 Watch List", "📊 History"])

    # =========================================================
    # TAB 1 — Today's Recommendations
    # =========================================================
    with tab1:
        if not allocations:
            st.warning("No actionable recommendations after filtering. Try running the pipeline again.")
        else:
            col1, col2, col3, col4 = st.columns(4)
            buy_count     = sum(1 for a in allocations if a["direction"] == "buy")
            watch_count   = sum(1 for a in allocations if a["direction"] == "watch")
            flagged_count = sum(1 for a in allocations if a["flagged"])

            col1.metric("Total stocks",  len(allocations))
            col2.metric("Buy signals",   buy_count)
            col3.metric("Watch signals", watch_count)
            col4.metric("⚠ Flagged",     flagged_count)

            st.divider()
            st.subheader("Portfolio allocation")

            df = pd.DataFrame(allocations)
            df = df[[
                "ticker", "company_name", "direction",
                "current_price", "change_pct",
                "dollar_amount", "percentage",
                "risk_level", "confidence_score",
                "exit_condition", "flagged"
            ]].rename(columns={
                "ticker":           "Ticker",
                "company_name":     "Company",
                "direction":        "Direction",
                "current_price":    "Price",
                "change_pct":       "Today",
                "dollar_amount":    "Amount ($)",
                "percentage":       "Allocation (%)",
                "risk_level":       "Risk",
                "confidence_score": "Confidence",
                "exit_condition":   "Sell when",
                "flagged":          "⚠ Flagged",
            })

            def color_direction(val):
                if val == "buy":   return "color: #2ecc71; font-weight: bold"
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

            styled_df = (
                df.style
                .map(color_direction, subset=["Direction"])
                .map(color_risk,      subset=["Risk"])
                .map(color_change,    subset=["Today"])
                .format({
                    "Amount ($)":     "${:.2f}",
                    "Allocation (%)": "{:.1f}%",
                    "Confidence":     "{:.2f}",
                })
            )

            st.dataframe(styled_df, use_container_width=True, hide_index=True)

            col_export, col_spacer = st.columns([1, 4])
            with col_export:
                if st.button("📤 Export to Google Sheets", use_container_width=True):
                    from storage.sheets import export_to_sheets
                    with st.spinner("Exporting..."):
                        success = export_to_sheets(allocations, budget)
                    if success:
                        sheet_id  = os.getenv("GOOGLE_SHEET_ID", "")
                        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
                        st.success(f"Exported! [Open sheet]({sheet_url})")
                    else:
                        st.error("Export failed — check your Google credentials and Sheet ID in .env")

            st.divider()
            st.subheader("Stock details")

            open_tickers = {p["ticker"] for p in get_open_positions()}

            for idx, a in enumerate(allocations):
                flag_label      = " ⚠ Unverified source" if a["flagged"] else ""
                direction_emoji = "🟢" if a["direction"] == "buy" else "🟡"
                is_open         = a["ticker"] in open_tickers

                with st.expander(
                    f"{direction_emoji} {a['ticker']} — {a['company_name']} "
                    f"| ${a['dollar_amount']:.2f} ({a['percentage']:.1f}%){flag_label}"
                ):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Direction",  a["direction"].upper())
                    c2.metric("Risk",       a["risk_level"].upper())
                    c3.metric("Confidence", f"{a['confidence_score']:.2f}")

                    st.markdown(f"**Why buy:** {a['entry_rationale']}")
                    st.markdown(f"**Exit when:** {a['exit_condition']}")
                    st.markdown(f"**Based on:** _{a['source_title']}_")

                    # White paper and info links for crypto assets
                    if a.get("asset_type") == "crypto":
                        from ingestion.coingecko import TICKER_TO_COINGECKO_ID
                        coin_id = TICKER_TO_COINGECKO_ID.get(a.get("ticker", ""), "")
                        if coin_id:
                            st.markdown(
                                f"🔗 [White paper](https://www.coingecko.com/en/coins/{coin_id}) · "
                                f"[CoinMarketCap](https://coinmarketcap.com/currencies/{coin_id}/)"
                            )

                    if a["flagged"]:
                        st.warning(
                            "This recommendation is based on an unverified source. "
                            "Treat with extra caution and verify independently before acting."
                        )

                    st.divider()

                    if is_open:
                        st.success(f"✓ {a['ticker']} is already in your open positions.")
                    else:
                        col_btn, col_price, col_spacer = st.columns([2, 2, 3])

                        with col_price:
                            manual_ref = st.number_input(
                                "Override reference price ($)",
                                min_value=0.01,
                                value=0.01,
                                step=0.01,
                                key=f"manual_price_{idx}_{a['ticker']}",
                                help="Leave at 0.01 to use the current market price automatically.",
                            )

                        with col_btn:
                            st.markdown("<div style='margin-top: 28px'>", unsafe_allow_html=True)
                            if st.button(
                                f"📌 Add {a['ticker']} to positions",
                                key=f"add_pos_{idx}_{a['ticker']}",
                                use_container_width=True,
                            ):
                                with st.spinner(f"Fetching current price for {a['ticker']}..."):
                                    if manual_ref > 0.01:
                                        ref_price    = manual_ref
                                        price_source = f"manual (${ref_price:.2f})"
                                    else:
                                        price_data = fetch_prices([a["ticker"]])
                                        pd_entry   = price_data.get(a["ticker"])
                                        if pd_entry:
                                            ref_price    = pd_entry["price"]
                                            price_source = f"market (${ref_price:.2f})"
                                        else:
                                            st.error(f"Could not fetch price for {a['ticker']}. Enter it manually.")
                                            ref_price = None

                                if ref_price:
                                    add_position(
                                        ticker=          a["ticker"],
                                        company_name=    a["company_name"],
                                        reference_price= ref_price,
                                        exit_condition=  a["exit_condition"],
                                        direction=       a["direction"],
                                        confidence=      a["confidence_score"],
                                        source_title=    a["source_title"],
                                    )
                                    open_tickers.add(a["ticker"])
                                    st.success(
                                        f"✓ {a['ticker']} added to positions "
                                        f"at reference price {price_source}."
                                    )
                            st.markdown("</div>", unsafe_allow_html=True)

    # =========================================================
    # TAB 2 — My Positions
    # =========================================================
    with tab2:
        all_positions = get_open_positions()

        if not all_positions:
            st.info("No open positions yet. Add stocks from the Recommendations tab.")
        else:
            pos_tickers = [p["ticker"] for p in all_positions]
            with st.spinner("Fetching live prices..."):
                live_prices = fetch_prices(pos_tickers)

            col1, col2 = st.columns(2)
            col1.metric("Open positions", len(all_positions))
            col2.metric("Stocks tracked", len(pos_tickers))

            st.divider()
            st.subheader("Open positions")

            rows = []
            for p in all_positions:
                ticker     = p["ticker"]
                ref_price  = p["manual_price"] or p["reference_price"]
                live       = live_prices.get(ticker)
                live_price = live["price"] if live else None
                change_pct = ((live_price - ref_price) / ref_price * 100) if live_price else None
                opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d")

                rows.append({
                    "Ticker":     ticker,
                    "Company":    p["company_name"],
                    "Ref Price":  f"${ref_price:.2f}",
                    "Live Price": f"${live_price:.2f}" if live_price else "N/A",
                    "Change":     f"{change_pct:+.1f}%" if change_pct is not None else "N/A",
                    "Exit when":  p["exit_condition"],
                    "Opened":     opened,
                })

            pos_df = pd.DataFrame(rows)

            def color_pos_change(val):
                if val == "N/A":        return ""
                if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
                if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
                return ""

            styled_pos = pos_df.style.map(color_pos_change, subset=["Change"])
            st.dataframe(styled_pos, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Manage positions")

            for p in all_positions:
                ticker     = p["ticker"]
                ref_price  = p["manual_price"] or p["reference_price"]
                live       = live_prices.get(ticker)
                live_price = live["price"] if live else None
                change_pct = ((live_price - ref_price) / ref_price * 100) if live_price else None
                opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d %H:%M")

                change_str = f"{change_pct:+.1f}%" if change_pct is not None else "N/A"
                emoji      = "📈" if (change_pct or 0) >= 0 else "📉"

                with st.expander(f"{emoji} {ticker} — {p['company_name']} | {change_str} since entry"):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Reference price", f"${ref_price:.2f}")
                    c2.metric("Live price",       f"${live_price:.2f}" if live_price else "N/A")
                    c3.metric("Change",           change_str)
                    c4.metric("Opened",           opened[:10])

                    st.markdown(f"**Exit when:** {p['exit_condition']}")
                    st.markdown(f"**Based on:** _{p['source_title']}_")

                    # White paper link for crypto positions
                    ticker = p.get("ticker", "")
                    # White paper link for crypto positions
                    from ingestion.coingecko import TICKER_TO_COINGECKO_ID
                    coin_id = TICKER_TO_COINGECKO_ID.get(p.get("ticker", ""), "")
                    if coin_id:
                        st.markdown(
                            f"🔗 [White paper & info](https://www.coingecko.com/en/coins/{coin_id})"
                        )

                    st.divider()

                    col_manual, col_spacer = st.columns([2, 3])
                    with col_manual:
                        new_price = st.number_input(
                            "Update reference price ($)",
                            min_value=0.01,
                            value=float(ref_price),
                            step=0.01,
                            key=f"update_price_{ticker}",
                        )
                        if st.button("💾 Update price", key=f"update_btn_{ticker}"):
                            update_manual_price(ticker, new_price)
                            st.success(f"Reference price updated to ${new_price:.2f}")

                    st.divider()

                    col_close, col_reason, col_spacer = st.columns([2, 3, 2])
                    with col_reason:
                        close_reason = st.text_input(
                            "Reason for closing (optional)",
                            placeholder="e.g. 10% gain reached",
                            key=f"reason_{ticker}",
                        )
                    with col_close:
                        st.markdown("<div style='margin-top: 28px'>", unsafe_allow_html=True)
                        if st.button(
                            f"❌ Close {ticker} position",
                            key=f"close_{ticker}",
                            use_container_width=True,
                        ):
                            reason = close_reason or f"Manually closed on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                            close_position(ticker, reason)
                            st.warning(f"{ticker} position closed.")
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

                    st.divider()

                    from alerts.snooze import is_snoozed, snooze_ticker, dismiss_ticker, clear_snooze
                    st.markdown("**Alert snooze**")
                    currently_snoozed = is_snoozed(ticker)

                    if currently_snoozed:
                        st.warning(f"Alerts for {ticker} are currently snoozed.")
                        if st.button(f"🔔 Re-enable alerts for {ticker}", key=f"unsnooze_{ticker}"):
                            clear_snooze(ticker)
                            st.success(f"Alerts re-enabled for {ticker}.")
                            st.rerun()
                    else:
                        snooze_col1, snooze_col2, snooze_col3 = st.columns(3)
                        with snooze_col1:
                            if st.button(f"😴 Snooze 1 day", key=f"snooze1_{ticker}", use_container_width=True):
                                snooze_ticker(ticker, days=1)
                                st.success(f"{ticker} snoozed for 1 day.")
                                st.rerun()
                        with snooze_col2:
                            if st.button(f"😴 Snooze 1 week", key=f"snooze7_{ticker}", use_container_width=True):
                                snooze_ticker(ticker, days=7)
                                st.success(f"{ticker} snoozed for 1 week.")
                                st.rerun()
                        with snooze_col3:
                            if st.button(f"🔕 Dismiss permanently", key=f"dismiss_{ticker}", use_container_width=True):
                                dismiss_ticker(ticker)
                                st.success(f"{ticker} alerts dismissed permanently.")
                                st.rerun()

    # =========================================================
    # TAB 3 — Watch List Editor
    # =========================================================
    with tab3:
        st.subheader("Watch list")
        st.caption(
            "These are the tickers Finnhub monitors for company-specific news. "
            "Changes take effect on the next pipeline run."
        )

        watchlist = load_watchlist()

        for asset_type, label, default_color in [
            ("stocks", "US Stocks",    "#1e3a2f"),
            ("etfs",   "ETFs",         "#1a2e3a"),
            ("crypto", "Crypto",       "#2e1a3a"),
        ]:
            text_colors = {
                "stocks": "#2ecc71",
                "etfs":   "#3498db",
                "crypto": "#9b59b6",
            }
            text_color = text_colors[asset_type]
            tickers    = watchlist.get(asset_type, [])

            st.markdown(f"### {label} — {len(tickers)} tickers")

            if tickers:
                cols_per_row = 5
                rows = [tickers[i:i+cols_per_row] for i in range(0, len(tickers), cols_per_row)]
                for row in rows:
                    cols = st.columns(cols_per_row)
                    for i, ticker in enumerate(row):
                        with cols[i]:
                            st.markdown(
                                f"<div style='background:{default_color}; color:{text_color}; "
                                f"padding:6px 10px; border-radius:6px; text-align:center; "
                                f"font-weight:bold; margin-bottom:6px'>{ticker}</div>",
                                unsafe_allow_html=True
                            )
                            if st.button("✕", key=f"remove_{asset_type}_{ticker}", use_container_width=True):
                                remove_ticker(ticker, asset_type)
                                st.success(f"{ticker} removed from {label}.")
                                st.rerun()
            else:
                st.caption(f"No {label} tickers yet.")

            # Add ticker for this asset type
            col_input, col_add, col_spacer = st.columns([2, 1, 3])
            with col_input:
                new_ticker = st.text_input(
                    f"Add {label} ticker",
                    placeholder="e.g. PLTR",
                    label_visibility="collapsed",
                    key=f"add_input_{asset_type}",
                ).strip().upper()
            with col_add:
                if st.button(f"➕ Add", key=f"add_btn_{asset_type}", use_container_width=True):
                    if not new_ticker:
                        st.warning("Enter a ticker symbol first.")
                    elif new_ticker in watchlist.get(asset_type, []):
                        st.warning(f"{new_ticker} is already in {label}.")
                    else:
                        add_ticker(new_ticker, asset_type)
                        st.success(f"{new_ticker} added to {label}.")
                        st.rerun()

            st.divider()

        # Bulk edit and reset at the bottom
        col_reset, col_spacer = st.columns([1, 4])
        with col_reset:
            if st.button("↺ Reset all to defaults", use_container_width=True):
                reset_to_defaults()
                st.success("All watch lists reset to defaults.")
                st.rerun()

    # =========================================================
    # TAB 4 — Historical Performance Chart
    # =========================================================
    with tab4:
        st.subheader("Historical performance")
        st.caption("Based on your exported runs in Google Sheets.")

        from storage.sheets import read_history

        with st.spinner("Loading history from Google Sheets..."):
            history = read_history()

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


else:
    st.info("Press **Run pipeline** in the sidebar to fetch today's news and generate recommendations.")
    st.markdown("""
    **How it works:**
    1. Fetches financial news from RSS feeds, Reddit, Finnhub, and SEC filings
    2. Scores each story for credibility — SEC filings score 1.0, Reddit posts score 0.15
    3. Sends validated stories to Claude for stock analysis
    4. Calculates how to distribute your budget across recommendations

    Adjust your budget in the sidebar at any time — the allocation recalculates instantly without re-running the pipeline.
    """)