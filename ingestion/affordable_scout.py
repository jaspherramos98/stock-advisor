"""
Affordable-universe scout (Phase 2) — keep the agent active when Argus's pipeline signals are
all too expensive for the pilot's buying power (the common case at $25: the pipeline surfaces
large-caps whose options run $200-$1000/contract).

Deterministic technical screen (NO LLM, no tokens): over a preset of liquid, LOW-PRICED
underlyings (where sub-$25 options actually exist), derive a directional lean + a modest
conviction from RSI so the EXISTING strategies act on them.

ONLY statistical extremes fire — those have a real (mean-reversion) basis:
  - RSI <= 35  → buy   (oversold bounce)   → mean_reversion (call)
  - RSI >= 68  → short (overbought fade)   → mean_reversion (put)
  - 36..67     → skip  (no basis)

The mid-range "momentum" buy (RSI 52-67 → call) was REMOVED: buying a call just because RSI sits
mid-range is noise with no edge — the underlying rarely moves enough to clear the option's own
premium + theta + spread, so those entries systematically bled the pilot. The scout has no news
feed, so it cannot gate on a real catalyst; restricting to extremes is the honest equivalent.

These are still TECHNICAL GAMBLES, not fundamental edges — appropriate only for the disposable
pilot, ranked BELOW real pipeline/chat signals (they fill leftover budget). The affordability
filter in options_data.select_contract still decides what the account can actually buy.
"""
from __future__ import annotations

# Liquid, optionable, VOLATILE underlyings, ordered cheapest-first so the affordability filter
# (and the tightest option spreads) get priority on a small account. The scout scans in order and
# stops once it has max_names qualifying signals, so the front of the list matters most.
AFFORDABLE_UNIVERSE = [
    # cheap + liquid (sub-$25 stocks, cheapest contracts, tightest spreads)
    "F", "SOFI", "NIO", "SNAP", "PLUG", "RIVN", "CCL", "AAL", "T", "WBD", "BAC", "NOK",
    "SIRI", "LCID", "CHPT",
    # high-beta biotech (catalyst swings — the MRNA-style pops; cheaper-option names first)
    "NTLA", "BEAM", "SRPT", "CRSP", "MRNA", "BNTX",
    # AI / quantum / space high-beta (big movers, cheap-ish contracts)
    "SOUN", "BBAI", "IONQ", "RGTI", "ACHR", "LUNR", "RKLB",
    # momentum / meme liquid
    "AMC", "GME", "DKNG", "AFRM", "HOOD", "PLTR",
    # crypto miners (track BTC beta)
    "MARA", "RIOT", "CLSK",
]


def _lean_from_rsi(rsi: float | None) -> tuple[str, int] | None:
    """Map an RSI reading to (direction, conviction), or None to skip. Only statistical extremes
    fire (mean_reversion needs conviction>=60 + the RSI extreme). The mid-range momentum buy was
    removed — no edge, it just paid premium/theta/spread on noise."""
    if rsi is None:
        return None
    if rsi <= 35:
        return ("buy", 65)     # oversold → mean_reversion call
    if rsi >= 68:
        return ("short", 65)   # overbought → mean_reversion put
    return None                # 36..67 → no basis, skip


def scout_signals(max_names: int = 6) -> list[dict]:
    """Scan the affordable universe and emit up to max_names technical buy/short signals
    (source='scout'), each carrying the RSI it was derived from so the strategies can reuse it."""
    from ingestion.signal_context import latest_rsi
    out: list[dict] = []
    for ticker in AFFORDABLE_UNIVERSE:
        if len(out) >= max_names:
            break
        rsi = latest_rsi(ticker)
        lean = _lean_from_rsi(rsi)
        if not lean:
            continue
        direction, conviction = lean
        out.append({"ticker": ticker, "direction": direction, "conviction": conviction,
                    "rsi": rsi, "source": "scout"})
    return out
