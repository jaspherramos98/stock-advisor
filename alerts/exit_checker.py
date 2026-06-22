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
from market_hours import market_session

load_dotenv()

# How many percentage points counts as "close enough" to trigger
# a percentage-based exit. Set to 0 for exact, or 0.5 for within
# half a percent of the target.
PCT_TOLERANCE = 0.5


def _check_percentage_exit(position: dict, current_price: float) -> dict | None:
    """
    Checks if a percentage-based exit condition has been met.
    Handles two patterns:
      - Gain targets:     "target 8% gain", "10% gain"
      - Stop loss exits:  "stop loss at 4%", "stop loss at 4"
    Returns an alert dict if triggered, None otherwise.
    """
    exit_cond     = position["exit_condition"].lower()
    effective_ref = get_effective_price(position)

    if effective_ref <= 0:
        return None

    change_pct = ((current_price - effective_ref) / effective_ref) * 100

    # Shorts profit when the price FALLS, so invert: a "gain" for a short = price down,
    # its "stop loss" = price up. With this flip the gain/stop parser below works as-is.
    if position.get("direction") == "short":
        change_pct = -change_pct

    import re

    # --- Check stop loss first — most important ---
    stop_match = re.search(r"stop\s*loss\s*at\s*(\d+(?:\.\d+)?)\s*%?", exit_cond)
    if stop_match:
        stop_pct = float(stop_match.group(1))
        if change_pct <= -(stop_pct - PCT_TOLERANCE):
            return {
                "ticker":         position["ticker"],
                "alert_type":     "stop_loss",
                "message":        (
                    f"🛑 STOP LOSS triggered for {position['ticker']}. "
                    f"Position is down {abs(change_pct):.1f}% from your entry of ${effective_ref:.2f}. "
                    f"Current price: ${current_price:.2f}. "
                    f"Stop loss was set at {stop_pct}%. "
                    f"Consider closing this position."
                ),
                "current_price":  current_price,
                "change_pct":     round(change_pct, 2),
                "exit_condition": position["exit_condition"],
            }

    # --- Check gain targets ---
    # Looks for: "target 8% gain", "10% gain", "8% rise"
    gain_matches = re.findall(
        r"(?:target\s+)?(\d+(?:\.\d+)?)\s*%\s*(gain|rise|profit|up)?",
        exit_cond
    )
    for match in gain_matches:
        target_pct = float(match[0])
        label      = match[1] if match[1] else "gain"

        # Skip if this looks like a stop loss percentage we already handled
        context = exit_cond[max(0, exit_cond.find(f"{target_pct}%") - 15): exit_cond.find(f"{target_pct}%") + 5]
        if "stop" in context or "loss" in context:
            continue

        if change_pct >= (target_pct - PCT_TOLERANCE):
            return {
                "ticker":         position["ticker"],
                "alert_type":     "percentage_gain",
                "message":        (
                    f"✅ Gain target reached for {position['ticker']}. "
                    f"Position is up {change_pct:.1f}% from your entry of ${effective_ref:.2f}. "
                    f"Current price: ${current_price:.2f}. "
                    f"Exit condition was: {position['exit_condition']}"
                ),
                "current_price":  current_price,
                "change_pct":     round(change_pct, 2),
                "exit_condition": position["exit_condition"],
            }

    return None

def _check_time_exit(position: dict) -> dict | None:
    """
    Checks if a time-based exit condition has been met.
    Uses entry_date (when user actually bought) if available,
    otherwise falls back to opened_at (when they added it to the app).
    """
    exit_cond = position["exit_condition"].lower()

    # Use actual purchase date if available, otherwise app-add date
    entry_date_str = position.get("entry_date")
    if entry_date_str:
        try:
            start_date = datetime.strptime(entry_date_str, "%Y-%m-%d")
        except Exception:
            start_date = datetime.fromisoformat(position["opened_at"])
    else:
        start_date = datetime.fromisoformat(position["opened_at"])

    days_open = (datetime.now() - start_date).days

    import re

    # Week-based exits
    week_match = re.search(r"(\d+)\s*week", exit_cond)
    if week_match:
        target_days = int(week_match.group(1)) * 7
        if days_open >= target_days:
            return {
                "ticker":         position["ticker"],
                "alert_type":     "time_based",
                "message":        (
                    f"⏰ Time exit for {position['ticker']}. "
                    f"Position has been open {days_open} days since purchase. "
                    f"Time limit of {week_match.group(1)} week(s) reached. "
                    f"Exit condition: {position['exit_condition']}"
                ),
                "days_open":      days_open,
                "exit_condition": position["exit_condition"],
            }

    # Day-based exits
    day_match = re.search(r"(\d+)\s*day", exit_cond)
    if day_match:
        target_days = int(day_match.group(1))
        if days_open >= target_days:
            return {
                "ticker":         position["ticker"],
                "alert_type":     "time_based",
                "message":        (
                    f"⏰ Time exit for {position['ticker']}. "
                    f"Position has been open {days_open} days since purchase. "
                    f"Time limit of {day_match.group(1)} day(s) reached. "
                    f"Exit condition: {position['exit_condition']}"
                ),
                "days_open":      days_open,
                "exit_condition": position["exit_condition"],
            }

    # Month-based exits
    month_match = re.search(r"(\d+)\s*month", exit_cond)
    if month_match:
        target_days = int(month_match.group(1)) * 30
        if days_open >= target_days:
            return {
                "ticker":         position["ticker"],
                "alert_type":     "time_based",
                "message":        (
                    f"⏰ Time exit for {position['ticker']}. "
                    f"Position has been open {days_open} days since purchase. "
                    f"Time limit of {month_match.group(1)} month(s) reached. "
                    f"Exit condition: {position['exit_condition']}"
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
    if os.getenv("MOCK_MODE", "false").lower() == "true":
        print("Event checker: MOCK MODE — skipping Claude call.")
        return []

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

    # --- Session awareness: an exit alert means "consider closing", which needs the
    # market open. Tag each alert with whether it's actionable right now and, if not,
    # append the session caveat so the user doesn't act on an unexecutable signal. ---
    if all_alerts:
        sess = market_session()
        for a in all_alerts:
            a["market_status"]  = sess["status"]
            a["actionable_now"] = sess["is_open"]
            if sess["action_note"]:
                a["message"] += f"\n⏰ {sess['action_note']}"

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