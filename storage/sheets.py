import gspread
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

HEADERS = [
    "Date",
    "Ticker",
    "Company",
    "Direction",
    "Amount ($)",
    "Allocation (%)",
    "Risk",
    "Confidence",
    "Entry Rationale",
    "Exit Condition",
    "Source Headline",
    "Flagged",
]


def _get_worksheet():
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    sheet_id         = os.getenv("GOOGLE_SHEET_ID")

    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not set in your .env file.")

    gc           = gspread.service_account(filename=credentials_file)
    spreadsheet  = gc.open_by_key(sheet_id)
    return spreadsheet.sheet1


def _is_sheet_empty(worksheet) -> bool:
    """
    Returns True if the sheet has no data at all yet.
    """
    all_values = worksheet.get_all_values()
    return len(all_values) == 0


def export_to_sheets(allocations: list[dict], budget: float) -> bool:
    """
    Exports today's allocations to Google Sheets.

    First export: writes headers then data rows.
    Subsequent exports: writes a blank divider row, a run label row,
    headers again, then data rows. This keeps history readable.
    """
    if not allocations:
        print("Sheets: nothing to export.")
        return False

    try:
        worksheet  = _get_worksheet()
        run_date   = datetime.now().strftime("%Y-%m-%d %H:%M")
        is_empty   = _is_sheet_empty(worksheet)
        rows       = []

        if is_empty:
            # First ever export — just write headers then data
            rows.append(HEADERS)
        else:
            # Subsequent export — add a visual divider block
            rows.append([])  # blank row for breathing room
            rows.append([f"── Run: {run_date}  |  Budget: ${budget:,.2f} ──"])
            rows.append(HEADERS)

        # Data rows
        for a in allocations:
            rows.append([
                run_date,
                a.get("ticker",           "???"),
                a.get("company_name",     "Unknown"),
                a.get("direction",        ""),
                a.get("dollar_amount",    0.0),
                a.get("percentage",       0.0),
                a.get("risk_level",       ""),
                a.get("confidence_score", 0.0),
                a.get("entry_rationale",  ""),
                a.get("exit_condition",   ""),
                a.get("source_title",     ""),
                "Yes" if a.get("flagged") else "No",
            ])

        worksheet.append_rows(rows)

        print(f"Sheets: exported {len(allocations)} rows for {run_date}.")
        return True

    except Exception as e:
        print(f"Sheets export error: {e}")
        return False

def read_history() -> list[dict]:
    """
    Reads all historical runs from the Google Sheet and returns
    them as a list of dicts, one per stock row.
    Skips divider rows and header rows automatically.
    """
    try:
        worksheet = _get_worksheet()
        all_rows  = worksheet.get_all_values()

        if not all_rows:
            return []

        records = []
        for row in all_rows:
            if not row or not row[0]:
                continue
            if row[0].startswith("──") or row[0] == "Date":
                continue
            try:
                records.append({
                    "date":           row[0]  if len(row) > 0  else "",
                    "ticker":         row[1]  if len(row) > 1  else "",
                    "company":        row[2]  if len(row) > 2  else "",
                    "direction":      row[3]  if len(row) > 3  else "",
                    "amount":         float(row[4]) if len(row) > 4 and row[4] else 0.0,
                    "allocation_pct": float(row[5]) if len(row) > 5 and row[5] else 0.0,
                    "risk":           row[6]  if len(row) > 6  else "",
                    "confidence":     float(row[7]) if len(row) > 7 and row[7] else 0.0,
                    "entry_rationale": row[8] if len(row) > 8  else "",
                    "exit_condition": row[9]  if len(row) > 9  else "",
                    "source_title":   row[10] if len(row) > 10 else "",
                    "flagged":        row[11] if len(row) > 11 else "No",
                })
            except Exception:
                continue

        return records

    except Exception as e:
        print(f"Sheets read error: {e}")
        return []

if __name__ == "__main__":
    test_allocations = [
        {
            "ticker":           "AAPL",
            "company_name":     "Apple Inc.",
            "direction":        "buy",
            "dollar_amount":    177.78,
            "percentage":       17.8,
            "risk_level":       "low",
            "confidence_score": 0.78,
            "entry_rationale":  "Strong AI product pipeline.",
            "exit_condition":   "10% gain or next earnings",
            "source_title":     "Apple announces new AI features",
            "flagged":          False,
        }
    ]
    success = export_to_sheets(test_allocations, 1000.0)
    print(f"Export successful: {success}")