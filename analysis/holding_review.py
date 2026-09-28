"""
Event-driven holding review (judgment plan J3).

The mechanical exits (stop / trailing / target / time) only look at PRICE. A held position whose thesis
breaks at 11 AM — a downgrade, a lawsuit, an earnings date the entry didn't plan for — waits for the
stop. This reviews a held position with the Sonnet judge ONLY when something happens:

  NEWS      a Finnhub headline about the ticker the review hasn't seen yet (each headline counts once)
  MOVE      price moved ≥ MOVE_ATR_MULT × its average daily range since the last reference price
            (the reference resets after each review, so one big move = one review, not one per cycle)
  EARNINGS  a report ≤ EARNINGS_DAYS away (once per day)

and only when the rules would HOLD (if they already sell, a review is moot). The verdict — keep / sell /
tighten_stop (shares only) — is logged as a `review` record; under config.AGENT_JUDGE = "shadow" it
changes NOTHING.

State (holding_review.json, gitignored), per ticker: seen headline hashes, the reference price, and the
once-a-day reads (average daily range, days to earnings) so a quiet position costs one quote per cycle.
events() is pure; review() does the reads + judge call and never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta

from agent_mode import state_file

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "holding_review.json")

MOVE_ATR_MULT = 1.5
EARNINGS_DAYS = 2
# Finnhub tags market-roundup pieces ("Which S&P500 stocks are moving?") to big tickers, so a heavily
# covered name would otherwise trigger a paid review on every new roundup. News alone re-triggers at most
# this often per ticker; headlines arriving in between stay unseen and are batched into the next review.
# Moves and earnings are not throttled.
NEWS_COOLDOWN_MINUTES = 120


def _load() -> dict:
    try:
        with open(state_file(_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    try:
        with open(state_file(_FILE), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
    except OSError as e:
        print(f"holding_review: could not save state — {e}")


def headline_id(title: str | None) -> str:
    return hashlib.sha1((title or "").strip().lower().encode("utf-8", "replace")).hexdigest()[:12]


def events(st: dict, price: float | None, headlines: list[dict], today: str,
           now: datetime | None = None) -> list[str]:
    """Pure: which review events fired for one position, given its state (mutated only by the caller
    after a review). `st` keys: anchor_price, adr_pct, days_to_earnings, seen (headline ids),
    earnings_flagged (date string), last_review (ISO datetime)."""
    fired = []
    seen = set(st.get("seen") or [])
    last = st.get("last_review")
    news_cooling = bool(last and now and
                        now - datetime.fromisoformat(last) < timedelta(minutes=NEWS_COOLDOWN_MINUTES))
    if not news_cooling:
        for h in headlines or []:
            if headline_id(h.get("title")) not in seen:
                fired.append(f"news: {h.get('title')}")
    anchor, adr = st.get("anchor_price"), st.get("adr_pct")
    if price and anchor and adr:
        move = (price - anchor) / anchor * 100
        if abs(move) >= MOVE_ATR_MULT * adr:
            fired.append(f"move {move:+.1f}% since ${anchor:g} (≥{MOVE_ATR_MULT:g}× its {adr:g}% daily range)")
    dte = st.get("days_to_earnings")
    if dte is not None and 0 <= dte <= EARNINGS_DAYS and st.get("earnings_flagged") != today:
        fired.append(f"earnings in {dte} day(s)")
    return fired


def mark_reviewed(st: dict, price: float | None, headlines: list[dict], today: str, fired: list[str],
                  now: datetime | None = None) -> dict:
    """Pure: the state after a review — headlines seen, reference price reset, earnings flagged today,
    review time stamped (starts the news cooldown)."""
    new = dict(st)
    if now:
        new["last_review"] = now.isoformat(timespec="seconds")
    new["seen"] = sorted(set(st.get("seen") or []) | {headline_id(h.get("title")) for h in headlines or []})[-200:]
    if price:
        new["anchor_price"] = price
    if any(f.startswith("earnings") for f in fired):
        new["earnings_flagged"] = today
    return new


def position_text(p: dict) -> str:
    """The held position, as the judge reads it (appended under the briefing)."""
    pnl = f" ({p['pnl_pct']:+.1f}%)" if p.get("pnl_pct") is not None else ""
    lines = [f"HELD POSITION ({p.get('kind')}): {p.get('ticker')} — entry {p.get('entry')} → now {p.get('now')}{pnl}",
             f"  Held {p.get('days_held') if p.get('days_held') is not None else 'n/a'} days | exit plan: "
             f"{p.get('plan') or 'default'} | current stop: {p.get('current_stop') or 'n/a'} | "
             f"peak: {p.get('peak') or 'n/a'}"]
    if p.get("thesis"):
        lines.append(f"  Original thesis: {p['thesis']}")
    if p.get("invalidation"):
        lines.append(f"  Original invalidation: {p['invalidation']}")
    lines.append(f"EVENTS THAT TRIGGERED THIS REVIEW: {'; '.join(p.get('events') or [])}")
    lines.append("RULES' ACTION: hold (no mechanical exit has fired).")
    return "\n".join(lines)


def _daily_reads(ticker: str, st: dict, today: str) -> dict:
    """Refresh the once-a-day reads (average daily range, days to earnings) when stale."""
    if st.get("reads_date") == today:
        return st
    new = dict(st, reads_date=today)
    try:
        from ingestion.prices import fetch_price_history
        new["adr_pct"] = (fetch_price_history([ticker]).get(ticker) or {}).get("avg_daily_range_pct")
    except Exception as e:  # noqa: BLE001
        print(f"holding_review: {ticker} daily range read failed — {e}")
    try:
        from ingestion.signal_context import earnings_context
        new["days_to_earnings"] = earnings_context(ticker).get("days_to_earnings")
    except Exception as e:  # noqa: BLE001
        print(f"holding_review: {ticker} earnings read failed — {e}")
    return new


def review(pos: dict, *, holdings: set[str], regime: dict | None = None, verbose: bool = False) -> dict | None:
    """Check one HELD position (the rules said hold) for events; if any fired, brief + judge it and log a
    `review` record. Returns the record's fields, or None when nothing fired. Never raises.

    pos: {ticker, key, kind 'stock'|'option', price (underlying), entry, now, pnl_pct, days_held, plan,
          current_stop, peak, opened_at (datetime|None)}"""
    t = (pos.get("ticker") or "").upper()
    try:
        from analysis.agent_context import _news_since, gather
        from analysis.agent_judge import judge_holding
        from storage import decision_log as dlog

        state = _load()
        today = date.today().isoformat()
        st = _daily_reads(t, state.get(t) or {}, today)
        if st.get("anchor_price") is None and pos.get("price"):
            st["anchor_price"] = pos["price"]        # first sighting: reference only, no "move" event
        since = max(pos.get("opened_at") or datetime.now() - timedelta(hours=24),
                    datetime.now() - timedelta(hours=24))
        try:
            headlines = _news_since(t, since)
        except Exception as e:  # noqa: BLE001
            print(f"holding_review: {t} news read failed — {e}")
            headlines = []
        now = datetime.now()
        fired = events(st, pos.get("price"), headlines, today, now)
        if not fired:
            state[t] = st
            _save(state)
            return None

        if regime is None:                            # fetched only when an event actually fired
            from alerts.agentic_options import _regime
            regime = _regime().get("detail")
        entry_rec = dlog.get(dlog.last_entry_id(pos.get("key"))) or {}
        entry_judge = entry_rec.get("judge") or {}
        sig = {**(entry_rec.get("inputs") or {}), "ticker": t, "direction": "buy",
               "entry_rationale": entry_judge.get("thesis")}
        ctx = gather(sig, regime=regime, holdings=set(holdings) - {t})
        verdict = judge_holding(ctx, position_text({**pos, "events": fired, "thesis": entry_judge.get("thesis"),
                                                    "invalidation": entry_judge.get("invalidation")}),
                                price=pos.get("price"), current_stop=pos.get("current_stop"),
                                allow_tighten=pos.get("kind") == "stock")
        if verbose:
            print(f"  review {t}: {fired} → {(verdict or {}).get('action')} — {(verdict or {}).get('reasoning', '')}")
        dlog.record("review", t, "hold", "; ".join(fired), key=pos.get("key"), entry_id=entry_rec.get("id"),
                    inputs={k: pos.get(k) for k in ("kind", "entry", "now", "pnl_pct", "days_held", "plan",
                                                    "current_stop", "peak")},
                    events=fired, context=ctx, judge=verdict)
        state[t] = mark_reviewed(st, pos.get("price"), headlines, today, fired, now)
        _save(state)
        return {"ticker": t, "events": fired, "judge": verdict}
    except Exception as e:  # noqa: BLE001 — a review must never break the exit pass
        print(f"holding_review: {t} review failed — {e}")
        return None
