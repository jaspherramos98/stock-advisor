"""
Agent — observe/control the autonomous options agent (paper book, live chart, ledger, kill switch, runs).
Extracted verbatim from dashboard/app.py (tab 6); rendered via render().
"""
import streamlit as st
import pandas as pd
import os
import plotly.graph_objects as go
from dashboard.common import (_c_agentic_bp, _c_agentic_equity, _c_day_trade_budget, _c_option_orders,
                              _c_option_positions, _c_option_quote)


def render() -> None:
    st.subheader("🤖 Agentic options agent")
    st.caption("Autonomous options trader on the **isolated Agentic pilot** account only "
                   "(never your main book). Observe what it holds and would do, override any "
                   "decision, and control run mode + the kill switch. Position size is uncapped "
                   "by design — this is a disposable-capital experiment, high-variance / likely -EV.")

    import config as _cfg
    import llm_budget as _lb
    try:
        from ingestion import robinhood_mcp as _amcp
        from ingestion import options_data as _aod
        from alerts.agentic_options import run_options_agent, _HALT_FLAG, _run_exits
        from analysis.options_strategies import option_exit_decision, DEFAULT_EXIT
        from trading_guards import OptionOrderIntent, GuardState
        from ingestion import mcp_auth as _amcpauth
        _agent_ok = True
    except Exception as _e:  # noqa: BLE001
        st.error(f"Agent modules unavailable: {_e}")
        _agent_ok = False

    if _agent_ok:
        _acct = _amcp.agentic_account_number() if _amcp.is_available() else None
        _halted = os.path.exists(_HALT_FLAG)
        _agbp = _c_agentic_bp(_acct) if _acct else None
        _led = _lb.get_state()

        # The scheduler decides live-vs-paper by the ARM FILE, not config.DRY_RUN (which is a
        # static default the scheduler overrides at runtime). So show the armed state — that's
        # what actually governs whether the agent trades real money.
        _arm_path = os.path.join(os.path.dirname(_HALT_FLAG), "agent_live.arm")
        _armed = os.path.exists(_arm_path)
        _pdt = _c_day_trade_budget(_acct, _agbp) if _acct else None
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Agent mode", "🔴 ARMED — LIVE" if _armed else "🟢 PAPER (unarmed)")
        m2.metric("Kill switch", "⛔ HALTED" if _halted else "▶ active")
        m3.metric("Agentic buying power", f"\\${_agbp:,.2f}" if _agbp is not None else "—")
        m4.metric("LLM credit left", f"\\${_led['remaining']:,.2f}"
                  if _led["balance"] > 0 else "not set")
        m5.metric("New entries (PDT)", "—" if _pdt is None else str(_pdt),
                  help="Positions the agent may still open without risking a pattern-day-trader flag: "
                       "3 day trades per 5 business days, minus trades used and positions opened today "
                       "(each reserves its same-day exit). Exits are never blocked.")

        if not _acct:
            st.warning("Agentic account not connected (USE_MCP off or not logged in). "
                           "Run scripts/mcp_login.py.")

        # --- Paper trading (simulation) ---
        st.markdown("### 📊 Paper trading — simulated, zero money at risk")
        st.caption("The agent trades a VIRTUAL account against live option prices so you can "
                       "judge the strategies before arming real money. The scheduler runs this every "
                       "cycle while unarmed.")
        try:
            from storage import paper_book as _pb
            from ingestion import options_data as _pod2
            _book = _pb.get_book()
            _marks = {}
            for _pp in _book.get("open", []):
                _q = _c_option_quote(_pp["option_id"])
                _marks[_pp["option_id"]] = float(_q.get("mark_price") or _q.get("bid_price") or _pp["entry_price"])
            _ps = _pb.summarize(_book, _marks)

            pm1, pm2, pm3, pm4 = st.columns(4)
            pm1.metric("Paper equity", f"\\${_ps['equity']:,.2f}",
                       f"{_ps['total_pnl']:+.2f} ({_ps['total_pnl_pct']:+.1f}%)")
            pm2.metric("Cash", f"\\${_ps['cash']:,.2f}")
            pm3.metric("Realized P&L", f"\\${_ps['realized']:,.2f}")
            pm4.metric("Win rate", f"{_ps['win_rate']:.0f}%" if _ps['win_rate'] is not None
                       else "—", f"{_ps['n_closed']} closed")

            _pc1, _pc2, _pc3 = st.columns([1, 1, 1])
            with _pc1:
                if st.button("▶ Run paper cycle now", use_container_width=True, key="paper_run"):
                    from alerts.agentic_options import run_paper_agent
                    st.session_state["paper_result"] = run_paper_agent(verbose=False)
                    st.rerun()
            with _pc2:
                _pstart = st.number_input("Reset with $", min_value=1.0, value=float(_book.get("start", 25.0)),
                                          step=25.0, key="paper_reset_amt")
            with _pc3:
                st.write("")
                if st.button("↺ Reset paper book", use_container_width=True, key="paper_reset"):
                    _pb.reset(_pstart)
                    st.rerun()
            if st.session_state.get("paper_result"):
                st.caption(f"Last paper cycle: {st.session_state['paper_result']}")

            if _book.get("open"):
                st.markdown("**Open (paper)**")
                st.dataframe(pd.DataFrame([{
                    "Ticker": p["ticker"], "Contract": f"{p['strike']:g}{p['right'][0].upper()} {p['expiration']}",
                    "Strategy": p["strategy"], "Qty": p["qty"], "Entry": f"${p['entry_price']:.2f}",
                    "Mark": f"${_marks.get(p['option_id'], p['entry_price']):.2f}",
                    "Unreal $": f"{_pb.position_pnl(p['entry_price'], _marks.get(p['option_id'], p['entry_price']), p['qty'])[0]:+.2f}",
                    "Unreal %": f"{_pb.position_pnl(p['entry_price'], _marks.get(p['option_id'], p['entry_price']), p['qty'])[1]:+.0f}%",
                } for p in _book["open"]]), use_container_width=True, hide_index=True)
            if _book.get("closed"):
                st.markdown("**Closed (paper)**")
                st.dataframe(pd.DataFrame([{
                    "Ticker": c["ticker"], "Contract": f"{c['strike']:g}{c['right'][0].upper()}",
                    "Entry→Exit": f"${c['entry_price']:.2f}→${c['exit_price']:.2f}",
                    "P&L $": f"{c['pnl']:+.2f}", "P&L %": f"{c['pnl_pct']:+.0f}%", "Why": c["reason"],
                } for c in reversed(_book["closed"][-20:])]), use_container_width=True, hide_index=True)
            if not _book.get("open") and not _book.get("closed"):
                st.caption("No paper trades yet — run a cycle (needs an affordable buy signal, e.g. F/NIO/SNAP).")
        except Exception as _e:  # noqa: BLE001
            st.caption(f"Paper book unavailable: {_e}")

        # --- Live candlestick chart ---
        st.markdown("### 🕯 Live chart")
        try:
            import datetime as _cdt
            _open_ticks = sorted({p["ticker"] for p in _book.get("open", [])}) if "_book" in dir() else []
            _chart_ticks = _open_ticks + [t for t in ("F", "NIO", "SNAP", "SPY") if t not in _open_ticks]
            _csel1, _csel2, _csel3 = st.columns([2, 1, 1])
            with _csel1:
                _csym = st.selectbox("Ticker", _chart_ticks, key="agent_chart_sym")
            with _csel2:
                _civ = st.selectbox("Interval", ["5minute", "10minute", "hour", "day"], key="agent_chart_iv")
            with _csel3:
                st.write("")
                _refresh = st.button("🔄 Refresh", use_container_width=True, key="agent_chart_refresh")

            _days = 2 if _civ in ("5minute", "10minute") else (10 if _civ == "hour" else 120)

            @st.cache_data(ttl=60, show_spinner=False)
            def _agent_candles(sym, interval, days):
                from ingestion import robinhood_mcp as _cm
                start = (_cdt.datetime.now(_cdt.timezone.utc) - _cdt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
                d = _cm._data(_cm._call_tool("get_equity_historicals",
                                             {"symbols": [sym], "start_time": start, "interval": interval}))
                res = (d.get("results") or []) if isinstance(d, dict) else []
                return res[0].get("bars", []) if res else []

            if _refresh:
                _agent_candles.clear()
            _bars = _agent_candles(_csym, _civ, _days)
            if _bars:
                _df = pd.DataFrame(_bars)
                # Candle times come back UTC; show them in the user's Pacific time so they line
                # up with the paper timestamps (opened_at/closed_at are naive local).
                _df["t"] = (pd.to_datetime(_df["begins_at"], utc=True)
                            .dt.tz_convert("America/Los_Angeles").dt.tz_localize(None))
                for _c in ("open_price", "high_price", "low_price", "close_price"):
                    _df[_c] = _df[_c].astype(float)
                figc = go.Figure(data=[go.Candlestick(
                    x=_df["t"], open=_df["open_price"], high=_df["high_price"],
                    low=_df["low_price"], close=_df["close_price"], name=_csym)])

                # WHERE the bot bought/sold: a ▲ (buy) / ▼ (sell) placed on the underlying's
                # price at that moment. The bot trades the option; the marker sits on the
                # underlying candle so you can see the entry/exit point on the chart.
                def _price_at(_ts):
                    try:
                        _tt = pd.to_datetime(_ts)
                    except Exception:  # noqa: BLE001
                        return None
                    _prior = _df[_df["t"] <= _tt]
                    return float(_prior.iloc[-1]["close_price"]) if len(_prior) else None

                _bx, _by, _bl = [], [], []
                for p in _book.get("open", []):
                    if p["ticker"] == _csym and p.get("opened_at"):
                        _y = _price_at(p["opened_at"])
                        if _y is not None:
                            _bx.append(pd.to_datetime(p["opened_at"])); _by.append(_y)
                            _bl.append(f"BUY {p['qty']}× {p['strike']:g}{p['right'][0].upper()} "
                                           f"{p.get('expiration','')} @ ${p['entry_price']:.2f} ({p.get('strategy','')})")
                _sx, _sy, _sl = [], [], []
                for c in _book.get("closed", []):
                    if c["ticker"] == _csym and c.get("closed_at"):
                        _y = _price_at(c["closed_at"])
                        if _y is not None:
                            _sx.append(pd.to_datetime(c["closed_at"])); _sy.append(_y)
                            _sl.append(f"SELL {c['strike']:g}{c['right'][0].upper()} · "
                                           f"{c['pnl']:+.2f} ({c['pnl_pct']:+.0f}%) · {c.get('reason','')}")
                if _bx:
                    figc.add_trace(go.Scatter(
                        x=_bx, y=_by, mode="markers", name="BUY (paper)", text=_bl,
                        marker=dict(symbol="triangle-up", color="#26a69a", size=15,
                                    line=dict(color="white", width=1.5)),
                        hovertemplate="%{text}<br>%{x|%b %d %H:%M} PT<extra></extra>"))
                if _sx:
                    figc.add_trace(go.Scatter(
                        x=_sx, y=_sy, mode="markers", name="SELL (paper)", text=_sl,
                        marker=dict(symbol="triangle-down", color="#ef5350", size=15,
                                    line=dict(color="white", width=1.5)),
                        hovertemplate="%{text}<br>%{x|%b %d %H:%M} PT<extra></extra>"))

                # REAL (LIVE) trades from the agentic account — gold, so they stand apart from
                # the paper markers. Buys from open positions, sells from filled close orders.
                _lbx, _lby, _lbl2 = [], [], []
                _lsx, _lsy, _lsl2 = [], [], []
                if _acct:
                    try:
                        for _r in _c_option_positions(_acct):
                            if (_r.get("chain_symbol") or "").upper() == _csym and _r.get("opened_at"):
                                _t = pd.to_datetime(_r["opened_at"], utc=True).tz_convert("America/Los_Angeles").tz_localize(None)
                                _y = _price_at(_t)
                                if _y is not None:
                                    _lbx.append(_t); _lby.append(_y)
                                    _lbl2.append(f"LIVE BUY {_r.get('type','')} ×{int(float(_r.get('quantity',0)))} @ ${float(_r.get('average_price',0))/100:.2f}")
                        for _o in _c_option_orders(_acct):
                            _legs = _o.get("legs") or []
                            _is_close = any((l.get("position_effect") == "close") for l in _legs)
                            if ((_o.get("chain_symbol") or "").upper() == _csym and _o.get("state") == "filled"
                                    and _is_close and (_o.get("updated_at") or _o.get("created_at"))):
                                _t = pd.to_datetime(_o.get("updated_at") or _o.get("created_at"), utc=True).tz_convert("America/Los_Angeles").tz_localize(None)
                                _y = _price_at(_t)
                                if _y is not None:
                                    _lsx.append(_t); _lsy.append(_y); _lsl2.append("LIVE SELL")
                    except Exception:  # noqa: BLE001 — real markers are best-effort
                        pass
                if _lbx:
                    figc.add_trace(go.Scatter(
                        x=_lbx, y=_lby, mode="markers", name="BUY (LIVE)", text=_lbl2,
                        marker=dict(symbol="star", color="#FFD700", size=17, line=dict(color="black", width=1)),
                        hovertemplate="%{text}<br>%{x|%b %d %H:%M} PT<extra></extra>"))
                if _lsx:
                    figc.add_trace(go.Scatter(
                        x=_lsx, y=_lsy, mode="markers", name="SELL (LIVE)", text=_lsl2,
                        marker=dict(symbol="x", color="#FFD700", size=15, line=dict(color="black", width=1)),
                        hovertemplate="%{text}<br>%{x|%b %d %H:%M} PT<extra></extra>"))

                figc.update_layout(height=440, margin=dict(l=0, r=0, t=10, b=0),
                                   xaxis_rangeslider_visible=False, showlegend=True,
                                   legend=dict(orientation="h", y=1.02, x=0))
                st.plotly_chart(figc, use_container_width=True)
                st.caption(f"{_csym} · {_civ} · times PT · ▲ teal/red = PAPER buy/sell · "
                               "★/✖ gold = **REAL (LIVE)** buy/sell. Hover for detail. Marker sits on "
                               "the underlying price at trade time. Refresh for latest (60s cache).")
            else:
                st.caption(f"No candles for {_csym} ({_civ}).")
        except Exception as _e:  # noqa: BLE001
            st.caption(f"Chart unavailable: {_e}")

        # --- LLM credit ledger (token budget halt) ---
        st.markdown("### 💳 LLM credit — token-budget halt")
        st.caption("Anthropic has no live-balance API, so this is a local ledger: set your "
                       "current console balance; Argus subtracts each Claude call's cost and "
                       "**halts new agent entries + chat when the remainder hits the reserve** "
                       "(default \\$0.50), leaving leeway to top up.")
        lc1, lc2, lc3 = st.columns([2, 1, 1])
        with lc1:
            _newbal = st.number_input("Set balance from console ($)", min_value=0.0,
                                      value=float(_led["balance"]), step=1.0, key="agent_setbal")
        with lc2:
            _newres = st.number_input("Reserve ($)", min_value=0.0,
                                      value=float(_led["reserve"]), step=0.25, key="agent_setres")
        with lc3:
            st.write("")
            if st.button("💾 Update ledger", use_container_width=True):
                _lb.set_balance(_newbal, _newres)
                st.rerun()
        if _led["balance"] > 0:
            st.caption(f"Spent since set: \\${_led['spent']:.4f} · remaining "
                           f"\\${_led['remaining']:.2f} · reserve \\${_led['reserve']:.2f}")
            if _led["remaining"] <= _led["reserve"]:
                st.error("⛔ Credit at/under reserve — new agent entries + chat are halted. "
                             "Top up, then update the balance above.")

        # --- controls ---
        st.markdown("### 🎛 Controls")
        k1, k2 = st.columns(2)
        with k1:
            if _halted:
                if st.button("▶ Remove kill switch (resume)", use_container_width=True):
                    try:
                        os.remove(_HALT_FLAG)
                    except OSError:
                        pass
                    st.rerun()
            else:
                if st.button("⛔ HALT agent (kill switch)", use_container_width=True, type="primary"):
                    with open(_HALT_FLAG, "w", encoding="utf-8") as _f:
                        _f.write("halted from dashboard\n")
                    st.rerun()
        with k2:
            _stocks_on = bool(getattr(_cfg, "AGENT_TRADE_STOCKS", False))
            _prev_stocks = st.checkbox("Include the stock leg in the preview", value=_stocks_on,
                                       key="agent_preview_stocks",
                                       help="Dry-run only. The LIVE cycle follows config.AGENT_TRADE_STOCKS "
                                            f"(currently {'ON' if _stocks_on else 'OFF'}).")
            if st.button("🔮 Preview cycle (dry run — places nothing)", use_container_width=True,
                         disabled=not _acct):
                _old = _cfg.DRY_RUN
                _cfg.DRY_RUN = True
                try:
                    st.session_state["agent_preview"] = run_options_agent(verbose=False,
                                                                          stocks=_prev_stocks)
                finally:
                    _cfg.DRY_RUN = _old
                st.rerun()

        # LIVE run — explicit, guarded, scoped (never flips DRY_RUN process-wide).
        with st.expander("🔴 Run a LIVE cycle (places REAL orders)"):
            st.warning("This places real orders on the agentic account with real money — options, plus "
                           f"shares when the stock leg is ON (it's {'ON' if _stocks_on else 'OFF'}). "
                           "Uncapped option size. Only runs during market hours.")
            _confirm = st.checkbox("I understand — trade real money now", key="agent_live_confirm")
            if st.button("Execute LIVE cycle", disabled=not (_confirm and _acct and not _halted)):
                _old = _cfg.DRY_RUN
                _cfg.DRY_RUN = False
                try:
                    st.session_state["agent_live_result"] = run_options_agent(verbose=False)
                finally:
                    _cfg.DRY_RUN = _old
                st.rerun()

        if st.session_state.get("agent_preview"):
            st.markdown("#### 🔮 Preview result (dry run)")
            st.json(st.session_state["agent_preview"])
        if st.session_state.get("agent_live_result"):
            st.markdown("#### 🔴 Last LIVE cycle result")
            st.json(st.session_state["agent_live_result"])

        # --- decision log (plan J0): every candidate/exit the agent considered + why ---
        with st.expander("🧾 Decision log — what the agent considered and why"):
            from storage import decision_log as _dl
            _only_live = st.checkbox("Live cycles only (hide dry previews)", value=True, key="agent_dlog_live")
            _recs = _dl.read(limit=60, mode="live" if _only_live else None)
            if _recs:
                st.dataframe(pd.DataFrame([{
                    "Time": r.get("ts", "").replace("T", " "), "Mode": r.get("mode"),
                    "Kind": r.get("kind"), "Ticker": r.get("ticker") or "—",
                    "Action": r.get("action"), "Why": r.get("reason"),
                    "Conv": (r.get("inputs") or {}).get("conviction"),
                    # Entry verdicts carry `decision` (+ size); holding reviews (J3) carry `action`.
                    "Judge (shadow)": (
                        (r["judge"].get("decision") or r["judge"].get("action") or "?")
                        + (f" ×{r['judge']['size_multiplier']:g}" if r["judge"].get("decision") == "enter" else "")
                        + (f" @ ${r['judge']['new_stop_price']:g}" if r["judge"].get("new_stop_price") else "")
                        if r.get("judge") else "—"),
                    "Judge why": ((r.get("judge") or {}).get("thesis")
                                  or (r.get("judge") or {}).get("reasoning")
                                  or (r.get("judge") or {}).get("error") or ""),
                    "P&L %": r.get("pnl_pct"),
                } for r in reversed(_recs)]), use_container_width=True, hide_index=True)
                st.caption("Judge = the Sonnet entry review (plan J2), SHADOW mode: logged only — the rules "
                           "still decide every trade until the J4 review shows the judge helps.")
            else:
                st.caption("No decisions logged yet — they appear after the agent's next cycle with a signal.")

        # --- judge scorecard (plan J4): does the shadow judge beat the rules? ---
        with st.expander("⚖ Judge scorecard — is the shadow judge worth letting it decide?"):
            st.caption("Scores every shadow verdict against what the market did next (1 and 5 trading days), "
                       "plus realized P&L of trades the rules took, split by the judge's call. Nothing is "
                       "concluded below 15 samples per group. Needs price history — computed on click.")
            if st.button("Compute scorecard", key="agent_judge_scorecard"):
                from analysis.judge_scorecard import scorecard as _scorecard
                with st.spinner("Scoring verdicts against price history..."):
                    st.session_state["judge_scorecard"] = _scorecard()
            _sc = st.session_state.get("judge_scorecard")
            if _sc:
                (st.success if _sc["conclusion"].get("helps") else st.info)(_sc["conclusion"]["text"])
                _rows = []
                for _g, _label in (("enter", "Judge: ENTER"), ("not_enter", "Judge: WAIT/SKIP")):
                    for _h, _s in _sc["entries"][_g].items():
                        _rows.append({"Group": _label, "Horizon": f"{_h}d", "n": _s["n"],
                                      "Avg return %": _s.get("avg"), "Win %": _s.get("win_rate")})
                st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
                _t = _sc["taken"]
                st.caption(f"Trades the rules took — judge said enter: n={_t['judge_enter']['n']} "
                           f"avg {_t['judge_enter'].get('avg', '—')}% | said wait/skip: "
                           f"n={_t['judge_wait_or_skip']['n']} avg {_t['judge_wait_or_skip'].get('avg', '—')}%. "
                           f"Holding reviews (1d after) — "
                           + " · ".join(f"{k}: n={v['n']} avg {v.get('avg', '—')}%" for k, v in _sc["reviews"].items())
                           + f". Judge calls {_sc['judge_calls']}, cost \\${_sc['judge_cost_usd']:.2f}.")
                st.caption(_sc["caveat"])

        # --- agentic equity positions + sync/refresh ---
        st.markdown("### 📈 Agentic positions")
        if st.button("🔄 Sync positions", use_container_width=True, disabled=not _acct,
                     key="agent_sync_positions"):
            st.rerun()
        _eq = []
        if _acct:
            try:
                _eq = _c_agentic_equity(_acct)   # agentic-account equity holdings
            except Exception as _e:  # noqa: BLE001
                st.caption(f"Could not read agentic equity positions: {_e}")
        if _eq:
            from alerts import agentic_stocks as _astk
            from storage import agent_stock_book as _abook
            from storage.peak_tracker import get_peak as _get_peak
            _plans = _abook.all_entries()
            for _sp in _eq:
                _t = (_sp.get("ticker") or "").upper()
                _plan = _plans.get(_t)
                try:
                    if _plan:
                        _sact, _swhy = _astk.decide_exit(_sp, _plan, _get_peak(f"eq:{_t}"))
                        _agent = (f"agent-managed · plan *{_plan.get('exit_condition') or 'default'}* · "
                                  f"opened {_plan.get('opened')} · agent: **{_sact}** ({_swhy})")
                    else:
                        _agent = "not agent-managed (bought by hand — the agent never touches it)"
                    sc1, sc2 = st.columns([4, 1])
                    with sc1:
                        st.markdown(
                            f"**{_t}** {_sp['shares']:g} sh · avg \\${_sp['avg_cost']:.2f} → "
                            f"\\${_sp['current_price']:.2f} (**{_sp['pnl_pct']:+.1f}%**, "
                            f"\\${_sp['equity']:.2f}) · {_agent}")
                    with sc2:
                        if st.button("Sell now", key=f"agent_sell_{_t}", use_container_width=True):
                            _old = _cfg.DRY_RUN
                            _cfg.DRY_RUN = False   # a manual override click IS the confirmation
                            try:
                                _r = _astk.close_position(_amcp, _acct, _sp, _agbp or 0,
                                                          "manual override sell (dashboard)")
                            finally:
                                _cfg.DRY_RUN = _old
                            st.success(f"Sell {_r['status']}: {_r.get('detail') or _r.get('reason')}")
                            _c_agentic_equity.clear()
                            st.rerun()
                except Exception as _e:  # noqa: BLE001 — one bad row must not break the tab
                    st.caption(f"{_t}: render error — {_e}")
            st.caption("Share exits: the agent re-checks every cycle; the whole-share part of an agent "
                       "position also rests on a GTC stop at its plan's stop (protects while the PC is off).")
        else:
            st.caption("No agentic equity positions.")

        # --- observe open option positions + per-position override ---
        st.markdown("### 📂 Open option positions (agentic) — override exits")
        _positions = []
        if _acct:
            try:
                _positions = _c_option_positions(_acct)
            except Exception as _e:  # noqa: BLE001
                st.caption(f"Could not read option positions: {_e}")
        if not _positions:
            st.caption("No open option positions on the agentic account.")
        for _p in _positions:
            try:
                _oid = _p.get("option_id") or _p.get("option") or _p.get("id")
                _qty = int(float(_p.get("quantity") or 0))
                if not _oid or _qty < 1:
                    continue
                _entry = float(_p.get("average_open_price") or _p.get("average_price") or 0)
                if _entry > 5:
                    _entry /= 100.0
                _q = _c_option_quote(_oid)
                _mark = float(_q.get("mark_price") or _q.get("bid_price") or 0)
                _exp = _p.get("expiration_date") or _p.get("expiration")
                _dte = _aod._dte(_exp) if _exp else None
                from storage.peak_tracker import get_peak as _get_peak
                _act, _why = option_exit_decision(_entry, _mark, _dte, DEFAULT_EXIT,
                                                  peak_mark=_get_peak(_oid))
                _pnl = ((_mark - _entry) / _entry * 100) if _entry else 0.0
                _sym = _p.get("chain_symbol") or _p.get("symbol") or "?"
                _rt = (_p.get("type") or "call").lower()
                cc1, cc2 = st.columns([4, 1])
                with cc1:
                    st.markdown(
                        f"**{_sym} {_rt.upper()}** ×{_qty} · exp {_exp} (DTE {_dte}) · "
                            f"entry \\${_entry:.2f} → mark \\${_mark:.2f} "
                            f"(**{_pnl:+.0f}%**) · agent: **{_act}** ({_why})")
                with cc2:
                    if st.button("Close now", key=f"agent_close_{_oid}", use_container_width=True):
                        _intent = OptionOrderIntent(
                            underlying=_sym, option_id=_oid, right=_rt, side="sell",
                            position_effect="close", quantity=_qty,
                            price=round(float(_q.get("bid_price") or _mark), 2),
                            direction="credit", expiration=_exp,
                            reason="manual override close (dashboard)",
                            client_id=f"uiclose-{_oid}")
                        _old = _cfg.DRY_RUN
                        _cfg.DRY_RUN = False   # a manual override click IS the confirmation
                        try:
                            _res = _amcp.place_option_order(
                                _intent, GuardState(start_equity=_agbp or 0),
                                buying_power=_agbp or 0, account_number=_acct)
                        finally:
                            _cfg.DRY_RUN = _old
                        st.success(f"Close {_res['status']}: {_res['reason']}")
                        st.rerun()
            except Exception as _e:  # noqa: BLE001 — one bad row must not break the tab
                st.caption(f"position render error: {_e}")

        st.caption("Option exits are POLL-based: the agent re-checks each cycle (not a resting stop). "
                       "Timeliness depends on how often the cycle runs.")
