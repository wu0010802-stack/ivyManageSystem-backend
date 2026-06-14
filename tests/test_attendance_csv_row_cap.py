"""考勤 CSV 上傳須有列數上限（C4，bug hunt money-auth 2026-06-14）。

POST /attendance/upload-csv 以 JSON body 接收 records: List[AttendanceCSVRow]，原無
max_length，與 xlsx 路徑（utils.excel_io MAX_IMPORT_ROWS=10000 主動拒絕）不對稱 →
認證後可送數百萬筆撐爆記憶體 DoS。修法：schema records 加 Field(max_length=MAX_IMPORT_ROWS)。

schema 層驗證直接測，不經 HTTP/auth，精準鎖住修補點。
"""

import pytest
from pydantic import ValidationError

from api.attendance._shared import AttendanceUploadRequest
from utils.excel_io import MAX_IMPORT_ROWS


def _row() -> dict:
    return {
        "department": "教學部",
        "employee_number": "E001",
        "name": "王測試",
        "date": "2026-06-01",
        "weekday": "一",
    }


def test_upload_request_rejects_over_cap():
    """records 超過 MAX_IMPORT_ROWS 應在 schema 驗證即被拒（防 OOM DoS）。"""
    with pytest.raises(ValidationError):
        AttendanceUploadRequest(
            records=[_row()] * (MAX_IMPORT_ROWS + 1),
            year=2026,
            month=6,
        )


def test_upload_request_accepts_within_cap():
    """正常列數（上限內）仍可建構，不誤擋合法批次匯入。"""
    req = AttendanceUploadRequest(records=[_row()] * 3, year=2026, month=6)
    assert len(req.records) == 3
