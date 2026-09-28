"""
Entry judge (judgment plan J2) — a Sonnet call that reviews each candidate trade against its fresh
briefing (analysis/agent_context.render) and answers ENTER / WAIT / SKIP plus how much of the planned
size to use.

Authority is deliberately one-sided: the judge can only REDUCE risk (skip, wait, shrink to a fraction
of the rules' size). It can never enlarge a trade or loosen a limit — parse_verdict clamps
size_multiplier to [0, 1], and every hard guard (trading_guards, caps, PDT, broker review) still runs
after it. Until the J4 shadow review proves it helps, config.AGENT_JUDGE = "shadow": the verdict is
logged next to the mechanical decision and changes NOTHING.

Structured output via a forced tool call (the model must fill `record_verdict`'s schema), then
validated + clamped here; a malformed or failed call becomes an explicit "skip" with an error tag
(in shadow that's just a logged verdict, never a trade change). Cost goes into llm_budget; the judge
is skipped when the credit ledger is at its reserve.
"""
from __future__ import annotations

import os

import config

DECISIONS = ("enter", "wait", "skip")
INSTRUMENTS = ("shares", "option", "either")
MAX_TOKENS = 700

SYSTEM = """You are the risk-focused trade reviewer for Argus's autonomous agent. It trades a SMALL, \
disposable Robinhood account (tens of dollars) in US stocks and single-leg long options. A 6:30 AM news \
pipeline produced the candidate; hours may have passed. Your job: decide whether THIS trade still makes \
sense RIGHT NOW, using ONLY the briefing below.

Hard rules:
- Use only facts in the briefing. Never invent news, prices, earnings dates, or company facts from memory; \
if something is n/a, treat it as unknown and be more cautious, not less.
- You can only REDUCE risk: enter at up to the planned size (size_multiplier 1.0), shrink it (0.25-0.75), \
WAIT for a better entry, or SKIP. You never enlarge a trade.
- Default to skepticism. Most candidates should be WAIT or SKIP; ENTER needs the case to hold up now.

Weigh especially:
1. Priced in? A big move since the pipeline's price, or price pinned at resistance, means the edge may be gone.
2. Confirmation: breakout triggers need volume (volume pace >= ~1.2x); "close above" conditions are not met \
intraday. Pullback entries need price holding support, not slicing through it.
3. Reward:risk: < 2 is a weak setup; the stop must sit beyond normal noise (ATR stop).
4. Events: earnings within ~5 days = binary gap risk (fine only if the thesis IS the earnings). Fresh \
headlines can confirm or break the thesis - say which.
5. Regime: don't fight a risk-off tape with fresh longs.
6. Book: the same sector already held = the same bet twice; shrink or skip.
7. Instrument: options add leverage and time decay - prefer shares unless the catalyst is sharp and near-term.
8. The agent's own record: if this source/strategy has been losing, demand more.

Record your verdict with the record_verdict tool: thesis (why it works, 1-2 sentences), invalidation (the \
specific price or event that proves it wrong), and up to 3 key risks."""

TOOL = {
    "name": "record_verdict",
    "description": "Record the verdict on the candidate trade.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "size_multiplier": {"type": "number", "minimum": 0, "maximum": 1,
                                "description": "Fraction of the planned size (1.0 = as planned). 0 unless enter."},
            "instrument": {"type": "string", "enum": list(INSTRUMENTS)},
            "stop_pct": {"type": ["number", "null"], "description": "Suggested stop % below entry, or null."},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "thesis": {"type": "string"},
            "invalidation": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        },
        "required": ["decision", "size_multiplier", "instrument", "confidence", "thesis", "invalidation"],
    },
}


def mode() -> str:
    """'off' | 'shadow'. (J5 will add 'binding' — until then anything else behaves as 'off'.)"""
    m = str(getattr(config, "AGENT_JUDGE", "off")).lower()
    return m if m in ("off", "shadow") else "off"


def _clip(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def parse_verdict(raw: dict | None) -> dict:
    """Pure: validate + clamp a tool-call payload. Anything malformed → an explicit skip with `error`."""
    if not isinstance(raw, dict) or raw.get("decision") not in DECISIONS:
        return {"decision": "skip", "size_multiplier": 0.0, "error": "malformed verdict", "raw": raw}
    decision = raw["decision"]
    size = _clip(raw.get("size_multiplier"), 0.0, 1.0, 0.0) if decision == "enter" else 0.0
    if decision == "enter" and size == 0.0:
        decision = "skip"                                   # "enter at zero size" is a skip
    stop = _clip(raw.get("stop_pct"), 0.5, 50.0, None) if raw.get("stop_pct") is not None else None
    return {
        "decision": decision,
        "size_multiplier": round(size, 2),
        "instrument": raw.get("instrument") if raw.get("instrument") in INSTRUMENTS else "either",
        "stop_pct": round(stop, 2) if stop is not None else None,
        "confidence": int(_clip(raw.get("confidence"), 0, 100, 0)),
        "thesis": str(raw.get("thesis") or "")[:400],
        "invalidation": str(raw.get("invalidation") or "")[:240],
        "risks": [str(r)[:160] for r in (raw.get("risks") or [])][:3],
    }


def plan_text(leg: str | None, stock_dollars: float | None) -> str:
    """What the mechanical rules intend for this candidate — the judge reviews THIS plan."""
    if leg == "option":
        return ("RULES' PLAN: buy a long option via the first matching playbook (catalyst/momentum/"
                "earnings/mean-reversion), sized by the playbook; falls back to shares if no affordable contract.")
    if leg == "stock":
        return f"RULES' PLAN: buy ${stock_dollars or 0:.2f} of shares (dollar-based market order)."
    return "RULES' PLAN: none."


def _call_model(briefing: str, plan: str) -> tuple[dict | None, dict]:
    """One forced-tool Sonnet call. Returns (tool input or None, usage dict). Raises on API errors."""
    import anthropic
    from dotenv import load_dotenv
    load_dotenv()   # the scheduled runner must not depend on some other import having loaded .env first
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model=config.CLAUDE_MODEL, max_tokens=MAX_TOKENS, temperature=0, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "record_verdict"},
        messages=[{"role": "user", "content": f"{briefing}\n\n{plan}"}],
    )
    usage = {"input_tokens": getattr(msg.usage, "input_tokens", 0),
             "output_tokens": getattr(msg.usage, "output_tokens", 0)}
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_verdict":
            return block.input, usage
    return None, usage


def judge_entry(ctx: dict, leg: str | None, stock_dollars: float | None = None) -> dict | None:
    """Verdict for one candidate, or None when the judge is off / has no briefing / no credit.
    Never raises: an API failure returns a skip verdict tagged with the error."""
    if mode() == "off" or not ctx or "signal" not in ctx:
        return None
    import llm_budget
    from analysis.agent_context import render
    if not llm_budget.can_spend():
        return {"decision": "skip", "size_multiplier": 0.0, "error": "LLM credit at reserve — judge not run"}
    try:
        raw, usage = _call_model(render(ctx), plan_text(leg, stock_dollars))
    except Exception as e:  # noqa: BLE001 — a judge failure must never break the cycle
        return {"decision": "skip", "size_multiplier": 0.0, "error": f"judge call failed: {e}"}
    cost = llm_budget.cost_of(config.CLAUDE_MODEL, usage["input_tokens"], usage["output_tokens"])
    llm_budget.record_cost(cost)
    return {**parse_verdict(raw), "model": config.CLAUDE_MODEL, "cost_usd": cost, "mode": mode()}
