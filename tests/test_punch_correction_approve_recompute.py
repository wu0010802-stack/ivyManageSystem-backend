"""審核補打卡時整套重算 Attendance 派生欄位（is_late / is_early_leave 等）回歸測試。

修補目標：approve PunchCorrectionRequest 後須以新的 punch_in/out 與員工排班
時間整套重算 is_late、late_minutes、is_early_leave、early_leave_minutes、status、
is_missing_*；否則舊值殘留會被薪資 engine
（services/salary/engine.py 與 salary_field_breakdown.py）讀到，造成補卡通過
卻仍扣遲到金的真實漏帳。

威脅：員工 09:30 上班打卡 → Attendance.is_late=True / late_minutes=90 →
申請補單 08:00 上班 → 主管核准 → punch_in_time 改 08:00 但 is_late=True /
late_minutes=90 殘留 → 薪資仍扣 90 分鐘遲到金。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#6)。
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.punch_corrections import router as punch_corrections_router
from models.database import (
    ApprovalPolicy,
    Attendance,
    Base,
    Employee,
    PunchCorrectionRequest,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def punch_client(tmp_path):
    db_path = tmp_path / "punch-correction-recompute.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(punch_corrections_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(
    session,
    *,
    employee_id: str,
    name: str,
    work_start: str = "08:00",
    work_end: str = "17:00",
) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
        work_start_time=work_start,
        work_end_time=work_end,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    username: str,
    role: str,
    permission_names,
    employee_id: int | None = None,
) -> User:
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    u = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


APPROVALS_PERMS = ["APPROVALS", "ATTENDANCE_READ"]


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


class TestApproveRecomputesAttendanceFields:
    """approve 後 Attendance 派生欄位必須整套重算。"""

    def test_approve_punch_in_clears_late_when_corrected_to_on_time(self, punch_client):
        """09:30 遲到 → 補單 08:00 → 核准 → is_late/late_minutes 清零。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_late", name="遲到員工")
            sup_emp = _make_employee(s, employee_id="E_sup", name="主管")
            _make_user(
                s,
                username="emp_late",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            policy = ApprovalPolicy(
                doc_type="punch_correction",
                submitter_role="teacher",
                approver_roles="supervisor,admin",
                is_active=True,
            )
            s.add(policy)
            # 既有 Attendance：09:30 遲到 90 分
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 9, 30),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                status="late",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=90,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 8, 0
                ),
                requested_punch_out=None,
                reason="忘了刷卡，實際 8:00 到",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.punch_in_time == datetime(
                on_date.year, on_date.month, on_date.day, 8, 0
            )
            # 核心驗證：補單通過後遲到欄位必須清零
            assert att.is_late is False, "補卡通過後 is_late 仍為 True → 薪資會誤扣"
            assert (
                att.late_minutes == 0
            ), f"補卡通過後 late_minutes={att.late_minutes}，應為 0"
            assert att.status == "normal"
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False

    def test_approve_punch_out_clears_early_leave(self, punch_client):
        """16:00 早退 → 補單 17:00 → 核准 → is_early_leave/early_leave_minutes 清零。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_early", name="早退員工")
            sup_emp = _make_employee(s, employee_id="E_sup2", name="主管2")
            _make_user(
                s,
                username="emp_early",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok2",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 8, 0),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 16, 0
                ),
                status="early_leave",
                is_late=False,
                is_early_leave=True,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=0,
                early_leave_minutes=60,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_out",
                requested_punch_in=None,
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                reason="忘了刷卡，實際 17:00 才下班",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok2").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.is_early_leave is False
            assert att.early_leave_minutes == 0
            assert att.status == "normal"

    def test_approve_both_recomputes_against_employee_schedule(self, punch_client):
        """correction_type='both' 整套重算，且依員工自訂上下班時間（09:00-18:00）。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(
                s,
                employee_id="E_custom",
                name="自訂排班",
                work_start="09:00",
                work_end="18:00",
            )
            sup_emp = _make_employee(s, employee_id="E_sup3", name="主管3")
            _make_user(
                s,
                username="emp_custom",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok3",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            # 缺打卡狀態：punch_in / punch_out 都缺，is_late/is_early_leave 殘存值
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=None,
                punch_out_time=None,
                status="late",  # 殘留錯誤值（實況為 missing）；dbck01 CHECK 後須為合法 enum 值
                is_late=True,  # 殘留錯誤值
                is_early_leave=True,  # 殘留錯誤值
                is_missing_punch_in=True,
                is_missing_punch_out=True,
                late_minutes=120,
                early_leave_minutes=30,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="both",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 9, 0
                ),
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 18, 0
                ),
                reason="一整天都忘了刷卡",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok3").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            # 09:00 準時上班、18:00 準時下班 → 無遲到、無早退、無缺打卡
            assert att.is_late is False
            assert att.is_early_leave is False
            assert att.late_minutes == 0
            assert att.early_leave_minutes == 0
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False
            assert att.status == "normal"


class TestApproveCrossNightNormalize:
    """補打卡跨夜：punch_out 時刻 < punch_in 時刻 同日 → punch_out 必須 +1 day。

    觸發情境：教師夜間活動晚 23:00 進、隔天 03:00 出。前端 PortalPunchCorrectionForm
    用 ``${attendance_date}T${time}:00`` 把兩個 datetime 都鎖在同日，後端 approve
    直接 assign 後，recompute_attendance_status 算出 work_hours 變負、
    early_leave_minutes 約 14 小時、加班費歸零 → 扣全薪（勞檢級別漏帳）。

    既有 normalize pattern 在 ``api/attendance/records.py:266-273`` 與
    ``api/attendance/upload.py:340-356``；punch_corrections.approve 是唯一漏掉的。

    Note: 本修正只 normalize punch_out += 1 day，並擋下「上下班時間相同」的明顯
    錯誤。教師夜間活動晚 22:00 進 但 work_start=08:00 仍會被算成 is_late=True /
    late_minutes 多（夜班 vs 排班時段的業務性 mismatch），屬獨立 scope，本回歸
    測試不 assert is_late 結果。
    """

    def test_approve_both_crossnight_punch_out_gets_next_day(self, punch_client):
        """22:00→04:00 同日 → approve 後 punch_out 應為次日 04:00（無 early_leave）。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_night", name="夜間活動員工")
            sup_emp = _make_employee(s, employee_id="E_sup_n", name="主管N")
            _make_user(
                s,
                username="emp_night",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_night",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="both",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 22, 0
                ),
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 4, 0
                ),
                reason="夜間園務活動，22:00 進、隔天 04:00 出",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_night").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            expected_out = datetime(
                on_date.year, on_date.month, on_date.day, 22, 0
            ) + timedelta(
                hours=6
            )  # = 次日 04:00
            assert att.punch_out_time == expected_out, (
                f"punch_out_time={att.punch_out_time}，應為次日 04:00 = {expected_out}"
                "（否則時數負值會讓加班費歸零）"
            )
            assert att.punch_in_time == datetime(
                on_date.year, on_date.month, on_date.day, 22, 0
            )
            work_seconds = (att.punch_out_time - att.punch_in_time).total_seconds()
            assert work_seconds > 0, f"work duration 為負或零：{work_seconds}s"
            assert (
                att.is_early_leave is False
            ), "punch_out 已 +1d 仍被誤判 early_leave → early_leave_minutes 仍會誤扣"
            assert att.early_leave_minutes == 0
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False

    def test_approve_same_time_returns_400(self, punch_client):
        """punch_in == punch_out 同時刻 → 400（與 records.py:269-273 一致的防呆）。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_dup", name="同時刻員工")
            sup_emp = _make_employee(s, employee_id="E_sup_d", name="主管D")
            _make_user(
                s,
                username="emp_dup",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_dup",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="both",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 9, 0
                ),
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 9, 0
                ),
                reason="輸入錯誤",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_dup").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 400, res.text
        assert "相同" in res.json().get("detail", ""), res.text

        with sf() as s:
            corr = (
                s.query(PunchCorrectionRequest)
                .filter(PunchCorrectionRequest.id == corr_id)
                .first()
            )
            assert corr.status == "pending"
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is None

    def test_approve_punch_out_only_normalizes_against_existing_punch_in(
        self, punch_client
    ):
        """既有 punch_in=22:00、補單只補 punch_out=04:00 同日 → punch_out 應 +1d。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_out_only", name="補下班員工")
            sup_emp = _make_employee(s, employee_id="E_sup_o", name="主管O")
            _make_user(
                s,
                username="emp_out_only",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_out_only",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 22, 0),
                punch_out_time=None,
                status="missing",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=True,
                late_minutes=840,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_out",
                requested_punch_in=None,
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 4, 0
                ),
                reason="夜間活動忘了打下班，實際 04:00 才走",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_out_only").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            expected_out = datetime(
                on_date.year, on_date.month, on_date.day, 22, 0
            ) + timedelta(hours=6)
            assert att.punch_out_time == expected_out, (
                f"既有 punch_in=22:00、補 punch_out=04:00 同日 → 應 +1d 變"
                f" {expected_out}，實際 {att.punch_out_time}"
            )
            assert att.is_missing_punch_out is False

    def test_approve_punch_in_only_does_not_double_normalize(self, punch_client):
        """既有 punch_out 已是次日 03:00、補單只補 punch_in=22:00 同日 → punch_out 不可再 +1d。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_in_only", name="補上班員工")
            sup_emp = _make_employee(s, employee_id="E_sup_i", name="主管I")
            _make_user(
                s,
                username="emp_in_only",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_in_only",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            existing_out = datetime(
                on_date.year, on_date.month, on_date.day, 22, 0
            ) + timedelta(
                hours=5
            )  # 次日 03:00
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=None,
                punch_out_time=existing_out,
                status="missing",
                is_late=False,
                is_early_leave=False,
                is_missing_punch_in=True,
                is_missing_punch_out=False,
                late_minutes=0,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 22, 0
                ),
                requested_punch_out=None,
                reason="補上班打卡",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_in_only").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.punch_in_time == datetime(
                on_date.year, on_date.month, on_date.day, 22, 0
            )
            assert att.punch_out_time == existing_out, (
                f"既有 punch_out 已 normalize 到次日 03:00；補 punch_in=22:00 同日"
                f" 不應觸發二次 +1d。expected={existing_out}, got={att.punch_out_time}"
            )
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False


class TestApproveLatePartialMinutes:
    """測試 partial late 重算（保留既有測試）。"""

    def test_approve_punch_in_partial_still_late_recomputes_minutes(self, punch_client):
        """補單 08:30（仍遲到）→ 核准 → late_minutes 應從原 90 改為 30。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_partial", name="部分補正")
            sup_emp = _make_employee(s, employee_id="E_sup4", name="主管4")
            _make_user(
                s,
                username="emp_partial",
                role="teacher",
                permission_names=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok4",
                role="supervisor",
                permission_names=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 9, 30),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                status="late",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=90,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 8, 30
                ),
                requested_punch_out=None,
                reason="實際 8:30 到，沒有 9:30 那麼晚",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok4").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.is_late is True
            assert (
                att.late_minutes == 30
            ), f"08:30 仍遲到 30 分鐘但 late_minutes={att.late_minutes}"
            assert att.status == "late"
