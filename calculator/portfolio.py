# Risk multipliers — how much of your budget goes to each risk tier.
RISK_MULTIPLIERS = {
    "low":    1.00,
    "medium": 0.65,
    "high":   0.35,
}

# Highly recommended signals get 2x the capital of regular buys.
HIGHLY_RECOMMENDED_MULTIPLIER = 2.0

# Safety cap — no single stock gets more than this
# percentage of your total budget.
MAX_SINGLE_ALLOCATION = 0.40


def _compute_weight(rec: dict) -> float:
    """
    Calculates a raw weight for one BUY recommendation.
    Weight = confidence_score x risk_multiplier x highly_recommended_boost
    """
    confidence   = rec.get("confidence_score", 0.5)
    risk         = rec.get("risk_level", "medium")
    risk_mult    = RISK_MULTIPLIERS.get(risk, 0.5)
    hr_mult      = HIGHLY_RECOMMENDED_MULTIPLIER if rec.get("highly_recommended") else 1.0
    return confidence * risk_mult * hr_mult


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
    watches = [r for r in recommendations if r.get("direction") == "watch"]

    if not buys and not watches:
        print("All recommendations were 'avoid' — nothing to allocate.")
        return []

    results = []

    # ── BUY allocations ──────────────────────────────────────────
    if buys:
        for rec in buys:
            rec["_raw_weight"] = _compute_weight(rec)

        total_weight = sum(r["_raw_weight"] for r in buys)

        if total_weight > 0:
            for rec in buys:
                rec["_fraction"] = rec["_raw_weight"] / total_weight

            # Apply concentration cap and renormalize
            for _ in range(10):
                capped   = [r for r in buys if r["_fraction"] >= MAX_SINGLE_ALLOCATION]
                uncapped = [r for r in buys if r["_fraction"] <  MAX_SINGLE_ALLOCATION]

                if not capped:
                    break

                excess = 0.0
                for r in capped:
                    excess += r["_fraction"] - MAX_SINGLE_ALLOCATION
                    r["_fraction"] = MAX_SINGLE_ALLOCATION

                if uncapped:
                    uncapped_total = sum(r["_fraction"] for r in uncapped)
                    for r in uncapped:
                        r["_fraction"] += excess * (r["_fraction"] / uncapped_total)

            for rec in buys:
                dollar_amount = round(rec["_fraction"] * budget, 2)
                pct           = round(rec["_fraction"] * 100, 1)
                results.append(_build_result(rec, dollar_amount, pct))

    # ── WATCH — always $0, sorted after buys ─────────────────────
    for rec in watches:
        results.append(_build_result(rec, 0.0, 0.0))

    # Sort: highly recommended first, then regular buys by amount, then watches
    hr_buys      = [r for r in results if r["direction"] == "buy" and r.get("highly_recommended")]
    regular_buys = [r for r in results if r["direction"] == "buy" and not r.get("highly_recommended")]
    watches_out  = [r for r in results if r["direction"] == "watch"]

    hr_buys.sort(key=lambda x: x["dollar_amount"], reverse=True)
    regular_buys.sort(key=lambda x: x["dollar_amount"], reverse=True)

    return hr_buys + regular_buys + watches_out


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
        "bull_case":          rec.get("bull_case", ""),
        "bear_case":          rec.get("bear_case", ""),
        "exit_condition":     rec.get("exit_condition"),
        "catalyst_timing":    rec.get("catalyst_timing", ""),
        "risk_level":         rec.get("risk_level"),
        "confidence_score":   rec.get("confidence_score"),
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
    total_allocated = sum(a["dollar_amount"] for a in allocations)
    hr_count        = sum(1 for a in allocations if a.get("highly_recommended"))
    print(f"  {'TOTAL BUY':<18} ${total_allocated:>9,.2f}   ⭐ {hr_count} highly recommended")
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