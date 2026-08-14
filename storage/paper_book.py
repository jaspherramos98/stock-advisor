"""
Paper-trading book for the options agent — validate the strategies with ZERO money at risk.

A virtual account: starts with a cash balance (mirrors the real pilot by default), the agent
"buys" option contracts against it, each cycle marks open positions to the live option mark and
closes them per the exit policy, tracking realized + unrealized P&L. This is how you judge
whether the agent actually makes money BEFORE arming real orders.

Persisted to paper_book.json (repo root, gitignored). P&L math is pure + unit-tested; the live
marking happens in the agent loop (alerts/agentic_options.py, mode='paper').

A long option's P&L: (exit_price − entry_price) × 100 × contracts. entry/exit are per-share
option prices (e.g. 0.18); ×100 = dollars per contract.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_book.json")
DEFAULT_START = 25.0


def _load() -> dict:
    try:
        with open(_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("cash", DEFAULT_START)
    d.setdefault("start", d["cash"])
    d.setdefault("open", [])
    d.setdefault("closed", [])
    return d


def _save(d: dict) -> None:
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def reset(start: float = DEFAULT_START) -> dict:
    d = {"cash": round(float(start), 2), "start": round(float(start), 2), "open": [], "closed": []}
    _save(d)
    return d


def get_book() -> dict:
    return _load()


def cash() -> float:
    return round(_load()["cash"], 2)


def open_option_ids() -> set:
    return {p.get("option_id") for p in _load()["open"]}


def open_tickers() -> set:
    return {(p.get("ticker") or "").upper() for p in _load()["open"]}


def open_position(pos: dict) -> bool:
    """Buy a paper contract: deduct premium from cash, append to open. Rejects if unaffordable
    or a duplicate option_id. `pos` needs option_id/ticker/right/strike/expiration/strategy/qty/
    entry_price."""
    d = _load()
    oid = pos.get("option_id")
    if not oid or oid in {p.get("option_id") for p in d["open"]}:
        return False
    qty = int(pos.get("qty") or 1)
    entry = float(pos.get("entry_price") or 0)
    cost = round(entry * 100 * qty, 2)
    if cost <= 0 or cost > d["cash"]:
        return False
    d["cash"] = round(d["cash"] - cost, 2)
    d["open"].append({
        "option_id": oid, "ticker": (pos.get("ticker") or "").upper(),
        "right": pos.get("right"), "strike": pos.get("strike"),
        "expiration": pos.get("expiration"), "strategy": pos.get("strategy"),
        "qty": qty, "entry_price": entry, "cost": cost,
        "opened_at": _dt.datetime.now().isoformat(timespec="seconds"),
    })
    _save(d)
    return True


def close_position(option_id: str, exit_price: float, reason: str = "") -> dict | None:
    """Sell a paper contract at exit_price: add proceeds to cash, move to closed with realized
    P&L. Returns the closed record or None if not found."""
    d = _load()
    pos = next((p for p in d["open"] if p.get("option_id") == option_id), None)
    if not pos:
        return None
    d["open"] = [p for p in d["open"] if p.get("option_id") != option_id]
    qty, entry = int(pos["qty"]), float(pos["entry_price"])
    exit_price = float(exit_price)
    proceeds = round(exit_price * 100 * qty, 2)
    pnl = round((exit_price - entry) * 100 * qty, 2)
    d["cash"] = round(d["cash"] + proceeds, 2)
    rec = {**pos, "exit_price": exit_price, "reason": reason, "pnl": pnl,
           "pnl_pct": round((exit_price - entry) / entry * 100, 1) if entry else 0.0,
           "closed_at": _dt.datetime.now().isoformat(timespec="seconds")}
    d["closed"].append(rec)
    _save(d)
    return rec


def get_open() -> list[dict]:
    return _load()["open"]


def get_closed() -> list[dict]:
    return _load()["closed"]


# --- pure P&L helpers (unit-tested) ---------------------------------------------------

def position_pnl(entry_price: float, mark: float, qty: int) -> tuple[float, float]:
    """(unrealized $ , unrealized %) for a long option marked at `mark`."""
    if not entry_price:
        return (0.0, 0.0)
    return (round((mark - entry_price) * 100 * qty, 2),
            round((mark - entry_price) / entry_price * 100, 1))


def summarize(book: dict, marks: dict[str, float]) -> dict:
    """Portfolio stats given current option marks {option_id: mark}. equity = cash + open value;
    realized = sum closed pnl; win_rate over closed trades."""
    cash_ = book.get("cash", 0.0)
    open_val, unreal = 0.0, 0.0
    for p in book.get("open", []):
        mk = float(marks.get(p["option_id"], p["entry_price"]))
        open_val += mk * 100 * p["qty"]
        unreal += (mk - p["entry_price"]) * 100 * p["qty"]
    closed = book.get("closed", [])
    realized = sum(c.get("pnl", 0.0) for c in closed)
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    start = book.get("start", DEFAULT_START)
    equity = cash_ + open_val
    return {
        "cash": round(cash_, 2), "open_value": round(open_val, 2),
        "unrealized": round(unreal, 2), "realized": round(realized, 2),
        "equity": round(equity, 2), "start": round(start, 2),
        "total_pnl": round(equity - start, 2),
        "total_pnl_pct": round((equity - start) / start * 100, 1) if start else 0.0,
        "n_open": len(book.get("open", [])), "n_closed": len(closed),
        "win_rate": round(wins / len(closed) * 100, 0) if closed else None,
    }
