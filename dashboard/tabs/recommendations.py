"""
Today's Recommendations — allocation table, stock details, watch pins, add-to-positions, Sheets export.
Extracted verbatim from dashboard/app.py (tab 1); rendered via render().
"""
import streamlit as st
import os
from datetime import datetime
from ingestion.coingecko import TICKER_TO_COINGECKO_ID
from ingestion.prices import fetch_prices
from storage.positions import add_position, get_open_positions
from dashboard.common import _cached_history, _render_alloc_table


def render(allocations, budget, prices, recs) -> None:
    if not allocations:
        if not recs:
            st.info("No recommendations yet. Click **🔄 Run pipeline** in the sidebar to fetch today's signals.")
        else:
            st.warning("No actionable recommendations after filtering. Try running the pipeline again.")
    else:
        # If sizing is $0 because buying power couldn't be read (Robinhood session
        # expired / not connected), say so — otherwise an all-$0 table looks like a
        # dead market when it's really just a login refresh needed.
        if budget <= 0:
            st.warning(
                "💤 **Buying power unavailable — showing analysis only, \\$0 sizing.** "
                    "This usually means the Robinhood session expired. Re-authenticate "
                    "(run `python ingestion\\robinhood.py` once and approve the app prompt), "
                    "then **💵 Refresh buying power** in the sidebar. The recommendations below "
                    "are still today's real read."
            )
        col1, col2, col3, col4, col5 = st.columns(5)
        buy_count     = sum(1 for a in allocations if a["direction"] == "buy")
        short_count   = sum(1 for a in allocations if a["direction"] == "short")
        watch_count   = sum(1 for a in allocations if a["direction"] == "watch")
        flagged_count = sum(1 for a in allocations if a["flagged"])

        col1.metric("Total stocks",  len(allocations))
        col2.metric("Buy signals",   buy_count)
        col3.metric("🔻 Short signals", short_count)
        col4.metric("Watch signals", watch_count)
        col5.metric("⚠ Flagged",     flagged_count)

        st.divider()
        st.subheader("Portfolio allocation")

        # Ensure highly_recommended exists even for old cache data
        for a in allocations:
            a.setdefault("highly_recommended", False)
            a["highly_recommended_display"] = "⭐" if a["highly_recommended"] else ""

        for a in allocations:
            a.setdefault("conviction", None)
            a.setdefault("entry_trigger", "")

        _render_alloc_table(allocations)   # single combined table (reverted R28 per-class split)
        # NOTE: escape every '$' as '\$' — Streamlit renders paired '$...$' as LaTeX,
        # which silently ate the dollar signs and mangled this caption.
        st.caption(
            "ℹ️ **Conviction** (0-100) — the analyst's EDGE score; drives position size.  "
                "**Confidence** — source credibility only (1.0 = SEC filing, 0.15 = Reddit), NOT trade edge.  "
                "**Amount (\\$)** — \\$0.00 means watch only, no capital allocated.  "
                "**Flags** — ⭐ highly recommended, ⚠ unverified source (treat with extra caution).  "
                "Buy/Sell text is shortened here — full wording is in **Stock details** below."
        )

        # Batch-add watch triggers: check several, add all at once. Inside st.form so the
        # checkboxes DON'T each fire a full rerun (that one-by-one lag was the complaint).
        _watch_recs = [a for a in allocations
                       if a.get("direction") == "watch"
                       and (a.get("entry_trigger") or "").lower() not in ("now", "n/a", "")]
        if _watch_recs:
            from storage.entry_watch import add_pinned, is_pinned
            with st.expander(f"📋 Add multiple to watch list ({len(_watch_recs)} watches)"):
                st.caption("Check the triggers to watch, then add them all with one button "
                               "(email when each 'buy when' level is hit).")
                with st.form("batch_watch_form"):
                    _wchecks = {}
                    for a in _watch_recs:
                        _wt = a["ticker"]
                        _pinned = is_pinned(_wt)
                        _trg = " ".join(str(a.get("entry_trigger") or "").split())
                        if len(_trg) > 70:
                            _trg = _trg[:69] + "…"
                        _wchecks[_wt] = st.checkbox(
                            f"**{_wt}** — {_trg}" + ("  ✓ already watching" if _pinned else ""),
                            key=f"bw_{_wt}", disabled=_pinned)
                    if st.form_submit_button("👁 Add checked to watch list"):
                        _n = 0
                        for a in _watch_recs:
                            if _wchecks.get(a["ticker"]) and not is_pinned(a["ticker"]):
                                add_pinned(a["ticker"], a["company_name"],
                                           a["entry_trigger"], a.get("exit_condition", ""))
                                _n += 1
                        st.success(f"Added {_n} to the watch list.")
                        st.rerun()

        col_export, col_spacer = st.columns([1, 4])
        with col_export:
            if st.button("📤 Export to Google Sheets", use_container_width=True):
                from storage.sheets import export_to_sheets
                with st.spinner("Exporting..."):
                    success = export_to_sheets(allocations, budget)
                if success:
                    _cached_history.clear()  # new rows → History tab must re-read
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
            direction_emoji = {"buy": "🟢", "short": "🔻", "watch": "🟡"}.get(a["direction"], "🟡")
            hr_badge        = " ⭐ HIGHLY RECOMMENDED" if a.get("highly_recommended") else ""
            amount_label    = f"short ${a['dollar_amount']:.2f}" if a["direction"] == "short" else f"${a['dollar_amount']:.2f}"
            is_open         = a["ticker"] in open_tickers

            with st.expander(
                f"{direction_emoji} {a['ticker']} — {a['company_name']} "
                    f"| {amount_label} ({a['percentage']:.1f}%){flag_label}{hr_badge}"
            ):
                # Colored bar — green for buy, red for short, orange for watch
                bar_color = (
                    "#FFD700" if a.get("highly_recommended")
                    else "#2ecc71" if a["direction"] == "buy"
                    else "#e74c3c" if a["direction"] == "short"
                    else "#f39c12"
                )
                st.markdown(
                    f'<div style="height:3px; background:{bar_color}; border-radius:2px; margin-bottom:12px"></div>',
                    unsafe_allow_html=True,
                )
                if a.get("highly_recommended"):
                    st.markdown(
                        '<div style="background:rgba(255,215,0,0.1); border:1px solid #FFD700; '
                            'border-radius:8px; padding:8px 14px; margin-bottom:12px; '
                            'color:#FFD700; font-size:13px; font-weight:700;">⭐ Highly Recommended — '
                            'Strong catalyst, high-conviction signal, aggressive targets set.</div>',
                        unsafe_allow_html=True,
                    )
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Direction",  a["direction"].upper())
                c2.metric("Risk",       a["risk_level"].upper())
                conv = a.get("conviction")
                c3.metric("Conviction", f"{int(conv)}" if conv is not None else "—",
                          help="The analyst's EDGE score (0-100): how strong/timely/un-priced-in the opportunity is. Drives position size. Separate from source credibility.")
                c4.metric("Confidence", f"{a['confidence_score']:.2f}",
                          help="SOURCE CREDIBILITY (how much to trust the report). 1.0 = SEC filing, 0.7 = Finnhub, 0.15 = Reddit. NOT a measure of trade edge — see Conviction.")

                why_label = {"buy": "Why buy", "short": "Why short", "watch": "Why watch"}.get(a["direction"], "Why watch")
                st.markdown(f"**{why_label}:** {a['entry_rationale']}")
                trigger = a.get("entry_trigger")
                if trigger and trigger.lower() not in ("now", "n/a", ""):
                    st.markdown(f"**Buy when:** {trigger}")
                    # Pin this watch so the entry checker keeps monitoring it even
                    # after a pipeline rerun overwrites pipeline_cache.json.
                    from storage.entry_watch import add_pinned, remove_pinned, is_pinned
                    pin_key = f"pin_{idx}_{a['ticker']}"
                    if is_pinned(a["ticker"]):
                        st.caption("👁 Watching — you'll get an email when this trigger is hit.")
                        if st.button("✕ Stop watching", key=f"unpin_{pin_key}"):
                            remove_pinned(a["ticker"])
                            st.success(f"Stopped watching {a['ticker']}.")
                            st.rerun()
                    else:
                        if st.button("👁 Watch this trigger", key=pin_key,
                                     help="Keep monitoring this 'buy when' level even after "
                                              "the pipeline reruns. Emails you when it's hit."):
                            add_pinned(a["ticker"], a["company_name"], trigger,
                                       a.get("exit_condition", ""))
                            st.success(f"Watching {a['ticker']} — alert when the trigger hits.")
                            st.rerun()
                st.markdown(f"**Sell when:** {a['exit_condition']}")
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
