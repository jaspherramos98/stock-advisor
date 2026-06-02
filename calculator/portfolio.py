# Risk multipliers — how much of your budget goes to each risk tier.
# These are conservative by design. You can tune them over time.
RISK_MULTIPLIERS = {
    "low":    1.00,
    "medium": 0.65,
    "high":   0.35,
}

# Watch items get less capital than buy items —
# you're interested but not fully committing.
DIRECTION_MULTIPLIERS = {
    "buy":   1.00,
    "watch": 0.50,
    "avoid": 0.00,
}

# Safety cap — no single stock gets more than this
# percentage of your total budget, no matter how strong
# the signal is. Prevents over-concentration.
MAX_SINGLE_ALLOCATION = 0.35


def _compute_weight(rec: dict) -> float:
    """
    Calculates a raw weight for one recommendation.
    Weight = confidence_score x risk_multiplier x direction_multiplier
    """
    confidence     = rec.get("confidence_score", 0.5)
    risk           = rec.get("risk_level", "medium")
    direction      = rec.get("direction", "watch")
    risk_mult      = RISK_MULTIPLIERS.get(risk, 0.5)
    direction_mult = DIRECTION_MULTIPLIERS.get(direction, 0.0)
    return confidence * risk_mult * direction_mult


def calculate_allocations(recommendations: list[dict], budget: float) -> list[dict]:
    """
    Takes Claude's recommendations and a user budget, and returns
    each stock with a dollar amount and percentage allocation.
    """
    if not recommendations or budget <= 0:
        print("No recommendations or zero budget — nothing to allocate.")
        return []

    # Step 1 — filter out avoids
    actionable = [
        r for r in recommendations
        if r.get("direction") in ("buy", "watch")
    ]

    if not actionable:
        print("All recommendations were 'avoid' — nothing to allocate.")
        return []

    # Step 2 — compute raw weights
    for rec in actionable:
        rec["_raw_weight"] = _compute_weight(rec)

    total_weight = sum(r["_raw_weight"] for r in actionable)

    if total_weight == 0:
        print("All weights are zero — cannot allocate.")
        return []

    # Step 3 — normalize to fractions that sum to 1.0
    for rec in actionable:
        rec["_fraction"] = rec["_raw_weight"] / total_weight

    # Step 4 — apply the concentration cap and renormalize
    for _ in range(10):
        capped   = [r for r in actionable if r["_fraction"] >= MAX_SINGLE_ALLOCATION]
        uncapped = [r for r in actionable if r["_fraction"] <  MAX_SINGLE_ALLOCATION]

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

    # Step 5 — multiply by budget and build output
    results = []
    for rec in actionable:
        dollar_amount = round(rec["_fraction"] * budget, 2)
        pct           = round(rec["_fraction"] * 100, 1)

        results.append({
            "ticker":           rec.get("ticker", "???"),
            "company_name":     rec.get("company_name", "Unknown"),
            "direction":        rec.get("direction"),
            "dollar_amount":    dollar_amount,
            "percentage":       pct,
            "entry_rationale":  rec.get("entry_rationale"),
            "exit_condition":   rec.get("exit_condition"),
            "risk_level":       rec.get("risk_level"),
            "confidence_score": rec.get("confidence_score"),
            "flagged":          rec.get("flagged", False),
            "source_title":     rec.get("source_title", ""),
        })

    results.sort(key=lambda x: x["dollar_amount"], reverse=True)
    return results


def print_allocation_table(allocations: list[dict], budget: float):
    """
    Prints a clean summary table of how the budget is distributed.
    """
    print(f"\n{'='*58}")
    print(f"  Portfolio allocation — ${budget:,.2f} budget")
    print(f"{'='*58}")
    print(f"  {'Ticker':<8} {'Direction':<8} {'Amount':>10} {'Pct':>6} {'Risk':<8}")
    print(f"  {'-'*54}")

    for a in allocations:
        flag   = " ⚠" if a["flagged"] else ""
        ticker = a["ticker"] + flag
        print(f"  {ticker:<10} {a['direction']:<8} ${a['dollar_amount']:>9,.2f} {a['percentage']:>5.1f}% {a['risk_level']:<8}")

    print(f"  {'-'*54}")
    total_allocated = sum(a["dollar_amount"] for a in allocations)
    print(f"  {'TOTAL':<18} ${total_allocated:>9,.2f} 100.0%")
    print(f"{'='*58}\n")

    flagged = [a for a in allocations if a["flagged"]]
    if flagged:
        print(f"  ⚠ Flagged stocks have unverified sources — treat with extra caution:")
        for f in flagged:
            print(f"    {f['ticker']} — {f['source_title'][:60]}")
        print()


if __name__ == "__main__":
    test_recs = [
        {"ticker": "TSLA",  "company_name": "Tesla",          "direction": "buy",   "confidence_score": 0.78, "risk_level": "medium", "flagged": False, "entry_rationale": "EV market share leader.",    "exit_condition": "12% gain", "source_title": "Tesla reclaims No.1 EV spot"},
        {"ticker": "AMZN",  "company_name": "Amazon",         "direction": "watch", "confidence_score": 0.78, "risk_level": "low",    "flagged": False, "entry_rationale": "UBS Buy rating reiterated.", "exit_condition": "8% gain",  "source_title": "UBS reiterates AMZN Buy"},
        {"ticker": "HPE",   "company_name": "HP Enterprise",  "direction": "watch", "confidence_score": 0.70, "risk_level": "medium", "flagged": False, "entry_rationale": "Earnings beat on AI.",       "exit_condition": "5% gain",  "source_title": "HPE beats earnings"},
        {"ticker": "GOOGL", "company_name": "Alphabet",       "direction": "avoid", "confidence_score": 0.58, "risk_level": "high",   "flagged": False, "entry_rationale": "Dilution risk.",             "exit_condition": "n/a",      "source_title": "Alphabet raises $80B"},
    ]

    budget      = 1000.00
    allocations = calculate_allocations(test_recs, budget)
    print_allocation_table(allocations, budget)