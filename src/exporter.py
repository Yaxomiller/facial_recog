from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from src.config import EXPORT_DIR
from src.db import attendance_between


def export_attendance_to_excel(start_date: str, end_date: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Attendance"

    headers = ["Name", "Date", "Time", "Status", "Confidence", "Source"]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    rows = attendance_between(start_date, end_date)
    for row in rows:
        sheet.append(
            [
                row["name"],
                row["attendance_date"],
                row["attendance_time"],
                row["status"],
                row["confidence"],
                row["source"],
            ]
        )

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = max(max_length + 2, 14)

    file_path = EXPORT_DIR / f"attendance_{start_date}_to_{end_date}.xlsx"
    workbook.save(file_path)
    return file_path


def export_today() -> Path:
    today = date.today().isoformat()
    return export_attendance_to_excel(today, today)
