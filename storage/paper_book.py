"""
Paper-trading book — the agent's virtual account (zero money at risk).

The paper cycle runs the SAME agent code as live (alerts/agentic_options.run_options_agent) against
alerts/paper_broker.PaperBroker, which fills orders here at live prices instead of sending them to
Robinhood. Both legs: option contracts and (fractional) shares.

Book (paper_book.json, repo root, gitignored):
    {"version": 2, "cash", "start",
     "options": [{option_id, ticker, right, strike, expiration, qty, entry_price, cost, opened_at}],
     "shares":  {TICKER: {shares, avg_cost, cost, opened_at}},
     "closed":  [{leg, ticker, entry_price, exit_price, qty, pnl, pnl_pct, reason, opened_at, closed_at, ...}],
     "fills":   [{key, effect, qty, ts}]}          ← PDT day-trade counting, same rule as live

Option P&L: (exit − entry) × 100 × contracts (prices are per share, e.g. 0.18). Share P&L:
(exit − avg_cost) × shares. File I/O + pure math only; no network.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_book.json")
DEFAULT_START = 25.0


def _fresh(start: float) -> dict:
    start = round(float(start), 2)
    return {"version": 2, "cash": start, "start": start, "options": [], "shares": {}, "closed": [], "fills": []}


def _load() -> dict:
    try:
        with open(_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    if "version" not in d:                       # v1 (options-only): its open list becomes "options"
        d["options"] = d.pop("open", d.get("options", []))
    base = _fresh(d.get("cash", DEFAULT_START))
    base.update(d)
    base.setdefault("start", base["cash"])
    base["version"] = 2
    return base


def _save(d: dict) -> None:
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _fill(d: dict, key: str, effect: str, qty: float) -> None:
    d["fills"].append({"key": key, "effect": effect, "qty": qty,
                       "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})


def reset(start: float = DEFAULT_START) -> dict:
    d = _fresh(start)
    _save(d)
    return d


def get_book() -> dict:
    return _load()


def cash() -> float:
    return round(_load()["cash"], 2)


# --- options ------------------------------------------------------------------------------

def open_option(pos: dict) -> str | None:
    """Buy paper contracts at pos['entry_price']. Returns None on success, else why it was refused."""
    d = _load()
    oid = pos.get("option_id")
    if not oid:
        return "missing option_id"
    if oid in {p.get("option_id") for p in d["options"]}:
        return "already hold this contract"
    qty = int(pos.get("qty") or 1)
    entry = float(pos.get("entry_price") or 0)
    cost = round(entry * 100 * qty, 2)
    if cost <= 0 or cost > d["cash"]:
        return f"premium ${cost:.2f} exceeds paper cash ${d['cash']:.2f}"
    d["cash"] = round(d["cash"] - cost, 2)
    d["options"].append({"option_id": oid, "ticker": (pos.get("ticker") or "").upper(),
                         "right": pos.get("right"), "strike": pos.get("strike"),
                         "expiration": pos.get("expiration"), "strategy": pos.get("strategy"),
                         "qty": qty, "entry_price": entry, "cost": cost, "opened_at": _now()})
    _fill(d, f"opt:{oid}", "open", qty)
    _save(d)
    return None


def close_option(option_id: str, exit_price: float, reason: str = "") -> dict | None:
    """Sell a paper contract at exit_price. Returns the closed record, or None if not held."""
    d = _load()
    pos = next((p for p in d["options"] if p.get("option_id") == option_id), None)
    if not pos:
        return None
    d["options"] = [p for p in d["options"] if p.get("option_id") != option_id]
    qty, entry, exit_price = int(pos["qty"]), float(pos["entry_price"]), float(exit_price)
    d["cash"] = round(d["cash"] + exit_price * 100 * qty, 2)
    pnl, pct = position_pnl(entry, exit_price, qty)
    rec = {**pos, "leg": "option", "exit_price": exit_price, "reason": reason, "pnl": pnl, "pnl_pct": pct,
           "closed_at": _now()}
    d["closed"].append(rec)
    _fill(d, f"opt:{option_id}", "close", qty)
    _save(d)
    return rec


# --- shares -------------------------------------------------------------------------------

def buy_shares(ticker: str, dollars: float, price: float) -> str | None:
    """Dollar-based paper buy at `price` (fractional). Adds to an existing position at a blended
    average. Returns None on success, else why it was refused."""
    t = (ticker or "").upper()
    dollars, price = round(float(dollars or 0), 2), float(price or 0)
    if not t or dollars <= 0 or price <= 0:
        return "needs a ticker, a positive dollar amount and a live price"
    d = _load()
    if dollars > d["cash"]:
        return f"${dollars:.2f} exceeds paper cash ${d['cash']:.2f}"
    qty = dollars / price
    held = d["shares"].get(t) or {"shares": 0.0, "cost": 0.0, "opened_at": _now()}
    held["shares"] = held["shares"] + qty
    held["cost"] = round(held["cost"] + dollars, 2)
    held["avg_cost"] = held["cost"] / held["shares"]
    d["shares"][t] = held
    d["cash"] = round(d["cash"] - dollars, 2)
    _fill(d, f"eq:{t}", "open", qty)
    _save(d)
    return None


def sell_shares(ticker: str, qty: float, price: float, reason: str = "") -> dict | None:
    """Sell `qty` paper shares (capped at what's held) at `price`. Returns the closed record, or None
    if nothing is held."""
    t = (ticker or "").upper()
    d = _load()
    held = d["shares"].get(t)
    if not held or not price or price <= 0:
        return None
    qty = min(float(qty or 0), held["shares"])
    if qty <= 0:
        return None
    avg = held["avg_cost"]
    d["cash"] = round(d["cash"] + qty * price, 2)
    rest = held["shares"] - qty
    if rest <= 1e-9:
        d["shares"].pop(t)
    else:
        d["shares"][t] = {**held, "shares": rest, "cost": round(avg * rest, 2)}
    rec = {"leg": "stock", "ticker": t, "qty": qty, "entry_price": avg, "exit_price": float(price),
           "pnl": round((price - avg) * qty, 2), "pnl_pct": round((price - avg) / avg * 100, 1) if avg else 0.0,
           "reason": reason, "opened_at": held.get("opened_at"), "closed_at": _now()}
    d["closed"].append(rec)
    _fill(d, f"eq:{t}", "close", qty)
    _save(d)
    return rec


# --- pure helpers (unit-tested) -------------------------------------------------------------

def position_pnl(entry_price: float, mark: float, qty: int) -> tuple[float, float]:
    """(unrealized $, unrealized %) for a long option marked at `mark`."""
    if not entry_price:
        return (0.0, 0.0)
    return (round((mark - entry_price) * 100 * qty, 2),
            round((mark - entry_price) / entry_price * 100, 1))


def summarize(book: dict, option_marks: dict[str, float], share_prices: dict[str, float] | None = None) -> dict:
    """Account stats given option marks {option_id: mark} and share prices {TICKER: price} (a missing
    price marks at cost). equity = cash + open value; realized = Σ closed pnl; win rate over closed."""
    share_prices = share_prices or {}
    open_val = unreal = 0.0
    for p in book.get("options", []):
        mk = float(option_marks.get(p["option_id"], p["entry_price"]))
        open_val += mk * 100 * p["qty"]
        unreal += (mk - p["entry_price"]) * 100 * p["qty"]
    for t, h in (book.get("shares") or {}).items():
        px = float(share_prices.get(t) or h["avg_cost"])
        open_val += px * h["shares"]
        unreal += (px - h["avg_cost"]) * h["shares"]
    closed = book.get("closed", [])
    wins = sum(1 for c in closed if c.get("pnl", 0) > 0)
    cash_, start = book.get("cash", 0.0), book.get("start", DEFAULT_START)
    equity = cash_ + open_val
    return {"cash": round(cash_, 2), "open_value": round(open_val, 2), "unrealized": round(unreal, 2),
            "realized": round(sum(c.get("pnl", 0.0) for c in closed), 2), "equity": round(equity, 2),
            "start": round(start, 2), "total_pnl": round(equity - start, 2),
            "total_pnl_pct": round((equity - start) / start * 100, 1) if start else 0.0,
            "n_open": len(book.get("options", [])) + len(book.get("shares") or {}), "n_closed": len(closed),
            "win_rate": round(wins / len(closed) * 100, 0) if closed else None}
