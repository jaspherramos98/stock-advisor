"""
My Positions — open/closed positions, manual entry, prices, snooze, suggested exits, scorecard.
Extracted verbatim from dashboard/app.py (tab 3); rendered via render().
"""
import streamlit as st
import pandas as pd
from alerts.snooze import clear_snooze, dismiss_ticker, is_snoozed, snooze_ticker
from datetime import datetime
from ingestion.coingecko import TICKER_TO_COINGECKO_ID
from ingestion.prices import fetch_prices
from storage.positions import add_position, close_position, get_closed_positions, get_open_positions, update_amount_invested, update_exit_condition, update_manual_price
from dashboard.common import _cached_prices, _cached_spy_benchmark, _stop_loss_price, _suggested_exit


def render() -> None:
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
            m_direction = st.selectbox("Direction", ["buy", "short", "watch"], key="manual_direction")

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
            live_prices = _cached_prices(tuple(sorted(set(pos_tickers))))

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
            if change_pct is not None and p.get("direction") == "short":
                change_pct = -change_pct  # shorts profit when price falls
            opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d")
            stop_px    = _stop_loss_price(p["exit_condition"], ref_price, p.get("direction", "buy"))

            rows.append({
                "Ticker":     ticker,
                "Company":    p["company_name"],
                "Side":       "SHORT" if p.get("direction") == "short" else "long",
                "Ref Price":  f"${ref_price:.2f}",
                "Live Price": f"${live_price:.2f}" if live_price else "N/A",
                "Change":     f"{change_pct:+.1f}%" if change_pct is not None else "N/A",
                "Stop @":     f"${stop_px:.2f}" if stop_px is not None else "—",
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
            if change_pct is not None and p.get("direction") == "short":
                change_pct = -change_pct  # shorts profit when price falls
            opened     = datetime.fromisoformat(p["opened_at"]).strftime("%Y-%m-%d %H:%M")

            change_str = f"{change_pct:+.1f}%" if change_pct is not None else "N/A"
            emoji      = "📈" if (change_pct or 0) >= 0 else "📉"

            side_label = " 🔻SHORT" if p.get("direction") == "short" else ""
            with st.expander(f"{emoji} {ticker}{side_label} — {p['company_name']} | {change_str} since entry"):
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Reference price", f"${ref_price:.2f}")
                c2.metric("Live price",       f"${live_price:.2f}" if live_price else "N/A")
                c3.metric("Change",           change_str)
                entry_date_display = p.get("entry_date") or opened[:10]
                c4.metric("Bought on", entry_date_display)

                st.markdown(f"**Exit when:** {p['exit_condition']}")
                stop_px = _stop_loss_price(p["exit_condition"], ref_price, p.get("direction", "buy"))
                if stop_px is not None:
                    st.markdown(f"**Stop-loss price:** \\${stop_px:.2f}  _(from {ref_price:.2f} reference)_")

                # Structure-anchored exit suggestion (R24) applied to this holding. Longs
                # only — the math (target = up to resistance) is long-oriented; a short's
                # target is down to support, so don't hand a wrong number for shorts.
                if p.get("direction") != "short":
                    _crypto = bool(TICKER_TO_COINGECKO_ID.get(ticker, ""))
                    kl = _suggested_exit(ticker, "crypto" if _crypto else "stock")
                    if kl:
                        tgt, stp, rr = kl.get("target_pct_resist"), kl.get("stop_pct_atr"), kl.get("reward_risk")
                        res = kl.get("nearest_resistance")
                        if tgt is not None and stp is not None:
                            weak = " · ⚠️ weak (R:R < 2 — consider trimming/watching)" if (rr and rr < 2) else ""
                            st.markdown(
                                f"**📐 Suggested exit (structure):** target **{tgt}%** "
                                    f"(to resistance \\${res}), stop **{stp}%** (ATR) · R:R {rr}{weak}"
                            )
                            _sugg = f"target {tgt}% gain, stop loss at {stp}%"
                            if st.button(f"Apply → {_sugg}", key=f"apply_exit_{ticker}"):
                                update_exit_condition(ticker, _sugg)
                                st.success(f"Exit updated: {_sugg}")
                                st.rerun()
                        elif stp is not None:
                            st.markdown(
                                f"**📐 Suggested exit (structure):** no overhead resistance "
                                    f"(room to run) — stop **{stp}%** (ATR); size the target to a "
                                    f"measured move ≥ **{round(2 * stp, 1)}%** to keep reward ≥ 2× the stop."
                            )
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

                new_exit = st.text_input(
                    "Exit strategy",
                    value=p["exit_condition"],
                    key=f"exit_cond_{ticker}",
                    help="e.g. 'target 10% gain, stop loss at 4%'. Edit this for synced positions.",
                )
                if st.button("💾 Save exit strategy", key=f"exit_btn_{ticker}"):
                    update_exit_condition(ticker, new_exit)
                    st.success("Exit strategy updated.")
                    st.rerun()

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

        # ── Scorecard: the honest read on whether Argus is actually earning ──
        from analysis.scorecard import compute_scorecard
        sc = compute_scorecard(closed_positions)

        if sc.get("trades"):
            net      = sc["net_dollars"]
            pf       = sc["profit_factor"]
            payoff   = sc["payoff_ratio"]
            disc     = sc["discipline"]

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Net realized",  f"${net:+,.2f}")
            m2.metric("Win rate",      f"{sc['win_rate']}%")
            m3.metric("Profit factor", f"{pf:.2f}" if pf is not None else "—",
                      help="Gross profit ÷ gross loss. >1 = profitable; >2 = strong.")
            m4.metric("Payoff ratio",  f"{payoff:.2f}" if payoff is not None else "—",
                      help="Avg win ÷ avg loss. >1 means winners are bigger than losers — "
                               "you can be profitable below a 50% win rate.")
            m5.metric("Expectancy",    f"{sc['expectancy_pct']:+.2f}%",
                      help="Average % you make per trade taken, long-run.")

            # Discipline callout — the headline leak. Let-run vs closed-early.
            lr_n, lr_avg = disc["let_run_count"], disc["let_run_avg"]
            e_n,  e_avg  = disc["early_count"],   disc["early_avg"]
            if e_n and lr_n and lr_avg - e_avg >= 2.0:
                st.error(
                    f"⚠️ **Discipline leak — you're closing trades early.** "
                        f"The **{lr_n}** trade(s) you let run to their target/stop averaged "
                        f"**{lr_avg:+.1f}%**. The **{e_n}** you manually closed *before* either "
                        f"band averaged just **{e_avg:+.1f}%**. The picks work; cutting them early "
                        f"at breakeven is what's flattening your P&L. Let the exit plan play out."
                )
            elif e_n:
                st.caption(
                    f"Let-run trades: {lr_n} (avg {lr_avg:+.1f}%) · "
                        f"Closed early: {e_n} (avg {e_avg:+.1f}%)."
                )

            # Concentration — is the profit one lucky trade or a repeatable process?
            top = sc.get("top_trade")
            share = sc.get("top_trade_profit_share")
            if top and share and share >= 50:
                st.warning(
                    f"📌 **Concentration risk:** {top['ticker']} alone "
                        f"(${top['pnl_dollars']:+,.2f}) is **{share}%** of all gross profit. "
                        f"Strip it and the rest of the book is near breakeven — the edge isn't "
                        f"proven across trades yet, so don't over-trust a single winner."
                )

            # SPY opportunity cost — did active trading beat just holding the index?
            bench = _cached_spy_benchmark(closed_positions)
            if bench:
                edge = bench["edge"]
                verdict = "beat" if edge >= 0 else "LAGGED"
                st.caption(
                    f"vs SPY over the same dates ({bench['trades']} trades): Argus "
                        f"${bench['argus_net']:+,.2f} vs SPY ${bench['spy_net']:+,.2f} → "
                        f"you **{verdict}** the index by ${abs(edge):,.2f}."
                )

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

        # --- Remove an individual closed trade from history ---
        with st.expander("🗑 Remove a closed trade from history"):
            st.caption("Permanently deletes the record so it no longer feeds the scorecard. "
                           "Does NOT touch Robinhood — history only.")
            _del_opts = {}
            for _i, _p in enumerate(closed_positions):
                _cd = (datetime.fromisoformat(_p["closed_at"]).strftime("%Y-%m-%d")
                       if _p.get("closed_at") else "—")
                _pnl = f"{_p['pnl_pct']:+.1f}%" if _p.get("pnl_pct") is not None else "—"
                _del_opts[f"{_i+1}. {_p['ticker']} · closed {_cd} · {_pnl}"] = _p.get("opened_at")
            _del_sel = st.selectbox("Closed trade", list(_del_opts.keys()), key="del_closed_sel")
            _del_confirm = st.checkbox("Confirm — this can't be undone", key="del_closed_confirm")
            if st.button("🗑 Remove from history", disabled=not _del_confirm, key="del_closed_btn"):
                from storage.positions import delete_position
                if delete_position(_del_opts.get(_del_sel)):
                    st.success(f"Removed: {_del_sel}")
                    st.rerun()
                else:
                    st.error("Could not remove (record not found).")
