"""
Print the judge scorecard (judgment plan J4) — does the shadow judge beat the rules?

    venv\\Scripts\\python.exe scripts\\judge_review.py          # live + paper cycles (default)
    venv\\Scripts\\python.exe scripts\\judge_review.py --all    # include dry runs

Read-only: reads agent_decisions.jsonl + yfinance closes. See analysis/judge_scorecard.py.
"""
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from analysis.judge_scorecard import HORIZONS, scorecard
    sc = scorecard(mode=None if "--all" in sys.argv else "acting")
    print("== Judge scorecard (J4) ==")
    print(f"Judge calls: {sc['judge_calls']} | cost ${sc['judge_cost_usd']:.4f}\n")
    print("Entry verdicts → underlying return after:")
    for group, label in (("enter", "judge ENTER"), ("not_enter", "judge WAIT/SKIP")):
        cells = " | ".join(f"{h}d: n={s['n']}" + (f" avg {s['avg']:+}% win {s['win_rate']}%" if s["n"] else "")
                           for h, s in sc["entries"][group].items())
        print(f"  {label:16} {cells}")
    print("\nTrades the rules took → realized P&L:")
    for k, s in sc["taken"].items():
        print(f"  {k:20} n={s['n']}" + (f" avg {s['avg']:+}% win {s['win_rate']}%" if s["n"] else ""))
    print(f"\nHolding reviews → underlying move after {HORIZONS[0]}d:")
    for k, s in sc["reviews"].items():
        print(f"  {k:14} n={s['n']}" + (f" avg {s['avg']:+}%" if s["n"] else ""))
    print(f"\nConclusion: {sc['conclusion']['text']}")
    print(f"Note: {sc['caveat']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
