"""驗證 excel_io 骨架：success / 缺欄 / 型別錯 / Chinese alias / extra=forbid 行為。"""

import io
import sys
import os
from typing import Optional

import pytest
from openpyxl import Workbook
from pydantic import Field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.excel_io import parse_excel, ExcelImportSchema, ImportResult


class _LeaveImportRow(ExcelImportSchema):
    """模擬 leaves 匯入：英文欄位（測試用）。"""

    employee_name: str
    start_date: str
    end_date: str
    leave_type: str


class _LeaveImportRowCN(ExcelImportSchema):
    """模擬 leaves 匯入：中文欄位 via alias（驗證 model_validate 對 alias 解析）。"""

    employee_code: Optional[str] = Field(default=None, alias="員工編號")
    employee_name: str = Field(alias="員工姓名")
    leave_type: str = Field(alias="假別代碼")
    start_date: str = Field(alias="開始日期")
    end_date: str = Field(alias="結束日期")
    leave_hours: Optional[float] = Field(default=None, alias="時數(可空)")
    reason: Optional[str] = Field(default=None, alias="原因(可空)")


def _build_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_parse_excel_success():
    xlsx = _build_xlsx(
        [
            ["employee_name", "start_date", "end_date", "leave_type"],
            ["王小明", "2026-05-15", "2026-05-15", "事假"],
        ]
    )
    result = parse_excel(xlsx, schema=_LeaveImportRow)
    assert result.errors == []
    assert len(result.rows) == 1
    assert result.rows[0].employee_name == "王小明"


def test_parse_excel_missing_column():
    xlsx = _build_xlsx(
        [
            ["employee_name", "start_date", "leave_type"],  # 缺 end_date
            ["王小明", "2026-05-15", "事假"],
        ]
    )
    result = parse_excel(xlsx, schema=_LeaveImportRow)
    assert any(e["error_code"] == "MISSING_COLUMN" for e in result.errors)


def test_parse_excel_row_validation_error_format():
    """error 格式 {row, col, value, error_code, message}"""
    xlsx = _build_xlsx(
        [
            ["employee_name", "start_date", "end_date", "leave_type"],
            [None, "2026-05-15", "2026-05-15", "事假"],
        ]
    )
    result = parse_excel(xlsx, schema=_LeaveImportRow)
    assert len(result.errors) > 0
    err = result.errors[0]
    assert all(k in err for k in ("row", "col", "value", "error_code", "message"))
    # 行號 = 2（header 1 + data 1）
    assert err["row"] == 2


def test_parse_excel_empty_file():
    wb = Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    result = parse_excel(buf, schema=_LeaveImportRow)
    assert any(
        e["error_code"] in ("EMPTY_FILE", "MISSING_COLUMN") for e in result.errors
    )


def test_parse_excel_chinese_alias_resolves():
    """驗證 ExcelImportSchema 用 model_validate 解析中文 alias headers。

    這是 leaves/overtimes 匯入的關鍵 path — 直接 schema(**record) 會無視 alias。
    """
    xlsx = _build_xlsx(
        [
            [
                "員工編號",
                "員工姓名",
                "假別代碼",
                "開始日期",
                "結束日期",
                "時數(可空)",
                "原因(可空)",
            ],
            ["E001", "王小明", "annual", "2026-03-15", "2026-03-15", 8.0, "年度特休"],
        ]
    )
    result = parse_excel(xlsx, schema=_LeaveImportRowCN)
    assert result.errors == [], f"unexpected errors: {result.errors}"
    assert len(result.rows) == 1
    assert result.rows[0].employee_name == "王小明"
    assert result.rows[0].leave_hours == 8.0


def test_parse_excel_optional_cells_can_be_empty():
    """時數/原因 為 Optional，空值不應觸發驗證錯誤。"""
    xlsx = _build_xlsx(
        [
            [
                "員工編號",
                "員工姓名",
                "假別代碼",
                "開始日期",
                "結束日期",
                "時數(可空)",
                "原因(可空)",
            ],
            ["E001", "王小明", "annual", "2026-03-15", "2026-03-15", None, None],
        ]
    )
    result = parse_excel(xlsx, schema=_LeaveImportRowCN)
    assert result.errors == [], f"unexpected errors: {result.errors}"
    assert result.rows[0].leave_hours is None
    assert result.rows[0].reason is None


def test_parse_excel_row_cap(monkeypatch):
    """R7-2：超過 MAX_IMPORT_ROWS 即停止解析 + 回報 TOO_MANY_ROWS（防超大檔 OOM）。"""
    import utils.excel_io as excel_io

    monkeypatch.setattr(excel_io, "MAX_IMPORT_ROWS", 3)
    header = ["employee_name", "start_date", "end_date", "leave_type"]
    data = ["王小明", "2026-05-15", "2026-05-15", "事假"]
    xlsx = _build_xlsx([header] + [data] * 5)  # 5 data rows > cap 3
    result = parse_excel(xlsx, schema=_LeaveImportRow)
    assert any(e["error_code"] == "TOO_MANY_ROWS" for e in result.errors)
    assert len(result.rows) <= 3
