"""
Judge scorecard (judgment plan J4) — does the shadow judge actually help?

While config.AGENT_JUDGE = "shadow", every judged decision is logged beside what the rules did
(storage/decision_log.py). This scores those verdicts against what the market did next, so the J5
decision ("let the judge bind") rests on numbers, not on how convincing its reasoning sounds.

  ENTRY VERDICTS  every judged candidate (traded or not) → the underlying's return N trading days after
                  the verdict (from the briefing's live price), sign-flipped for bearish ideas. Grouped by
                  enter vs not-enter (wait/skip). If the judge has skill, its "enter" group beats its
                  "wait/skip" group. Counts candidates the rules never traded, so data accrues far faster
                  than waiting for real round-trips. One sample per ticker per day (the first judgment;
                  reused verdicts aren't samples).
  TAKEN TRADES    trades the rules actually placed → realized P&L (linked exits), split by what the judge
                  said. The question J5 asks: would its skips have avoided the losers?
  HOLDING REVIEWS sell / tighten verdicts vs keep → the underlying's move afterwards. A useful "sell" is
                  followed by a drop.

Caveat stated in the output: an underlying's return is a PROXY for the judge's directional read, not the
trade's P&L (options add leverage + decay). Nothing is concluded below MIN_SAMPLES per group.
Pure scoring (price lookup injected) — unit-tested; scorecard() does the live reads.
"""
from __future__ import annotations

from datetime import date, datetime

MIN_SAMPLES = 15
HORIZONS = (1, 5)
HELPS_MARGIN_PP = 1.0     # "enter" must beat "wait/skip" by ≥1 percentage point at 5 days


def _day(ts: str | None) -> date | None:
    try:
        return datetime.fromisoformat(ts).date()
    except (TypeError, ValueError):
        return None


def forward_return(closes: list[tuple], from_day: date, from_price: float | None, days: int) -> float | None:
    """Pure: % return from `from_price` (or the close on/before `from_day`) to the close `days` trading
    days AFTER `from_day`. None when that day hasn't happened yet or the data is missing."""
    if not closes or not from_day:
        return None
    after = [(d, c) for d, c in closes if d > from_day]
    if len(after) < days:
        return None
    if not from_price:
        before = [c for d, c in closes if d <= from_day]
        from_price = before[-1] if before else None
    if not from_price:
        return None
    return round((after[days - 1][1] - from_price) / from_price * 100, 2)


def first_per_day(records: list[dict], kind: str) -> list[dict]:
    """Pure: the first genuinely judged record per (ticker, day) — skips reused verdicts and errors."""
    out, seen = [], set()
    for r in records:
        v = r.get("judge") or {}
        if r.get("kind") != kind or not v or v.get("error") or v.get("reused_from"):
            continue
        key = (r.get("ticker"), (r.get("ts") or "")[:10])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _stats(values: list[float]) -> dict:
    return ({"n": len(values), "avg": round(sum(values) / len(values), 2),
             "win_rate": round(sum(v > 0 for v in values) / len(values) * 100)} if values else {"n": 0})


def entry_scores(records: list[dict], closes_for) -> dict:
    """Pure (closes_for(ticker) → [(date, close)] injected): forward returns by entry verdict group."""
    groups = {g: {h: [] for h in HORIZONS} for g in ("enter", "not_enter")}
    for r in first_per_day(records, "entry"):
        v = r["judge"]
        group = "enter" if v.get("decision") == "enter" else "not_enter"
        sign = -1 if ((r.get("inputs") or {}).get("direction") == "short") else 1
        price = (((r.get("context") or {}).get("price")) or {}).get("now")
        closes = closes_for(r.get("ticker"))
        for h in HORIZONS:
            ret = forward_return(closes, _day(r.get("ts")), price, h)
            if ret is not None:
                groups[group][h].append(sign * ret)
    return {g: {h: _stats(v) for h, v in hs.items()} for g, hs in groups.items()}


def taken_trade_scores(records: list[dict]) -> dict:
    """Pure: realized P&L of trades the rules placed, split by what the judge said at entry."""
    entries = {r["id"]: r for r in records if r.get("kind") == "entry" and r.get("action") == "placed"}
    split = {"judge_enter": [], "judge_wait_or_skip": []}
    for x in records:
        e = entries.get(x.get("entry_id"))
        if x.get("kind") != "exit" or x.get("action") != "placed" or x.get("pnl_pct") is None or not e:
            continue
        v = e.get("judge") or {}
        if not v or v.get("error"):
            continue
        split["judge_enter" if v.get("decision") == "enter" else "judge_wait_or_skip"].append(float(x["pnl_pct"]))
    return {k: _stats(v) for k, v in split.items()}


def review_scores(records: list[dict], closes_for, horizon: int = 1) -> dict:
    """Pure: the underlying's move after each holding-review verdict (a good "sell" precedes a drop)."""
    groups = {"sell": [], "tighten_stop": [], "keep": []}
    for r in first_per_day(records, "review"):
        action = (r.get("judge") or {}).get("action")
        if action not in groups:
            continue
        price = (((r.get("context") or {}).get("price")) or {}).get("now")
        ret = forward_return(closes_for(r.get("ticker")), _day(r.get("ts")), price, horizon)
        if ret is not None:
            groups[action].append(ret)
    return {k: _stats(v) for k, v in groups.items()}


def _skip_only_conclusion(n: dict, h: int, n_enter: int) -> dict:
    """The judge almost never says 'enter' (first paper week: 0 of 8), so the enter-vs-skip comparison can't
    form. Binding it would then mean ONE thing — "don't take these trades" — which is testable on its own:
    did the names it waved off go on to lose? ≤0% avg = its skips dodged losers; ≥ +HELPS_MARGIN_PP = its skips
    cost real gains; in between = no evidence either way."""
    avg = n["avg"]
    base = (f"The judge said 'enter' only {n_enter} time(s); what it waved off returned {avg:+}% on average over "
            f"{h} days (n={n['n']}, {n['win_rate']}% up). ")
    if avg <= 0:
        return {"ready": True, "helps": True, "basis": "skip_only", "text": base +
                "Its skips dodged losers — binding it (J5) would mostly mean the agent trades far less, and that "
                "looks right on this sample."}
    if avg >= HELPS_MARGIN_PP:
        return {"ready": True, "helps": False, "basis": "skip_only", "text": base +
                "Its skips cost real gains — binding it would block winners. Keep it in shadow or turn it off."}
    return {"ready": True, "helps": False, "basis": "skip_only", "text": base +
            "Roughly flat — no evidence the skips help or hurt yet. Keep it in shadow."}


def conclusion(entries: dict, taken: dict) -> dict:
    """Pure: is there enough data, and does the judge help? (The J5 go/no-go input — the user decides.)
    Normal test: 'enter' beats 'wait/skip' by HELPS_MARGIN_PP. When the judge has (almost) never said enter,
    falls back to the skip-only test (_skip_only_conclusion)."""
    h = max(HORIZONS)
    e, n = entries["enter"][h], entries["not_enter"][h]
    if e.get("n", 0) < MIN_SAMPLES and n.get("n", 0) >= MIN_SAMPLES:
        return _skip_only_conclusion(n, h, e.get("n", 0))
    if e.get("n", 0) < MIN_SAMPLES or n.get("n", 0) < MIN_SAMPLES:
        return {"ready": False,
                "text": f"Not enough data: {e.get('n', 0)} 'enter' and {n.get('n', 0)} 'wait/skip' verdicts have a "
                        f"{h}-day outcome (need {MIN_SAMPLES} each). Keep the judge in shadow."}
    edge = round(e["avg"] - n["avg"], 2)
    tk, ts = taken.get("judge_enter", {}), taken.get("judge_wait_or_skip", {})
    taken_ok = not (tk.get("n") and ts.get("n")) or tk["avg"] >= ts["avg"]
    helps = edge >= HELPS_MARGIN_PP and taken_ok
    return {"ready": True, "helps": helps, "edge_pp": edge,
            "text": (f"'Enter' verdicts returned {e['avg']:+}% vs {n['avg']:+}% for 'wait/skip' over {h} days "
                     f"(edge {edge:+} pp, n={e['n']}/{n['n']}). "
                     + ("The judge adds value — J5 (binding) is justified."
                        if helps else "No clear edge — keep it in shadow or turn it off."))}


# --- live --------------------------------------------------------------------------------------

_CLOSES: dict[str, list] = {}


def live_closes(ticker: str) -> list[tuple]:
    """Daily closes for the last ~3 months (yfinance), cached per process."""
    if ticker not in _CLOSES:
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="3mo", interval="1d")
            _CLOSES[ticker] = [(ix.date(), float(c)) for ix, c in hist["Close"].items()]
        except Exception as e:  # noqa: BLE001
            print(f"judge_scorecard: price history failed for {ticker} — {e}")
            _CLOSES[ticker] = []
    return _CLOSES[ticker]


def scorecard(mode: str | tuple | None = "acting") -> dict:
    """The full J4 scorecard from the decision log. Default "acting" = live + paper cycles (dry previews
    repeat decisions); None = every record. Paper verdicts count: the judge saw the same live data —
    only the fills were virtual."""
    from storage import decision_log
    records = decision_log.read(mode=decision_log.ACTING_MODES if mode == "acting" else mode)
    entries = entry_scores(records, live_closes)
    taken = taken_trade_scores(records)
    judged = [r for r in records if (r.get("judge") or {}) and not (r.get("judge") or {}).get("reused_from")]
    return {"entries": entries, "taken": taken, "reviews": review_scores(records, live_closes),
            "conclusion": conclusion(entries, taken),
            "judge_calls": len(judged),
            "judge_cost_usd": round(sum(float((r.get("judge") or {}).get("cost_usd") or 0) for r in judged), 4),
            "caveat": "Entry/review scores use the underlying's return as a proxy for the judge's read — "
                      "not the trade's P&L (options add leverage + time decay)."}
