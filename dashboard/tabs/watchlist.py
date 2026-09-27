"""
Watch List — what Argus is monitoring (exit + entry alerts, pins) and the ticker watchlist editor.
Extracted verbatim from dashboard/app.py (tab 4); rendered via render().
"""
import streamlit as st
from storage.positions import get_open_positions
from storage.watchlist import add_ticker, load_watchlist, remove_ticker, reset_to_defaults
from dashboard.common import _cached_prices, _stop_loss_price


def render() -> None:
    # --- Pinned buy triggers (entry alerts) -------------------------------
    # Central place to review/remove what the entry checker is monitoring. Without
    # this, a pin whose ticker dropped out of the recommendations was orphaned:
    # still alerting every 15 min with no UI to remove it.
    from storage.entry_watch import (
        get_pinned as _get_pinned, remove_pinned as _remove_pinned,
        get_chat_suggestions as _get_chat_sugg, set_chat_suggestions as _set_chat_sugg,
        pin_days_left as _pin_days_left, PIN_TTL_DAYS as _PIN_TTL,
        clear_all_pinned as _clear_all_pinned,
    )
    from alerts.entry_checker import _parse_triggers as _parse_trig

    # Streamlit renders paired '$...$' as LaTeX, which mangles trigger text full of
    # prices. Escape every '$' before displaying any analyst-written string.
    def _esc(s: str) -> str:
        return str(s or "").replace("$", "\\$")

    st.subheader("🔎 What Argus is monitoring")
    st.caption(
        "Everything checked every 15 minutes during market hours. **Open positions** are "
            "watched for EXIT (stop / target / time / news). **Pinned triggers** are watched for "
            "ENTRY. Owned tickers are deliberately excluded from entry alerts — you're already in."
    )

    # --- Open positions → exit alerts (auto-included, nothing to pin) ------
    st.markdown("##### 📍 Open positions — exit alerts")
    _open_pos = get_open_positions()
    if not _open_pos:
        st.caption("No open positions.")
    else:
        _pos_px = {}
        try:
            _pos_px = _cached_prices(tuple(sorted({p["ticker"] for p in _open_pos}))) or {}
        except Exception:
            pass
        for p in _open_pos:
            t = p["ticker"]
            ref = p.get("manual_price") or p.get("reference_price", 0)
            q = _pos_px.get(t)
            live = q["price"] if q else ref
            pnl = ((live - ref) / ref * 100) if ref else 0
            if p.get("direction") == "short":
                pnl = -pnl
            stop_px = _stop_loss_price(p.get("exit_condition", ""), ref, p.get("direction", "buy"))
            stop_str = f" · stop @ \\${stop_px:,.2f}" if stop_px else ""
            st.markdown(
                f"**{t}** — entry \\${ref:,.2f} · live \\${live:,.2f} · "
                    f"P&L {pnl:+.1f}%{stop_str}"
            )
            st.caption(f"Exit when: {_esc(p.get('exit_condition', 'not set'))}")
        st.caption("Manage these under **My Positions**.")

    st.divider()
    st.markdown("##### 📌 Pinned buy triggers — entry alerts")
    _pinned = _get_pinned()
    if not _pinned:
        st.caption(
            "Nothing pinned. On a watch recommendation (Today's Recommendations → "
                "Stock details), click **👁 Watch this trigger** to get an email when its "
                "\"buy when\" level is hit — it keeps working after the pipeline reruns."
        )
    else:
        st.caption(
            f"{len(_pinned)} trigger(s) checked every 15 minutes during market hours. "
                "These survive pipeline reruns — remove any you no longer want. "
                f"A pin auto-expires {_PIN_TTL} day(s) after it was pinned (its clock resets "
                "whenever Argus surfaces the ticker again), so a stale thesis can't keep an "
                "orphaned price level armed."
        )
        # Clear-all: two-step so a whole watch list isn't wiped by a stray click.
        if not st.session_state.get("_confirm_clear_pins"):
            if st.button(f"🗑 Clear all {len(_pinned)} pins", key="clear_pins_btn"):
                st.session_state["_confirm_clear_pins"] = True
                st.rerun()
        else:
            st.warning(f"Remove all {len(_pinned)} pinned triggers? This can't be undone.")
            _cc1, _cc2 = st.columns(2)
            if _cc1.button("✅ Yes, clear all", key="clear_pins_yes"):
                _n = _clear_all_pinned()
                st.session_state["_confirm_clear_pins"] = False
                st.success(f"Cleared {_n} pinned trigger(s).")
                st.rerun()
            if _cc2.button("Cancel", key="clear_pins_no"):
                st.session_state["_confirm_clear_pins"] = False
                st.rerun()
        _live = {}
        try:
            _live = _cached_prices(tuple(sorted({p["ticker"] for p in _pinned}))) or {}
        except Exception:
            pass

        for p in _pinned:
            t = p["ticker"]
            c_info, c_btn = st.columns([9, 1])
            with c_info:
                levels = _parse_trig(p.get("trigger_text", ""))
                q = _live.get(t)
                live_str = f" · live \\${q['price']:,.2f}" if q else ""
                if levels:
                    lvl_str = ", ".join(
                        f"{'breakout' if d == 'above' else 'pullback'} \\${v:,.2f}"
                        for d, v in levels
                    )
                    st.markdown(f"**{t}** — {lvl_str}{live_str}")
                else:
                    # No price in the trigger means the checker can never fire on it —
                    # say so instead of leaving a pin that silently does nothing.
                    st.markdown(f"**{t}** — ⚠️ no price level{live_str}")
                    st.warning(
                        "This trigger is event-based (no \\$ price), so it will NEVER "
                            "fire an alert. Remove it and watch the news instead, or re-pin "
                            "once a recommendation gives a concrete price.",
                        icon="⚠️",
                    )
                _left = _pin_days_left(p)
                if _left is None:
                    _exp = "expiry unknown"
                elif _left == 0:
                    _exp = "⏳ expires today"
                elif _left == 1:
                    _exp = "⏳ expires in 1 day"
                else:
                    _exp = f"expires in {_left} days"
                st.caption(
                    f"Pinned {p.get('pinned_at', '?')} · {_exp} · "
                        f"{_esc(p.get('trigger_text', ''))[:180]}"
                )
            with c_btn:
                if st.button("✕", key=f"rm_pin_{t}", help=f"Stop watching {t}"):
                    _remove_pinned(t)
                    st.success(f"Removed {t} from pinned triggers.")
                    st.rerun()

    # Chat-sourced suggestions also drive entry alerts — surface them so they can
    # be cleared too (they're replaced automatically on the next chat suggestion).
    _chat_sugg = _get_chat_sugg()
    if _chat_sugg:
        with st.expander(f"💬 From Argus chat's last suggestion ({len(_chat_sugg)})"):
            st.caption(
                "Captured from the last action list Argus chat gave. Replaced automatically "
                    "the next time it suggests moves."
            )
            for s in _chat_sugg:
                st.markdown(
                    f"**{s['ticker']}** ({s.get('action', 'watch')}) — "
                        f"{_esc(s.get('trigger_text', ''))[:180]}"
                )
            if st.button("Clear chat suggestions"):
                _set_chat_sugg([])
                st.success("Cleared.")
                st.rerun()

    st.divider()

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
