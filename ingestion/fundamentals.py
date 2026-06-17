"""
Fact-based company fundamentals from Yahoo Finance (via yfinance).

Valuation, growth, margins, and balance-sheet health — reported numbers, not
opinion — used by the analyst as a quality check ("is this a real company or a
pump?"). Missing metrics come back as None; nothing here ever raises.
"""

# Process-lifetime cache. Fundamentals change slowly (quarterly), so re-fetching
# within a session is wasteful — one lookup per ticker per run of the app.
_CACHE: dict[str, dict | None] = {}


def _pct(x):
    """yfinance reports margins/growth as decimals (0.23 = 23%). Return % or None."""
    try:
        return round(float(x) * 100, 1)
    except (TypeError, ValueError):
        return None


def _num(x, ndigits=2):
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return None


def _extract_fundamentals(info: dict) -> dict:
    """
    Pulls a clean, fact-based subset from a yfinance `.info` dict. Missing values
    are None. Separated from the network call so it can be unit-tested.
    """
    if not info:
        return {}
    return {
        "sector":              info.get("sector"),
        "industry":            info.get("industry"),
        "market_cap":          _num(info.get("marketCap"), 0),
        "trailing_pe":         _num(info.get("trailingPE")),
        "forward_pe":          _num(info.get("forwardPE")),
        "price_to_book":       _num(info.get("priceToBook")),
        "profit_margin_pct":   _pct(info.get("profitMargins")),
        "revenue_growth_pct":  _pct(info.get("revenueGrowth")),
        "earnings_growth_pct": _pct(info.get("earningsGrowth")),
        "debt_to_equity":      _num(info.get("debtToEquity")),
        "free_cash_flow":      _num(info.get("freeCashflow"), 0),
    }


def fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """
    Returns {ticker: fundamentals_dict} for the given stock tickers. Tickers that
    error out (or have no data — e.g. crypto, obscure symbols) map to None.
    Cached per process so repeated lookups in a session are free.
    """
    import yfinance as yf

    results: dict[str, dict | None] = {}
    for ticker in tickers:
        if ticker in _CACHE:
            results[ticker] = _CACHE[ticker]
            continue
        try:
            info = yf.Ticker(ticker).info
            data = _extract_fundamentals(info)
            # Treat an all-empty result as no data.
            results[ticker] = data if any(v is not None for v in data.values()) else None
        except Exception as e:
            print(f"Fundamentals error for {ticker}: {e}")
            results[ticker] = None
        _CACHE[ticker] = results[ticker]

    fetched = sum(1 for v in results.values() if v)
    print(f"Fundamentals: fetched {fetched}/{len(tickers)} tickers")
    return results


if __name__ == "__main__":
    for t, d in fetch_fundamentals(["AAPL", "NVDA"]).items():
        print(f"\n{t}: {d}")
