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
) -> dict:
    """
    Adds a new open position. If the ticker already exists as an
    open position, it updates it instead of adding a duplicate.
    Returns the position dict that was saved.
    """
    positions = load_positions()

    # Check if this ticker is already open
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            print(f"Positions: {ticker} already open, updating reference price.")
            p["reference_price"] = reference_price
            p["exit_condition"]  = exit_condition
            p["updated_at"]      = datetime.now().isoformat()
            save_positions(positions)
            return p

    position = {
        "ticker":           ticker,
        "company_name":     company_name,
        "reference_price":  reference_price,
        "manual_price":     None,   # set later if user overrides
        "exit_condition":   exit_condition,
        "direction":        direction,
        "confidence":       confidence,
        "source_title":     source_title,
        "opened_at":        datetime.now().isoformat(),
        "updated_at":       datetime.now().isoformat(),
        "status":           "open",   # open | closed
        "closed_at":        None,
        "close_reason":     None,     # what triggered the exit
        "alerts_sent":      [],       # log of alerts already sent
    }

    positions.append(position)
    save_positions(positions)
    print(f"Positions: added {ticker} at ${reference_price}")
    return position


def close_position(ticker: str, reason: str):
    """
    Marks a position as closed. Keeps the record for history —
    never deletes, just changes status to 'closed'.
    """
    positions = load_positions()
    for p in positions:
        if p["ticker"] == ticker and p["status"] == "open":
            p["status"]     = "closed"
            p["closed_at"]  = datetime.now().isoformat()
            p["close_reason"] = reason
            save_positions(positions)
            print(f"Positions: closed {ticker} — {reason}")
            return
    print(f"Positions: {ticker} not found or already closed.")


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


def get_open_positions() -> list[dict]:
    """Returns only positions with status 'open'."""
    return [p for p in load_positions() if p["status"] == "open"]


def get_effective_price(position: dict) -> float:
    """
    Returns the price to use for exit calculations.
    Manual price takes priority over the auto reference price.
    """
    return position["manual_price"] or position["reference_price"]


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