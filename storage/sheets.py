import gspread
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

HEADERS = [
    "Date", "Ticker", "Company", "Direction",
    "Amount ($)", "Allocation (%)", "Risk", "Confidence",
    "Entry Rationale", "Exit Condition", "Source Headline", "Flagged",
]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_clients():
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    sheet_id         = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEET_ID is not set in your .env file.")
    gc          = gspread.service_account(filename=credentials_file)
    worksheet   = gc.open_by_key(sheet_id).sheet1
    creds       = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    service     = build("sheets", "v4", credentials=creds)
    return worksheet, service, sheet_id


def _get_worksheet():
    worksheet, _, _ = _get_clients()
    return worksheet


def _get_last_data_row(worksheet) -> int:
    """
    Scans from bottom up to find the last row with any content.
    Returns 0 if the sheet is completely empty.
    More reliable than len(get_all_values()) which can include empty trailing rows.
    """
    all_values = worksheet.get_all_values()
    for i in range(len(all_values) - 1, -1, -1):
        if any(str(cell).strip() for cell in all_values[i]):
            return i + 1
    return 0


def _format_headers(service, sheet_id: str, row: int, col_count: int):
    """Dark green background, white bold text, centered."""
    zero_row = row - 1
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": zero_row, "endRowIndex": zero_row + 1,
                    "startColumnIndex": 0, "endColumnIndex": col_count,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.07, "green": 0.35, "blue": 0.18},
                    "textFormat": {
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        "bold": True,
                    },
                    "horizontalAlignment": "CENTER",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        }]}
    ).execute()


def _format_divider(service, sheet_id: str, row: int, col_count: int):
    """Dark background, white bold text, merged across all columns."""
    zero_row = row - 1
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {
                "mergeCells": {
                    "range": {
                        "sheetId": 0,
                        "startRowIndex": zero_row, "endRowIndex": zero_row + 1,
                        "startColumnIndex": 0, "endColumnIndex": col_count,
                    },
                    "mergeType": "MERGE_ALL",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": 0,
                        "startRowIndex": zero_row, "endRowIndex": zero_row + 1,
                        "startColumnIndex": 0, "endColumnIndex": col_count,
                    },
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 0.12, "green": 0.12, "blue": 0.12},
                        "textFormat": {
                            "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                            "bold": True, "fontSize": 11,
                        },
                        "horizontalAlignment": "CENTER",
                        "verticalAlignment": "MIDDLE",
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)",
                }
            },
        ]}
    ).execute()


def _format_data_rows(service, sheet_id: str, start_row: int, allocations: list[dict]):
    """Bold ticker (col B), color-coded direction (col D)."""
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

        requests.append({"repeatCell": {
            "range": {
                "sheetId": 0,
                "startRowIndex": zero_row, "endRowIndex": zero_row + 1,
                "startColumnIndex": 1, "endColumnIndex": 2,
            },
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }})

        requests.append({"repeatCell": {
            "range": {
                "sheetId": 0,
                "startRowIndex": zero_row, "endRowIndex": zero_row + 1,
                "startColumnIndex": 3, "endColumnIndex": 4,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg_color,
                "horizontalAlignment": "CENTER",
                "textFormat": {
                    "bold": True,
                    "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                },
            }},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)",
        }})

    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": requests},
        ).execute()


def export_to_sheets(allocations: list[dict], budget: float) -> bool:
    """
    Exports allocations to Google Sheets.

    Strategy: calculate ALL row positions upfront from a single read,
    then write ALL rows in ONE worksheet.update call, then format.
    This avoids any row-tracking drift from multiple writes.
    """
    if not allocations:
        print("Sheets: nothing to export.")
        return False

    try:
        worksheet, service, sheet_id = _get_clients()
        run_date  = datetime.now().strftime("%Y-%m-%d %H:%M")
        col_count = len(HEADERS)

        # Filter valid rows only
        filtered = [
            a for a in allocations
            if a.get("ticker")
            and a.get("ticker") != "???"
            and a.get("company_name")
            and a.get("direction")
            and a.get("dollar_amount", 0) > 0
        ]

        if not filtered:
            print("Sheets: no valid rows to export after filtering.")
            return False

        # Build data rows
        data_rows = []
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

        # Read sheet state ONCE — calculate all positions before any writes
        last_data_row = _get_last_data_row(worksheet)
        is_empty      = (last_data_row == 0)

        if is_empty:
            # First export: header at row 1, data starts at row 2
            write_start    = 1
            header_row_num = 1
            data_start_row = 2
            all_rows       = [HEADERS] + data_rows
        else:
            # Subsequent exports:
            # last_data_row + 1 = blank row
            # last_data_row + 2 = divider
            # last_data_row + 3 = header
            # last_data_row + 4 = data start
            label          = f"Run: {run_date}   |   Budget: ${budget:,.2f}"
            write_start    = last_data_row + 1
            divider_row_num = last_data_row + 2
            header_row_num  = last_data_row + 3
            data_start_row  = last_data_row + 4
            all_rows        = [[""], [label], HEADERS] + data_rows

        # Write ALL rows in one call — no drift possible
        worksheet.update(f'A{write_start}', all_rows, value_input_option='RAW')

        # Format using pre-calculated positions
        _format_headers(service, sheet_id, header_row_num, col_count)
        if not is_empty:
            _format_divider(service, sheet_id, divider_row_num, col_count)
        _format_data_rows(service, sheet_id, data_start_row, filtered)

        print(f"Sheets: exported {len(filtered)} rows for {run_date}.")
        return True

    except Exception as e:
        import traceback
        print(f"Sheets export error: {e}")
        print(traceback.format_exc())
        return False


def read_history() -> list[dict]:
    """Reads all historical data, skipping dividers and headers."""
    try:
        worksheet = _get_worksheet()
        all_rows  = worksheet.get_all_values()
        if not all_rows:
            return []
        records = []
        for row in all_rows:
            if not row or not row[0]:
                continue
            if row[0] in ("Date", "") or row[0].startswith("Run:"):
                continue
            try:
                records.append({
                    "date":            row[0]  if len(row) > 0  else "",
                    "ticker":          row[1]  if len(row) > 1  else "",
                    "company":         row[2]  if len(row) > 2  else "",
                    "direction":       row[3]  if len(row) > 3  else "",
                    "amount":          float(row[4])  if len(row) > 4  and row[4]  else 0.0,
                    "allocation_pct":  float(row[5])  if len(row) > 5  and row[5]  else 0.0,
                    "risk":            row[6]  if len(row) > 6  else "",
                    "confidence":      float(row[7])  if len(row) > 7  and row[7]  else 0.0,
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
            "dollar_amount":    250.00,
            "percentage":       25.0,
            "risk_level":       "low",
            "confidence_score": 0.78,
            "entry_rationale":  "Strong AI pipeline and services growth.",
            "exit_condition":   "target 8% gain, stop loss at 3%",
            "source_title":     "Apple announces new AI features",
            "flagged":          False,
        },
        {
            "ticker":           "TSLA",
            "company_name":     "Tesla Inc.",
            "direction":        "watch",
            "dollar_amount":    150.00,
            "percentage":       15.0,
            "risk_level":       "medium",
            "confidence_score": 0.65,
            "entry_rationale":  "EV market share recovery signals.",
            "exit_condition":   "target 6% gain, stop loss at 5%",
            "source_title":     "Tesla reclaims EV market share in Q2",
            "flagged":          False,
        },
        {
            "ticker":           "ETH",
            "company_name":     "Ethereum",
            "direction":        "watch",
            "dollar_amount":    100.00,
            "percentage":       10.0,
            "risk_level":       "high",
            "confidence_score": 0.55,
            "entry_rationale":  "Layer 2 adoption accelerating.",
            "exit_condition":   "target 10% gain, stop loss at 6%",
            "source_title":     "Ethereum Layer 2 volumes hit record high",
            "flagged":          False,
        },
    ]
    result = export_to_sheets(test_allocations, 1000.0)
    print(f"Export successful: {result}")