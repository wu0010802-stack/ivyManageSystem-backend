"""回歸測試：Excel 匯入無排班分支改走共用班別視窗 util（含跨夜 normalize）

原始問題（BE-3 修補目標）：
  api/attendance/upload.py 的 Excel 新格式路徑，當員工無 DailyShift / 週排班時，
  inline 計算 work_end_dt = datetime.combine(attendance_date, work_end) ──
  不知道 work_end < work_start 代表跨夜，導致跨夜班早退完全無法偵測：
    punch_out_time（已被 cross-night 修正為 next_day 05:00）
      < work_end_dt（today 06:00）→ False（無法偵測早退）。

修補方向：改呼叫 compute_status_for_employee_date()，
  該 util 的 resolve_shift_window 會做跨夜 +1day normalize：
    end_dt（next_day 06:00）> punch_out（next_day 05:00）→ 正確偵測早退。

測試 A（跨夜早退，RED → GREEN）：
  員工 work_start_time="22:00", work_end_time="06:00"，無排班。
  打卡 22:05 / 05:00（早退 1 小時）。
  修補前：is_early_leave=False（跨夜 end_dt 未 normalize，偵測失效）。
  修補後：is_early_leave=True, early_leave_minutes=60。

測試 B（一般班次，特徵化回歸）：
  員工 work_start_time="09:00", work_end_time="18:00"，無排班。
  打卡 09:05 / 18:00。
  修補前後皆應：is_late=True, late_minutes=5, is_early_leave=False。
"""

import io
import os
import sys
from datetime import date

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.database import Attendance, Employee, User
from utils.auth import hash_password


@pytest.fixture
def excel_client(tmp_path, monkeypatch):
    """隔離 SQLite app + 將 LocalStorage 寫入 tmp_path 避免汙染 repo。"""
    # 重置 storage singleton，確保不用到其他測試留下的 backend
    import utils.storage as storage_mod
    from utils.cache_layer import reset_cache_for_testing

    monkeypatch.setattr(storage_mod, "_BACKEND_SINGLETON", None)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "uploads"))

    # 重置 cache singleton，防止前一個測試把空 ShiftType dict 快取住
    reset_cache_for_testing()

    db_path = tmp_path / "excel_no_shift.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()

    # 收尾重置 cache，防止把空 ShiftType dict 污染後續測試
    reset_cache_for_testing()

    # 回收 singleton，不影響後續測試
    monkeypatch.setattr(storage_mod, "_BACKEND_SINGLETON", None)


def _seed_employee_and_admin(
    sf, *, work_start: str, work_end: str, emp_id: str, emp_name: str
):
    """建立指定工時的員工 + 純 admin 帳號（無 employee_id 的自我守衛豁免帳）。"""
    with sf() as s:
        emp = Employee(
            employee_id=emp_id,
            name=emp_name,
            base_salary=30000,
            is_active=True,
            work_start_time=work_start,
            work_end_time=work_end,
        )
        s.add(emp)
        s.flush()
        emp_db_id = emp.id

        admin = User(
            username=f"pure_admin_{emp_id}",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,  # 純 admin，不觸發自我守衛
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
    return emp_db_id


def _make_excel_bytes(rows: list[dict]) -> bytes:
    """產生含「部門/編號/姓名/日期/星期/上班時間/下班時間」欄位的 xlsx（in-memory）。"""
    df = pd.DataFrame(
        rows, columns=["部門", "編號", "姓名", "日期", "星期", "上班時間", "下班時間"]
    )
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf.getvalue()


def _login(client, username: str):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )
    assert res.status_code == 200, res.text
    return res


def _upload_excel(client, xlsx_bytes: bytes):
    return client.post(
        "/api/attendance/upload",
        files={
            "file": (
                "attendance.xlsx",
                xlsx_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


# --------------------------------------------------------------------------- #
# 測試 A：跨夜班無排班 → 早退偵測（RED 前 / GREEN 後）
# --------------------------------------------------------------------------- #


class TestOvernightNoShiftEarlyLeave:
    """跨夜班員工無排班，Excel 匯入應正確偵測早退。

    員工設定 work_start_time="22:00", work_end_time="06:00"（跨夜）。
    無 DailyShift / 週排班（非導師/助教）→ 走無 shift_data 分支。
    打卡：22:05 上班（遲到 5 分）/ 05:00 下班（早退 60 分）。

    修補前（inline 分支）：
      work_end_dt = datetime.combine(date, time("06:00")) = today 06:00
      punch_out_time = next_day 05:00（cross-night 修正後）
      next_day 05:00 < today 06:00 → FALSE → is_early_leave=False （漏偵測）

    修補後（compute_status_for_employee_date）：
      resolve_shift_window 做跨夜 normalize → end_dt = next_day 06:00
      next_day 05:00 < next_day 06:00 → TRUE → is_early_leave=True, 60 分
    """

    ATTENDANCE_DATE = date(2026, 3, 10)
    EMP_ID = "E_NIGHT"
    EMP_NAME = "跨夜師"
    USERNAME = "pure_admin_E_NIGHT"

    def test_overnight_early_leave_detected(self, excel_client):
        """修補後，跨夜班員工早退 60 分鐘應被正確偵測。"""
        client, sf = excel_client
        _seed_employee_and_admin(
            sf,
            work_start="22:00",
            work_end="06:00",
            emp_id=self.EMP_ID,
            emp_name=self.EMP_NAME,
        )
        _login(client, self.USERNAME)

        xlsx = _make_excel_bytes(
            [
                {
                    "部門": "夜班",
                    "編號": self.EMP_ID,
                    "姓名": self.EMP_NAME,
                    "日期": self.ATTENDANCE_DATE.strftime("%Y/%m/%d"),
                    "星期": "二",
                    "上班時間": "22:05",  # 遲到 5 分
                    "下班時間": "05:00",  # 早退 60 分（相對 06:00）
                }
            ]
        )
        res = _upload_excel(client, xlsx)
        assert res.status_code == 200, res.text

        data = res.json()
        assert data["anomaly_count"] == 0, f"不應有錯誤：{data.get('anomalies')}"

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter_by(attendance_date=self.ATTENDANCE_DATE)
                .first()
            )
        assert att is not None, "考勤記錄未寫入"
        assert att.is_late is True, f"期望遲到，實際 is_late={att.is_late}"
        assert (
            att.late_minutes == 5
        ), f"期望遲到 5 分（22:05 對 22:00），實際 late_minutes={att.late_minutes}"
        assert att.is_early_leave is True, (
            f"修補前 Bug：跨夜班早退應被偵測（05:00 vs 06:00 next day），"
            f"實際 is_early_leave={att.is_early_leave}。"
            f"這表示 inline work_end_dt 未做跨夜 normalize。"
        )
        assert (
            att.early_leave_minutes == 60
        ), f"期望早退 60 分（05:00 vs 06:00），實際 early_leave_minutes={att.early_leave_minutes}"


# --------------------------------------------------------------------------- #
# 測試 B：一般班次特徵化（修補前後皆應綠）
# --------------------------------------------------------------------------- #


class TestNormalShiftNoShiftDataFeaturization:
    """一般白班員工無排班，late/early 計算行為特徵化。

    保證修補後一般行為不改變。
    員工 09:00–18:00，打卡 09:05 / 18:00 → late=5, is_early_leave=False。
    """

    ATTENDANCE_DATE = date(2026, 3, 11)
    EMP_ID = "E_DAY"
    EMP_NAME = "白班師"
    USERNAME = "pure_admin_E_DAY"

    def test_normal_shift_late_five_minutes(self, excel_client):
        """一般班次遲到 5 分、準時下班 → late_minutes=5, is_early_leave=False。"""
        client, sf = excel_client
        _seed_employee_and_admin(
            sf,
            work_start="09:00",
            work_end="18:00",
            emp_id=self.EMP_ID,
            emp_name=self.EMP_NAME,
        )
        _login(client, self.USERNAME)

        xlsx = _make_excel_bytes(
            [
                {
                    "部門": "行政",
                    "編號": self.EMP_ID,
                    "姓名": self.EMP_NAME,
                    "日期": self.ATTENDANCE_DATE.strftime("%Y/%m/%d"),
                    "星期": "三",
                    "上班時間": "09:05",  # 遲到 5 分
                    "下班時間": "18:00",  # 準時下班
                }
            ]
        )
        res = _upload_excel(client, xlsx)
        assert res.status_code == 200, res.text

        data = res.json()
        assert data["anomaly_count"] == 0, f"不應有錯誤：{data.get('anomalies')}"

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter_by(attendance_date=self.ATTENDANCE_DATE)
                .first()
            )
        assert att is not None, "考勤記錄未寫入"
        assert att.is_late is True, f"期望遲到，實際 is_late={att.is_late}"
        assert (
            att.late_minutes == 5
        ), f"期望遲到 5 分（09:05 對 09:00），實際 late_minutes={att.late_minutes}"
        assert (
            att.is_early_leave is False
        ), f"18:00 準時下班，不應早退；實際 is_early_leave={att.is_early_leave}"
        assert (
            att.early_leave_minutes == 0
        ), f"準時下班，early_leave_minutes 應為 0；實際={att.early_leave_minutes}"
