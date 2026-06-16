"""考勤異常清單 Excel 匯出須過 SafeWorksheet 防公式注入（SEC-005 / 資安掃描 2026-06-15 P2）。

download_anomaly_report 原以裸 wb.active 寫入 emp.name / att.status，Employee.name 由
HR/admin 自由編輯且無 pattern 驗證，若含 =cmd|'/C calc'!A0 會在主管/HR 端 Excel 開啟
時被當公式執行（DDE/HYPERLINK），異常清單整列員工資料可被外滲。

修法：抽出純函式 _build_anomaly_report_workbook(rows) 並改用 SafeWorksheet(wb.active)。
"""

from datetime import date, time
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from api.attendance.reports import _build_anomaly_report_workbook

EVIL = "=cmd|'/C calc'!A0"


def _all_values(wb) -> list:
    return [
        c.value for row in wb.active.iter_rows() for c in row if c.value is not None
    ]


def _row(name: str, status: str = "遲到"):
    att = SimpleNamespace(
        is_late=True,
        is_early_leave=False,
        is_missing_punch_in=False,
        is_missing_punch_out=False,
        attendance_date=date(2026, 5, 6),
        punch_in_time=time(9, 5),
        punch_out_time=time(18, 0),
        status=status,
        late_minutes=5,
        early_leave_minutes=0,
    )
    emp = SimpleNamespace(name=name)
    return (att, emp)


def test_anomaly_workbook_sanitizes_employee_name_injection():
    wb = _build_anomaly_report_workbook([_row(EVIL)])
    vals = _all_values(wb)
    assert EVIL not in vals, "員工姓名公式注入未被清理（原樣寫入 → 主管/HR 端可被執行）"
    assert ("'" + EVIL) in vals, "員工姓名應被 sanitize 為 ' 前綴純字串"


def test_anomaly_workbook_sanitizes_status_injection():
    # status 在無 state flag 命中時會 fall back 到 att.status（直寫）
    att = SimpleNamespace(
        is_late=False,
        is_early_leave=False,
        is_missing_punch_in=False,
        is_missing_punch_out=False,
        attendance_date=date(2026, 5, 6),
        punch_in_time=None,
        punch_out_time=None,
        status=EVIL,
        late_minutes=0,
        early_leave_minutes=0,
    )
    emp = SimpleNamespace(name="王小明")
    wb = _build_anomaly_report_workbook([(att, emp)])
    vals = _all_values(wb)
    assert EVIL not in vals, "狀態欄公式注入未被清理"
    assert ("'" + EVIL) in vals


def test_anomaly_workbook_preserves_normal_name():
    wb = _build_anomaly_report_workbook([_row("王小明")])
    vals = _all_values(wb)
    assert "王小明" in vals, "正常姓名不應被竄改"
