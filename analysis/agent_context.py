"""
Fresh, per-candidate briefing for the agent's judgment (plan J1).

The pipeline's view of a ticker is frozen at its 6:30 AM run. By the time the agent acts, the stock
may have run, news may have landed, and the agent may already hold something correlated. This builds
what a trader would check RIGHT BEFORE acting — every field from a live read or the agent's own log,
never from model memory:

  price      now vs the pipeline's price (move since the rec), today's change
  tape       RSI / MACD / trend / 52-week position, volume PACE (today's partial volume scaled by how
             much of the session has elapsed — raw vol_vs_avg makes every stock look quiet at 10 AM)
  structure  support / resistance, ATR, R24 stop / target / reward:risk
  events     days to earnings, company headlines published since the pipeline ran
  market     regime (SPY trend + VIX)
  book       what the agent already holds + same-sector overlap (two chip names are one bet)
  record     the agent's own realized results by signal source and by leg/strategy (decision log)

build() and render() are pure (unit-tested); gather() does the live reads and never raises — a
missing piece is simply absent from the briefing. render() is the text block the J2 judge will read.
"""
from __future__ import annotations

from datetime import datetime, timedelta

_SESSION_MINUTES = 390        # 9:30–16:00 ET
_HALF_DAY_MINUTES = 210       # 9:30–13:00 ET


# --- pure helpers ---------------------------------------------------------------------------

def pct_move(now: float | None, then: float | None) -> float | None:
    if not now or not then:
        return None
    return round((now - then) / then * 100, 2)


def session_elapsed(now_et: datetime, half_day: bool = False) -> float:
    """Fraction (0..1) of today's regular session that has elapsed at `now_et`."""
    minutes = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    total = _HALF_DAY_MINUTES if half_day else _SESSION_MINUTES
    return max(0.0, min(1.0, minutes / total))


def volume_pace(vol_vs_avg: float | None, elapsed: float) -> float | None:
    """Today's volume projected to a full session, vs the 30-day average (1.0 = normal day).
    None before ~15 minutes in (too early to extrapolate) or with no volume data."""
    if vol_vs_avg is None or elapsed < 0.04:
        return None
    return round(vol_vs_avg / elapsed, 2)


def track_record(records: list[dict]) -> dict:
    """Realized results of the agent's closed trades, from the decision log: overall, by signal
    source, and by leg/strategy. An exit counts only when its entry is known (linked by entry_id)."""
    entries = {r["id"]: r for r in records if r.get("kind") == "entry"}
    groups: dict[str, dict[str, list]] = {"overall": {"all": []}, "by_source": {}, "by_strategy": {}}
    for x in records:
        if x.get("kind") != "exit" or x.get("action") != "placed" or x.get("pnl_pct") is None:
            continue
        e = entries.get(x.get("entry_id"))
        if not e:
            continue
        pnl = float(x["pnl_pct"])
        src = (e.get("inputs") or {}).get("source") or "pipeline"
        strat = (e.get("order") or {}).get("strategy") or e.get("leg") or "unknown"
        groups["overall"]["all"].append(pnl)
        groups["by_source"].setdefault(src, []).append(pnl)
        groups["by_strategy"].setdefault(strat, []).append(pnl)

    def stats(pnls):
        return {"n": len(pnls), "win_rate": round(sum(p > 0 for p in pnls) / len(pnls) * 100),
                "avg_pnl": round(sum(pnls) / len(pnls), 1)} if pnls else {"n": 0}

    return {"overall": stats(groups["overall"]["all"]),
            "by_source": {k: stats(v) for k, v in groups["by_source"].items()},
            "by_strategy": {k: stats(v) for k, v in groups["by_strategy"].items()}}


def build(sig: dict, *, history: dict | None = None, price_now: float | None = None,
          rec_price: float | None = None, news: list[dict] | None = None, earnings: dict | None = None,
          sector: str | None = None, regime: dict | None = None, holdings: dict | None = None,
          record: dict | None = None, elapsed: float | None = None) -> dict:
    """Pure: assemble the briefing from already-fetched pieces. `holdings` = {TICKER: sector|None}."""
    h = history or {}
    kl = h.get("key_levels") or {}
    now = price_now or h.get("current_price")
    held = holdings or {}
    signal = {k: sig.get(k) for k in ("direction", "conviction", "highly_recommended", "risk_level",
                                      "entry_rationale", "entry_trigger", "exit_condition",
                                      "catalyst_date", "trigger_fired") if sig.get(k) is not None}
    signal["source"] = sig.get("source") or "pipeline"      # pipeline recs carry no source field
    return {
        "ticker": (sig.get("ticker") or "").upper(),
        "signal": signal,
        "price": {"now": now, "at_rec": rec_price, "move_since_rec_pct": pct_move(now, rec_price),
                  "change_14d_pct": h.get("pct_change_14d"), "trend_14d": h.get("trend_14d")},
        "tape": {"rsi_14": h.get("rsi_14"), "macd": h.get("macd_state"), "macd_cross": h.get("macd_cross"),
                 "vs_sma50": h.get("price_vs_sma50"), "vs_sma200": h.get("price_vs_sma200"),
                 "pct_from_52w_high": h.get("pct_from_52w_high"),
                 "volume_pace": volume_pace(h.get("vol_vs_avg"), elapsed) if elapsed is not None else None,
                 "avg_daily_range_pct": h.get("avg_daily_range_pct")},
        "structure": {"support": kl.get("nearest_support"), "resistance": kl.get("nearest_resistance"),
                      "atr": kl.get("atr_abs"), "stop_pct_atr": kl.get("stop_pct_atr"),
                      "target_pct_resist": kl.get("target_pct_resist"), "reward_risk": kl.get("reward_risk")},
        "events": {"days_to_earnings": (earnings or {}).get("days_to_earnings"),
                   "days_since_earnings": (earnings or {}).get("days_since_earnings"),
                   "news_since_rec": [{"title": n.get("title"), "source": n.get("source"),
                                       "published": n.get("published")} for n in (news or [])][:5]},
        "market": {k: (regime or {}).get(k) for k in ("regime", "vix", "vix_state", "ma_trend")},
        "book": {"holdings": sorted(held), "sector": sector,
                 "same_sector": sorted(t for t, s in held.items() if sector and s == sector)},
        "record": record or {"overall": {"n": 0}},
    }


def _fmt(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


def render(ctx: dict) -> str:
    """Pure: the briefing as compact text for the judge prompt."""
    p, t, s, e, m, b = (ctx[k] for k in ("price", "tape", "structure", "events", "market", "book"))
    sig = ctx["signal"]
    rec = ctx["record"]
    lines = [
        f"CANDIDATE {ctx['ticker']} — {sig.get('direction')} | conviction {_fmt(sig.get('conviction'))}"
        f"{' | HIGHLY RECOMMENDED' if sig.get('highly_recommended') else ''} | risk {_fmt(sig.get('risk_level'))}"
        f" | source {_fmt(sig.get('source'), '')}",
        f"  Thesis: {sig.get('entry_rationale') or 'n/a'}",
        f"  Entry trigger: {sig.get('entry_trigger') or 'n/a'}"
        + (f"  [FIRED: {sig['trigger_fired']}]" if sig.get("trigger_fired") else ""),
        f"  Exit plan: {sig.get('exit_condition') or 'n/a'} | catalyst date {_fmt(sig.get('catalyst_date'))}",
        f"PRICE now ${_fmt(p['now'])} vs ${_fmt(p['at_rec'])} at the pipeline run "
        f"({_fmt(p['move_since_rec_pct'], '%')} since) | 14d {_fmt(p['change_14d_pct'], '%')} ({_fmt(p['trend_14d'])})",
        f"TAPE RSI {_fmt(t['rsi_14'])} | MACD {_fmt(t['macd'])} ({_fmt(t['macd_cross'])}) | "
        f"vs SMA50 {_fmt(t['vs_sma50'])}, SMA200 {_fmt(t['vs_sma200'])} | {_fmt(t['pct_from_52w_high'], '%')} from 52w high"
        f" | volume pace {_fmt(t['volume_pace'], 'x')} of normal | avg daily range {_fmt(t['avg_daily_range_pct'], '%')}",
        f"STRUCTURE support ${_fmt(s['support'])} | resistance ${_fmt(s['resistance'])} | ATR ${_fmt(s['atr'])} | "
        f"ATR stop {_fmt(s['stop_pct_atr'], '%')} | target to resistance {_fmt(s['target_pct_resist'], '%')} | "
        f"reward:risk {_fmt(s['reward_risk'])}",
        f"EVENTS earnings in {_fmt(e['days_to_earnings'])} days (last {_fmt(e['days_since_earnings'])} days ago)",
    ]
    if e["news_since_rec"]:
        lines.append("  Headlines since the pipeline ran:")
        lines += [f"   - {n['title']} ({n['source']}, {n['published']})" for n in e["news_since_rec"]]
    else:
        lines.append("  No company headlines since the pipeline ran.")
    lines += [
        f"MARKET {_fmt(m['regime'])} | VIX {_fmt(m['vix'])} ({_fmt(m['vix_state'])})",
        f"BOOK holding {', '.join(b['holdings']) or 'nothing'} | this ticker's sector {_fmt(b['sector'])}"
        + (f" | SAME SECTOR already held: {', '.join(b['same_sector'])}" if b["same_sector"] else ""),
        f"AGENT RECORD {rec['overall'].get('n', 0)} closed"
        + (f", {rec['overall']['win_rate']}% win, avg {rec['overall']['avg_pnl']:+}%" if rec["overall"].get("n") else "")
        + "".join(f" | {k}: {v['n']} trades {v['win_rate']}% win {v['avg_pnl']:+}%"
                  for k, v in (rec.get("by_source") or {}).items() if v.get("n")),
    ]
    return "\n".join(lines)


# --- live gathering -------------------------------------------------------------------------

def _pipeline_run_time(cache: dict | None) -> datetime | None:
    try:
        return datetime.strptime((cache or {}).get("last_run", ""), "%B %d, %Y at %I:%M %p")
    except ValueError:
        return None


def _news_since(ticker: str, since: datetime) -> list[dict]:
    """Finnhub company headlines for `ticker` published after `since` (newest first, ≤5)."""
    from ingestion.finnhub_news import get_client
    day = since.strftime("%Y-%m-%d")
    items = get_client().company_news(ticker, _from=day, to=datetime.now().strftime("%Y-%m-%d")) or []
    fresh = [i for i in items if datetime.fromtimestamp(i.get("datetime", 0)) > since]
    fresh.sort(key=lambda i: i.get("datetime", 0), reverse=True)
    return [{"title": i.get("headline"), "source": i.get("source"),
             "published": datetime.fromtimestamp(i.get("datetime", 0)).strftime("%H:%M")} for i in fresh[:5]]


def gather(sig: dict, *, regime: dict | None, holdings: set[str], records: list[dict] | None = None) -> dict:
    """Live: fetch every piece for one candidate and build() the briefing. Never raises — a failed
    read leaves that piece empty (the judge is told n/a rather than the cycle dying)."""
    import market_hours as mh
    from ingestion.fundamentals import fetch_fundamentals
    from ingestion.prices import fetch_price_history
    from ingestion.signal_context import earnings_context
    from storage import decision_log, pipeline_cache
    t = (sig.get("ticker") or "").upper()

    def safe(label: str, fn, default=None):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — a missing piece must never block a decision
            print(f"agent_context: {t} — {label} failed: {e}")
            return default

    cache, _ = safe("pipeline cache", pipeline_cache.load_today, (None, False))
    history = safe("price history", lambda: fetch_price_history([t]).get(t)) or {}
    rec = ((cache or {}).get("prices") or {}).get(t)
    since = _pipeline_run_time(cache) or (datetime.now() - timedelta(hours=24))
    news = safe("news", lambda: _news_since(t, since), [])
    earnings = safe("earnings", lambda: earnings_context(t), {})
    fund = safe("fundamentals", lambda: fetch_fundamentals(sorted({t, *holdings})), {}) or {}
    sector_of = {k: (v or {}).get("sector") for k, v in fund.items()}
    now = mh._now_et()
    session = safe("market session", lambda: mh.market_session(now), {}) or {}
    if records is None:
        # The agent's own record in THIS book: live trades in a live cycle, paper trades in a paper one.
        records = safe("decision log", lambda: decision_log.read(mode=decision_log._mode()), [])
    return build(sig, history=history, rec_price=rec.get("price") if isinstance(rec, dict) else None,
                 news=news, earnings=earnings, sector=sector_of.get(t), regime=regime,
                 holdings={h: sector_of.get(h) for h in holdings}, record=track_record(records),
                 elapsed=session_elapsed(now, bool(session.get("is_early")))
                 if session.get("status") in ("open", "open_half_day") else None)
