# Pyramid budget tiers (fraction of the long budget per RISK tier). This is the core
# allocation shape: the risk tier picks the POOL, conviction sizes WITHIN it.
#   top  (high risk / high reward)        → small satellite
#   mid  (medium risk / medium-high reward) → the CORE, most of the budget
#   base (low risk / low reward)          → ballast
# Reward comes for free: the analyst already scales exit targets with risk (HR 12-20%,
# regular 6-10%), so "medium risk = medium-high reward" falls out of the tier mapping.
# Must sum to 1.0.
PYRAMID_TIERS = {
    "high":   0.20,   # top
    "medium": 0.55,   # core
    "low":    0.25,   # base
}

# Empty tier (no recs that day) is NOT redistributed — its share is held as CASH.
# Don't force capital into a tier that has no real ideas today.

# Risk multipliers — SHORTS-ONLY sizing now. The long book is sized by the pyramid
# tiers above; shorts are a small separate margin sleeve and keep the older
# conviction × risk × HR weighting via _compute_weight.
RISK_MULTIPLIERS = {
    "low":    1.00,
    "medium": 0.65,
    "high":   0.35,
}

# Highly recommended signals get 2x the capital of regular buys (applied WITHIN a
# pyramid pool — HR sizes you bigger inside the tier, it doesn't jump tiers).
HIGHLY_RECOMMENDED_MULTIPLIER = 2.0

# Safety cap — no single stock gets more than this
# percentage of your total budget.
MAX_SINGLE_ALLOCATION = 0.40

# Shorts are a separate, capped sleeve so the book stays long-biased. This is the
# max TOTAL short exposure as a fraction of budget (shorts use margin, not your cash,
# so they don't reduce the long budget — they're shown as separate "short exposure").
MAX_SHORT_EXPOSURE = 0.30

# Below this budget we don't allocate dollars — the budget can still be set to any
# value >= 0 (manual entry), but recommendations just show with $0 until it reaches
# this floor. Keeps tiny/placeholder budgets from producing meaningless sub-dollar buys.
MIN_ALLOCATION_BUDGET = 10.0


def _conviction_base(rec: dict) -> float:
    """
    Conviction (0-100) as a 0-1 fraction, driving position size. Back-compat: recs
    from before the conviction field fall back to confidence_score × 100.
    """
    conviction = rec.get("conviction")
    if conviction is None:
        conviction = rec.get("confidence_score", 0.5) * 100  # back-compat for old cached recs
    return max(0.0, min(float(conviction), 100.0)) / 100.0


def _compute_weight(rec: dict) -> float:
    """
    Raw weight for one SHORT recommendation (shorts sleeve only).
    Weight = (conviction/100) × risk_multiplier × highly_recommended_boost.
    (Long buys are sized by the pyramid tiers via _pool_weight, not this.)
    """
    risk      = rec.get("risk_level", "medium")
    risk_mult = RISK_MULTIPLIERS.get(risk, 0.5)
    hr_mult   = HIGHLY_RECOMMENDED_MULTIPLIER if rec.get("highly_recommended") else 1.0
    return _conviction_base(rec) * risk_mult * hr_mult


def _tier_of(rec: dict) -> str:
    """Map a buy's risk_level to its pyramid tier. Unknown/missing → 'medium' (core)."""
    r = (rec.get("risk_level") or "medium").lower()
    return r if r in PYRAMID_TIERS else "medium"


def _pool_weight(rec: dict) -> float:
    """
    Intra-pool weight for a buy: conviction (edge) × HR boost. Risk is NOT a factor
    here — the risk tier already chose the pool; this only ranks names within it.
    """
    hr_mult = HIGHLY_RECOMMENDED_MULTIPLIER if rec.get("highly_recommended") else 1.0
    return _conviction_base(rec) * hr_mult


def _allocate_pool(recs: list[dict], pool_dollars: float, budget: float) -> list[tuple]:
    """
    Distribute one tier's dollar pool across its recs by pool weight, honoring the
    global single-name cap (MAX_SINGLE_ALLOCATION of the TOTAL budget). Excess from a
    capped name spills to uncapped names in the SAME tier; if every name hits the cap,
    the remainder stays uninvested (held as cash — consistent with empty-tier handling).
    Returns [(rec, dollars), ...]; never exceeds pool_dollars.
    """
    if not recs or pool_dollars <= 0:
        return []

    for r in recs:
        r["_pw"] = _pool_weight(r)
    total = sum(r["_pw"] for r in recs)
    if total <= 0:
        return []

    # Cap expressed as a fraction of THIS pool (derived from the global budget cap).
    cap_frac = min(1.0, (MAX_SINGLE_ALLOCATION * budget) / pool_dollars)
    for r in recs:
        r["_pf"] = r["_pw"] / total

    for _ in range(10):
        capped   = [r for r in recs if r["_pf"] >= cap_frac]
        uncapped = [r for r in recs if r["_pf"] <  cap_frac]
        if not capped or not uncapped:
            break
        excess = 0.0
        for r in capped:
            excess += r["_pf"] - cap_frac
            r["_pf"] = cap_frac
        unc_total = sum(r["_pf"] for r in uncapped)
        if unc_total <= 0:
            break
        for r in uncapped:
            r["_pf"] += excess * (r["_pf"] / unc_total)

    return [(r, round(min(r["_pf"], cap_frac) * pool_dollars, 2)) for r in recs]


def calculate_allocations(recommendations: list[dict], budget: float) -> list[dict]:
    """
    Takes Claude's recommendations and a user budget.

    - BUY signals (highly recommended) → 2x weighted allocation
    - BUY signals (regular)            → standard weighted allocation
    - WATCH signals                    → appear with $0 / 0%
    - AVOID signals                    → filtered out entirely
    """
    if not recommendations or budget <= 0:
        print("No recommendations or zero budget — nothing to allocate.")
        return []

    buys    = [r for r in recommendations if r.get("direction") == "buy"]
    shorts  = [r for r in recommendations if r.get("direction") == "short"]
    watches = [r for r in recommendations if r.get("direction") == "watch"]

    if not buys and not shorts and not watches:
        print("All recommendations were 'avoid' — nothing to allocate.")
        return []

    # Below the allocation floor: keep the budget but allocate no dollars. Surface every
    # rec at $0 (still sorted) so the user sees the analysis without sub-dollar buys.
    if budget < MIN_ALLOCATION_BUDGET:
        print(f"Budget ${budget:,.2f} is below the ${MIN_ALLOCATION_BUDGET:,.0f} allocation floor — "
              f"showing recommendations with $0 allocation.")
        zero = [_build_result(rec, 0.0, 0.0) for rec in (buys + shorts + watches)]
        hr_b = [r for r in zero if r["direction"] == "buy" and r.get("highly_recommended")]
        rb   = [r for r in zero if r["direction"] == "buy" and not r.get("highly_recommended")]
        sh   = [r for r in zero if r["direction"] == "short"]
        wt   = [r for r in zero if r["direction"] == "watch"]
        return hr_b + rb + sh + wt

    results = []

    # ── BUY allocations — PYRAMID by risk tier ───────────────────
    # Risk tier picks the budget POOL (top 20% / core 55% / base 25%); conviction
    # sizes within it. An empty tier is held as CASH, not redistributed — so total
    # invested < budget on days a tier has no ideas (that's intentional ballast).
    if buys:
        for tier, tier_pct in PYRAMID_TIERS.items():
            tier_recs = [r for r in buys if _tier_of(r) == tier]
            if not tier_recs:
                continue  # empty tier → held as cash
            pool = tier_pct * budget
            for rec, dollars in _allocate_pool(tier_recs, pool, budget):
                pct = round(dollars / budget * 100, 1) if budget else 0.0
                results.append(_build_result(rec, dollars, pct))

    # ── SHORT allocations — separate sleeve, capped at MAX_SHORT_EXPOSURE ──
    # Uses margin, not the long cash budget, so it does NOT reduce buy allocations.
    # dollar_amount here = short exposure $ (how much to short).
    if shorts:
        short_pool = MAX_SHORT_EXPOSURE * budget
        for rec in shorts:
            rec["_raw_weight"] = _compute_weight(rec)
        total_short_weight = sum(r["_raw_weight"] for r in shorts)
        if total_short_weight > 0:
            for rec in shorts:
                frac          = rec["_raw_weight"] / total_short_weight
                dollar_amount = round(min(frac * short_pool, MAX_SINGLE_ALLOCATION * budget), 2)
                pct           = round(dollar_amount / budget * 100, 1) if budget else 0.0
                results.append(_build_result(rec, dollar_amount, pct))

    # ── WATCH — always $0, sorted after buys/shorts ──────────────
    for rec in watches:
        results.append(_build_result(rec, 0.0, 0.0))

    # Sort: HR buys → regular buys → shorts (by exposure) → watches
    hr_buys      = [r for r in results if r["direction"] == "buy" and r.get("highly_recommended")]
    regular_buys = [r for r in results if r["direction"] == "buy" and not r.get("highly_recommended")]
    shorts_out   = [r for r in results if r["direction"] == "short"]
    watches_out  = [r for r in results if r["direction"] == "watch"]

    hr_buys.sort(key=lambda x: x["dollar_amount"], reverse=True)
    regular_buys.sort(key=lambda x: x["dollar_amount"], reverse=True)
    shorts_out.sort(key=lambda x: x["dollar_amount"], reverse=True)

    return hr_buys + regular_buys + shorts_out + watches_out


def _build_result(rec: dict, dollar_amount: float, pct: float) -> dict:
    """Builds a clean output dict for one recommendation."""
    return {
        "ticker":             rec.get("ticker", "???"),
        "company_name":       rec.get("company_name", "Unknown"),
        "direction":          rec.get("direction"),
        "asset_type":         rec.get("asset_type", "stock"),
        "dollar_amount":      dollar_amount,
        "percentage":         pct,
        "entry_rationale":    rec.get("entry_rationale"),
        "entry_trigger":      rec.get("entry_trigger", ""),
        "exit_condition":     rec.get("exit_condition"),
        "risk_level":         rec.get("risk_level"),
        "confidence_score":   rec.get("confidence_score"),
        "conviction":         rec.get("conviction"),
        "flagged":            rec.get("flagged", False),
        "source_title":       rec.get("source_title", ""),
        "highly_recommended": rec.get("highly_recommended", False),
    }


def print_allocation_table(allocations: list[dict], budget: float):
    """Prints a clean summary table of how the budget is distributed."""
    print(f"\n{'='*68}")
    print(f"  Portfolio allocation — ${budget:,.2f} budget")
    print(f"{'='*68}")
    print(f"  {'Ticker':<8} {'Direction':<8} {'Amount':>10} {'Pct':>6} {'Risk':<8} {'HR':>4}")
    print(f"  {'-'*64}")

    for a in allocations:
        flag   = " ⚠" if a["flagged"] else ""
        ticker = a["ticker"] + flag
        amount = f"${a['dollar_amount']:>9,.2f}" if a["dollar_amount"] > 0 else "      watch"
        pct    = f"{a['percentage']:>5.1f}%" if a["percentage"] > 0 else "   —"
        hr     = "⭐" if a.get("highly_recommended") else ""
        print(f"  {ticker:<10} {a['direction']:<8} {amount} {pct} {a['risk_level']:<8} {hr}")

    print(f"  {'-'*64}")
    total_buy = sum(a["dollar_amount"] for a in allocations if a["direction"] == "buy")
    hr_count  = sum(1 for a in allocations if a.get("highly_recommended"))
    cash_held = max(0.0, budget - total_buy)
    print(f"  {'TOTAL BUY':<18} ${total_buy:>9,.2f}   ⭐ {hr_count} highly recommended")
    print(f"  {'HELD AS CASH':<18} ${cash_held:>9,.2f}   (empty pyramid tiers + capped names)")
    print(f"{'='*68}\n")


if __name__ == "__main__":
    test_recs = [
        {
            "ticker": "AAPL", "company_name": "Apple Inc.",
            "direction": "buy", "confidence_score": 0.78,
            "risk_level": "low", "flagged": False,
            "asset_type": "stock", "highly_recommended": True,
            "entry_rationale": "Beat earnings by 18%, raised guidance.",
            "exit_condition": "target 15% gain, stop loss at 5%",
            "source_title": "Apple Q2 earnings massive beat",
        },
        {
            "ticker": "NVDA", "company_name": "NVIDIA Corp.",
            "direction": "buy", "confidence_score": 0.72,
            "risk_level": "medium", "flagged": False,
            "asset_type": "stock", "highly_recommended": False,
            "entry_rationale": "Data center demand accelerating.",
            "exit_condition": "target 10% gain, stop loss at 4%",
            "source_title": "NVDA data center revenue surges",
        },
        {
            "ticker": "TSLA", "company_name": "Tesla Inc.",
            "direction": "watch", "confidence_score": 0.58,
            "risk_level": "medium", "flagged": False,
            "asset_type": "stock", "highly_recommended": False,
            "entry_rationale": "EV recovery signals but unclear timing.",
            "exit_condition": "post-earnings or 2 weeks, stop loss at 5%",
            "source_title": "Tesla Q2 delivery numbers",
        },
    ]

    budget      = 1000.00
    allocations = calculate_allocations(test_recs, budget)
    print_allocation_table(allocations, budget)