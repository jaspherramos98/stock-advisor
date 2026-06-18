"""
Fact-based ETF facts from Yahoo Finance (via yfinance).

The ETF analog of `fundamentals.py`: company P/E and margins are meaningless for a
fund, so for ETFs we surface fund facts instead — category, sponsor, AUM, expense
ratio, yield, and (when yfinance exposes them) top holdings and sector weights.
Reported facts, not opinion. Missing values come back as None; nothing here ever raises.
"""

# Process-lifetime cache. Fund facts change slowly, so one lookup per ETF per run.
_CACHE: dict[str, dict | None] = {}


def _pct(x):
    """yfinance reports ratios as decimals (0.0009 = 0.09%). Return % or None."""
    try:
        return round(float(x) * 100, 2)
    except (TypeError, ValueError):
        return None


def _num(x, ndigits=2):
    try:
        return round(float(x), ndigits)
    except (TypeError, ValueError):
        return None


def _expense_pct(x):
    """
    Expense ratio, normalized to a percentage. yfinance is inconsistent across
    versions/funds: it may report 0.08 (already a percent, = 0.08%) or 0.0008 (a
    decimal fraction). Real ER percentages live ~0.02-1.5%; decimal fractions are
    < 0.02. Normalize so we always return the percent (e.g. 0.08 → 0.08%).
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v * 100, 3) if abs(v) < 0.02 else round(v, 3)


def _extract_etf_facts(info: dict, top_holdings=None, sector_weights=None) -> dict:
    """
    Pulls a clean, fact-based subset from a yfinance ETF `.info` dict plus optional
    holdings/sector data. Missing values are None. Separated from the network call
    so it can be unit-tested. Expense ratio lives under different keys across
    yfinance versions, so we try the common ones.
    """
    info = info or {}
    expense = info.get("annualReportExpenseRatio")
    if expense is None:
        expense = info.get("netExpenseRatio")
    # NOTE on units: yfinance returns `yield` as a decimal (0.004 = 0.4% → ×100),
    # but the expense ratio is already in percent units on most versions. ytdReturn's
    # scale is unreliable across versions, so we omit it (the RRG rel-perf vs SPY is
    # the trustworthy relative-return number).
    return {
        "category":          info.get("category"),
        "fund_family":       info.get("fundFamily"),
        "aum":               _num(info.get("totalAssets"), 0),
        "expense_ratio_pct": _expense_pct(expense),
        "yield_pct":         _pct(info.get("yield")),
        "beta_3y":           _num(info.get("beta3Year")),
        "top_holdings":      top_holdings,    # list[str] or None
        "sector_weights":    sector_weights,  # dict[str, float] or None
    }


def fetch_etf_facts(tickers: list[str]) -> dict[str, dict]:
    """
    Returns {ticker: etf_facts_dict} for the given ETF tickers. Tickers that error
    out or have no data map to None. Cached per process. Top holdings / sector
    weights come from yfinance `funds_data`, which varies by version — wrapped in
    try/except so missing it never costs the rest of the facts.
    """
    import yfinance as yf

    results: dict[str, dict | None] = {}
    for ticker in tickers:
        if ticker in _CACHE:
            results[ticker] = _CACHE[ticker]
            continue
        try:
            tk   = yf.Ticker(ticker)
            info = tk.info or {}

            top_holdings = None
            sector_weights = None
            try:
                fd = tk.funds_data
                th = fd.top_holdings  # DataFrame indexed by symbol, with a holding-% column
                if th is not None and not th.empty:
                    pct_col = next((c for c in th.columns if "percent" in c.lower()), None)
                    rows = []
                    for sym, row in th.head(5).iterrows():
                        if pct_col is not None:
                            rows.append(f"{sym} {float(row[pct_col]) * 100:.1f}%")
                        else:
                            rows.append(str(sym))
                    top_holdings = rows or None
                sw = fd.sector_weightings  # dict {sector: weight as decimal}
                if sw:
                    top_sectors = sorted(sw.items(), key=lambda kv: kv[1], reverse=True)[:5]
                    sector_weights = {k: round(float(v) * 100, 1) for k, v in top_sectors}
            except Exception:
                pass  # funds_data unavailable on this yfinance version — facts below still apply

            data = _extract_etf_facts(info, top_holdings, sector_weights)
            results[ticker] = data if any(v for v in data.values()) else None
        except Exception as e:
            print(f"ETF facts error for {ticker}: {e}")
            results[ticker] = None
        _CACHE[ticker] = results[ticker]

    fetched = sum(1 for v in results.values() if v)
    print(f"ETF facts: fetched {fetched}/{len(tickers)} ETFs")
    return results


if __name__ == "__main__":
    for t, d in fetch_etf_facts(["XLK", "QQQ", "SPY"]).items():
        print(f"\n{t}: {d}")
