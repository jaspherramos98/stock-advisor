import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# --- Windows desktop notification ---
# Uses the built-in Windows toast notification system.
# No extra library needed for basic notifications, but
# win10toast gives nicer formatting.


def _build_email_html(alerts: list[dict]) -> str:
    """
    Builds a clean HTML email body summarizing all triggered alerts.
    """
    now        = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    alert_rows = ""

    for a in alerts:
        alert_type = a["alert_type"].replace("_", " ").title()

        if a["alert_type"] == "percentage_gain":
            color      = "#2ecc71"
            type_label = f"📈 {alert_type}"
        elif a["alert_type"] == "stop_loss":
            color      = "#e74c3c"
            type_label = f"🛑 Stop Loss"
        elif a["alert_type"] == "time_based":
            color      = "#f39c12"
            type_label = f"⏰ {alert_type}"
        elif a["alert_type"] == "event_based":
            color      = "#3498db"
            type_label = f"📰 {alert_type}"
        else:
            color      = "#aaaaaa"
            type_label = alert_type

        alert_rows += f"""
        <tr>
            <td style="padding: 12px; border-bottom: 1px solid #2a2a2a;">
                <strong style="font-size: 16px;">{a['ticker']}</strong>
            </td>
            <td style="padding: 12px; border-bottom: 1px solid #2a2a2a;">
                <span style="color: {color}; font-weight: bold;">{type_label}</span>
            </td>
            <td style="padding: 12px; border-bottom: 1px solid #2a2a2a; color: #cccccc;">
                {a['message']}
            </td>
        </tr>
        """

    return f"""
    <html>
    <body style="background-color: #1a1a1a; color: #ffffff; font-family: Arial, sans-serif; padding: 20px;">
        <div style="max-width: 700px; margin: 0 auto;">

            <h1 style="color: #ffffff; border-bottom: 2px solid #2ecc71; padding-bottom: 10px;">
                📈 Argus — Exit Alert
            </h1>

            <p style="color: #aaaaaa;">
                {len(alerts)} exit condition(s) triggered on {now}
            </p>

            <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                <thead>
                    <tr style="background-color: #2a2a2a;">
                        <th style="padding: 12px; text-align: left; color: #2ecc71;">Ticker</th>
                        <th style="padding: 12px; text-align: left; color: #2ecc71;">Type</th>
                        <th style="padding: 12px; text-align: left; color: #2ecc71;">Details</th>
                    </tr>
                </thead>
                <tbody>
                    {alert_rows}
                </tbody>
            </table>

            <div style="margin-top: 30px; padding: 15px; background-color: #2a2a2a; border-radius: 8px;">
                <p style="color: #aaaaaa; font-size: 12px; margin: 0;">
                    This is an automated alert from your personal Argus app.
                    This is not financial advice. Always do your own research before acting.
                </p>
            </div>

        </div>
    </body>
    </html>
    """


def _send_email(alerts: list[dict]):
    """
    Sends an HTML email summarizing all triggered alerts.
    Uses Gmail SMTP with an app password.
    """
    sender_email   = os.getenv("ALERT_EMAIL_SENDER")
    sender_password = os.getenv("ALERT_EMAIL_PASSWORD")
    receiver_email = os.getenv("ALERT_EMAIL_RECEIVER")

    if not all([sender_email, sender_password, receiver_email]):
        print("Email notifier: credentials not set in .env, skipping.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"📈 Argus — {len(alerts)} Exit Alert(s) Triggered"
        msg["From"]    = sender_email
        msg["To"]      = receiver_email

        html_body = _build_email_html(alerts)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())

        print(f"Email sent to {receiver_email} with {len(alerts)} alert(s).")

    except Exception as e:
        print(f"Email send error: {e}")


def send_alerts(alerts: list[dict]):
    """
    Main entry point. Sends both desktop and email notifications
    for all triggered alerts.
    """
    if not alerts:
        print("Notifier: no alerts to send.")
        return

    print(f"Notifier: sending {len(alerts)} alert(s)...")

    # Email — one combined email for all alerts
    _send_email(alerts)


if __name__ == "__main__":
    # Test with a fake alert
    test_alerts = [
        {
            "ticker":      "AAPL",
            "alert_type":  "percentage_gain",
            "message":     "AAPL has gained 60.4% from reference price of $191.00. Current price: $306.31. Exit condition was: 10% gain or next earnings",
            "exit_condition": "10% gain or next earnings",
        },
        {
            "ticker":      "AAPL",
            "alert_type":  "event_based",
            "message":     "AAPL exit condition triggered: next earnings. News: Apple WWDC 2026 signals earnings period approaching.",
            "exit_condition": "next earnings",
        },
    ]
    send_alerts(test_alerts)