"""
LLM credit ledger — a LOCAL approximation of the Anthropic prepaid balance.

WHY LOCAL: Anthropic exposes no API to read the remaining credit balance
(GET /v1/organizations/balance → 404; the Admin API reports spend, not balance). So Argus
tracks it itself: the user enters the current balance (from console.anthropic.com), and every
Claude call decrements it by its computed cost. When the remaining balance falls to the reserve
(default $0.50) Argus stops spending tokens — leaving leeway to top up before the next run.

This is an ESTIMATE: it drifts if the same API key is used outside Argus, and per-call cost is
computed from token counts × model price, not billed exactly. Re-set the balance from the console
whenever it drifts. State persists in credit_ledger.json (gitignored — no secrets, just numbers).

Pure/file-only + no network, so it's unit-testable and safe to import anywhere (unlike app.py).
"""
from __future__ import annotations

import json
import os
import threading

_LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credit_ledger.json")
_LOCK = threading.Lock()

DEFAULT_RESERVE = 0.50   # halt LLM spend when remaining balance falls to this ($)

# Per-model USD price per 1M tokens (input, output). Keep in sync with config models.
_PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}


def _read() -> dict:
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    with open(_LEDGER, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def get_state() -> dict:
    """{balance, reserve, spent, remaining}. balance = last user-set prepaid amount; spent =
    accumulated cost since it was set; remaining = balance − spent."""
    d = _read()
    balance = float(d.get("balance", 0.0))
    spent = float(d.get("spent", 0.0))
    reserve = float(d.get("reserve", DEFAULT_RESERVE))
    return {"balance": balance, "reserve": reserve, "spent": round(spent, 4),
            "remaining": round(balance - spent, 4)}


def set_balance(amount: float, reserve: float | None = None) -> dict:
    """Reset the ledger to a freshly-observed console balance (zeroes accumulated spend)."""
    with _LOCK:
        d = _read()
        d["balance"] = round(float(amount), 4)
        d["spent"] = 0.0
        if reserve is not None:
            d["reserve"] = round(float(reserve), 4)
        elif "reserve" not in d:
            d["reserve"] = DEFAULT_RESERVE
        _write(d)
    return get_state()


def set_reserve(reserve: float) -> dict:
    with _LOCK:
        d = _read()
        d["reserve"] = round(float(reserve), 4)
        _write(d)
    return get_state()


def cost_of(model: str, input_tokens: int, output_tokens: int,
            cache_read_tokens: int = 0) -> float:
    """USD cost of one call. Cache reads bill at ~0.1× input (Anthropic prompt-cache read rate)."""
    pin, pout = _PRICES.get(model, _PRICES["claude-sonnet-4-6"])
    return round(
        (input_tokens * pin + output_tokens * pout + cache_read_tokens * pin * 0.1) / 1_000_000,
        6,
    )


def record_cost(usd: float) -> None:
    """Add a call's cost to accumulated spend. Best-effort; never raises (never break a reply)."""
    if not usd or usd <= 0:
        return
    try:
        with _LOCK:
            d = _read()
            d["spent"] = round(float(d.get("spent", 0.0)) + float(usd), 6)
            _write(d)
    except Exception:  # noqa: BLE001
        pass


def remaining() -> float:
    return get_state()["remaining"]


def can_spend() -> bool:
    """True if there's headroom above the reserve to make another Claude call. When the ledger
    was never set (balance 0), returns True — the guard is opt-in, not a hard block on a fresh
    install; the user activates it by setting a balance."""
    st = get_state()
    if st["balance"] <= 0:
        return True
    return st["remaining"] > st["reserve"]
