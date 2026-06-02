import json
import os

WATCHLIST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "watchlist.json"
)

DEFAULT_WATCHLIST = {
    "stocks": [
        "AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
        "META", "GOOGL", "AMD", "JPM", "BAC",
    ],
    "etfs": [
        "SPY", "QQQ", "VTI", "IWM", "GLD",
        "ARKK", "XLF", "XLK", "VNQ", "TLT",
    ],
    "crypto": [
        "BTC", "ETH", "SOL", "BNB", "XRP",
    ],
}


def load_watchlist() -> dict:
    """
    Loads the full watch list from the JSON file.
    Returns defaults if the file doesn't exist yet.
    Handles both old format (flat list) and new format (dict with asset types).
    """
    if not os.path.exists(WATCHLIST_FILE):
        save_watchlist(DEFAULT_WATCHLIST)
        return {k: v.copy() for k, v in DEFAULT_WATCHLIST.items()}

    try:
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)

        # Handle old flat list format from before this update
        if isinstance(data.get("tickers"), list):
            upgraded = {
                "stocks": data["tickers"],
                "etfs":   DEFAULT_WATCHLIST["etfs"].copy(),
                "crypto": DEFAULT_WATCHLIST["crypto"].copy(),
            }
            save_watchlist(upgraded)
            return upgraded

        return {
            "stocks": data.get("stocks", DEFAULT_WATCHLIST["stocks"].copy()),
            "etfs":   data.get("etfs",   DEFAULT_WATCHLIST["etfs"].copy()),
            "crypto": data.get("crypto", DEFAULT_WATCHLIST["crypto"].copy()),
        }

    except Exception as e:
        print(f"Watchlist load error: {e}")
        return {k: v.copy() for k, v in DEFAULT_WATCHLIST.items()}


def save_watchlist(watchlist: dict):
    """Saves the full watch list dict to the JSON file."""
    try:
        cleaned = {}
        for asset_type, tickers in watchlist.items():
            cleaned[asset_type] = sorted(
                set(t.strip().upper() for t in tickers if t.strip())
            )
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(cleaned, f, indent=2)
        return cleaned
    except Exception as e:
        print(f"Watchlist save error: {e}")
        return watchlist


def get_tickers(asset_type: str) -> list[str]:
    """Returns tickers for a specific asset type: 'stocks', 'etfs', or 'crypto'."""
    return load_watchlist().get(asset_type, [])


def add_ticker(ticker: str, asset_type: str = "stocks") -> bool:
    watchlist = load_watchlist()
    ticker    = ticker.strip().upper()
    if ticker in watchlist.get(asset_type, []):
        return False
    watchlist.setdefault(asset_type, []).append(ticker)
    save_watchlist(watchlist)
    return True


def remove_ticker(ticker: str, asset_type: str = "stocks") -> bool:
    watchlist = load_watchlist()
    ticker    = ticker.strip().upper()
    if ticker not in watchlist.get(asset_type, []):
        return False
    watchlist[asset_type].remove(ticker)
    save_watchlist(watchlist)
    return True


def reset_to_defaults():
    save_watchlist(DEFAULT_WATCHLIST)
    return {k: v.copy() for k, v in DEFAULT_WATCHLIST.items()}


if __name__ == "__main__":
    wl = load_watchlist()
    print("Stocks:", wl["stocks"])
    print("ETFs:  ", wl["etfs"])
    print("Crypto:", wl["crypto"])