import gspread
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
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

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_clients():
    """
    Returns both a gspread worksheet and a raw Google Sheets API service.
    gspread handles reading/writing rows.
    The raw API handles formatting and merging.
    """
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    sheet_id         = os.getenv("GOOGLE_SHEET_ID")

    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not set in your .env file.")

    gc          = gspread.service_account(filename=credentials_file)
    spreadsheet = gc.open_by_key(sheet_id)
    worksheet   = spreadsheet.sheet1

    creds   = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds)

    return worksheet, service, sheet_id


def _get_worksheet():
    worksheet, _, _ = _get_clients()
    return worksheet


def _is_sheet_empty(worksheet) -> bool:
    return len(worksheet.get_all_values()) == 0


def _get_next_row(worksheet) -> int:
    """Returns the index of the next empty row (1-based)."""
    return len(worksheet.get_all_values()) + 1


def _merge_and_format_divider(service, sheet_id: str, row: int, col_count: int, label: str):
    """Merges and styles the run divider row."""
    zero_row = row - 1
    requests = [
        {
            "mergeCells": {
                "range": {
                    "sheetId":          0,
                    "startRowIndex":    zero_row,
                    "endRowIndex":      zero_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex":   col_count,
                },
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId":          0,
                    "startRowIndex":    zero_row,
                    "endRowIndex":      zero_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex":   col_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.15},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold":     True,
                            "fontSize": 11,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment":   "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
            }
        },
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": requests}
    ).execute()


def _format_headers(service, sheet_id: str, row: int, col_count: int):
    """Styles the header row with dark green background and white bold text."""
    zero_row = row - 1
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId":          0,
                    "startRowIndex":    zero_row,
                    "endRowIndex":      zero_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex":   col_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.07, "green": 0.35, "blue": 0.18},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True,
                        },
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        }
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": requests}
    ).execute()


def _format_data_rows(service, sheet_id: str, start_row: int, allocations: list[dict]):
    """
    Formats each data row:
    - Bolds the ticker cell (column B)
    - Color codes the direction cell (column D)
      buy   → green
      watch → orange
      avoid → red
    """
    COLOR_MAP = {
        "buy":   {"red": 0.18, "green": 0.80, "blue": 0.44},
        "watch": {"red": 0.95, "green": 0.61, "blue": 0.07},
        "avoid": {"red": 0.91, "green": 0.30, "blue": 0.24},
    }

    requests = []

    for i, alloc in enumerate(allocations):
        zero_row  = (start_row + i) - 1
        direction = alloc.get("direction", "").lower()
        bg_color  = COLOR_MAP.get(direction, {"red": 1.0, "green": 1.0, "blue": 1.0})

        # Bold ticker — column B (index 1)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId":          0,
                    "startRowIndex":    zero_row,
                    "endRowIndex":      zero_row + 1,
                    "startColumnIndex": 1,
                    "endColumnIndex":   2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True}
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })

        # Color direction — column D (index 3)
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId":          0,
                    "startRowIndex":    zero_row,
                    "endRowIndex":      zero_row + 1,
                    "startColumnIndex": 3,
                    "endColumnIndex":   4,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg_color,
                        "horizontalAlignment": "CENTER",
                        "textFormat": {
                            "bold":            True,
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
            }
        })

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": requests},
        ).execute()


def export_to_sheets(allocations: list[dict], budget: float) -> bool:
    """
    Exports allocations to Google Sheets with formatting.
    First export: headers then data.
    Subsequent exports: divider, headers, data.
    Each data row gets bold ticker and color-coded direction.
    """
    if not allocations:
        print("Sheets: nothing to export.")
        return False

    try:
        worksheet, service, sheet_id = _get_clients()
        run_date  = datetime.now().strftime("%Y-%m-%d %H:%M")
        is_empty  = _is_sheet_empty(worksheet)
        col_count = len(HEADERS)

        if is_empty:
            header_row = _get_next_row(worksheet)
            worksheet.append_row(HEADERS)
            _format_headers(service, sheet_id, header_row, col_count)
        else:
            worksheet.append_row([])
            divider_row = _get_next_row(worksheet)
            label = f"Run: {run_date}   |   Budget: ${budget:,.2f}"
            worksheet.append_row([label])
            _merge_and_format_divider(service, sheet_id, divider_row, col_count, label)

            header_row = _get_next_row(worksheet)
            worksheet.append_row(HEADERS)
            _format_headers(service, sheet_id, header_row, col_count)

        # Write data rows
        data_rows = []
        filtered  = [a for a in allocations if a.get("ticker") and a.get("ticker") != "???"]
        for a in filtered:
            data_rows.append([
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

        data_start_row = _get_next_row(worksheet)
        worksheet.append_rows(data_rows)
        _format_data_rows(service, sheet_id, data_start_row, filtered)

        print(f"Sheets: exported {len(filtered)} rows for {run_date}.")
        return True

    except Exception as e:
        print(f"Sheets export error: {e}")
        return False


def read_history() -> list[dict]:
    """
    Reads all historical runs from the Google Sheet.
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
            if row[0].startswith("──") or row[0].startswith("Run:") or row[0] == "Date":
                continue
            try:
                records.append({
                    "date":            row[0]  if len(row) > 0  else "",
                    "ticker":          row[1]  if len(row) > 1  else "",
                    "company":         row[2]  if len(row) > 2  else "",
                    "direction":       row[3]  if len(row) > 3  else "",
                    "amount":          float(row[4]) if len(row) > 4 and row[4] else 0.0,
                    "allocation_pct":  float(row[5]) if len(row) > 5 and row[5] else 0.0,
                    "risk":            row[6]  if len(row) > 6  else "",
                    "confidence":      float(row[7]) if len(row) > 7 and row[7] else 0.0,
                    "entry_rationale": row[8]  if len(row) > 8  else "",
                    "exit_condition":  row[9]  if len(row) > 9  else "",
                    "source_title":    row[10] if len(row) > 10 else "",
                    "flagged":         row[11] if len(row) > 11 else "No",
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
            "exit_condition":   "target 8% gain, stop loss at 3%",
            "source_title":     "Apple announces new AI features",
            "flagged":          False,
        },
        {
            "ticker":           "TSLA",
            "company_name":     "Tesla Inc.",
            "direction":        "watch",
            "dollar_amount":    100.00,
            "percentage":       10.0,
            "risk_level":       "medium",
            "confidence_score": 0.65,
            "entry_rationale":  "EV market share recovery.",
            "exit_condition":   "target 6% gain, stop loss at 5%",
            "source_title":     "Tesla reclaims EV market share",
            "flagged":          False,
        },
    ]
    success = export_to_sheets(test_allocations, 1000.0)
    print(f"Export successful: {success}")