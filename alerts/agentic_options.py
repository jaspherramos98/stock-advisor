"""
Autonomous options agent (Phase 2) — the pilot's aggressive options trader.

Each run:
  ENTRIES — take Argus's directional signals (pipeline_cache), match each to an options strategy
            (analysis.options_strategies), resolve a concrete contract (ingestion.options_data),
            size it, and place a buy-to-open via robinhood_mcp.place_option_order.
  EXITS   — for each open agentic option position, apply the exit policy (profit target / stop /
            near-expiry) and place a sell-to-close when it fires.

SAFETY (engineering, NOT limits on aggression):
  - config.DRY_RUN True → logs intended option orders, places nothing.
  - review_option_order before every place (in place_option_order); is_error honored.
  - Kill switch: a file `agentic_halt.flag` in the repo root → the loop halts immediately.
  - Market-hours gated for live placement (options fill in regular hours).
  - trading_guards vets each order (idempotency, runaway day-cap, premium ≤ buying power).
Position SIZE is uncapped (pilot) — up to the strategy's allocation of buying power.

AGENTIC ACCOUNT ONLY. Requires option_level_2 (approved 2026-08-14). Reads/orders never touch main.
"""
from __future__ import annotations

import json
import os

import config
from trading_guards import GuardState, OptionOrderIntent

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HALT_FLAG = os.path.join(_REPO, "agentic_halt.flag")
_CACHE = os.path.join(_REPO, "pipeline_cache.json")


def _halted() -> bool:
    return os.path.exists(_HALT_FLAG)


def _market_open() -> bool:
    try:
        import market_hours as mh
        return mh.market_session()["status"] in ("open", "open_half_day")
    except Exception:  # noqa: BLE001 — if the calendar fails, let RH be the gate
        return True


def _risk_bucket(regime_text: str | None) -> str:
    """'risk-on (favorable for longs)' → 'risk_on'; 'risk-off (defensive)' → 'risk_off'; else 'neutral'.
    (The old mapping kept the parenthetical — 'risk_on (favorable for longs)' — so the strategies'
    == 'risk_on' check never matched and short_dte_momentum could never fire.)"""
    t = (regime_text or "").lower()
    return "risk_on" if t.startswith("risk-on") else "risk_off" if t.startswith("risk-off") else "neutral"


def _regime() -> dict:
    """{'risk': bucket for the strategies, 'detail': the full fetch_market_regime dict (J1 briefing)}."""
    try:
        from ingestion.prices import fetch_market_regime
        r = fetch_market_regime() or {}
        return {"risk": _risk_bucket(r.get("regime")), "detail": r}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_options: regime lookup failed — {e}")
        return {"risk": "neutral", "detail": {}}


# Argus chat is the sharper, more-decisive brain (it upgrades pipeline 'watch' ideas to real
# buys — e.g. it called RDDT's S&P-inclusion catalyst a Buy while the pipeline left it a watch).
# Chat picks carry no numeric conviction, so a chat "Buy" is treated as this conviction — high
# enough to clear catalyst_momentum (≥70) but below the short_dte/pre-earnings bars (75-80).
CHAT_BUY_CONVICTION = 72

# When True, after pipeline + chat signals, scan a preset of affordable cheap-underlying names
# for a technical lean (ingestion.affordable_scout) so the agent isn't idle when every pipeline
# idea is too expensive for the pilot. Ranked LAST → only fills leftover budget.
# OFF by default: RSI-extreme technical gambles have no proven edge and just churned premium/theta/
# spread on the pilot. The agent now trades ONLY real catalyst signals (pipeline + chat); it stays
# idle when nothing affordable qualifies — idle beats -EV noise. Flip True to re-enable the scout.
SCOUT_AFFORDABLE = False


def _chat_direction(sugg: dict) -> str | None:
    """Resolve an entry direction from a chat suggestion's verb + text.
      buy   → 'buy' (agent buys calls)      short → 'short' (agent buys puts)
    A 'buy' whose text mentions short/bearish is treated as SHORT (fixes chat lines like
    'Buy — TLT (short)' that flipped a bearish call into a bought call). 'sell'/'watch'/'hold'
    are NOT new entries (sell = exit an existing long) → return None."""
    action = (sugg.get("action") or "").lower()
    text = (sugg.get("trigger_text") or "").lower()
    if action == "short" or (action == "buy" and ("short" in text or "bearish" in text)):
        return "short"
    if action == "buy":
        return "buy"
    return None  # sell (exit) / watch / hold → not an agent entry


def _is_today(stamp: str | None) -> bool:
    """True when an ISO date/datetime string is from today (local date — the same clock the pipeline
    and chat use when they stamp it). Missing/garbled → False (fail closed: no trade on unknown age)."""
    from datetime import date
    return bool(stamp) and str(stamp)[:10] == date.today().isoformat()


def _signals() -> list[dict]:
    """Directional entry signals for the agent, deduped by ticker — TODAY's only:
      1. buy/short recommendations from the pipeline cache (real conviction), then
      2. Argus chat's last 'Buy' suggestions (get_chat_suggestions) for tickers the pipeline
         didn't already flag buy/short — this is choice B: let the chat's calls reach the agent.
    A cache or chat suggestion from an earlier day is ignored: the pipeline only overwrites the cache
    when it runs, so without this a Friday recommendation would still open positions on Monday
    (the dashboard already applies the same today-only rule to the cache)."""
    out, seen = [], set()
    try:
        with open(_CACHE, encoding="utf-8") as f:
            cache = json.load(f) or {}
        recs = (cache.get("recommendations", []) or []) if _is_today(cache.get("date")) else []
    except (OSError, ValueError):
        recs = []
    for r in recs:
        t = (r.get("ticker") or "").upper()
        if t and t not in seen and (r.get("direction") or "").lower() in ("buy", "short"):
            seen.add(t)
            out.append(r)

    try:
        from storage.entry_watch import get_chat_suggestions
        for s in get_chat_suggestions():
            if not _is_today(s.get("created_at")):
                continue
            t = (s.get("ticker") or "").upper()
            direction = _chat_direction(s)
            if t and t not in seen and direction:
                seen.add(t)
                out.append({"ticker": t, "direction": direction,
                            "conviction": CHAT_BUY_CONVICTION, "source": "chat"})
    except Exception as e:  # noqa: BLE001 — chat merge is additive; never break pipeline signals
        print(f"agentic_options: chat-suggestion merge failed — {e}")

    # 3. Affordable-universe scout (last resort so the pilot isn't idle on unaffordable pipelines).
    if SCOUT_AFFORDABLE:
        try:
            from ingestion.affordable_scout import scout_signals
            for s in scout_signals():
                t = (s.get("ticker") or "").upper()
                if t and t not in seen:
                    seen.add(t)
                    out.append(s)
        except Exception as e:  # noqa: BLE001
            print(f"agentic_options: affordable scout failed — {e}")
    return out


def _held_underlyings(mcp, acct) -> set[str]:
    """Underlyings we already hold an open option position on — don't stack a second entry."""
    try:
        from ingestion import mcp_auth
        data = mcp._data(mcp._unwrap_tool_result(
            mcp_auth.call_tool("get_option_positions", {"account_number": acct, "nonzero": True})))
        rows = data.get("positions", []) if isinstance(data, dict) else []
        return {(p.get("chain_symbol") or p.get("symbol") or "").upper() for p in rows if p}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_options: option positions read failed — {e}")
        return set()


def _closed_underlyings(exits: list[dict], live: bool) -> set[str]:
    """Underlyings actually closed this cycle → excluded from re-entry (anti-churn). Live closes
    only count when the order was placed/dry_run; paper closes always execute."""
    ok = (lambda r: r.get("status") in ("placed", "dry_run")) if live else (lambda r: True)
    return {(r.get("close") or "").upper() for r in exits if r.get("close") and ok(r)}


def _entry_budget(mcp, acct, equity: float, verbose: bool) -> int | None:
    """PDT budget = how many new positions this cycle may open (None = unlimited). Fails CLOSED:
    if the fill history can't be read we can't prove a same-day exit won't flag the account, so
    no entries this cycle (exits are unaffected)."""
    try:
        budget = mcp.day_trade_budget(acct, equity)
    except Exception as e:  # noqa: BLE001
        print(f"agentic_options: day-trade count unavailable — {e}; entries blocked this cycle")
        return 0
    if verbose:
        print(f"  PDT: {'exempt' if budget is None else f'{budget} new position(s) allowed'}")
    return budget


def _try_option_entry(mcp, acct, sig: dict, regime: dict, avail: float, state,
                      verbose: bool) -> tuple[dict | None, float]:
    """Options leg for one signal: strategy → affordable contract → sized buy-to-open.
    Returns (result row, premium committed) or (None, 0) when no strategy/contract fits."""
    from ingestion import options_data as od
    from ingestion import account_reads as ar
    from analysis.options_strategies import applicable_plans, size_contracts
    from ingestion.signal_context import enrich

    ticker = (sig.get("ticker") or "").upper()
    sig = enrich(sig)   # add rsi + earnings context for the technical/earnings strategies
    plans = applicable_plans(sig, regime)
    if not plans:
        return None, 0.0
    spot = (ar.quotes([ticker]).get(ticker) or {}).get("price")
    if not spot:
        return None, 0.0
    # Try strategies in priority order until one yields a tradable/affordable contract.
    plan, contract = None, None
    for cand in plans:
        c = od.select_contract(ticker, spot, cand["right"], cand["dte_min"],
                               cand["dte_max"], cand["otm_pct"], max_premium=avail)
        if c:
            plan, contract = cand, c
            break
    if not contract:
        if verbose:
            print(f"  {ticker}: no affordable/liquid contract for any strategy")
        return None, 0.0
    n = size_contracts(plan["alloc_pct"], avail, contract["cost_1x"])
    if n < 1:
        return None, 0.0
    price = round(contract["ask"], 2)
    intent = OptionOrderIntent(
        underlying=ticker, option_id=contract["instrument_id"], right=plan["right"],
        side="buy", position_effect="open", quantity=n, price=price,
        strike=contract["strike"], expiration=contract["expiration"],
        reason=f"{plan['strategy']} (conv {sig.get('conviction')})",
        client_id=f"opt-{ticker}-{contract['instrument_id']}-{contract['expiration']}",
    )
    res = mcp.place_option_order(intent, state, buying_power=avail, account_number=acct)
    row = {"ticker": ticker, "leg": "option", "strategy": plan["strategy"], "qty": n,
           "contract": f"{contract['strike']}{plan['right'][0].upper()} {contract['expiration']}",
           "option_id": contract["instrument_id"], "premium": intent.premium,
           **{k: res[k] for k in ("status", "reason")}}
    return row, (intent.premium if res["status"] in ("placed", "dry_run") else 0.0)


def _run_entries(mcp, acct, buying_power, verbose, exclude=None, max_new: int | None = None,
                 stocks: bool | None = None) -> list[dict]:
    """Entries across both legs, best idea first, from ONE shared buying-power pool. Each signal is
    routed (agentic_stocks.route) to options or shares; an options-routed buy with no affordable
    contract falls back to shares when the stock leg is on."""
    from alerts import agentic_stocks as stk

    regime = _regime()
    use_stocks = stk.stocks_enabled(stocks)
    base = _signals()
    # J2b: today's watches / pins whose "buy when" trigger has fired join as shares-only candidates.
    try:
        from alerts.agentic_watch import watch_signals
        base += watch_signals(exclude={(s.get("ticker") or "").upper() for s in base}, verbose=verbose)
    except Exception as e:  # noqa: BLE001 — watch triggers are additive; never block real signals
        print(f"agentic_options: watch-trigger scan failed — {e}")
    signals = stk.rank(base)
    # `exclude` = underlyings we closed THIS cycle. Exits run before entries, so a just-sold
    # position no longer shows as held — without this the same signal would re-buy it the same
    # cycle, paying the round-trip spread and undoing the exit we just took (the churn bug).
    held = _held_underlyings(mcp, acct) | (exclude or set())
    stock_dollars, stock_left = {}, 0.0
    if use_stocks:
        from storage.agent_stock_book import invested
        held |= stk.held_tickers(mcp, acct)
        # config.AGENT_STOCK_BUDGET_CAP limits the TOTAL entry cost the agent holds in shares. The
        # pyramid sizes on the smaller of buying power and the cap, so a $20 cap still spreads
        # across several names (40% single-name cap → ≥3 positions to deploy it all).
        cap = stk.budget_cap()
        stock_left = stk.stock_room(invested(), cap)
        stock_dollars = stk.size_buys(signals, min(buying_power, cap) if cap is not None else buying_power)
    state = GuardState(start_equity=buying_power)
    avail = buying_power
    results: list[dict] = []

    from storage import decision_log as dlog

    def log(sig, action, reason, **extra):
        # One record per candidate considered — the J0 measuring stick (storage/decision_log.py).
        dlog.record("entry", sig.get("ticker"), action, reason, inputs={
            **dlog.signal_inputs(sig), "buying_power": round(avail, 2),
            "stock_room": None if stock_left == float("inf") else round(stock_left, 2),
            "regime": regime.get("risk"), "stocks_enabled": use_stocks}, **extra)

    def briefing(sig) -> dict | None:
        # J1: the fresh per-candidate context the J2 judge will read; logged with the decision now
        # so the shadow review can see what was knowable at that moment. Never blocks an entry.
        try:
            from analysis.agent_context import gather
            return gather(sig, regime=regime.get("detail"), holdings=set(held))
        except Exception as e:  # noqa: BLE001
            print(f"agentic_options: briefing failed for {sig.get('ticker')} — {e}")
            return None

    def shadow_judge(ctx, leg, ticker) -> dict | None:
        try:
            from analysis.agent_judge import judge_entry
            verdict = judge_entry(ctx, leg, stock_dollars.get(ticker))
            if verdict and verbose:
                print(f"  judge {ticker}: {verdict['decision']} ×{verdict.get('size_multiplier')} — "
                      f"{verdict.get('thesis') or verdict.get('error', '')}")
            return verdict
        except Exception as e:  # noqa: BLE001 — the judge must never break a cycle
            print(f"agentic_options: judge failed for {ticker} — {e}")
            return None

    for i, sig in enumerate(signals):
        ticker = (sig.get("ticker") or "").upper()
        leg = stk.route(sig, use_stocks)
        if not ticker:
            continue
        if ticker in held:
            log(sig, "skip", "already held (or closed this cycle)")
            continue
        if leg is None:
            ctx = verdict = None
            if sig.get("shares_only"):
                # A fired watch the rules can't take (stock leg off): still brief + judge it, so the
                # shadow record shows what the judge would have done with watch triggers.
                ctx = briefing(sig)
                verdict = shadow_judge(ctx, "stock", ticker)
            log(sig, "skip", "not tradeable by the agent (e.g. crypto, or shares-only with the stock leg off)",
                context=ctx, judge=verdict)
            continue
        # PDT: each open position reserves a same-day exit. Out of budget → stop opening (the
        # remaining, lower-ranked signals would only be skipped one by one anyway).
        if max_new is not None and max_new <= 0:
            if verbose:
                print(f"  PDT day-trade budget exhausted — no more entries this cycle (next: {ticker})")
            results.append({"ticker": ticker, "status": "skipped", "reason": "PDT day-trade budget exhausted"})
            for rest in signals[i:]:
                if (rest.get("ticker") or "").upper() not in held:
                    log(rest, "skip", "PDT day-trade budget exhausted")
            break

        ctx = briefing(sig)
        # J2 entry judge. SHADOW: the verdict is logged beside the rules' decision and changes
        # nothing below — the J4 review scores it before it's ever allowed to bind.
        verdict = shadow_judge(ctx, leg, ticker)
        row, spent, why = None, 0.0, []
        if leg == "option":
            row, spent = _try_option_entry(mcp, acct, sig, regime, avail, state, verbose)
            if row is None:
                why.append("options: no applicable strategy or affordable/liquid contract")
        if row is None and use_stocks and stk.can_hold_shares(sig):   # stock leg, or options fallback
            row, spent = stk.enter(mcp, acct, sig, stock_dollars.get(ticker, 0.0), avail, state,
                                   verbose, room=stock_left)
            stock_left -= spent
            if row is None:
                why.append("shares: size below the $1 minimum or stock cap full")
        if row is None:
            log(sig, "skip", "; ".join(why) or "no leg produced an order", leg=leg, context=ctx, judge=verdict)
            continue
        results.append(row)
        key = row.get("option_id") or f"eq:{ticker}"
        log(sig, row["status"] if row["status"] in ("placed", "dry_run") else "skip",
            row.get("reason") if row["status"] not in ("placed", "dry_run") else f"{row['leg']} entry",
            key=key, leg=row.get("leg"), order={k: row.get(k) for k in
                                                ("strategy", "qty", "contract", "dollars", "premium")
                                                if row.get(k) is not None}, status=row["status"],
            context=ctx, judge=verdict)
        if spent:
            avail -= spent   # reserve so later entries don't oversize
            held.add(ticker)
            if max_new is not None:
                max_new -= 1
    return results


def _run_exits(mcp, acct, buying_power, verbose) -> list[dict]:
    from ingestion import mcp_auth, options_data as od
    from analysis.options_strategies import option_exit_decision, DEFAULT_EXIT
    import datetime as _dt

    try:
        data = mcp._data(mcp._unwrap_tool_result(
            mcp_auth.call_tool("get_option_positions", {"account_number": acct, "nonzero": True})))
        rows = data.get("positions", []) if isinstance(data, dict) else []
    except Exception as e:  # noqa: BLE001
        print(f"agentic_options: exit read failed — {e}")
        return []

    state = GuardState(start_equity=buying_power)
    results: list[dict] = []
    held_syms = {(r.get("chain_symbol") or "").upper() for r in rows if r.get("chain_symbol")}
    for p in rows:
        try:
            oid = p.get("option_id") or p.get("option") or p.get("id")
            qty = int(float(p.get("quantity") or 0))
            if not oid or qty < 1:
                continue
            entry = float(p.get("average_open_price") or p.get("average_price") or 0)
            # Robinhood option avg_open_price is per-contract ($ paid) → normalize to per-share.
            if entry > 5:  # heuristic: > $5 means it's the ×100 premium, not the per-share price
                entry = entry / 100.0
            quote = od.fetch_quote(oid) or {}
            mark = float(quote.get("mark_price") or quote.get("bid_price") or 0)
            exp = p.get("expiration_date") or p.get("expiration")
            dte = od._dte(exp) if exp else None
            from storage import peak_tracker
            peak = peak_tracker.update_peak(oid, mark)
            action, reason = option_exit_decision(entry, mark, dte, DEFAULT_EXIT, peak_mark=peak)
            if verbose:
                print(f"  {p.get('chain_symbol','?')} {oid[:8]}: entry {entry} mark {mark} peak {peak} dte {dte} → {action} ({reason})")
            if action != "close":
                # J3: the rules hold — review on news / an unusual underlying move / imminent earnings
                # (SHADOW: logged only; keep|sell for options, no stop to tighten).
                sym = (p.get("chain_symbol") or "").upper()
                try:
                    from analysis.holding_review import review
                    from ingestion import account_reads as ar
                    underlying = (ar.quotes([sym]).get(sym) or {}).get("price")
                    review({"ticker": sym, "key": oid, "kind": "option", "price": underlying,
                            "entry": entry, "now": mark,
                            "pnl_pct": round((mark - entry) / entry * 100, 1) if entry else None,
                            "plan": f"option exits: -{DEFAULT_EXIT['stop_pct']}% stop, trail after "
                                    f"+{DEFAULT_EXIT['trail_activate']}%, +{DEFAULT_EXIT['profit_pct']}% target, "
                                    f"close at ≤{DEFAULT_EXIT['close_dte']} DTE (now {dte} DTE)",
                            "peak": peak},
                           holdings=held_syms, verbose=verbose)
                except Exception as e:  # noqa: BLE001 — a review must never break the exit pass
                    print(f"agentic_options: holding review failed for {sym} — {e}")
                continue
            right = (p.get("type") or "call").lower()
            sym = p.get("chain_symbol") or "?"
            intent = OptionOrderIntent(
                underlying=sym, option_id=oid, right=right,
                side="sell", position_effect="close", quantity=qty,
                price=round(float(quote.get("bid_price") or mark), 2), direction="credit",
                expiration=exp, reason=f"exit: {reason}",
                client_id=f"optexit-{oid}-{reason[:12]}",
            )
            res = mcp.place_option_order(intent, state, buying_power=buying_power, account_number=acct)
            # Forget the high-water mark only once the close is really sent — a rejected close (or a
            # dry preview) used to wipe it, silently resetting the trailing stop.
            if res["status"] == "placed":
                peak_tracker.clear_peak(oid)
            from storage import decision_log as dlog
            dlog.record("exit", sym, res["status"], reason, key=oid,
                        entry_id=dlog.last_entry_id(oid),
                        inputs={"entry": entry, "mark": mark, "peak": peak, "dte": dte, "qty": qty},
                        pnl_pct=round((mark - entry) / entry * 100, 1) if entry else None,
                        detail=res.get("reason"))
            # `reason` = why we exited; the order status text goes in `detail` (it used to overwrite it).
            results.append({"close": sym, "reason": reason, "status": res["status"],
                            "detail": res.get("reason")})
        except Exception as e:  # noqa: BLE001 — one bad position must not abort the rest
            print(f"agentic_options: exit eval failed for a position — {e}")
    return results


def _run_paper_exits(verbose: bool) -> list[dict]:
    from storage import paper_book as pb
    from ingestion import options_data as od
    from analysis.options_strategies import option_exit_decision, DEFAULT_EXIT
    results = []
    for p in pb.get_open():
        try:
            from storage import peak_tracker
            q = od.fetch_quote(p["option_id"]) or {}
            mark = float(q.get("mark_price") or q.get("bid_price") or 0)
            dte = od._dte(p.get("expiration")) if p.get("expiration") else None
            peak = peak_tracker.update_peak(p["option_id"], mark)
            action, reason = option_exit_decision(p["entry_price"], mark, dte, DEFAULT_EXIT, peak_mark=peak)
            if verbose:
                print(f"  [paper] {p['ticker']} entry {p['entry_price']} mark {mark} peak {peak} dte {dte} → {action} ({reason})")
            if action == "close":
                rec = pb.close_position(p["option_id"], exit_price=mark, reason=reason)
                peak_tracker.clear_peak(p["option_id"])
                if rec:
                    results.append({"close": p["ticker"], "pnl": rec["pnl"], "reason": reason})
        except Exception as e:  # noqa: BLE001
            print(f"agentic_options: paper exit failed for {p.get('ticker')} — {e}")
    return results


def _run_paper_entries(verbose: bool, exclude=None) -> list[dict]:
    from storage import paper_book as pb
    from ingestion import options_data as od
    from ingestion import account_reads as ar
    from analysis.options_strategies import applicable_plans, size_contracts
    from ingestion.signal_context import enrich

    regime = _regime()
    held = pb.open_tickers() | (exclude or set())  # exclude = closed this cycle (anti-churn)
    results = []
    for sig in _signals():
        ticker = (sig.get("ticker") or "").upper()
        if not ticker or ticker in held:
            continue
        cash = pb.cash()
        if cash <= 0:
            break
        sig = enrich(sig)
        plans = applicable_plans(sig, regime)
        if not plans:
            continue
        spot = (ar.quotes([ticker]).get(ticker) or {}).get("price")
        if not spot:
            continue
        plan, contract = None, None
        for cand in plans:
            c = od.select_contract(ticker, spot, cand["right"], cand["dte_min"],
                                   cand["dte_max"], cand["otm_pct"], max_premium=cash)
            if c:
                plan, contract = cand, c
                break
        if not contract:
            continue
        n = size_contracts(plan["alloc_pct"], cash, contract["cost_1x"])
        if n < 1:
            continue
        ok = pb.open_position({
            "option_id": contract["instrument_id"], "ticker": ticker, "right": plan["right"],
            "strike": contract["strike"], "expiration": contract["expiration"],
            "strategy": plan["strategy"], "qty": n, "entry_price": round(contract["ask"], 2)})
        if ok:
            held.add(ticker)
            results.append({"ticker": ticker, "strategy": plan["strategy"], "qty": n,
                            "contract": f"{contract['strike']:g}{plan['right'][0].upper()} {contract['expiration']}",
                            "cost": round(contract["ask"] * 100 * n, 2)})
            if verbose:
                print(f"  [paper] BUY {n} {ticker} {contract['strike']:g}{plan['right'][0].upper()} "
                      f"@ {contract['ask']} = ${contract['ask']*100*n:.0f} ({plan['strategy']})")
    return results


def run_paper_agent(verbose: bool = True) -> dict:
    """Simulation cycle — exits then entries against the paper book (storage.paper_book). No real
    orders, no real money; uses LIVE option quotes so P&L is realistic. Honors the kill switch.
    This is what the scheduler runs while UNARMED, building a track record to judge the strategies."""
    from ingestion import robinhood_mcp as mcp
    if _halted():
        return {"status": "halted", "reason": "kill switch present"}
    if not mcp.is_available():
        return {"status": "skipped", "reason": "USE_MCP off"}
    if verbose:
        print("== Paper options agent ==\n-- paper exits --")
    exits = _run_paper_exits(verbose)
    if verbose:
        print("-- paper entries --")
    entries = _run_paper_entries(verbose, exclude=_closed_underlyings(exits, live=False))
    return {"entries": entries, "exits": exits, "mode": "paper"}


def run_options_agent(verbose: bool = True, stocks: bool | None = None) -> dict:
    """One full cycle: exits first (free capital), then entries. Honors the kill switch, market
    hours (for live placement), and DRY_RUN. Returns {'entries': [...], 'exits': [...]}.
    `stocks` overrides config.AGENT_TRADE_STOCKS for this cycle (e.g. a dry preview of the stock leg)."""
    from ingestion import robinhood_mcp as mcp

    if _halted():
        return {"status": "halted", "reason": f"kill switch present ({_HALT_FLAG})"}
    if not mcp.is_available():
        return {"status": "skipped", "reason": "USE_MCP off"}
    acct = mcp.agentic_account_number()
    if not acct:
        return {"status": "skipped", "reason": "no agentic account"}
    if not config.DRY_RUN and not _market_open():
        return {"status": "skipped", "reason": "market closed — options place in regular hours"}

    bp = mcp.fetch_buying_power(acct) or 0.0
    if verbose:
        print(f"== Autonomous options agent ==  DRY_RUN={config.DRY_RUN} | agentic BP=${bp:.2f}\n-- exits --")
    # Exits always run (token-free, capital-protecting) — options, then any share positions the agent
    # opened (runs even with AGENT_TRADE_STOCKS off, so turning the flag off never strands a position;
    # it's a no-op without reads when the agent owns no shares).
    exits = _run_exits(mcp, acct, bp, verbose)
    try:
        from alerts import agentic_stocks as _stk
        exits += _stk.run_exits(mcp, acct, bp, verbose)
    except Exception as e:  # noqa: BLE001 — a stock-exit failure must not block options entries/exits
        print(f"agentic_options: stock exit pass failed — {e}")
    bp = mcp.fetch_buying_power(acct) or bp  # refresh after any closes

    # Entries depend on fresh Argus signals (which cost tokens). Halt NEW entries when the LLM
    # credit ledger is at/under its reserve, so the user has leeway to top up (per request:
    # stop on token balance, not just buying power). Exits above still ran.
    try:
        from llm_budget import can_spend, get_state
        if not can_spend():
            st = get_state()
            if verbose:
                print(f"-- entries SKIPPED: LLM credit ${st['remaining']:.2f} ≤ reserve ${st['reserve']:.2f} --")
            from storage import decision_log as dlog
            dlog.record("halt", None, "entries_halted", "LLM credit at/under reserve",
                        inputs={"remaining": st["remaining"], "reserve": st["reserve"]})
            return {"entries": [], "exits": exits,
                    "entries_halted": f"LLM credit ${st['remaining']:.2f} ≤ reserve ${st['reserve']:.2f}"}
    except Exception as e:  # noqa: BLE001 — ledger must never block exits/reads
        print(f"agentic_options: credit-ledger check failed — {e}")

    if verbose:
        print("-- entries --")
    # Budget is read AFTER exits so this cycle's same-day closes are already counted.
    entries = _run_entries(mcp, acct, bp, verbose, exclude=_closed_underlyings(exits, live=True),
                           max_new=_entry_budget(mcp, acct, bp, verbose), stocks=stocks)
    return {"entries": entries, "exits": exits}
