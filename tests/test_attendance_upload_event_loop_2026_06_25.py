"""根因 B（2026-06-25 嚴重度門檻稽核）：考勤 Excel 上傳 handler 不可在 async def 內
同步跑重工作（pd.read_excel + 全表查詢 + 逐列 LeaveRecord 合併 + commit），否則單一
uvicorn worker 部署下會凍結 event loop → 上傳期間家長 LIFF / 教師 portal / /health 全
停擺（health 逾時還可能觸發容器重啟）。對照同檔 CSV 版（def upload_attendance_csv）為
正確的同步 def（FastAPI 會丟 threadpool）。

修法：保留 async handler 做唯一的 await（chunked 檔案讀取），其後所有同步重工作抽成
helper _process_attendance_upload，經 asyncio.to_thread 丟 threadpool 執行。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.attendance.upload as upload_mod


class _FakeUpload:
    """最小 async UploadFile：.filename + 單塊 async .read()。"""

    filename = "punch.xlsx"

    def __init__(self) -> None:
        self._sent = False

    async def read(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        # xlsx magic bytes（PK\x03\x04）讓 size-check/validate 過；真正 pandas 解析在
        # 被 patch 掉的 helper 內，不會實際執行。
        return b"PK\x03\x04dummy-content"


def test_process_attendance_upload_is_sync_function():
    """重工作 helper 必須是同步函式（非 coroutine），才能安全在 threadpool 跑。"""
    assert hasattr(
        upload_mod, "_process_attendance_upload"
    ), "應抽出同步 helper _process_attendance_upload 承載重工作"
    assert not inspect.iscoroutinefunction(upload_mod._process_attendance_upload)


def test_upload_handler_delegates_heavy_work_off_event_loop():
    """handler 仍 async（檔案讀取需 await），但 pandas/DB 重工作不留在 handler body，
    改經 asyncio.to_thread 丟 threadpool。"""
    assert inspect.iscoroutinefunction(upload_mod.upload_attendance)
    handler_src = inspect.getsource(upload_mod.upload_attendance)
    assert (
        "asyncio.to_thread" in handler_src
    ), "handler 應以 asyncio.to_thread 把重工作丟 threadpool，避免阻塞 event loop"
    assert (
        "pd.read_excel" not in handler_src
    ), "pd.read_excel 等重工作不應留在 async handler body（會阻塞 event loop）"
    proc_src = inspect.getsource(upload_mod._process_attendance_upload)
    assert (
        "pd.read_excel" in proc_src
    ), "pandas 解析應移到 _process_attendance_upload 內"


def test_processing_runs_in_different_thread_than_event_loop(monkeypatch):
    """行為驗證：_process_attendance_upload 確實在「不同於 event loop」的 thread 執行
    （證明重工作未在 event loop thread 上同步阻塞）。"""
    loop_thread: dict = {}
    proc_thread: dict = {}

    def fake_process(content, raw_ext, current_user):
        proc_thread["id"] = threading.get_ident()
        return {"ok": True, "ext": raw_ext}

    monkeypatch.setattr(upload_mod, "_process_attendance_upload", fake_process)

    async def driver():
        loop_thread["id"] = threading.get_ident()
        return await upload_mod.upload_attendance(
            file=_FakeUpload(), current_user={"user_id": 1, "role": "admin"}
        )

    result = asyncio.run(driver())
    assert result == {"ok": True, "ext": ".xlsx"}
    assert (
        proc_thread["id"] != loop_thread["id"]
    ), "重工作應在 threadpool thread 執行（≠ event loop thread），證明未阻塞 event loop"
