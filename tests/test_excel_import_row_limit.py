"""假日 / 班表 Excel 匯入須有列數上限防 DoS（SEC-006 / 資安掃描 2026-06-15 P2）。

events.import_holidays 與 shifts._import_shifts_sync 原以 pd.read_excel(BytesIO(content))
無 nrows 上限載入。xlsx 為 ZIP 高度可壓縮，~1,048,575 列可壓到 <10MB 通過 size cap，
pd.read_excel 會在任何 per-row loop 前全載入（實測 ~68 秒 CPU + ~315MB），events 在
async def 內同步執行更直接阻塞事件迴圈凍結全服務。

修法：仿 api/attendance/upload.py:132 既有守衛，read_excel 帶 nrows=MAX_IMPORT_ROWS+1
後 if len(df) > MAX_IMPORT_ROWS 即 raise HTTPException(400)。

註：本檔測試一律請求 db_session fixture，將全域 engine swap 成隔離 SQLite，
避免（修補前的 RED 路徑）誤打開發 DB 產生副作用。
"""

import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from utils.excel_io import MAX_IMPORT_ROWS


def _xlsx_with_rows(n_data_rows: int) -> bytes:
    """產一份 header + n_data_rows 列的 xlsx bytes（欄位內容對列數守衛無關緊要）。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["日期", "備註"])
    for i in range(n_data_rows):
        ws.append(["2026-01-01", i])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_shifts_import_rejects_over_row_limit(test_db_session):
    from api.shifts import _import_shifts_sync

    content = _xlsx_with_rows(MAX_IMPORT_ROWS + 1)
    with pytest.raises(HTTPException) as ei:
        _import_shifts_sync(content, "2026-01-05", "tester")
    assert ei.value.status_code == 400
    assert "上限" in str(ei.value.detail), "應因超過匯入列數上限被拒絕"


def test_holidays_import_rejects_over_row_limit(test_db_session):
    from api.events import import_holidays

    content = _xlsx_with_rows(MAX_IMPORT_ROWS + 1)
    upload = UploadFile(file=BytesIO(content), filename="holidays.xlsx")

    async def _call():
        return await import_holidays(
            file=upload,
            force=False,
            force_reason=None,
            current_user={"username": "tester", "employee_id": 1, "role": "admin"},
        )

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_call())
    assert ei.value.status_code == 400
    assert "上限" in str(ei.value.detail), "應因超過匯入列數上限被拒絕"


def test_shifts_import_under_limit_does_not_trip_row_guard(test_db_session):
    """列數在上限內時不可在列數守衛處被拒。

    用非法 week_start 讓流程於『列數守衛之後』停下並 raise 400，
    驗證該 400 不是『匯入列數超過上限』。
    """
    from api.shifts import _import_shifts_sync

    content = _xlsx_with_rows(3)
    with pytest.raises(HTTPException) as ei:
        _import_shifts_sync(content, "not-a-date", "tester")
    assert ei.value.status_code == 400
    assert "上限" not in str(ei.value.detail)
