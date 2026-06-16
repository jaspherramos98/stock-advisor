import sys

# Force UTF-8 stdout/stderr. Pipeline print()s contain non-ASCII symbols
# (→, —, ⭐, ⚠, ✓); on a Windows cp1252 console these raise UnicodeEncodeError
# and crash the pipeline mid-run (symptom: "0 recommendations"). errors="replace"
# guarantees a print can never crash the run even if reconfigure is unavailable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from analysis.claude_analyst import run_analysis
from ingestion.rss           import fetch_rss_news
from ingestion.reddit        import fetch_reddit_news
from ingestion.finnhub_news  import fetch_finnhub_news
from ingestion.sec           import fetch_sec_filings
from validation.scorer       import run_scorer
from calculator.portfolio    import calculate_allocations, print_allocation_table
import os
import concurrent.futures


def run_ingestion_and_analysis(
    include_stocks: bool = True,
    include_etfs:   bool = False,
    include_crypto: bool = False,
) -> list[dict]:
    """
    Runs layers 1 through 3 — ingestion, scoring, and Claude analysis.
    Asset type flags control which categories get fetched and analyzed.
    Ingestion sources run in parallel to minimize wall-clock time.
    """
    if os.getenv("MOCK_INGESTION", "false").lower() == "true":
        print("\n⚠️  MOCK INGESTION — all news fetching skipped.")
        return run_analysis(
            [],
            include_stocks=include_stocks,
            include_etfs=include_etfs,
            include_crypto=include_crypto,
        )

    print("\n==============================")
    print("  ARGUS — PIPELINE    ")
    print("==============================\n")

    # --- Layer 1: Parallel ingestion ---
    # All sources run simultaneously instead of sequentially.
    # Wall-clock time drops from ~40s to ~10s.
    all_items = []

    def fetch_rh_news():
        try:
            from ingestion.robinhood import is_available, fetch_robinhood_news
            if is_available():
                return fetch_robinhood_news()
        except Exception as e:
            print(f"Robinhood news fetch error: {e}")
        return []

    def fetch_finnhub():
        return fetch_finnhub_news(
            include_stocks=include_stocks,
            include_etfs=include_etfs,
            include_crypto=include_crypto,
        )

    tasks = {
        "rss":      fetch_rss_news,
        "reddit":   fetch_reddit_news,
        "finnhub":  fetch_finnhub,
        "sec":      fetch_sec_filings,
        "robinhood": fetch_rh_news,
    }

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                print(f"Ingestion error ({name}): {e}")
                results[name] = []

    all_items = (
        results.get("rss", []) +
        results.get("reddit", []) +
        results.get("finnhub", []) +
        results.get("sec", []) +
        results.get("robinhood", [])
    )

    print(f"\nTotal raw items: {len(all_items)}")

    # --- Layer 2: Scoring ---
    scored             = run_scorer(all_items)
    items_for_analysis = scored["high"] + scored["medium"]
    print(f"Items passing scorer: {len(items_for_analysis)}")

    # --- Layer 3: Claude Analysis ---
    recommendations = run_analysis(
        items_for_analysis,
        include_stocks=include_stocks,
        include_etfs=include_etfs,
        include_crypto=include_crypto,
    )
    if not recommendations:
        print("No recommendations returned.")
        return []
    print(f"Recommendations received: {len(recommendations)}")
    return recommendations


def run_pipeline(budget: float = None):
    recommendations = run_ingestion_and_analysis()

    if not recommendations:
        print("No recommendations returned. Exiting.")
        return

    if budget is None:
        try:
            budget = float(input("\nHow much are you willing to invest today? $"))
        except ValueError:
            print("Invalid amount. Using $1,000 as default.")
            budget = 1000.0

    allocations = calculate_allocations(recommendations, budget)
    print_allocation_table(allocations, budget)
    print("Pipeline complete.")
    return allocations


if __name__ == "__main__":
    run_pipeline()