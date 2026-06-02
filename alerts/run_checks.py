import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts.exit_checker import run_exit_checks
from alerts.notifier import send_alerts
from datetime import datetime
import pytz
import traceback

# US market hours in Eastern Time
MARKET_OPEN  = 9
MARKET_CLOSE = 17
MARKET_TZ    = pytz.timezone("America/New_York")
MARKET_DAYS  = {0, 1, 2, 3, 4}

# Log file sits in the project root
LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "exit_checker.log"
)


def log(message: str):
    """
    Writes a timestamped message to both the terminal and the log file.
    The log file accumulates over time so you can review history.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Log write error: {e}")


def is_market_hours() -> bool:
    now_et     = datetime.now(MARKET_TZ)
    is_weekday = now_et.weekday() in MARKET_DAYS
    is_hours   = MARKET_OPEN <= now_et.hour < MARKET_CLOSE
    return is_weekday and is_hours


def main():
    log(f"{'='*50}")
    log(f"EXIT CHECK STARTED")
    log(f"{'='*50}")

    if not is_market_hours():
        now_et = datetime.now(MARKET_TZ)
        log(
            f"Market closed "
            f"({now_et.strftime('%A %I:%M %p')} ET). "
            f"Skipping."
        )
        return

    try:
        log("Market is open — running exit checks...")
        alerts = run_exit_checks()

        if alerts:
            log(f"{len(alerts)} alert(s) triggered — sending notifications...")
            send_alerts(alerts)
            for a in alerts:
                log(f"  ALERT [{a['alert_type']}] {a['ticker']} — {a['message'][:80]}")
            log("Notifications sent.")
        else:
            log("No exit conditions triggered.")

    except Exception as e:
        error_msg = traceback.format_exc()
        log(f"ERROR — exit checker crashed:\n{error_msg}")

        # Email yourself the error so you know something broke
        try:
            from alerts.notifier import _send_email
            _send_email([{
                "ticker":     "SYSTEM",
                "alert_type": "error",
                "message":    (
                    f"The exit checker crashed at "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M')}.\n\n"
                    f"Error:\n{error_msg}"
                ),
            }])
            log("Error notification email sent.")
        except Exception as email_err:
            log(f"Could not send error email: {email_err}")

    log("EXIT CHECK COMPLETE\n")


if __name__ == "__main__":
    main()