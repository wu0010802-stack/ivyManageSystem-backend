"""Regression: api/overtimes.py:import_overtimes 改用 utils.excel_io.parse_excel。

2026-05-19：把 pd.read_excel 換成 parse_excel + OvertimeImportRow（仿 leaves.py
2026-05-13 改動）。本檔僅 schema-level 回歸，確認：
- OvertimeImportRow 的 9 個 alias 與 /overtimes/import-template 範本 header 對齊
- parse_excel + OvertimeImportRow 能正確 parse 真實 xlsx
- 可空欄位（開始時間/結束時間/原因/補休）留空時 row 屬性為 None，業務驗證
  仍交給 endpoint
- 缺 header 時 schema 因全 Optional 不擋（與 LeaveImportRow 行為一致），
  讓 endpoint 在後續逐筆迴圈 raise ValueError

業務行為（時數上限、加班類型 enum、HH:MM 解析、重疊檢查、月度上限）已由
test_leave_overtime_security_fixes.py 等既有 E2E 測試覆蓋，本檔不重複。
"""

import io
import os
import sys

from openpyxl import Workbook

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.overtimes import OvertimeImportRow
from utils.excel_io import parse_excel

_TEMPLATE_HEADERS = [
    "員工編號",
    "員工姓名",
    "加班日期",
    "加班類型",
    "時數",
    "開始時間(可空)",
    "結束時間(可空)",
    "原因(可空)",
    "補休(是/否,可空)",
]


def _xlsx_bytes(rows: list[list], headers: list[str] | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers if headers is not None else _TEMPLATE_HEADERS)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestOvertimeImportRowSchema:
    def test_aliases_match_import_template(self):
        # 範本下載 endpoint (get_overtime_import_template) 寫死 9 個中文 header，
        # OvertimeImportRow alias 集合必須完全一致；否則 user 用範本填的檔案會被
        # parse_excel 忽略對應欄位，全部 row 變 None。
        aliases = {info.alias for info in OvertimeImportRow.model_fields.values()}
        assert aliases == set(_TEMPLATE_HEADERS)

    def test_parse_valid_row(self):
        data = _xlsx_bytes(
            [
                [
                    "E001",
                    "王小明",
                    "2026-03-15",
                    "weekday",
                    2,
                    "18:00",
                    "20:00",
                    "開學準備",
                    "否",
                ],
            ]
        )
        result = parse_excel(io.BytesIO(data), schema=OvertimeImportRow)
        assert result.errors == []
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.employee_code == "E001"
        assert row.employee_name == "王小明"
        assert row.overtime_type == "weekday"
        assert row.hours == 2
        assert row.reason == "開學準備"
        # 業務層用 str(raw).strip() + split(":")，所以 row 屬性保留原始值即可
        assert row.start_time is not None
        assert row.end_time is not None
        assert row.use_comp_leave is not None

    def test_parse_empty_optional_columns(self):
        data = _xlsx_bytes(
            [
                ["E001", "王小明", "2026-03-15", "weekday", 2, None, None, None, None],
            ]
        )
        result = parse_excel(io.BytesIO(data), schema=OvertimeImportRow)
        assert result.errors == []
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.start_time is None
        assert row.end_time is None
        assert row.reason is None
        assert row.use_comp_leave is None

    def test_missing_header_not_blocked_by_schema(self):
        # 全 Optional 設計：使用者自製檔漏掉「加班類型」header，schema 不擋，
        # row.overtime_type 變 None，由 endpoint 逐筆 raise ValueError("無效的加班類型")。
        # 這與 LeaveImportRow 行為一致，避免整檔 reject 造成 UX 砍掉重練。
        headers = [h for h in _TEMPLATE_HEADERS if h != "加班類型"]
        data = _xlsx_bytes(
            [["E001", "王小明", "2026-03-15", 2, "18:00", "20:00", "x", "否"]],
            headers=headers,
        )
        result = parse_excel(io.BytesIO(data), schema=OvertimeImportRow)
        assert result.errors == []
        assert len(result.rows) == 1
        assert result.rows[0].overtime_type is None

    def test_empty_workbook_yields_file_level_error(self):
        # 完全空檔（沒 header 也沒資料）→ EMPTY_FILE，endpoint 應 raise 400
        wb = Workbook()
        buf = io.BytesIO()
        wb.save(buf)
        result = parse_excel(buf, schema=OvertimeImportRow)
        codes = {e["error_code"] for e in result.errors}
        assert "EMPTY_FILE" in codes
