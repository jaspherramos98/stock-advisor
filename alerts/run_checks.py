import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alerts.exit_checker import run_exit_checks
from alerts.entry_checker import run_entry_checks
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


def log(message: str, to_file: bool = True):
    """
    Writes a timestamped message to the terminal and (by default) the log file.

    `to_file=False` is for high-frequency noise: the scheduled task fires every 15
    minutes around the clock and self-skips outside market hours, so writing those
    skips would add ~96 useless lines a day to the log.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line      = f"[{timestamp}] {message}"
    print(line)
    if not to_file:
        return
    try:
        # Explicit UTF-8 — alert text contains em-dashes and symbols, and without this
        # the encoding follows the Windows locale (cp1252), producing mojibake in the
        # log or an outright UnicodeEncodeError.
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Log write error: {e}")


def is_market_hours() -> bool:
    now_et     = datetime.now(MARKET_TZ)
    is_weekday = now_et.weekday() in MARKET_DAYS
    is_hours   = MARKET_OPEN <= now_et.hour < MARKET_CLOSE
    return is_weekday and is_hours


def main():
    # Closed-market skips stay out of the log file — the scheduled task runs every 15
    # minutes all day, so only real check runs should leave a trace.
    if not is_market_hours():
        now_et = datetime.now(MARKET_TZ)
        log(f"Market closed ({now_et.strftime('%A %I:%M %p')} ET). Skipping.", to_file=False)
        return

    log(f"{'='*50}")
    log(f"ALERT CHECK STARTED (exit + entry)")
    log(f"{'='*50}")

    try:
        log("Market is open — running exit checks...")
        alerts = run_exit_checks()

        # Entry ("buy when") checks — watch triggers from today's recommendations plus
        # the last buy/watch ideas Argus chat gave. Failing here must not lose the exit
        # alerts we already have, so it's guarded separately.
        try:
            log("Running entry (buy-trigger) checks...")
            alerts += run_entry_checks()
        except Exception as entry_err:
            log(f"Entry checker failed (exit alerts unaffected): {entry_err}")

        if alerts:
            log(f"{len(alerts)} alert(s) triggered — sending notifications...")
            send_alerts(alerts)
            for a in alerts:
                log(f"  ALERT [{a['alert_type']}] {a['ticker']} — {a['message'][:80]}")
            log("Notifications sent.")
        else:
            log("No exit or entry conditions triggered.")

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

    log("ALERT CHECK COMPLETE\n")


if __name__ == "__main__":
    main()