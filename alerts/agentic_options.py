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


def _regime() -> dict:
    try:
        from ingestion.prices import fetch_market_regime
        r = fetch_market_regime() or {}
        risk = r.get("risk") or r.get("overall") or r.get("regime") or "neutral"
        return {"risk": str(risk).lower().replace("-", "_")}
    except Exception as e:  # noqa: BLE001
        print(f"agentic_options: regime lookup failed — {e}")
        return {"risk": "neutral"}


# Argus chat is the sharper, more-decisive brain (it upgrades pipeline 'watch' ideas to real
# buys — e.g. it called RDDT's S&P-inclusion catalyst a Buy while the pipeline left it a watch).
# Chat picks carry no numeric conviction, so a chat "Buy" is treated as this conviction — high
# enough to clear catalyst_momentum (≥70) but below the short_dte/pre-earnings bars (75-80).
CHAT_BUY_CONVICTION = 72


def _signals() -> list[dict]:
    """Directional entry signals for the agent, deduped by ticker:
      1. buy/short recommendations from today's pipeline cache (real conviction), then
      2. Argus chat's last 'Buy' suggestions (get_chat_suggestions) for tickers the pipeline
         didn't already flag buy/short — this is choice B: let the chat's calls reach the agent."""
    out, seen = [], set()
    try:
        with open(_CACHE, encoding="utf-8") as f:
            recs = (json.load(f) or {}).get("recommendations", []) or []
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
            t = (s.get("ticker") or "").upper()
            if t and t not in seen and (s.get("action") or "").lower() == "buy":
                seen.add(t)
                out.append({"ticker": t, "direction": "buy",
                            "conviction": CHAT_BUY_CONVICTION, "source": "chat"})
    except Exception as e:  # noqa: BLE001 — chat merge is additive; never break pipeline signals
        print(f"agentic_options: chat-suggestion merge failed — {e}")
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


def _run_entries(mcp, acct, buying_power, verbose) -> list[dict]:
    from ingestion import options_data as od
    from ingestion import account_reads as ar
    from analysis.options_strategies import applicable_plans, size_contracts

    regime = _regime()
    held = _held_underlyings(mcp, acct)
    state = GuardState(start_equity=buying_power)
    avail = buying_power
    results: list[dict] = []

    from ingestion.signal_context import enrich

    for sig in _signals():
        ticker = (sig.get("ticker") or "").upper()
        if ticker in held:
            continue
        sig = enrich(sig)   # add rsi + earnings context for the technical/earnings strategies
        plans = applicable_plans(sig, regime)
        if not plans:
            continue
        spot = (ar.quotes([ticker]).get(ticker) or {}).get("price")
        if not spot:
            continue
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
                print(f"  {ticker}: no affordable/liquid contract for any strategy (skip)")
            continue
        n = size_contracts(plan["alloc_pct"], avail, contract["cost_1x"])
        if n < 1:
            continue
        price = round(contract["ask"], 2)
        intent = OptionOrderIntent(
            underlying=ticker, option_id=contract["instrument_id"], right=plan["right"],
            side="buy", position_effect="open", quantity=n, price=price,
            strike=contract["strike"], expiration=contract["expiration"],
            reason=f"{plan['strategy']} (conv {sig.get('conviction')})",
            client_id=f"opt-{ticker}-{contract['instrument_id']}-{contract['expiration']}",
        )
        res = mcp.place_option_order(intent, state, buying_power=avail, account_number=acct)
        results.append({"ticker": ticker, "strategy": plan["strategy"], "qty": n,
                        "contract": f"{contract['strike']}{plan['right'][0].upper()} {contract['expiration']}",
                        **{k: res[k] for k in ("status", "reason")}})
        if res["status"] in ("placed", "dry_run"):
            avail -= intent.premium  # reserve so later entries don't oversize
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
            action, reason = option_exit_decision(entry, mark, dte, DEFAULT_EXIT)
            if verbose:
                print(f"  {p.get('chain_symbol','?')} {oid[:8]}: entry {entry} mark {mark} dte {dte} → {action} ({reason})")
            if action != "close":
                continue
            right = (p.get("type") or "call").lower()
            intent = OptionOrderIntent(
                underlying=(p.get("chain_symbol") or "?"), option_id=oid, right=right,
                side="sell", position_effect="close", quantity=qty,
                price=round(float(quote.get("bid_price") or mark), 2), direction="credit",
                expiration=exp, reason=f"exit: {reason}",
                client_id=f"optexit-{oid}-{reason[:12]}",
            )
            res = mcp.place_option_order(intent, state, buying_power=buying_power, account_number=acct)
            results.append({"close": p.get("chain_symbol"), "reason": reason,
                            **{k: res[k] for k in ("status", "reason")}})
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
            q = od.fetch_quote(p["option_id"]) or {}
            mark = float(q.get("mark_price") or q.get("bid_price") or 0)
            dte = od._dte(p.get("expiration")) if p.get("expiration") else None
            action, reason = option_exit_decision(p["entry_price"], mark, dte, DEFAULT_EXIT)
            if verbose:
                print(f"  [paper] {p['ticker']} entry {p['entry_price']} mark {mark} dte {dte} → {action} ({reason})")
            if action == "close":
                rec = pb.close_position(p["option_id"], exit_price=mark, reason=reason)
                if rec:
                    results.append({"close": p["ticker"], "pnl": rec["pnl"], "reason": reason})
        except Exception as e:  # noqa: BLE001
            print(f"agentic_options: paper exit failed for {p.get('ticker')} — {e}")
    return results


def _run_paper_entries(verbose: bool) -> list[dict]:
    from storage import paper_book as pb
    from ingestion import options_data as od
    from ingestion import account_reads as ar
    from analysis.options_strategies import applicable_plans, size_contracts
    from ingestion.signal_context import enrich

    regime = _regime()
    held = pb.open_tickers()
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
    entries = _run_paper_entries(verbose)
    return {"entries": entries, "exits": exits, "mode": "paper"}


def run_options_agent(verbose: bool = True) -> dict:
    """One full cycle: exits first (free capital), then entries. Honors the kill switch, market
    hours (for live placement), and DRY_RUN. Returns {'entries': [...], 'exits': [...]}."""
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
    # Exits always run (token-free, capital-protecting).
    exits = _run_exits(mcp, acct, bp, verbose)
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
            return {"entries": [], "exits": exits,
                    "entries_halted": f"LLM credit ${st['remaining']:.2f} ≤ reserve ${st['reserve']:.2f}"}
    except Exception as e:  # noqa: BLE001 — ledger must never block exits/reads
        print(f"agentic_options: credit-ledger check failed — {e}")

    if verbose:
        print("-- entries --")
    entries = _run_entries(mcp, acct, bp, verbose)
    return {"entries": entries, "exits": exits}
