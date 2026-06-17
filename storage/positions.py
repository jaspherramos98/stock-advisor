import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

POSITIONS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "positions.json"
)


def load_positions() -> list[dict]:
    """
    Loads all open positions from the local JSON file.
    Returns an empty list if the file doesn't exist yet.
    """
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Positions load error: {e}")
        return []


def save_positions(positions: list[dict]):
    """Saves the full positions list back to the JSON file."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        print(f"Positions save error: {e}")


def add_position(
    ticker:          str,
    company_name:    str,
    reference_price: float,
    exit_condition:  str,
    direction:       str,
    confidence:      float,
    source_title:    str,
    entry_date:      str  = None,  # ISO date string e.g. "2026-06-04" — defaults to today
) -> dict:
    """
    Adds a new open position. If the ticker already exists as an
    open position, updates it instead of adding a duplicate.
    entry_date lets the user record when they actually bought,
    which may differ from when they added it to the app.
    """
    positions = load_positions()

    # Use today if no entry date provided
    if not entry_date:
        entry_date = datetime.now().strftime("%Y-%m-%d")

    # Check if this ticker is already open
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            print(f"Positions: {ticker} already open, updating reference price.")
            p["reference_price"] = reference_price
            p["exit_condition"]  = exit_condition
            p["entry_date"]      = entry_date
            p["updated_at"]      = datetime.now().isoformat()
            save_positions(positions)
            return p

    position = {
        "ticker":           ticker,
        "company_name":     company_name,
        "reference_price":  reference_price,
        "manual_price":     None,
        "exit_condition":   exit_condition,
        "direction":        direction,
        "confidence":       confidence,
        "source_title":     source_title,
        "entry_date":       entry_date,        # when the user actually bought
        "opened_at":        datetime.now().isoformat(),  # when they added it to the app
        "updated_at":       datetime.now().isoformat(),
        "status":           "open",
        "closed_at":        None,
        "close_reason":     None,
        "alerts_sent":      [],
    }

    positions.append(position)
    save_positions(positions)
    print(f"Positions: added {ticker} at ${reference_price} (entry date: {entry_date})")
    return position

def get_effective_price(position: dict) -> float:
    """
    Returns the price to use for exit calculations.
    Manual price takes priority over the auto reference price.
    """
    return position["manual_price"] or position["reference_price"]

def close_position(ticker: str, reason: str, close_price: float = None):
    """
    Marks a position as closed. Keeps the record for history —
    never deletes, just changes status to 'closed'.
    Optionally records the closing price and calculates final P&L.
    """
    positions = load_positions()
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            p["status"]       = "closed"
            p["closed_at"]    = datetime.now().isoformat()
            p["close_reason"] = reason
            p["close_price"]  = close_price

            # Calculate final P&L if we have a closing price.
            # Shorts invert: you profit when the price FALLS below entry.
            if close_price:
                entry_price = get_effective_price(p)
                if entry_price and entry_price > 0:
                    if p.get("direction") == "short":
                        p["pnl_pct"]     = round(((entry_price - close_price) / entry_price) * 100, 2)
                        p["pnl_dollars"] = round(entry_price - close_price, 2)
                    else:
                        p["pnl_pct"]     = round(((close_price - entry_price) / entry_price) * 100, 2)
                        p["pnl_dollars"] = round(close_price - entry_price, 2)
                else:
                    p["pnl_pct"]     = None
                    p["pnl_dollars"] = None
            else:
                p["pnl_pct"]     = None
                p["pnl_dollars"] = None

            save_positions(positions)
            pnl_str = f" | P&L: {p['pnl_pct']:+.1f}%" if p.get("pnl_pct") is not None else ""
            print(f"Positions: closed {ticker} — {reason}{pnl_str}")
            return
    print(f"Positions: {ticker} not found or already closed.")


def get_closed_positions() -> list[dict]:
    """Returns only positions with status 'closed', most recent first."""
    closed = [p for p in load_positions() if p["status"] == "closed"]
    return sorted(closed, key=lambda x: x.get("closed_at", ""), reverse=True)


def update_manual_price(ticker: str, price: float):
    """
    Lets the user override the reference price for a position.
    The exit checker will use this instead of the auto reference price.
    """
    positions = load_positions()
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            p["manual_price"] = price
            p["updated_at"]   = datetime.now().isoformat()
            save_positions(positions)
            print(f"Positions: {ticker} manual price set to ${price}")
            return
    print(f"Positions: {ticker} not found.")

def update_exit_condition(ticker: str, exit_condition: str):
    """
    Lets the user edit the exit strategy for an open position.
    Useful for positions synced from Robinhood, which come in
    without a real exit condition set.
    """
    positions = load_positions()
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            p["exit_condition"] = exit_condition
            p["updated_at"]     = datetime.now().isoformat()
            save_positions(positions)
            print(f"Positions: {ticker} exit condition set to '{exit_condition}'")
            return
    print(f"Positions: {ticker} not found.")

def update_amount_invested(ticker: str, amount: float):
    """
    Records how many actual dollars the user invested in this position.
    Used to calculate real portfolio value and P&L in dollar terms.
    """
    positions = load_positions()
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            p["amount_invested"] = amount
            p["updated_at"]      = datetime.now().isoformat()
            save_positions(positions)
            print(f"Positions: {ticker} amount invested set to ${amount:.2f}")
            return
    print(f"Positions: {ticker} not found.")

def get_open_positions() -> list[dict]:
    """Returns only positions with status 'open'."""
    return [p for p in load_positions() if p["status"] == "open"]



if __name__ == "__main__":
    # Test — add two positions, update one manually, close one
    print("--- Adding positions ---")
    add_position("AAPL", "Apple Inc.",  189.42, "10% gain or next earnings", "buy",   0.78, "Apple AI news")
    add_position("NVDA", "Nvidia Corp.", 875.30, "15% gain or 2 weeks",      "buy",   0.78, "NVDA GTC event")

    print("\n--- Updating AAPL manual price ---")
    update_manual_price("AAPL", 191.00)

    print("\n--- Open positions ---")
    for p in get_open_positions():
        effective = get_effective_price(p)
        print(f"  {p['ticker']} | ref: ${p['reference_price']} | effective: ${effective} | exit: {p['exit_condition']}")

    print("\n--- Closing NVDA ---")
    close_position("NVDA", "15% gain reached")

    print("\n--- Open positions after close ---")
    for p in get_open_positions():
        print(f"  {p['ticker']} | status: {p['status']}")