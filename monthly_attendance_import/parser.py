"""The agreed reference layout only. No Frappe dependency or formula evaluation."""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from zipfile import ZipFile, BadZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

MAX_BYTES = 5 * 1024 * 1024
MAX_EXPANDED_BYTES = 30 * 1024 * 1024
MAX_ROWS, MAX_COLUMNS, MAX_EMPLOYEES = 2000, 80, 100
CODES = ("EA", "AB", "SB", "RH", "HD", "SL", "UH", "MD")
MONTHS = {m.upper(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


class WorkbookError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    identifier: str
    date: date
    normal_hours: Decimal | None
    overtime_hours: Decimal | None
    code: str | None
    cells: str


def hours(cell, allow_code=False):
    value = cell.value
    if cell.data_type in ("f", "e"):
        raise WorkbookError(f"{cell.coordinate}: formulas and Excel errors are forbidden")
    if value is None or value == "":
        return None, None
    if allow_code and isinstance(value, str) and value.strip().upper() in CODES:
        return None, value.strip().upper()
    if isinstance(value, bool):
        raise WorkbookError(f"{cell.coordinate}: expected hours, not Boolean")
    try:
        number = Decimal(str(value).strip())
        if not number.is_finite() or number < 0 or number > 24 or number != number.quantize(Decimal("0.01")):
            raise ValueError()
        return number, None
    except (InvalidOperation, ValueError):
        raise WorkbookError(f"{cell.coordinate}: expected 0–24 finite hours (at most two decimals) or a supported N.T code") from None


def parse(content: bytes, year: int, month: int):
    """Return entries (including blanks) and cell-addressed errors; structural errors raise."""
    if not 1900 <= year <= 2200 or not 1 <= month <= 12:
        raise WorkbookError("Select a valid month and year (1900–2200)")
    if not content or len(content) > MAX_BYTES:
        raise WorkbookError("Excel file must be nonempty and at most 5 MiB")
    try:
        with ZipFile(BytesIO(content)) as z:
            if len(z.infolist()) > 1000 or sum(i.file_size for i in z.infolist()) > MAX_EXPANDED_BYTES:
                raise WorkbookError("Expanded workbook exceeds safety limits")
        wb = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, KeyError, OSError, ValueError) as exc:
        raise WorkbookError(f"Invalid xlsx: {exc}") from exc
    try:
        if len(wb.worksheets) != 1:
            raise WorkbookError("Exactly one worksheet is required")
        ws = wb.worksheets[0]
        if not ws.max_row or not ws.max_column or ws.max_row > MAX_ROWS or ws.max_column > MAX_COLUMNS:
            raise WorkbookError("Worksheet dimensions exceed 2000 rows × 80 columns")
        # One bounded pass prevents quadratic streaming reads and ignores totals/notes.
        rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=36))
        if len(rows) < 7:
            raise WorkbookError("Missing input rows")
        if rows[3][3].value != "EMPLOYEE NUMBER" or rows[3][4].value != "HOURS":
            raise WorkbookError("D4/E4 must be EMPLOYEE NUMBER / HOURS; use Download Template")
        period = rows[2][12]
        match = re.fullmatch(r"MONTH OF\s*-\s*([A-Za-z]{3})\s*-?\s*(\d{4})", str(period.value or "").strip())
        if not match or (int(match[2]), MONTHS.get(match[1].upper())) != (year, month):
            raise WorkbookError("M3: explicit MONTH OF -MMM- YYYY must agree with selected period")
        for day, c in enumerate(rows[4][5:36], 1):
            # The reference November workbook leaves day 31's header blank.
            if c.data_type in ("f", "e") or not (c.value == day or (day > monthrange(year, month)[1] and c.value is None)):
                raise WorkbookError(f"Day header column {day + 5}, row 5: expected day {day}")
        entries, errors, seen = [], [], set()
        ended = False
        for index in range(5, len(rows), 2):
            nt = rows[index]
            if index + 1 >= len(rows):
                if nt[4].value in ("N.T", "O.T") or nt[3].value is not None:
                    raise WorkbookError(f"E{index + 1}: missing paired O.T row")
                break
            ot = rows[index + 1]
            if nt[4].value != "N.T":
                if nt[4].value is not None or ot[4].value == "O.T" or nt[3].value is not None:
                    raise WorkbookError(f"D{index + 1}/E{index + 1}: expected employee and N.T/O.T pair")
                ended = True
                break
            if ot[4].value != "O.T":
                raise WorkbookError(f"E{index + 2}: missing paired O.T row")
            if len(seen) >= MAX_EMPLOYEES:
                raise WorkbookError("At most 100 employee pairs are supported")
            cell = nt[3]
            identifier = cell.value
            # Existing reference numbers are integers. Text is mandatory in the template
            # and for zero-padded numbers; never infer digits from display formats.
            if (isinstance(identifier, int) and not isinstance(identifier, bool)
                and 0 <= identifier < 10**15 and cell.number_format in ("General", "0")):
                identifier = str(identifier)
            if cell.data_type in ("f", "e") or not isinstance(identifier, str) or not identifier.strip():
                raise WorkbookError(f"D{index + 1}: employee number must be text (unformatted integers also accepted)")
            identifier = identifier.strip()
            if identifier in seen:
                raise WorkbookError(f"D{index + 1}: duplicate employee number {identifier}")
            if ot[3].value not in (None, "", cell.value):
                raise WorkbookError(f"D{index + 2}: N.T/O.T employee numbers disagree")
            seen.add(identifier)
            for day in range(1, 32):
                a, b = nt[day + 4], ot[day + 4]
                column = get_column_letter(day + 5)
                cells = f"{ws.title}!{column}{index + 1}/{column}{index + 2}"
                context = f"{identifier} {year:04d}-{month:02d}-{day:02d} ({cells})"
                if day > monthrange(year, month)[1]:
                    if a.value is not None or b.value is not None:
                        errors.append(f"{context}: populated invalid calendar day")
                    continue
                try:
                    normal, code = hours(a, True)
                    overtime, _ = hours(b)
                    if (normal or 0) + (overtime or 0) > 24:
                        raise WorkbookError("normal plus overtime hours exceed 24")
                    entries.append(Entry(identifier, date(year, month, day), normal, overtime, code, cells))
                except WorkbookError as exc:
                    errors.append(f"{context}: {exc}")
        if not seen:
            raise WorkbookError("No employee N.T/O.T pairs found from row 6")
        if ended:
            # A gap must not silently discard a later employee block.
            for row in rows[index + 2:]:
                if row[4].value in ("N.T", "O.T"):
                    raise WorkbookError(f"E{row[4].row}: employee pairs must be contiguous")
        return entries, errors
    finally:
        wb.close()
