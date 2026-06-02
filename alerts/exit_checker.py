import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import anthropic
from datetime import datetime, timedelta
from dotenv import load_dotenv

from storage.positions import (
    load_positions,
    get_open_positions,
    get_effective_price,
    close_position,
)
from ingestion.prices import fetch_prices
from ingestion.finnhub_news import fetch_finnhub_news
from ingestion.rss import fetch_rss_news

load_dotenv()

# How many percentage points counts as "close enough" to trigger
# a percentage-based exit. Set to 0 for exact, or 0.5 for within
# half a percent of the target.
PCT_TOLERANCE = 0.5


def _check_percentage_exit(position: dict, current_price: float) -> dict | None:
    """
    Checks if a percentage-based exit condition has been met.
    Looks for patterns like '10% gain', '5% loss', '15% gain or 2 weeks'.
    Returns an alert dict if triggered, None otherwise.
    """
    exit_cond     = position["exit_condition"].lower()
    effective_ref = get_effective_price(position)

    if effective_ref <= 0:
        return None

    # Calculate actual price change
    change_pct = ((current_price - effective_ref) / effective_ref) * 100

    # Look for percentage targets in the exit condition string
    import re
    pct_matches = re.findall(r"(\d+(?:\.\d+)?)\s*%\s*(gain|loss|drop|rise|move)?", exit_cond)

    for match in pct_matches:
        target_pct = float(match[0])
        direction  = match[1] if match[1] else "gain"

        # Check gain targets
        if direction in ("gain", "rise", "move", "") and change_pct >= (target_pct - PCT_TOLERANCE):
            return {
                "ticker":      position["ticker"],
                "alert_type":  "percentage_gain",
                "message":     (
                    f"{position['ticker']} has gained {change_pct:.1f}% "
                    f"from your reference price of ${effective_ref:.2f}. "
                    f"Current price: ${current_price:.2f}. "
                    f"Exit condition was: {position['exit_condition']}"
                ),
                "current_price": current_price,
                "change_pct":    round(change_pct, 2),
                "exit_condition": position["exit_condition"],
            }

        # Check loss targets
        if direction in ("loss", "drop") and change_pct <= -(target_pct - PCT_TOLERANCE):
            return {
                "ticker":      position["ticker"],
                "alert_type":  "percentage_loss",
                "message":     (
                    f"{position['ticker']} has dropped {abs(change_pct):.1f}% "
                    f"from your reference price of ${effective_ref:.2f}. "
                    f"Current price: ${current_price:.2f}. "
                    f"Exit condition was: {position['exit_condition']}"
                ),
                "current_price": current_price,
                "change_pct":    round(change_pct, 2),
                "exit_condition": position["exit_condition"],
            }

    return None


def _check_time_exit(position: dict) -> dict | None:
    """
    Checks if a time-based exit condition has been met.
    Looks for patterns like '2 weeks', '3 days', '1 month'.
    Returns an alert dict if triggered, None otherwise.
    """
    exit_cond  = position["exit_condition"].lower()
    opened_at  = datetime.fromisoformat(position["opened_at"])
    now        = datetime.now()
    days_open  = (now - opened_at).days

    import re

    # Check for week-based exits
    week_match = re.search(r"(\d+)\s*week", exit_cond)
    if week_match:
        target_days = int(week_match.group(1)) * 7
        if days_open >= target_days:
            return {
                "ticker":     position["ticker"],
                "alert_type": "time_based",
                "message":    (
                    f"{position['ticker']} has been open for {days_open} days. "
                    f"Exit condition was: {position['exit_condition']}. "
                    f"Time limit of {week_match.group(1)} week(s) reached."
                ),
                "days_open":      days_open,
                "exit_condition": position["exit_condition"],
            }

    # Check for day-based exits
    day_match = re.search(r"(\d+)\s*day", exit_cond)
    if day_match:
        target_days = int(day_match.group(1))
        if days_open >= target_days:
            return {
                "ticker":     position["ticker"],
                "alert_type": "time_based",
                "message":    (
                    f"{position['ticker']} has been open for {days_open} days. "
                    f"Exit condition was: {position['exit_condition']}. "
                    f"Time limit of {day_match.group(1)} day(s) reached."
                ),
                "days_open":      days_open,
                "exit_condition": position["exit_condition"],
            }

    # Check for month-based exits
    month_match = re.search(r"(\d+)\s*month", exit_cond)
    if month_match:
        target_days = int(month_match.group(1)) * 30
        if days_open >= target_days:
            return {
                "ticker":     position["ticker"],
                "alert_type": "time_based",
                "message":    (
                    f"{position['ticker']} has been open for {days_open} days. "
                    f"Exit condition was: {position['exit_condition']}. "
                    f"Time limit of {month_match.group(1)} month(s) reached."
                ),
                "days_open":      days_open,
                "exit_condition": position["exit_condition"],
            }

    return None


def _check_event_exits(positions: list[dict]) -> list[dict]:
    """
    Uses Claude to check if any recent news signals that an
    event-based exit condition has been met for any open position.
    Sends all positions and recent news in one Claude call to save tokens.
    Returns a list of alert dicts for any triggered exits.
    """
    if not positions:
        return []

    # Fetch fresh news
    print("Event checker: fetching fresh news...")
    try:
        news_items = fetch_finnhub_news() + fetch_rss_news()
    except Exception as e:
        print(f"Event checker: news fetch error: {e}")
        return []

    # Build a summary of recent headlines
    headlines = []
    for item in news_items[:30]:
        headlines.append(f"- {item['title']} ({item['source']})")
    news_block = "\n".join(headlines)

    # Build a summary of open positions and their event-based exits
    position_lines = []
    for p in positions:
        position_lines.append(
            f"- {p['ticker']} ({p['company_name']}): exit when '{p['exit_condition']}'"
        )
    positions_block = "\n".join(position_lines)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    system_prompt = """You are an exit condition monitor for a personal stock advisor app.
You will be given a list of open stock positions with their exit conditions,
and a list of recent news headlines.

Your job is to check if any news headline signals that an exit condition has been met.

Focus on EVENT-BASED exit conditions like:
- 'earnings release' → look for earnings announcements
- 'completion of capital raise' → look for offering completion news
- 'merger approval' → look for merger/acquisition completion news
- 'FDA approval' → look for FDA decision news
- 'next product launch' → look for product launch announcements

Ignore percentage and time-based conditions — those are handled separately.

Respond with ONLY a valid JSON array. No preamble, no explanation.
Each object must have exactly these fields:
{
  "ticker":         string,
  "exit_condition": string,
  "headline":       string (the news headline that triggered this),
  "reasoning":      string (one sentence explaining why this headline signals the exit)
}

If no exit conditions are met, return an empty array: []"""

    user_prompt = f"""Open positions and their exit conditions:
{positions_block}

Recent news headlines:
{news_block}

Check if any headlines signal that an exit condition has been met."""

    try:
        print("Event checker: asking Claude to evaluate exit conditions...")
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        triggered = json.loads(raw)

        alerts = []
        for t in triggered:
            alerts.append({
                "ticker":      t["ticker"],
                "alert_type":  "event_based",
                "message":     (
                    f"{t['ticker']} exit condition triggered: '{t['exit_condition']}'. "
                    f"News: {t['headline']}. "
                    f"Reason: {t['reasoning']}"
                ),
                "headline":       t["headline"],
                "exit_condition": t["exit_condition"],
            })

        print(f"Event checker: Claude found {len(alerts)} event-based triggers.")
        return alerts

    except Exception as e:
        print(f"Event checker Claude error: {e}")
        return []


def run_exit_checks() -> list[dict]:
    """
    Main entry point. Runs all three exit checks across all open positions.
    Returns a flat list of all triggered alerts.
    Marks already-alerted exits so we don't send duplicate notifications.
    """
    positions = get_open_positions()

    if not positions:
        print("Exit checker: no open positions to check.")
        return []

    print(f"Exit checker: checking {len(positions)} open positions...")

    # Fetch current prices for all open tickers
    tickers = [p["ticker"] for p in positions]
    prices  = fetch_prices(tickers)

    all_alerts = []

    # --- Percentage and time checks (no API cost) ---
    from alerts.snooze import is_snoozed

    # --- Percentage and time checks (no API cost) ---
    for position in positions:
        ticker     = position["ticker"]
        price_data = prices.get(ticker)

        # Skip if snoozed or already alerted today
        if is_snoozed(ticker):
            print(f"  {ticker}: snoozed, skipping.")
            continue

        today = datetime.now().strftime("%Y-%m-%d")
        if today in position.get("alerts_sent", []):
            print(f"  {ticker}: alert already sent today, skipping.")
            continue

        # Percentage check
        if price_data:
            current_price = price_data["price"]
            pct_alert     = _check_percentage_exit(position, current_price)
            if pct_alert:
                all_alerts.append(pct_alert)
                continue  # no need to check time if pct already triggered

        # Time check
        time_alert = _check_time_exit(position)
        if time_alert:
            all_alerts.append(time_alert)

    # --- Event check (one Claude call for all positions) ---
    event_alerts = _check_event_exits(positions)
    all_alerts.extend(event_alerts)

    # Mark alerts as sent so we don't duplicate on the next check
    if all_alerts:
        all_positions  = load_positions()
        triggered_tickers = {a["ticker"] for a in all_alerts}
        today = datetime.now().strftime("%Y-%m-%d")

        for p in all_positions:
            if p["ticker"] in triggered_tickers and p["status"] == "open":
                if today not in p.get("alerts_sent", []):
                    p.setdefault("alerts_sent", []).append(today)

        from storage.positions import save_positions
        save_positions(all_positions)

    print(f"Exit checker: {len(all_alerts)} alerts triggered.")
    return all_alerts


if __name__ == "__main__":
    alerts = run_exit_checks()
    if alerts:
        print("\n--- Triggered alerts ---")
        for a in alerts:
            print(f"\n[{a['alert_type'].upper()}] {a['ticker']}")
            print(f"  {a['message']}")
    else:
        print("No exit conditions met.")