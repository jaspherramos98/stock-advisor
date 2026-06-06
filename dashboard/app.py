import streamlit as st
import sys
import os
import json
import pandas as pd
from datetime import datetime
import datetime as dt
import plotly.express as px
import plotly.graph_objects as go

# --- THIS MUST COME BEFORE ANY LOCAL IMPORTS ---
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Local imports after path is set ---
from ingestion.prices import fetch_prices
from ingestion.coingecko import TICKER_TO_COINGECKO_ID
from main import run_ingestion_and_analysis
from calculator.portfolio import calculate_allocations
from storage.positions import add_position, get_open_positions, get_closed_positions, close_position, update_manual_price, update_amount_invested
from storage.watchlist import load_watchlist, save_watchlist, add_ticker, remove_ticker, reset_to_defaults
from alerts.snooze import is_snoozed, snooze_ticker, dismiss_ticker, clear_snooze
from dotenv import load_dotenv
load_dotenv()


# --- File paths ---
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache.json")
CACHE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline_cache_backup.json")
BUDGET_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "budget.json")


# --- Helper functions (must be defined before any UI code) ---
def save_budget(amount: float):
    with open(BUDGET_FILE, "w") as f:
        json.dump({"budget": amount}, f)

# =========================================================
# CHATBOT PROXY SERVER — keeps API key server-side
# =========================================================
import threading
import requests as _requests

def _start_proxy_server():
    """
    Starts a tiny Flask proxy on port 8502.
    The chatbot iframe calls this instead of Anthropic directly.
    The real API key never leaves the server.
    """
    try:
        from flask import Flask, request, jsonify
        from flask_cors import CORS
    except ImportError:
        print("Proxy: flask or flask-cors not installed — chatbot will be disabled.")
        return

    proxy_app = Flask(__name__)
    CORS(proxy_app, origins=["http://localhost:8501", "http://127.0.0.1:8501"])

    @proxy_app.route("/chat", methods=["POST"])
    def chat():
        try:
            data    = request.get_json()
            api_key = os.getenv("ANTHROPIC_API_KEY", "")

            if not api_key:
                return jsonify({"error": "API key not configured"}), 500

            # Validate required fields
            messages = data.get("messages", [])
            system   = data.get("system", "")
            if not messages:
                return jsonify({"error": "No messages provided"}), 400

            # Forward to Anthropic
            resp = _requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type":    "application/json",
                    "x-api-key":       api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model":      "claude-sonnet-4-5",
                    "max_tokens": 512,
                    "system":     system,
                    "messages":   messages,
                },
                timeout=30,
            )
            return jsonify(resp.json()), resp.status_code

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @proxy_app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"}), 200

    # Run in background thread — daemon=True means it dies when Streamlit dies
    thread = threading.Thread(
        target=lambda: proxy_app.run(
            host="127.0.0.1",
            port=8502,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    )
    thread.start()
    print("Proxy: chatbot proxy server started on localhost:8502")

# Start once — Streamlit rerenders the script on every interaction
# so we guard against starting multiple threads
if "proxy_started" not in st.session_state:
    _start_proxy_server()
    st.session_state.proxy_started = True



def load_budget() -> float:
    if not os.path.exists(BUDGET_FILE):
        return 1000.0
    try:
        with open(BUDGET_FILE, "r") as f:
            return json.load(f).get("budget", 1000.0)
    except Exception:
        return 1000.0


def save_cache(recommendations, prices, last_run):
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
    page_title="Argus",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 Argus")
st.caption("AI-powered market intelligence. Experimental — not financial advice.")

# Mock mode banner
if os.getenv("MOCK_MODE", "false").lower() == "true":
    st.warning("⚠️ MOCK MODE active — showing test data. No real Claude API calls. Set MOCK_MODE=false in .env for real analysis.")

# --- Sidebar ---
with st.sidebar:
    st.header("Settings")

    budget = st.number_input(
        "Investment budget ($)",
        min_value=10.0,
        max_value=1_000_000.0,
        value=load_budget(),
        step=50.0,
        help="Only buy signals receive allocations. Watch signals show $0. This tool is experimental — only invest what you're comfortable with.",
    )
    st.caption("⚠️ Experimental. Start small.")
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

    # Robinhood sync
    from ingestion.robinhood import is_available as rh_available, fetch_positions as rh_fetch
    if rh_available():
        st.divider()
        st.subheader("Robinhood")
        if st.button("🔄 Sync positions", use_container_width=True):
            with st.spinner("Connecting to Robinhood..."):
                rh_positions = rh_fetch()
            if rh_positions:
                synced = 0
                skipped = 0
                existing_tickers = {p["ticker"] for p in get_open_positions()}
                for rp in rh_positions:
                    if rp["ticker"] in existing_tickers:
                        skipped += 1
                        continue
                    add_position(
                        ticker=          rp["ticker"],
                        company_name=    rp["company_name"],
                        reference_price= rp["avg_cost"],
                        exit_condition=  "Synced from Robinhood — set exit condition manually",
                        direction=       "buy",
                        confidence=      0.0,
                        source_title=    "Robinhood sync",
                    )
                    update_amount_invested(rp["ticker"], rp["amount_invested"])
                    synced += 1
                st.success(f"Synced {synced} positions. Skipped {skipped} already in Argus.")
                if synced > 0:
                    st.rerun()
            else:
                st.error("Could not fetch Robinhood positions. Check credentials in .env.")
        st.caption("Read-only — imports positions, does not trade.")

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

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Today's Recommendations", "💼 Portfolio", "📌 My Positions", "🔭 Watch List", "📊 History"])

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
            st.caption(
                "ℹ️ **Confidence** — how verified the source is (1.0 = SEC filing, 0.15 = Reddit).  "
                "**Amount ($)** — $0.00 means watch only, no capital allocated.  "
                "**⚠ Flagged** — unverified source, treat with extra caution."
            )

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
                    # Colored bar — green for buy, orange for watch
                    bar_color = "#2ecc71" if a["direction"] == "buy" else "#f39c12"
                    st.markdown(
                        f'<div style="height:3px; background:{bar_color}; border-radius:2px; margin-bottom:12px"></div>',
                        unsafe_allow_html=True,
                    )
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Direction",  a["direction"].upper())
                    c2.metric("Risk",       a["risk_level"].upper())
                    c3.metric("Confidence", f"{a['confidence_score']:.2f}",
                              help="Signal strength. 1.0 = SEC filing (highest trust). 0.68 = Finnhub verified news. 0.15 = Reddit post (lowest trust).")

                    why_label = "Why buy" if a["direction"] == "buy" else "Why watch"
                    st.markdown(f"**{why_label}:** {a['entry_rationale']}")
                    st.markdown(f"**Exit when:** {a['exit_condition']}")
                    st.markdown(f"**Based on:** _{a['source_title']}_")

                    # White paper and info links for crypto assets
                    
                    if a.get("asset_type") == "crypto":
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
                        # --- Add to positions UI ---
                        ticker_key = f"{idx}_{a['ticker']}"

                        # Toggle: did you buy at a different price?
                        different_price = st.checkbox(
                            "I bought this at a different price",
                            key=f"diff_price_toggle_{ticker_key}",
                        )

                        ref_price    = None
                        entry_date   = datetime.now().strftime("%Y-%m-%d")
                        price_source = "market"

                        if different_price:
                            col_price, col_date = st.columns(2)

                            with col_price:
                                manual_ref = st.number_input(
                                    "Price you paid per share ($)",
                                    min_value=0.01,
                                    value=0.01,
                                    step=0.01,
                                    key=f"manual_price_{ticker_key}",
                                )

                            with col_date:
                                # Quick date buttons + date picker
                                st.markdown("**When did you buy it?**")
                                d_col1, d_col2, d_col3 = st.columns(3)
                                with d_col1:
                                    if st.button("Today", key=f"date_today_{ticker_key}", use_container_width=True):
                                        st.session_state[f"entry_date_{ticker_key}"] = datetime.now().strftime("%Y-%m-%d")
                                with d_col2:
                                    if st.button("Yesterday", key=f"date_yest_{ticker_key}", use_container_width=True):
                                        from datetime import timedelta
                                        st.session_state[f"entry_date_{ticker_key}"] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                                with d_col3:
                                    st.write("")  # spacer

                                
                                picked_date = st.date_input(
                                    "Or pick a date",
                                    value=datetime.today().date(),
                                    max_value=datetime.today().date(),
                                    key=f"date_picker_{ticker_key}",
                                    label_visibility="collapsed",
                                )
                                entry_date = picked_date.strftime("%Y-%m-%d")

                            # Validate price against market price
                            if manual_ref > 0.01:
                                market_data  = prices.get(a["ticker"])
                                market_price = market_data["price"] if market_data else None

                                if market_price:
                                    ratio = manual_ref / market_price
                                    if ratio < 0.5 or ratio > 2.0:
                                        st.warning(
                                            f"⚠️ You entered ${manual_ref:.2f} but the current market price is "
                                            f"${market_price:.2f}. That's a {abs(1-ratio)*100:.0f}% difference. "
                                            f"Double-check before adding."
                                        )

                                ref_price    = manual_ref
                                price_source = f"manual entry (${manual_ref:.2f})"

                        # Add button — always visible
                        if st.button(
                            f"📌 Add {a['ticker']} to positions",
                            key=f"add_pos_{ticker_key}",
                            use_container_width=True,
                            type="primary",
                        ):
                            if different_price and manual_ref <= 0.01:
                                st.error("Enter the price you paid before adding.")
                            else:
                                if not different_price:
                                    # Fetch current market price
                                    with st.spinner(f"Fetching current price for {a['ticker']}..."):
                                        price_data = fetch_prices([a["ticker"]])
                                        pd_entry   = price_data.get(a["ticker"])
                                        if pd_entry:
                                            ref_price    = pd_entry["price"]
                                            price_source = f"market (${ref_price:.2f})"
                                        else:
                                            st.error(f"Could not fetch price for {a['ticker']}. Enter it manually.")

                                if ref_price:
                                    add_position(
                                        ticker=          a["ticker"],
                                        company_name=    a["company_name"],
                                        reference_price= ref_price,
                                        exit_condition=  a["exit_condition"],
                                        direction=       a["direction"],
                                        confidence=      a["confidence_score"],
                                        source_title=    a["source_title"],
                                        entry_date=      entry_date,
                                    )
                                    open_tickers.add(a["ticker"])
                                    st.success(
                                        f"✓ {a['ticker']} added to positions "
                                        f"at {price_source}, entry date {entry_date}."
                                    )

    # =========================================================
    # TAB 2 — Portfolio Overview
    # =========================================================
    with tab2:
        st.subheader("Portfolio overview")
        st.caption("Your real invested money across all open positions.")

        open_positions = get_open_positions()
        invested_positions = [p for p in open_positions if p.get("amount_invested", 0) > 0]

        if not invested_positions:
            st.info(
                "No investment amounts recorded yet. "
                "Go to **My Positions** → expand a position → set **Amount invested ($)**."
            )
        else:
            # Fetch live prices for all invested positions
            inv_tickers = [p["ticker"] for p in invested_positions]
            with st.spinner("Fetching live prices..."):
                inv_prices = fetch_prices(inv_tickers)

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
            import pandas as pd

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
  
    # =========================================================
    # TAB 3 — My Positions
    # =========================================================
    with tab3:
        all_positions = get_open_positions()

       # --- Manual position entry ---
        with st.expander("➕ Add a position manually"):
            st.caption("Use this to track stocks you already own that weren't recommended by the pipeline.")

            m_col1, m_col2 = st.columns(2)
            with m_col1:
                m_ticker  = st.text_input("Ticker symbol", placeholder="e.g. MSFT", key="manual_ticker").strip().upper()
                m_company = st.text_input("Company name", placeholder="e.g. Microsoft Corp.", key="manual_company").strip()
                m_price   = st.number_input("Price you paid per share ($)", min_value=0.01, value=100.00, step=0.01, key="manual_entry_price")
            with m_col2:
                m_exit    = st.text_input("Exit condition", placeholder="e.g. target 10% gain, stop loss at 5%", key="manual_exit")
                m_date    = st.date_input("Date you bought it", value=datetime.today().date(), max_value=datetime.today().date(), key="manual_date")
                m_direction = st.selectbox("Direction", ["buy", "watch"], key="manual_direction")

            if st.button("📌 Add to positions", key="manual_add_btn", use_container_width=True, type="primary"):
                import re as _re
                if not m_ticker:
                    st.error("Enter a ticker symbol.")
                elif not _re.match(r'^[A-Z0-9.\-]{1,10}$', m_ticker):
                    st.error("Invalid ticker symbol. Use letters, numbers, dots, or hyphens only (e.g. AAPL, BRK.B).")
                elif not m_company:
                    st.error("Enter a company name.")
                elif m_price <= 0.01:
                    st.error("Enter the price you paid per share.")
                else:
                    # Validate price against market
                    with st.spinner(f"Checking current price for {m_ticker}..."):
                        market_data = fetch_prices([m_ticker])
                        market_info = market_data.get(m_ticker)

                    if market_info:
                        ratio = m_price / market_info["price"]
                        if ratio < 0.5 or ratio > 2.0:
                            st.warning(
                                f"⚠️ You entered ${m_price:.2f} but the current market price is "
                                f"${market_info['price']:.2f}. That's a {abs(1-ratio)*100:.0f}% difference. "
                                f"The position has been added but double-check your entry price."
                            )

                    add_position(
                        ticker=          m_ticker,
                        company_name=    m_company,
                        reference_price= m_price,
                        exit_condition=  m_exit or "No exit condition set",
                        direction=       m_direction,
                        confidence=      0.0,
                        source_title=    "Manually added",
                        entry_date=      m_date.strftime("%Y-%m-%d"),
                    )
                    st.success(f"✓ {m_ticker} added at ${m_price:.2f}, bought on {m_date}.")
                    st.rerun()

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
                    "Bought":     p.get("entry_date") or opened,
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
                    entry_date_display = p.get("entry_date") or opened[:10]
                    c4.metric("Bought on", entry_date_display)

                    st.markdown(f"**Exit when:** {p['exit_condition']}")
                    st.markdown(f"**Based on:** _{p['source_title']}_")

                    # White paper link for crypto positions
                    coin_id = TICKER_TO_COINGECKO_ID.get(p.get("ticker", ""), "")
                    if coin_id:
                        st.markdown(
                            f"🔗 [White paper & info](https://www.coingecko.com/en/coins/{coin_id})"
                        )

                    st.divider()

                    col_manual, col_invest, col_spacer = st.columns([2, 2, 1])
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

                    with col_invest:
                        current_invested = p.get("amount_invested", 0.0) or 0.0
                        new_amount = st.number_input(
                            "Amount invested ($)",
                            min_value=0.0,
                            value=float(current_invested),
                            step=10.0,
                            key=f"amount_invested_{ticker}",
                            help="How much real money you put into this position.",
                        )
                        if st.button("💾 Save amount", key=f"amount_btn_{ticker}"):
                            update_amount_invested(ticker, new_amount)
                            st.success(f"Amount invested set to ${new_amount:.2f}")

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
                            reason      = close_reason or f"Manually closed on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                            close_price = live_price  # use the live price we already fetched
                            close_position(ticker, reason, close_price=close_price)
                            pnl = ((close_price - ref_price) / ref_price * 100) if close_price else None
                            pnl_str = f" | P&L: {pnl:+.1f}%" if pnl is not None else ""
                            st.warning(f"{ticker} position closed at ${close_price:.2f}{pnl_str}.")
                            st.rerun()
                        st.markdown("</div>", unsafe_allow_html=True)

                    st.divider()

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
                # --- Closed positions history ---
        closed_positions = get_closed_positions()
        if closed_positions:
            st.divider()
            st.subheader("Closed positions")

            # Summary metrics
            with_pnl    = [p for p in closed_positions if p.get("pnl_pct") is not None]
            winners     = [p for p in with_pnl if p["pnl_pct"] > 0]
            losers      = [p for p in with_pnl if p["pnl_pct"] <= 0]
            win_rate    = round(len(winners) / len(with_pnl) * 100) if with_pnl else 0
            avg_pnl     = round(sum(p["pnl_pct"] for p in with_pnl) / len(with_pnl), 1) if with_pnl else 0
            best        = max(with_pnl, key=lambda x: x["pnl_pct"]) if with_pnl else None
            worst       = min(with_pnl, key=lambda x: x["pnl_pct"]) if with_pnl else None

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Total closed",  len(closed_positions))
            m2.metric("Win rate",      f"{win_rate}%")
            m3.metric("Avg P&L",       f"{avg_pnl:+.1f}%")
            m4.metric("Best trade",    f"{best['ticker']} {best['pnl_pct']:+.1f}%" if best else "—")
            m5.metric("Worst trade",   f"{worst['ticker']} {worst['pnl_pct']:+.1f}%" if worst else "—")

            st.divider()

            # Closed positions table
            closed_rows = []
            for p in closed_positions:
                entry_price  = p.get("manual_price") or p.get("reference_price", 0)
                close_price  = p.get("close_price")
                pnl_pct      = p.get("pnl_pct")
                closed_at    = datetime.fromisoformat(p["closed_at"]).strftime("%Y-%m-%d") if p.get("closed_at") else "—"
                entry_date   = p.get("entry_date") or "—"

                # Calculate days held
                if p.get("entry_date") and p.get("closed_at"):
                    try:
                        entry_dt  = datetime.strptime(p["entry_date"], "%Y-%m-%d")
                        close_dt  = datetime.fromisoformat(p["closed_at"])
                        days_held = (close_dt - entry_dt).days
                    except Exception:
                        days_held = "—"
                else:
                    days_held = "—"

                closed_rows.append({
                    "Ticker":       p["ticker"],
                    "Company":      p["company_name"],
                    "Entry ($)":    f"${entry_price:.2f}",
                    "Close ($)":    f"${close_price:.2f}" if close_price else "—",
                    "P&L %":        f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—",
                    "Days held":    days_held,
                    "Reason":       p.get("close_reason", "—"),
                    "Closed":       closed_at,
                })

            closed_df = pd.DataFrame(closed_rows)

            def color_pnl(val):
                if val == "—":          return ""
                if val.startswith("+"): return "color: #2ecc71; font-weight: bold"
                if val.startswith("-"): return "color: #e74c3c; font-weight: bold"
                return ""

            styled_closed = closed_df.style.map(color_pnl, subset=["P&L %"])
            st.dataframe(styled_closed, use_container_width=True, hide_index=True)

    # =========================================================
    # TAB 4 — Watch List Editor
    # =========================================================
    with tab4:
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
    # TAB 5 — History
    # =========================================================
    with tab5:
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
    st.markdown("## Get started with Argus")
    st.markdown("Three steps to your first recommendation.")
    st.divider()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("### 1️⃣ Set your budget")
        st.markdown(
            "Enter how much you want to allocate in the sidebar. "
            "This is an experimental tool — start with an amount you're comfortable with. "
            "Only **buy** signals receive allocations."
        )
    with col2:
        st.markdown("### 2️⃣ Run the pipeline")
        st.markdown(
            "Click **🔄 Run pipeline**. Argus fetches today's financial news from 8+ sources, "
            "scores each story for credibility, and sends the strongest signals to Claude for analysis."
        )
    with col3:
        st.markdown("### 3️⃣ Track your positions")
        st.markdown(
            "Add stocks you buy to **My Positions** to track real P&L, set exit conditions, "
            "and get alerts when your targets are hit."
        )

    st.divider()
    st.caption("Argus is experimental and not financial advice. Past signals do not guarantee future results.")
    # =========================================================
# ARGUS CHATBOT — Floating assistant widget
# =========================================================


from streamlit.components.v1 import html as st_html

st_html(f"""<!DOCTYPE html>
<html>
<head>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{
    background: transparent !important;
    overflow: hidden;
    font-family: 'Segoe UI', sans-serif;
  }}

  #argus-chat-btn {{
    position: fixed;
    bottom: 12px;
    right: 12px;
    width: 52px;
    height: 52px;
    border-radius: 50%;
    background: linear-gradient(135deg, #2ecc71, #1a8a4a);
    border: none;
    cursor: pointer;
    z-index: 9999;
    box-shadow: 0 4px 24px rgba(46,204,113,0.4);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    color: white;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }}
  #argus-chat-btn:hover {{
    transform: scale(1.08);
    box-shadow: 0 6px 32px rgba(46,204,113,0.6);
  }}

  #argus-chat-panel {{
    position: fixed;
    bottom: 76px;
    right: 12px;
    width: 370px;
    height: 500px;
    background: #0f1a14;
    border: 1px solid #2ecc71;
    border-radius: 16px;
    z-index: 9998;
    display: none;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 8px 48px rgba(0,0,0,0.6);
  }}
  #argus-chat-panel.open {{ display: flex; }}

  #argus-chat-header {{
    padding: 14px 18px;
    background: #0d1f12;
    border-bottom: 1px solid rgba(46,204,113,0.3);
    display: flex; align-items: center; gap: 10px; flex-shrink: 0;
  }}
  .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: #2ecc71; box-shadow: 0 0 8px #2ecc71;
    animation: pulse 2s infinite; flex-shrink: 0;
  }}
  @keyframes pulse {{ 0%,100%{{opacity:1;}} 50%{{opacity:0.4;}} }}
  .title {{ color:#2ecc71; font-size:13px; font-weight:700; letter-spacing:0.05em; text-transform:uppercase; }}
  .subtitle {{ color:rgba(255,255,255,0.4); font-size:11px; font-family:monospace; margin-left:auto; }}
  #close-btn {{ background:none; border:none; color:rgba(255,255,255,0.4); font-size:16px; cursor:pointer; padding:0 0 0 8px; }}
  #close-btn:hover {{ color:#fff; }}

  #argus-chat-messages {{
    flex:1; overflow-y:auto; padding:14px;
    display:flex; flex-direction:column; gap:12px;
    scrollbar-width:thin; scrollbar-color:#2ecc71 transparent;
  }}
  #argus-chat-messages::-webkit-scrollbar {{ width:4px; }}
  #argus-chat-messages::-webkit-scrollbar-thumb {{ background:#2ecc71; border-radius:2px; }}

  .msg {{ max-width:85%; padding:10px 14px; border-radius:12px; font-size:13px; line-height:1.5; }}
  .user {{ align-self:flex-end; background:linear-gradient(135deg,#1a4a2e,#2ecc71); color:#fff; border-bottom-right-radius:4px; }}
  .assistant {{
    align-self:flex-start; background:rgba(255,255,255,0.05);
    color:rgba(255,255,255,0.9); border:1px solid rgba(46,204,113,0.15);
    border-bottom-left-radius:4px; font-family:monospace; font-size:12px;
  }}
  .disclaimer {{ margin-top:6px; font-size:10px; color:rgba(255,255,255,0.3); border-top:1px solid rgba(255,255,255,0.1); padding-top:6px; }}
  .thinking {{
    align-self:flex-start; color:#2ecc71; font-family:monospace; font-size:11px;
    padding:8px 14px; background:rgba(46,204,113,0.05);
    border:1px solid rgba(46,204,113,0.2); border-radius:12px;
  }}

  #argus-chat-input-area {{
    padding:12px 16px; border-top:1px solid rgba(46,204,113,0.2);
    background:#0a1210; display:flex; gap:8px; align-items:flex-end; flex-shrink:0;
  }}
  #argus-chat-input {{
    flex:1; background:rgba(255,255,255,0.05);
    border:1px solid rgba(46,204,113,0.3); border-radius:8px;
    color:#fff; padding:10px 12px; font-size:13px;
    font-family:'Segoe UI',sans-serif;
    resize:none; outline:none; min-height:40px; max-height:100px;
    transition:border-color 0.2s;
  }}
  #argus-chat-input:focus {{ border-color:#2ecc71; }}
  #argus-chat-input::placeholder {{ color:rgba(255,255,255,0.25); }}
  #argus-chat-send {{
    background:linear-gradient(135deg,#2ecc71,#1a8a4a);
    border:none; border-radius:8px; width:40px; height:40px;
    cursor:pointer; color:white; font-size:16px; flex-shrink:0;
    display:flex; align-items:center; justify-content:center;
    transition:opacity 0.2s;
  }}
  #argus-chat-send:hover {{ opacity:0.85; }}
  #argus-chat-send:disabled {{ opacity:0.4; cursor:not-allowed; }}
</style>
</head>
<body>

<button id="argus-chat-btn" onclick="toggleArgusChat()">🔍</button>

<div id="argus-chat-panel">
  <div id="argus-chat-header">
    <div class="dot"></div>
    <div class="title">Argus Assistant</div>
    <div class="subtitle">investing only</div>
    <button id="close-btn" onclick="toggleArgusChat()">✕</button>
  </div>
  <div id="argus-chat-messages">
    <div class="msg assistant">
      Hey — I'm Argus. Ask me anything about investing, how this app works, or what any of the signals mean.
      <div class="disclaimer">Not financial advice. For informational purposes only.</div>
    </div>
  </div>
  <div id="argus-chat-input-area">
    <textarea id="argus-chat-input" placeholder="Ask about investing or how Argus works..." rows="1"
      onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendArgusMessage();}}"></textarea>
    <button id="argus-chat-send" onclick="sendArgusMessage()">➤</button>
  </div>
</div>

<script>
let myIframe = null;
let argusOpen = false;

function findMyIframe() {{
  if (myIframe) return myIframe;
  try {{
    const iframes = window.parent.document.querySelectorAll('iframe');
    for (let i = 0; i < iframes.length; i++) {{
      try {{
        if (iframes[i].contentWindow === window) {{
          myIframe = iframes[i];
          return myIframe;
        }}
      }} catch(e) {{}}
    }}
  }} catch(e) {{}}
  return null;
}}

function setIframeSize(expanded) {{
  const iframe = findMyIframe();
  if (!iframe) return;
  if (expanded) {{
    iframe.style.cssText = `
      position:fixed !important; bottom:0 !important; right:0 !important;
      width:420px !important; height:640px !important;
      border:none !important; z-index:999999 !important;
      background:transparent !important;
    `;
  }} else {{
    iframe.style.cssText = `
      position:fixed !important; bottom:20px !important; right:20px !important;
      width:72px !important; height:72px !important;
      border:none !important; z-index:999999 !important;
      background:transparent !important;
    `;
  }}
}}

// Start small — just the button
function init() {{
  if (!findMyIframe()) {{
    setTimeout(init, 100);
    return;
  }}
  setIframeSize(false);
}}
init();

function toggleArgusChat() {{
  argusOpen = !argusOpen;
  document.getElementById('argus-chat-panel').className = argusOpen ? 'open' : '';
  setIframeSize(argusOpen);
  if (argusOpen) setTimeout(() => document.getElementById('argus-chat-input').focus(), 150);
}}

// API key is handled server-side via proxy at localhost:8502
const ARGUS_SYSTEM = `You are Argus Assistant, the built-in helper for the Argus stock advisor app.
STRICT RULES:
1. You ONLY discuss investing topics and how the Argus app works. Nothing else.
2. If asked about anything unrelated say: "I can only help with investing topics and how Argus works."
3. Keep responses concise — 3-5 sentences max unless detail is genuinely needed.
4. Never recommend specific stocks to buy or sell.
5. Always end with: "Not financial advice — always do your own research."
ABOUT ARGUS:
- AI stock advisor fetching news from 8+ sources daily
- Scores: SEC=1.0, Finnhub=0.68, RSS=0.50, Reddit=0.15
- Claude analyzes top 25 stories, returns buy/watch/avoid signals
- Buy signals get real budget allocations. Watch=$0. Avoid=filtered out.
- Confidence score = source verification level, not stock prediction confidence
- Track positions with entry price, date, exit conditions
- Exit checker monitors stop loss, gain targets, time limits, news events
- Portfolio tab shows invested money and combined value trend graph
- Mock mode (MOCK_MODE=true in .env) skips Claude API for zero-token testing
INVESTING TOPICS:
- Stop loss, P&L, confidence score, allocation, position, signal definitions
- How to read the recommendation table, buy vs watch vs avoid
- Diversification, risk management, dollar cost averaging
- How to use any Argus feature`;

let argusHistory = [];

function appendMsg(role, text) {{
  const c = document.getElementById('argus-chat-messages');
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  if (role === 'assistant') {{
    d.innerHTML = text.replace(/\\n/g,'<br>').replace(/[*][*](.*?)[*][*]/g,'<strong>$1</strong>') +
      '<div class="disclaimer">Not financial advice — always do your own research.</div>';
  }} else {{
    d.textContent = text;
  }}
  c.appendChild(d);
  c.scrollTop = c.scrollHeight;
}}

async function sendArgusMessage() {{
  const input = document.getElementById('argus-chat-input');
  const sendBtn = document.getElementById('argus-chat-send');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  sendBtn.disabled = true;
  appendMsg('user', text);
  argusHistory.push({{role:'user', content:text}});

  const c = document.getElementById('argus-chat-messages');
  const thinking = document.createElement('div');
  thinking.className = 'thinking';
  thinking.textContent = '▋ analyzing...';
  c.appendChild(thinking);
  c.scrollTop = c.scrollHeight;

  try {{
    const response = await fetch('http://localhost:8502/chat', {{
      method:'POST',
      headers:{{ 'Content-Type':'application/json' }},
      body: JSON.stringify({{
        system: ARGUS_SYSTEM,
        messages: argusHistory,
      }}),
    }});
    const data = await response.json();
    thinking.remove();
    if (data.content && data.content[0]) {{
      const reply = data.content[0].text;
      argusHistory.push({{role:'assistant', content:reply}});
      appendMsg('assistant', reply);
    }} else {{
      appendMsg('assistant','Error: ' + JSON.stringify(data));
    }}
  }} catch(e) {{
    thinking.remove();
    appendMsg('assistant','Could not reach the API. Check your connection.');
  }}
  sendBtn.disabled = false;
  input.focus();
}}
</script>
</body>
</html>""", height=640)