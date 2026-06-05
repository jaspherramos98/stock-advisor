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


def _login() -> bool:
    """
    Logs into Robinhood using credentials from .env.
    Returns True if successful, False otherwise.
    
    If robin_stocks breaks in the future, update this function
    to use whatever new auth method is required.
    """
    if not RH_AVAILABLE:
        print("Robinhood sync: robin_stocks not installed. Run: pip install robin_stocks")
        return False

    username = os.getenv("ROBINHOOD_USERNAME")
    password = os.getenv("ROBINHOOD_PASSWORD")

    if not username or not password:
        print("Robinhood sync: ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD not set in .env")
        return False

    try:
        login = rh.login(
            username,
            password,
            store_session=True,       # caches auth token so MFA isn't needed every time
            expiresIn=86400,          # 24 hour session
        )
        if login:
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
        _logout()
        return positions

    except Exception as e:
        print(f"Robinhood sync: fetch failed — {e}")
        _logout()
        return []
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

    _logout()
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