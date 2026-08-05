from __future__ import annotations

import io
from collections import defaultdict
from datetime import date as date_cls, datetime
from typing import Any


def parse_timesheet(
    fileobj,
    *,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """Parse a regular attendance timesheet (1 = present, 0/blank = absent).

    Formula:
      Mon–Fri : 9 h × 1 = 9 wh per person
      Sat/Sun : 9 h × 2 = 18 wh per person
    Weekend multiplier only applied when year + month are supplied.

    Returns:
        {
          "total_manpower": int,          # peak daily headcount
          "total_man_hours": float,       # sum of weighted work-hours
          "daily_breakdown": [{"date": str, "headcount": int, "hours": float}, ...]
        }
    """
    rows = _load_rows(fileobj)
    result = _try_layout_a(rows, year=year, month=month) or _try_layout_b(rows, year=year, month=month)
    if not result:
        raise ValueError(
            "Could not detect a recognised timesheet format. "
            "Expected (A) an employee × date matrix with day-number column headers (1–31), "
            "or (B) daily summary rows: date | headcount | man-hours."
        )
    return result


def parse_overtime_timesheet(
    fileobj,
    *,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, Any]:
    """Parse an OT timesheet where cell values are actual OT hours worked.

    Format: same employee × day matrix as the regular timesheet, but cells
    contain the number of OT hours (e.g. 2, 3.5) instead of 0/1 attendance marks.
    OT hours are counted at flat 1× (no weekend multiplier).

    Returns:
        {
          "total_ot_hours": float,
          "daily_ot_breakdown": [{"date": str, "headcount": int, "hours": float}, ...]
        }
    """
    rows = _load_rows(fileobj)
    result = _try_layout_a_ot(rows, year=year, month=month)
    if not result:
        raise ValueError(
            "Could not detect an OT timesheet layout. "
            "Expected an employee × date matrix with day-number column headers (1–31) "
            "and numeric OT hours as cell values."
        )
    return result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ATTENDANCE_MARKS = {"v", "✓", "√", "x", "y", "h", "hadir", "p", "1"}
_SKIP_LABELS = {"total", "jumlah", "amount", "sub total", "sub-total", "grand total"}

REGULAR_HOURS = 9.0
WEEKEND_MULTIPLIER = 2.0


def _load_rows(fileobj) -> list[list]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to parse Excel timesheets") from exc
    raw = fileobj.read() if hasattr(fileobj, "read") else fileobj
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows = [list(row) for row in ws.iter_rows(values_only=True)]
    wb.close()
    rows = [r for r in rows if any(c is not None for c in r)]
    if not rows:
        raise ValueError("Timesheet appears to be empty.")
    return rows


def _num(cell, default: float = 0.0) -> float:
    if isinstance(cell, bool):
        return default
    if isinstance(cell, (int, float)):
        return float(cell)
    try:
        return float(str(cell or "").replace(",", "").strip())
    except ValueError:
        return default


def _weighted_hours(day_num: int, year: int | None, month: int | None) -> float:
    """Return weighted work-hours for one present employee on the given day."""
    if year and month:
        try:
            d = date_cls(year, month, day_num)
            multiplier = WEEKEND_MULTIPLIER if d.weekday() >= 5 else 1.0
            return REGULAR_HOURS * multiplier
        except ValueError:
            pass
    return REGULAR_HOURS


def _day_date_str(day_num: int, year: int | None, month: int | None) -> str:
    if year and month:
        try:
            return date_cls(year, month, day_num).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return f"day-{day_num:02d}"


def _detect_day_cols(row: list) -> dict[int, int]:
    """Return {col_index: day_number} for a header row containing day numbers 1–31.

    Requires at least 10 *distinct* day numbers spanning a range of at least 10,
    so that data rows filled with 1s (present/absent marks) are never mistaken
    for date-header rows.
    """
    day_cols: dict[int, int] = {}
    for j, cell in enumerate(row):
        if isinstance(cell, bool):
            continue
        if isinstance(cell, int) and 1 <= cell <= 31:
            day_cols[j] = cell
        elif isinstance(cell, (date_cls, datetime)):
            day_cols[j] = cell.day
    distinct = set(day_cols.values())
    if len(distinct) < 10:
        return {}
    if max(distinct) - min(distinct) < 9:
        return {}
    return day_cols


# ---------------------------------------------------------------------------
# Layout A — employee × date matrix (attendance 0/1)
# ---------------------------------------------------------------------------

def _try_layout_a(
    rows: list[list],
    *,
    year: int | None,
    month: int | None,
) -> dict[str, Any] | None:
    for header_idx, row in enumerate(rows[:20]):
        day_cols = _detect_day_cols(row)
        if not day_cols:
            continue

        daily: dict[int, dict[str, float]] = defaultdict(lambda: {"headcount": 0.0, "hours": 0.0})

        for data_row in rows[header_idx + 1:]:
            if not any(data_row):
                continue
            first = str(data_row[0] or "").strip().lower()
            if first in _SKIP_LABELS:
                continue
            # Skip rows that look like another header (distinct day numbers)
            if _detect_day_cols(data_row):
                continue
            for col_idx, day_num in day_cols.items():
                if col_idx >= len(data_row):
                    continue
                cell = data_row[col_idx]
                mark = str(cell or "").strip().lower()
                is_present = mark in _ATTENDANCE_MARKS
                if not is_present:
                    h = _num(cell)
                    is_present = h > 0
                if is_present:
                    daily[day_num]["headcount"] += 1
                    daily[day_num]["hours"] += _weighted_hours(day_num, year, month)

        breakdown = [
            {
                "date": _day_date_str(d, year, month),
                "headcount": int(daily[d]["headcount"]),
                "hours": round(daily[d]["hours"], 2),
            }
            for d in sorted(daily)
            if daily[d]["headcount"] > 0
        ]
        if not breakdown:
            return None

        return {
            "total_manpower": max(b["headcount"] for b in breakdown),
            "total_man_hours": round(sum(b["hours"] for b in breakdown), 2),
            "daily_breakdown": breakdown,
        }
    return None


# ---------------------------------------------------------------------------
# Layout B — daily summary rows (date | headcount | hours)
# ---------------------------------------------------------------------------

def _try_layout_b(
    rows: list[list],
    *,
    year: int | None,
    month: int | None,
) -> dict[str, Any] | None:
    breakdown = []
    for row in rows:
        cells = [c for c in row if c is not None]
        if len(cells) < 2:
            continue

        date_cell = cells[0]
        day_num: int | None = None
        date_str = ""

        if isinstance(date_cell, (date_cls, datetime)):
            day_num = date_cell.day
            date_str = date_cell.strftime("%Y-%m-%d")
        elif isinstance(date_cell, (int, float)) and 1 <= int(date_cell) <= 31:
            day_num = int(date_cell)
            date_str = _day_date_str(day_num, year, month)
        else:
            s = str(date_cell or "").strip()
            try:
                sep = "/" if "/" in s else "-"
                parts = s.split(sep)
                candidate = int(parts[0])
                if not 1 <= candidate <= 31:
                    continue
                day_num = candidate
                date_str = _day_date_str(day_num, year, month)
            except (ValueError, IndexError):
                continue

        if day_num is None:
            continue

        nums = [_num(c) for c in cells[1:] if _num(c, -1) >= 0]
        if not nums:
            continue
        headcount = int(round(nums[0]))
        # If hours column present use it; otherwise calculate from headcount
        if len(nums) > 1:
            hours = nums[1]
        else:
            hours = headcount * _weighted_hours(day_num, year, month)
        if headcount <= 0 and hours <= 0:
            continue
        breakdown.append({"date": date_str, "headcount": headcount, "hours": round(hours, 2)})

    if len(breakdown) < 3:
        return None

    return {
        "total_manpower": max(b["headcount"] for b in breakdown),
        "total_man_hours": round(sum(b["hours"] for b in breakdown), 2),
        "daily_breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# OT Layout A — employee × date matrix (cell = actual OT hours, flat 1×)
# ---------------------------------------------------------------------------

def _try_layout_a_ot(
    rows: list[list],
    *,
    year: int | None,
    month: int | None,
) -> dict[str, Any] | None:
    for header_idx, row in enumerate(rows[:20]):
        day_cols = _detect_day_cols(row)
        if not day_cols:
            continue

        daily: dict[int, dict[str, float]] = defaultdict(lambda: {"headcount": 0.0, "hours": 0.0})

        for data_row in rows[header_idx + 1:]:
            if not any(data_row):
                continue
            first = str(data_row[0] or "").strip().lower()
            if first in _SKIP_LABELS:
                continue
            if _detect_day_cols(data_row):
                continue
            for col_idx, day_num in day_cols.items():
                if col_idx >= len(data_row):
                    continue
                ot_hours = _num(data_row[col_idx])
                if ot_hours > 0:
                    daily[day_num]["headcount"] += 1
                    daily[day_num]["hours"] += ot_hours  # flat 1× — no multiplier

        breakdown = [
            {
                "date": _day_date_str(d, year, month),
                "headcount": int(daily[d]["headcount"]),
                "hours": round(daily[d]["hours"], 2),
            }
            for d in sorted(daily)
            if daily[d]["hours"] > 0
        ]
        if not breakdown:
            return None

        return {
            "total_ot_hours": round(sum(b["hours"] for b in breakdown), 2),
            "daily_ot_breakdown": breakdown,
        }
    return None
