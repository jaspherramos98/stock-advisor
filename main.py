from ingestion.rss              import fetch_rss_news
from ingestion.reddit           import fetch_reddit_news
from ingestion.finnhub_news     import fetch_finnhub_news
from ingestion.sec              import fetch_sec_filings
from validation.scorer          import run_scorer
from analysis.claude_analyst    import run_analysis
from calculator.portfolio       import calculate_allocations, print_allocation_table
import os

def run_ingestion_and_analysis(
    include_stocks: bool = True,
    include_etfs:   bool = False,
    include_crypto: bool = False,
) -> list[dict]:
    # ── MOCK INGESTION ─────────────────────────────────────────────
    # When MOCK_INGESTION=true, skip all news fetching and go straight
    # to Claude (which will also be mocked if MOCK_MODE=true).
    # Full pipeline completes in ~2 seconds instead of ~30.
    if os.getenv("MOCK_INGESTION", "false").lower() == "true":
        print("\n⚠️  MOCK INGESTION — all news fetching skipped.")
        from analysis.claude_analyst import run_analysis
        return run_analysis(
            [],
            include_stocks=include_stocks,
            include_etfs=include_etfs,
            include_crypto=include_crypto,
        )
    # ── END MOCK INGESTION ─────────────────────────────────────────


    """
    Runs layers 1 through 3 — ingestion, scoring, and Claude analysis.
    Asset type flags control which categories get fetched and analyzed.
    """
    print("\n==============================")
    print("  STOCK ADVISOR — PIPELINE    ")
    print("==============================\n")

    # --- Layer 1: Ingestion ---
    rss_articles  = fetch_rss_news()
    reddit_posts  = fetch_reddit_news()
    finnhub_items = fetch_finnhub_news(
        include_stocks=include_stocks,
        include_etfs=include_etfs,
        include_crypto=include_crypto,
    )
    sec_filings   = fetch_sec_filings()
    all_items     = rss_articles + reddit_posts + finnhub_items + sec_filings
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