"""
🧭 Strategy — the agent's systematic strategy, shown so the user can decide whether to copy it (overhaul Phase 3).

Three parts, top to bottom:
  1. What the agent holds now and WHY: the rule's live signal (QQQ close vs its 200-day average, distance to a
     switch), last + next rebalance, and the agent account's value since the strategy started vs SPY.
  2. The rule's model performance vs SPY (last 5 years) — the backtest engine on real prices. A model, not fills.
  3. "Copy these trades": the orders that make a chosen dollar amount of the MAIN account mirror the strategy (same
     planner as the agent). Argus can't trade the main account — the user places them.
Escape every "$" in st.markdown/caption ("\\$") — paired dollars render as LaTeX.
"""
from contextlib import nullcontext
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.common import (_c_agentic_bp, _c_agentic_equity, _c_main_positions, _c_strategy_history,
                              _cached_prices, _live_buying_power)

_NAMES = {"qqq_trend": "QQQ trend filter — hold QQQ while it's above its 200-day average, else T-bills (BIL)"}


def _money(v) -> str:
    return "—" if v is None else f"\\${v:,.2f}"


def _close_on_or_before(rows, day):
    vals = [c for d, c in rows if d <= day]
    return vals[-1] if vals else None


def render() -> None:
    import agent_mode
    import config
    from storage import decision_log
    from strategy import rebalancer as rb
    from strategy.rules import STRATEGIES

    name = getattr(config, "AGENT_STRATEGY", None)
    st.subheader("🧭 Agent strategy")
    if name not in STRATEGIES:
        st.info("No systematic strategy is configured (config.AGENT_STRATEGY) — the agent runs the old pick-based mode.")
        return
    st.caption(f"**{_NAMES.get(name, name)}.** Rebalanced once a month. Chosen by backtest (2005–2026): better than "
               "SPY risk-adjusted in both test periods, about half SPY's worst crash. No AI and no news involved.")

    _, tickers = STRATEGIES[name]
    today = date.today()
    prices = _c_strategy_history(tuple(sorted(set(tickers) | {"SPY"})), today.isoformat())
    if not prices.get(tickers[0]):
        st.error("Couldn't load price history (yfinance). Try again in a minute.")
        return
    target, signal = rb.target_weights(name, prices, today)
    mode = agent_mode.get_mode()

    # --- 1. holding now + why -------------------------------------------------------------------------------
    st.markdown("### Holding now — and why")
    q = signal.get(tickers[0]) or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("Target", ", ".join(f"{t} {w:.0%}" for t, w in target.items()) or "cash")
    if q:
        gap = (q["close"] / q["sma200"] - 1) * 100
        c2.metric(f"{tickers[0]} close vs 200-day avg", _money(q["close"]), f"{gap:+.1f}% vs {_money(q['sma200'])}")
        c3.metric("Switch point", _money(q["sma200"]),
                  help="The rule flips to T-bills if a month starts with the close below this average (and back "
                       "to QQQ when it's above).")
        st.caption(f"Signal as of the {signal.get('asof')} close. "
                   + (f"QQQ would have to fall about {gap:.1f}% to go to T-bills." if gap > 0 else
                      f"QQQ would have to rise about {-gap:.1f}% to go back in."))

    state_mode = "paper" if mode != "live" else "live"
    records = [r for r in decision_log.read(mode=state_mode) if r.get("kind") == "rebalance"
               and r.get("key") == f"strategy:{name}"]
    with agent_mode.paper_scope() if state_mode == "paper" else nullcontext():
        state = rb._load()
    nxt = rb.next_rebalance_date(state, today)
    last = records[-1] if records else None
    r1, r2 = st.columns(2)
    r1.metric("Last rebalance", last["ts"][:10] if last else "not yet",
              help=("Orders: " + "; ".join(f"{o['side']} {o['ticker']} → {o['status']}" for o in last.get("orders", []))
                    if last else None))
    r2.metric("Next rebalance", nxt.isoformat() if nxt > today else "next market-hours cycle")
    if mode == "off":
        st.warning("Agent mode is Off — no rebalances happen until it's set to Paper or Live (🤖 Agent tab).")

    # --- the agent account since the strategy started --------------------------------------------------------
    st.markdown(f"### The agent's account ({'paper' if state_mode == 'paper' else 'live'})")
    held, cash = _agent_holdings(state_mode, tickers)
    value = cash + sum(h["shares"] * h["price"] for h in held.values())
    first = records[0] if records else None
    a1, a2, a3 = st.columns(3)
    a1.metric("Value", _money(value), help="Cash + the strategy's holdings (positions left from the old agent excluded).")
    if first:
        start_day = date.fromisoformat(first["ts"][:10])
        start_eq = (first.get("inputs") or {}).get("equity") or 0
        spy0, spy1 = _close_on_or_before(prices["SPY"], start_day - timedelta(days=1)), prices["SPY"][-1][1]
        mine = (value / start_eq - 1) * 100 if start_eq else None
        spy = (spy1 / spy0 - 1) * 100 if spy0 else None
        a2.metric(f"Since {start_day}", "—" if mine is None else f"{mine:+.2f}%")
        a3.metric("SPY same period", "—" if spy is None else f"{spy:+.2f}%")
    else:
        a2.metric("Since start", "starts at the first rebalance")
    if held:
        st.dataframe(pd.DataFrame([{"Ticker": t, "Shares": round(h["shares"], 6), "Price": round(h["price"], 2),
                                    "Value": round(h["shares"] * h["price"], 2)} for t, h in held.items()]),
                     hide_index=True, use_container_width=True)

    # --- 2. model performance -----------------------------------------------------------------------------
    st.markdown("### Model performance vs SPY — last 5 years")
    _model_chart(name, prices, today)

    # --- 3. copy these trades -----------------------------------------------------------------------------
    st.markdown("### 🧾 Copy these trades — your main account")
    st.caption("What to place in Robinhood so this amount of your main account mirrors the strategy. Argus can't "
               "trade your main account — you place them. Market orders in regular hours; rebalance again when the "
               "agent does (once a month).")
    try:
        main_pos = _c_main_positions() or []
    except Exception:  # noqa: BLE001
        main_pos = []
    mine_held = {p["ticker"]: {"shares": p.get("shares"), "price": p.get("current_price")}
                 for p in main_pos if p.get("ticker") in tickers and p.get("shares")}
    held_val = float(sum(h["shares"] * h["price"] for h in mine_held.values() if h["price"]))   # empty sum is int 0

    bp = _live_buying_power()
    # Default = what already follows the strategy (QQQ/BIL held) — never pre-fill the whole buying power.
    budget = st.number_input("Amount to follow (\\$)", min_value=0.0, value=round(held_val, 2), step=10.0,
                             help="Includes any QQQ/BIL you already hold in the main account. "
                                  f"Main buying power: {_money(bp)}.")
    orders = rb.copy_orders(target, mine_held, budget)
    if budget <= 0:
        st.info(f"Enter how much of your main account should follow the strategy (buying power {_money(bp)}).")
    elif not orders:
        st.success("Your main account already matches the strategy for this amount — nothing to do.")
    else:
        st.dataframe(pd.DataFrame([{
            "Action": o["side"].upper(), "Ticker": o["ticker"],
            "Amount": (f"${o['dollars']:,.2f}" if o["side"] == "buy" else
                       f"{o['quantity']:g} shares" + (" (all)" if o["quantity"] == (mine_held.get(o["ticker"]) or {}).get("shares") else "")),
            "Order": "market, dollar amount" if o["side"] == "buy" else "market, by shares"}
            for o in orders]), hide_index=True, use_container_width=True)
        if bp is not None and budget - held_val > bp + 0.01:
            st.warning(f"This needs \\${budget - held_val:,.2f} of new cash but buying power is {_money(bp)}.")


def _agent_holdings(state_mode: str, tickers) -> tuple[dict, float]:
    """({ticker: {shares, price}} for the rule's tickers, cash) in the agent's paper book or live account."""
    if state_mode == "paper":
        from storage import paper_book
        book = paper_book.get_book()
        shares = {t: h["shares"] for t, h in book["shares"].items() if t in tickers}
        quotes = _cached_prices(tuple(sorted(shares))) if shares else {}
        return ({t: {"shares": s, "price": (quotes.get(t) or {}).get("price") or 0.0} for t, s in shares.items()},
                book["cash"])
    try:
        from ingestion import robinhood_mcp as m
        acct = m.agentic_account_number() if m.is_available() else None
    except Exception:  # noqa: BLE001
        acct = None
    if not acct:
        return {}, 0.0
    pos = _c_agentic_equity(acct) or []
    return ({p["ticker"]: {"shares": p["shares"], "price": p["current_price"]} for p in pos if p["ticker"] in tickers},
            _c_agentic_bp(acct) or 0.0)


def _model_chart(name: str, prices: dict, today: date) -> None:
    from backtest.strategy_backtest import metrics, run
    from strategy.rules import STRATEGIES, buy_hold
    start = today - timedelta(days=5 * 365)
    model = run(prices, STRATEGIES[name][0](), start, today)
    spy = run(prices, buy_hold("SPY"), start, today)
    if not model["curve"]:
        st.caption("Not enough price history for the model chart.")
        return
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[d for d, _ in model["curve"]], y=[v * 100 for _, v in model["curve"]],
                             name="Strategy (model)", line={"width": 2}))
    fig.add_trace(go.Scatter(x=[d for d, _ in spy["curve"]], y=[v * 100 for _, v in spy["curve"]],
                             name="SPY buy & hold", line={"width": 1.5, "dash": "dot"}))
    fig.update_layout(height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10}, yaxis_title="Growth of $100",
                      legend={"orientation": "h", "y": 1.08})
    st.plotly_chart(fig, use_container_width=True)
    m, s = metrics(model, spy), metrics(spy)
    st.dataframe(pd.DataFrame([
        {"": "Strategy (model)", "Return/yr %": m.get("cagr_pct"), "Worst drop %": m.get("max_dd_pct"),
         "Sharpe": m.get("sharpe"), "Switches/yr": m.get("trades_per_yr")},
        {"": "SPY", "Return/yr %": s.get("cagr_pct"), "Worst drop %": s.get("max_dd_pct"),
         "Sharpe": s.get("sharpe"), "Switches/yr": None}]), hide_index=True, use_container_width=True)
    st.caption("Model = the same rule on daily adjusted closes, 5 bp cost per trade, rebalanced at the 1st trading "
               "day's close. Past results — the rule isn't guaranteed to beat SPY going forward.")
