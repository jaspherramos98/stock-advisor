"""
Signal enrichment for the options agent — RSI + earnings context per ticker.

The agent's signals (from pipeline_cache) carry direction + conviction but not the technicals
some option strategies need (mean-reversion wants RSI; the earnings plays want the next/last
report date). This module fetches those from the MCP equity tools and exposes them as plain
fields the pure strategy functions read.

Pure parsers (_parse_rsi / _parse_earnings) are unit-tested; the live fetchers wrap the MCP.
Confirmed shapes (2026-08-14):
  get_equity_technical_indicators(symbol, type='rsi', interval='day', start_time=<RFC3339>)
      → data.indicators[0].series[] {begins_at, value}   (latest = last entry)
  get_earnings_results(symbol) → data.results[] {report:{date,timing,verified}, eps:{estimate,actual}}
"""
from __future__ import annotations

import datetime as _dt


def _parse_rsi(payload) -> float | None:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    inds = data.get("indicators", []) if isinstance(data, dict) else []
    for ind in inds:
        if (ind.get("type") or "").lower() == "rsi":
            series = ind.get("series") or []
            if series:
                try:
                    return round(float(series[-1].get("value")), 2)
                except (ValueError, TypeError):
                    return None
    return None


def _parse_earnings(payload, today: _dt.date | None = None) -> dict:
    """Return {days_to_earnings, days_since_earnings, last_beat} from an earnings payload.
    days_to_earnings = days until the next scheduled report (None if none upcoming);
    days_since_earnings = days since the most recent past report (None if none);
    last_beat = True/False/None whether the last report's actual EPS beat estimate."""
    today = today or _dt.date.today()
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    rows = data.get("results", []) if isinstance(data, dict) else []
    future, past = [], []
    for r in rows:
        rep = (r.get("report") or {})
        d = rep.get("date")
        try:
            dd = _dt.date.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        (future if dd >= today else past).append((dd, r))
    out = {"days_to_earnings": None, "days_since_earnings": None, "last_beat": None}
    if future:
        out["days_to_earnings"] = (min(future, key=lambda x: x[0])[0] - today).days
    if past:
        last_d, last_r = max(past, key=lambda x: x[0])
        out["days_since_earnings"] = (today - last_d).days
        try:
            eps = last_r.get("eps") or {}
            est, act = float(eps.get("estimate")), float(eps.get("actual"))
            out["last_beat"] = act > est
        except (ValueError, TypeError):
            pass
    return out


def _call(name: str, args: dict):
    from ingestion import robinhood_mcp as mcp
    return mcp._call_tool(name, args)


def latest_rsi(symbol: str) -> float | None:
    try:
        start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=40)).strftime("%Y-%m-%dT00:00:00Z")
        return _parse_rsi(_call("get_equity_technical_indicators",
                                {"symbol": symbol, "type": "rsi", "interval": "day", "start_time": start}))
    except Exception as e:  # noqa: BLE001 — enrichment is best-effort; missing RSI just disables MR
        print(f"signal_context: RSI fetch failed for {symbol} — {e}")
        return None


def earnings_context(symbol: str) -> dict:
    try:
        return _parse_earnings(_call("get_earnings_results", {"symbol": symbol}))
    except Exception as e:  # noqa: BLE001
        print(f"signal_context: earnings fetch failed for {symbol} — {e}")
        return {"days_to_earnings": None, "days_since_earnings": None, "last_beat": None}


def enrich(signal: dict) -> dict:
    """Return a copy of `signal` with rsi + earnings fields added (best-effort; Nones on failure)."""
    ticker = (signal.get("ticker") or "").upper()
    if not ticker:
        return dict(signal)
    enriched = dict(signal)
    enriched["rsi"] = latest_rsi(ticker)
    enriched.update(earnings_context(ticker))
    return enriched
