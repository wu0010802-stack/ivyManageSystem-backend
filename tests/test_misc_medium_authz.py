"""IDOR audit Phase 2 M3：5 個獨立 Medium finding 共一檔（不共享 helper）。

涵蓋 5 個 finding：
- F-005 portal/leaves _check_substitute_leave_conflict：409 detail 改 generic
  訊息，不再洩漏代理人請假/加班區間與審核狀態
- F-030 activity/public POST /public/register：existing / pending_dup / IntegrityError
  三條重複報名路徑改成「matched 才看到 400 明確訊息；unmatched/未驗證身分一律
  silent-success」，避免攻擊者用 (學生姓名+生日) 或 parent_phone 探測存在性
- F-033 exports / gov_reports：補 write_explicit_audit 至 7 個 export +
  4 個 gov_report 端點，PII / 身分證匯出留下稽核軌跡
- F-043 main.py dev_router：改用 ENV ∈ {development, dev, local, test} 白名單
  掛載；staging / 未設 ENV 不掛 dev_router
- F-045 announcements PUT /{id}/parent-recipients：受眾範圍守衛，非
  is_unrestricted caller 必須對應 accessible_classroom_ids；scope='all' 限
  admin/hr/supervisor

每個 test class 對應一個 finding；測試資料 fixture 各自 setup（不共享）。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.exports as exports_module
import api.gov_reports as gov_reports_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.announcements import router as announcements_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.exports import router as exports_router
from api.gov_reports import router as gov_reports_router
from api.leaves import router as leaves_router
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Announcement,
    AttendancePolicy,
    Base,
    Classroom,
    Employee,
    LeaveRecord,
    OvertimeRecord,
    Student,
    User,
)
from models.guardian import Guardian
from utils.auth import hash_password
from utils.permissions import Permission

# ─────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────


def _create_user(
    session,
    *,
    username,
    role,
    permissions,
    employee_id=None,
    password="Pass1234",
):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=int(permissions),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _create_employee(session, code: str, name: str) -> Employee:
    emp = Employee(
        employee_id=code,
        name=name,
        base_salary=32000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client: TestClient, username: str, password: str = "Pass1234"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────
# F-005：portal/leaves substitute conflict generic message
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f005_client(tmp_path):
    db_path = tmp_path / "f005.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(portal_router)
    app.include_router(leaves_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f005(session) -> dict:
    """員工 A（攻擊者）+ 員工 B（受害者，已有假單）+ shift policy。"""
    emp_a = _create_employee(session, "EA01", "員工 A")
    emp_b = _create_employee(session, "EB01", "員工 B")
    _create_user(
        session,
        username="emp_a",
        role="staff",
        permissions=int(Permission.LEAVES_READ),
        employee_id=emp_a.id,
    )

    # B 已有 2026/4/15 ~ 4/15 已核准請假
    leave = LeaveRecord(
        employee_id=emp_b.id,
        leave_type="annual",
        start_date=date(2026, 4, 15),
        end_date=date(2026, 4, 15),
        leave_hours=8,
        is_approved=True,
        deduction_ratio=0,
    )
    session.add(leave)
    session.commit()
    session.refresh(emp_a)
    session.refresh(emp_b)
    return {"emp_a": emp_a, "emp_b": emp_b}


class TestF005_SubstituteConflictMessage:
    """portal/leaves 代理人衝突 detail 改 generic，不再洩漏受害者排程。"""

    def _post_leave(self, client, sub_id, start_d="2026-04-15", end_d="2026-04-15"):
        return client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "annual",
                "start_date": start_d,
                "end_date": end_d,
                "leave_hours": 8,
                "reason": "事假",
                "substitute_employee_id": sub_id,
            },
        )

    def test_409_detail_does_not_contain_dates(self, f005_client):
        """detail 不可洩漏受害者請假/加班的具體日期區間。"""
        client, sf = f005_client
        with sf() as s:
            seed = _seed_f005(s)
            sub_id = seed["emp_b"].id
        _login(client, "emp_a")
        res = self._post_leave(client, sub_id)
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        # 受害者具體日期（2026-04-15）不可出現在 detail 內
        assert "2026-04-15" not in detail
        assert "04-15" not in detail
        assert "~" not in detail  # 舊訊息含 "{start_date} ~ {end_date}"

    def test_409_detail_does_not_contain_approval_status(self, f005_client):
        """detail 不可洩漏「已核准 / 待審核」這類審核狀態字。"""
        client, sf = f005_client
        with sf() as s:
            seed = _seed_f005(s)
            sub_id = seed["emp_b"].id
        _login(client, "emp_a")
        res = self._post_leave(client, sub_id)
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        for forbidden in ("已核准", "待審核", "approved", "pending"):
            assert (
                forbidden not in detail
            ), f"detail 不應包含審核狀態 '{forbidden}'，實際 detail={detail}"

    def test_legit_workflow_still_returns_409_with_generic_message(self, f005_client):
        """合法流程依舊得到 409（代理人不可用），detail 為 generic 訊息。"""
        client, sf = f005_client
        with sf() as s:
            seed = _seed_f005(s)
            sub_id = seed["emp_b"].id
        _login(client, "emp_a")
        res = self._post_leave(client, sub_id)
        assert res.status_code == 409
        detail = res.json()["detail"]
        # 主要 generic 訊息應出現「代理人」+「請改選 / 改派」這類動詞
        assert "代理人" in detail
        # 任一指引動詞（請改派 / 請改選）皆可
        assert ("改派" in detail) or ("改選" in detail)

    def test_overtime_conflict_also_generic(self, f005_client):
        """加班衝突路徑同樣回 generic detail（不含日期 / 狀態）。"""
        client, sf = f005_client
        with sf() as s:
            seed = _seed_f005(s)
            # 移除請假，改放加班（為了測試另一條 conflict 路徑）
            s.query(LeaveRecord).delete()
            s.commit()
            ot = OvertimeRecord(
                employee_id=seed["emp_b"].id,
                overtime_date=date(2026, 4, 15),
                overtime_type="weekday_extra",
                hours=2,
                is_approved=True,
            )
            s.add(ot)
            s.commit()
            sub_id = seed["emp_b"].id
        _login(client, "emp_a")
        res = self._post_leave(client, sub_id)
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert "2026-04-15" not in detail
        assert "已核准" not in detail
        assert "待審核" not in detail


# ─────────────────────────────────────────────────────────────────────────
# F-030：activity/public/register enumeration oracle
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f030_client(tmp_path):
    db_path = tmp_path / "f030.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f030(session, *, with_existing_reg: bool = False) -> dict:
    """幼稚園裡有學生「王小明 (2020-05-10) parent_phone=0912345678」。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    classroom = Classroom(
        name="大象班",
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋",
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    student = Student(
        student_id="S001",
        name="王小明",
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        parent_phone="0912345678",
        is_active=True,
    )
    session.add(student)
    if with_existing_reg:
        # 已有有效報名
        session.add(
            ActivityRegistration(
                student_name="王小明",
                birthday="2020-05-10",
                class_name="大象班",
                school_year=sy,
                semester=sem,
                parent_phone="0912345678",
                student_id=None,
                classroom_id=classroom.id,
                pending_review=False,
                match_status="matched",
                is_active=True,
            )
        )
    session.commit()
    return {"classroom_id": classroom.id}


def _f030_register_payload(
    *, name="王小明", birthday="2020-05-10", phone="0912345678", class_="大象班"
):
    return {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": class_,
        "courses": [{"name": "圍棋", "price": "1"}],
        "supplies": [],
    }


class TestF030_PublicRegisterEnumeration:
    """existing / pending_dup 檢查需在三欄身分驗證後才會 raise 400。"""

    def test_unauthenticated_probe_with_invalid_identity_returns_generic_message(
        self, f030_client
    ):
        """探測 (real_name, real_birthday, fake_phone)：應 silent-success（201 + 中性訊息），
        且 DB 不應多寫一筆 ActivityRegistration（已有 1 筆 baseline）。
        """
        client, sf = f030_client
        with sf() as s:
            _seed_f030(s, with_existing_reg=True)
        # 攻擊者用 real_name + real_birthday + fake_phone 探測
        res = client.post(
            "/api/activity/public/register",
            json=_f030_register_payload(phone="0999999999"),
        )
        assert res.status_code == 201, res.text
        body = res.json()
        # 中性訊息 + id=0（silent-success 標記）
        assert body["id"] == 0
        assert "已送出" in body["message"]
        # 不應透露「此學生本學期已有有效報名」這類 4xx 訊息
        # （探測者拿到的回應與正常新報名完全一樣）
        assert "已有" not in body["message"]
        # DB 仍只有 baseline 那 1 筆（攻擊者的探測不應進 DB）
        with sf() as s:
            assert s.query(ActivityRegistration).count() == 1

    def test_pending_dup_phone_probe_returns_silent_success(self, f030_client):
        """pending_dup 路徑：用同 phone + 不同 (name, birthday) 探測現有 pending 報名。

        舊版會回 400「您的報名仍在確認中」洩漏電話存在性；修補後 unmatched 探測
        應 silent-success（201），且 DB 不增筆。
        """
        client, sf = f030_client
        with sf() as s:
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            # 不建學生 → unmatched；先建立一筆 pending 報名
            s.add(
                Classroom(
                    name="大象班",
                    is_active=True,
                    school_year=sy,
                    semester=sem,
                )
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.add(
                ActivityRegistration(
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    school_year=sy,
                    semester=sem,
                    parent_phone="0912345678",
                    student_id=None,
                    classroom_id=None,
                    pending_review=True,
                    match_status="pending",
                    is_active=True,
                )
            )
            s.commit()
        # 同 phone + 不同 (name, birthday) → unmatched（無 Student 對應）
        res = client.post(
            "/api/activity/public/register",
            json=_f030_register_payload(name="李小華", birthday="2021-01-01"),
        )
        # 修補後：unmatched 探測 → silent-success
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["id"] == 0
        # DB 不應從 1 筆變成 2 筆（dedup 守住）
        with sf() as s:
            assert s.query(ActivityRegistration).count() == 1

    def test_verified_parent_with_existing_registration_400(self, f030_client):
        """已驗證身分（matched）+ 已有有效報名 → 仍回 400 明確訊息（保留 UX）。"""
        client, sf = f030_client
        with sf() as s:
            _seed_f030(s, with_existing_reg=True)
        # 三欄（name + birthday + phone）全對 → matched
        res = client.post(
            "/api/activity/public/register",
            json=_f030_register_payload(),  # 預設三欄與學生資料一致
        )
        assert res.status_code == 400, res.text
        assert "已有有效報名" in res.json()["detail"]

    def test_unmatched_fresh_registration_still_succeeds(self, f030_client):
        """正常 unmatched 新報名（無 dup）→ silent-success，且 DB 多 1 筆 pending。"""
        client, sf = f030_client
        with sf() as s:
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            s.add(
                Classroom(
                    name="大象班",
                    is_active=True,
                    school_year=sy,
                    semester=sem,
                )
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()
        res = client.post(
            "/api/activity/public/register",
            json=_f030_register_payload(),
        )
        assert res.status_code == 201
        # 正常新報名 id 不為 0（仍會寫 DB）
        body = res.json()
        assert body["id"] != 0
        with sf() as s:
            assert s.query(ActivityRegistration).count() == 1


# ─────────────────────────────────────────────────────────────────────────
# F-033：exports / gov_reports audit trail
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f033_client(tmp_path):
    db_path = tmp_path / "f033.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(exports_router)
    app.include_router(gov_reports_router)

    # 停用 5/min 匯出限流
    app.dependency_overrides[exports_module._export_rate_limit] = lambda: None
    app.dependency_overrides[gov_reports_module._rate_limit] = lambda: None

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f033(session) -> None:
    """admin 帳號 + 1 個學生 + 1 個員工，足夠讓匯出端點不空跑。"""
    classroom = Classroom(name="大象班", is_active=True)
    session.add(classroom)
    session.flush()
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            classroom_id=classroom.id,
            is_active=True,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
    )
    emp = _create_employee(session, "E001", "員工")
    emp.id_number = "A123456789"
    emp.insurance_salary_level = 30000
    _create_user(
        session,
        username="adm_export",
        role="admin",
        permissions=-1,
    )
    session.commit()


class TestF033_ExportsAuditTrail:
    """匯出端點呼叫 write_explicit_audit。

    不依賴 AuditLog 真寫入（背景 thread 在 TestClient 不可預期），
    改 monkeypatch write_explicit_audit 為 mock，斷言其呼叫參數。
    """

    def test_exports_students_writes_audit(self, f033_client, monkeypatch):
        client, sf = f033_client
        with sf() as s:
            _seed_f033(s)
        _login(client, "adm_export")

        calls = []

        def fake_audit(request, *, action, entity_type, summary, **kwargs):
            calls.append(
                {
                    "action": action,
                    "entity_type": entity_type,
                    "summary": summary,
                    "kwargs": kwargs,
                }
            )

        monkeypatch.setattr(exports_module, "write_explicit_audit", fake_audit)
        res = client.get("/api/exports/students")
        assert res.status_code == 200, res.text
        assert any(
            c["entity_type"] == "student" and c["action"] == "EXPORT" for c in calls
        ), f"expected student EXPORT audit, got {calls}"

    def test_exports_attendance_writes_audit(self, f033_client, monkeypatch):
        client, sf = f033_client
        with sf() as s:
            _seed_f033(s)
        _login(client, "adm_export")

        calls = []

        def fake_audit(request, *, action, entity_type, summary, **kwargs):
            calls.append({"action": action, "entity_type": entity_type})

        monkeypatch.setattr(exports_module, "write_explicit_audit", fake_audit)
        res = client.get("/api/exports/attendance?year=2026&month=4")
        assert res.status_code == 200, res.text
        assert any(
            c["entity_type"] == "attendance" and c["action"] == "EXPORT" for c in calls
        )

    def test_exports_leaves_writes_audit(self, f033_client, monkeypatch):
        client, sf = f033_client
        with sf() as s:
            _seed_f033(s)
        _login(client, "adm_export")

        calls = []
        monkeypatch.setattr(
            exports_module,
            "write_explicit_audit",
            lambda r, **kw: calls.append(kw),
        )
        res = client.get("/api/exports/leaves?year=2026&month=4")
        assert res.status_code == 200, res.text
        assert any(c.get("entity_type") == "leave" for c in calls)

    def test_gov_reports_withholding_writes_audit(self, f033_client, monkeypatch):
        client, sf = f033_client
        with sf() as s:
            _seed_f033(s)
        _login(client, "adm_export")

        calls = []
        monkeypatch.setattr(
            gov_reports_module,
            "write_explicit_audit",
            lambda r, **kw: calls.append(kw),
        )
        # 注意：withholding 端點需要 SalaryRecord，或 records=[] 也能跑
        res = client.get("/api/gov-reports/withholding?year=2026")
        assert res.status_code == 200, res.text
        # gov_report 類別 + report=withholding
        gov_calls = [c for c in calls if c.get("entity_type") == "gov_report"]
        assert gov_calls, f"expected gov_report audit, got {calls}"
        assert gov_calls[0].get("changes", {}).get("report") == "withholding"
        # 政府申報含全員身分證：is_full_id_number=True 為 SOC 告警旗標
        assert gov_calls[0]["changes"]["is_full_id_number"] is True


# ─────────────────────────────────────────────────────────────────────────
# F-043：dev_router mount allowlist
# ─────────────────────────────────────────────────────────────────────────


class TestF043_DevRouterMount:
    """ENV 白名單決定是否掛載 /api/dev/*。"""

    def test_should_mount_dev_router_when_env_is_development(self, monkeypatch):
        monkeypatch.setenv("ENV", "development")
        # 重新 import 確保拿到最新 helper（main 模組可能已快取）
        import importlib
        import main as main_module

        importlib.reload(main_module)
        try:
            assert main_module._should_mount_dev_router() is True
        finally:
            # 復原 ENV，避免影響後續測試
            monkeypatch.delenv("ENV", raising=False)
            importlib.reload(main_module)

    @pytest.mark.parametrize("allowed_env", ["development", "dev", "local", "test"])
    def test_should_mount_dev_router_for_each_allowed_env(self, allowed_env):
        # 直接呼叫 helper（不必 reload main，邏輯在 helper 內以 os.environ 為準）
        with patch.dict(os.environ, {"ENV": allowed_env}):
            from main import _should_mount_dev_router

            assert _should_mount_dev_router() is True

    @pytest.mark.parametrize(
        "blocked_env", ["staging", "production", "prod", "qa", "stage"]
    )
    def test_should_not_mount_dev_router_for_non_allowed_env(self, blocked_env):
        with patch.dict(os.environ, {"ENV": blocked_env}):
            from main import _should_mount_dev_router

            assert (
                _should_mount_dev_router() is False
            ), f"ENV={blocked_env} 不應掛 dev_router"

    def test_should_not_mount_dev_router_when_env_unset(self):
        # 完全清掉 ENV 環境變數
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENV", None)
            from main import _should_mount_dev_router

            assert (
                _should_mount_dev_router() is False
            ), "未設 ENV 不應掛 dev_router（白名單收斂）"

    def test_main_uses_should_mount_dev_router_helper(self):
        """確保 main.py 的 mount 條件確實是呼叫 _should_mount_dev_router(),
        而不是舊的 not _is_production() 黑名單。
        """
        import inspect

        import main as main_module

        src = inspect.getsource(main_module)
        # 必須出現新的白名單調用
        assert (
            "_should_mount_dev_router()" in src
        ), "main.py 必須使用 _should_mount_dev_router() 作 dev_router mount 條件"
        # 不應再用舊的 not _is_production() 包 dev_router
        # （仍可保留 _is_production 給其他用途，但不能直接守 dev_router 掛載）
        # 用簡單檢查：沒有 'if not _is_production():' 後面緊跟 include_router(dev_router)
        assert "if not _is_production():\n    from api.dev import" not in src


# ─────────────────────────────────────────────────────────────────────────
# F-045：announcements parent-recipients audience scope
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def f045_client(tmp_path):
    db_path = tmp_path / "f045.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(announcements_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_f045(session):
    """A 班（教師 A 為班導）+ A 班學生 + B 班 + B 班學生；建立公告 + 教師 A user。"""
    emp_a = _create_employee(session, "EA01", "教師 A")
    emp_b = _create_employee(session, "EB01", "教師 B")
    cls_a = Classroom(
        name="A 班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp_a.id,
    )
    cls_b = Classroom(
        name="B 班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp_b.id,
    )
    session.add_all([cls_a, cls_b])
    session.flush()

    st_a = Student(
        student_id="SA01",
        name="A 班學生",
        classroom_id=cls_a.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    st_b = Student(
        student_id="SB01",
        name="B 班學生",
        classroom_id=cls_b.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add_all([st_a, st_b])
    session.flush()

    ann = Announcement(
        title="A 班公告",
        content="家長請注意",
        priority="normal",
        created_by=emp_a.id,
    )
    session.add(ann)
    session.flush()

    g_a = Guardian(
        student_id=st_a.id,
        name="A 家長",
        phone="0911111111",
        relation="父",
        is_primary=True,
    )
    g_b = Guardian(
        student_id=st_b.id,
        name="B 家長",
        phone="0922222222",
        relation="父",
        is_primary=True,
    )
    session.add_all([g_a, g_b])
    session.flush()

    _create_user(
        session,
        username="t_a",
        role="staff",
        permissions=int(Permission.ANNOUNCEMENTS_READ | Permission.ANNOUNCEMENTS_WRITE),
        employee_id=emp_a.id,
    )
    _create_user(
        session,
        username="adm_ann",
        role="admin",
        permissions=-1,
    )
    session.commit()
    return {
        "ann_id": ann.id,
        "cls_a_id": cls_a.id,
        "cls_b_id": cls_b.id,
        "st_a_id": st_a.id,
        "st_b_id": st_b.id,
        "g_a_id": g_a.id,
        "g_b_id": g_b.id,
    }


class TestF045_AnnouncementsParentRecipients:
    """非 unrestricted caller 必須對應 accessible_classroom_ids；scope='all' 限管理角色。"""

    def test_teacher_cannot_target_student_outside_class(self, f045_client):
        """教師 A（A 班導）對 B 班學生發公告 → 403。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "student", "student_id": seed["st_b_id"]},
                ]
            },
        )
        assert res.status_code == 403, res.text

    def test_teacher_cannot_target_classroom_outside_class(self, f045_client):
        """教師 A 對 B 班發公告 → 403。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": seed["cls_b_id"]},
                ]
            },
        )
        assert res.status_code == 403, res.text

    def test_teacher_cannot_target_guardian_outside_class(self, f045_client):
        """教師 A 對 B 班家長 (guardian) 發公告 → 403。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "guardian", "guardian_id": seed["g_b_id"]},
                ]
            },
        )
        assert res.status_code == 403, res.text

    def test_teacher_cannot_use_scope_all(self, f045_client):
        """非 unrestricted caller 不能用 scope='all'（全校發送）。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={"recipients": [{"scope": "all"}]},
        )
        assert res.status_code == 403, res.text

    def test_teacher_can_target_student_in_class(self, f045_client):
        """教師 A 對自己班學生發公告 → 200。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "student", "student_id": seed["st_a_id"]},
                ]
            },
        )
        assert res.status_code == 200, res.text

    def test_teacher_can_target_classroom_in_scope(self, f045_client):
        """教師 A 對自己 A 班發公告 → 200。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "t_a")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": seed["cls_a_id"]},
                ]
            },
        )
        assert res.status_code == 200, res.text

    def test_admin_unrestricted(self, f045_client):
        """admin 不受 scope 限制：可用 'all' 與任意 classroom/student/guardian。"""
        client, sf = f045_client
        with sf() as s:
            seed = _seed_f045(s)
        _login(client, "adm_ann")
        res = client.put(
            f"/api/announcements/{seed['ann_id']}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "all"},
                    {"scope": "classroom", "classroom_id": seed["cls_b_id"]},
                    {"scope": "student", "student_id": seed["st_b_id"]},
                    {"scope": "guardian", "guardian_id": seed["g_b_id"]},
                ]
            },
        )
        assert res.status_code == 200, res.text
