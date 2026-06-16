"""
Robinhood portfolio sync — read-only.
All robin_stocks API calls are isolated here.
If the unofficial API breaks, only this file needs updating.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Flag to track if robin_stocks is available
try:
    import robin_stocks.robinhood as rh
    RH_AVAILABLE = True
except ImportError:
    RH_AVAILABLE = False


# Track login state within this process session
_LOGGED_IN = False

def _login() -> bool:
    global _LOGGED_IN
    if _LOGGED_IN:
        return True

    if not RH_AVAILABLE:
        print("Robinhood sync: robin_stocks not installed.")
        return False

    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")

    if not username or not password:
        print("Robinhood sync: credentials not set in .env")
        return False

    try:
        login = rh.login(
            username,
            password,
            store_session=True,
            expiresIn=86400,
        )
        if login:
            _LOGGED_IN = True
            print("Robinhood sync: logged in successfully.")
            return True
        else:
            print("Robinhood sync: login returned None — check credentials or MFA.")
            return False
    except Exception as e:
        print(f"Robinhood sync: login failed — {e}")
        return False


def _logout():
    """Logs out of Robinhood."""
    if RH_AVAILABLE:
        try:
            rh.logout()
        except Exception:
            pass


def fetch_positions() -> list[dict]:
    """
    Fetches all open stock positions from Robinhood.
    Returns a normalized list of dicts that Argus can use directly.
    
    Each dict has:
      - ticker: str
      - company_name: str
      - shares: float
      - avg_cost: float  (average cost basis per share)
      - current_price: float
      - amount_invested: float (shares × avg_cost)
      - equity: float (shares × current_price)
      - pnl_pct: float
    
    If the API changes, update this function to parse the new format
    but keep the return structure the same.
    """
    if not _login():
        return []

    try:
        # robin_stocks returns a list of position dicts
        raw_positions = rh.account.build_holdings()

        if not raw_positions:
            print("Robinhood sync: no positions found.")
            _logout()
            return []

        positions = []
        for ticker, data in raw_positions.items():
            try:
                shares     = float(data.get("quantity", 0))
                avg_cost   = float(data.get("average_buy_price", 0))
                current    = float(data.get("price", 0))
                equity     = float(data.get("equity", 0))
                pnl_pct    = float(data.get("percent_change", 0))
                name       = data.get("name", ticker)

                if shares <= 0:
                    continue

                positions.append({
                    "ticker":          ticker,
                    "company_name":    name,
                    "shares":          shares,
                    "avg_cost":        avg_cost,
                    "current_price":   current,
                    "amount_invested": round(shares * avg_cost, 2),
                    "equity":          round(equity, 2),
                    "pnl_pct":         round(pnl_pct, 2),
                })
            except (ValueError, TypeError) as e:
                print(f"Robinhood sync: skipping {ticker} — {e}")
                continue

        print(f"Robinhood sync: fetched {len(positions)} positions.")
        return positions

    except Exception as e:
        print(f"Robinhood sync: fetch failed — {e}")
        return []
    

    
def fetch_buying_power() -> float | None:
    """
    Fetches the account's current buying power (cash available to invest)
    from Robinhood. Returns a float in dollars, or None if it can't be read.

    Used to sync the Argus investment budget to real available funds.
    All robin_stocks access stays in this file — if the unofficial API
    changes, update the field parsing here but keep the return type.
    """
    if not _login():
        return None

    try:
        profile = rh.profiles.load_account_profile()
        if not profile:
            print("Robinhood buying power: empty account profile.")
            return None

        # Prefer the most spendable figure. Robinhood exposes several;
        # buying_power is the standard "available to invest" number.
        for field in ("buying_power", "cash_available_for_withdrawal", "cash"):
            raw = profile.get(field)
            if raw is not None:
                try:
                    value = float(raw)
                    print(f"Robinhood buying power: {field} = ${value:.2f}")
                    return round(value, 2)
                except (ValueError, TypeError):
                    continue

        print("Robinhood buying power: no usable balance field found.")
        return None

    except Exception as e:
        print(f"Robinhood buying power: fetch failed — {e}")
        return None


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Fetches current quote data for a list of tickers from Robinhood.
    Returns a dict keyed by ticker with normalized price details, matching
    the structure produced by ingestion.prices.fetch_prices:

        {"AAPL": {"price": .., "change": .., "change_pct": .., "high": .., "low": ..}}

    Tickers Robinhood can't resolve (most crypto, delisted symbols) map to None
    so the caller can fall back to another source. Robinhood quotes don't expose
    intraday high/low, so those are returned as 0.0 (not consumed by the app).

    All robin_stocks access lives in this file — if the unofficial API changes,
    update the parsing here but keep the return structure the same.
    """
    if not tickers:
        return {}

    if not _login():
        return {}

    results: dict[str, dict] = {}

    try:
        # get_quotes returns a list aligned with the input symbols; invalid
        # symbols come back as None.
        quotes = rh.stocks.get_quotes(tickers)
    except Exception as e:
        print(f"Robinhood quotes: fetch failed — {e}")
        return {}

    for ticker, quote in zip(tickers, quotes or []):
        if not quote:
            results[ticker] = None
            continue

        try:
            # Prefer the extended-hours print when the regular session is closed.
            last_raw = quote.get("last_extended_hours_trade_price") or quote.get("last_trade_price")
            prev_raw = quote.get("previous_close") or quote.get("adjusted_previous_close")

            last = float(last_raw) if last_raw else 0.0
            prev = float(prev_raw) if prev_raw else 0.0

            if last == 0:
                results[ticker] = None
                continue

            change     = last - prev if prev else 0.0
            change_pct = (change / prev * 100) if prev else 0.0

            results[ticker] = {
                "price":      round(last, 2),
                "change":     round(change, 2),
                "change_pct": round(change_pct, 2),
                "high":       0.0,
                "low":        0.0,
            }
        except (ValueError, TypeError) as e:
            print(f"Robinhood quotes: skipping {ticker} — {e}")
            results[ticker] = None

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"Robinhood quotes: fetched {fetched}/{len(tickers)} tickers successfully")
    return results


def fetch_robinhood_news(tickers: list[str] = None) -> list[dict]:
    """
    Fetches news from Robinhood for the given tickers.
    Returns normalized items compatible with the scoring pipeline.
    
    If robin_stocks breaks, update the parsing logic here
    but keep the return format the same.
    """
    if not _login():
        return []

    if not tickers:
        # Default to watchlist tickers
        try:
            from storage.watchlist import get_tickers
            tickers = get_tickers("stocks")
        except Exception:
            tickers = []

    all_news = []
    seen_titles = set()

    for ticker in tickers:
        try:
            stories = rh.stocks.get_news(ticker)
            if not stories:
                continue

            for story in stories[:3]:  # top 3 per ticker to avoid flooding
                title = story.get("title", "")
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)

                all_news.append({
                    "title":            title,
                    "summary":          story.get("preview_text", "") or story.get("summary", ""),
                    "source":           story.get("source", "Robinhood"),
                    "source_type":      "robinhood_news",
                    "ticker":           ticker,
                    "url":              story.get("url", ""),
                    "published":        story.get("published_at", ""),
                    "confidence_score": 0,  # scorer will assign this
                    "flagged":          False,
                })
        except Exception as e:
            print(f"Robinhood news error for {ticker}: {e}")
            continue

    print(f"Robinhood news: fetched {len(all_news)} articles")
    return all_news

def is_available() -> bool:
    """Check if Robinhood sync is configured and ready."""
    if not RH_AVAILABLE:
        return False
    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")
    return bool(username and password)


if __name__ == "__main__":
    """Test — fetch and print all positions."""
    positions = fetch_positions()
    if positions:
        print(f"\n--- {len(positions)} Robinhood positions ---")
        for p in positions:
            print(
                f"  {p['ticker']:<6} | {p['shares']:.2f} shares "
                f"@ ${p['avg_cost']:.2f} avg | "
                f"${p['amount_invested']:.2f} invested | "
                f"{p['pnl_pct']:+.1f}%"
            )
    else:
        print("No positions fetched.")