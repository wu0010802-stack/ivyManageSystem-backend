"""懲處系統測試：service / engine 整合 / API CRUD。"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.disciplinary import router as disciplinary_router
from api.salary import init_salary_services
from api.salary import router as salary_router
from models.database import (
    Base,
    DisciplinaryAction,
    Employee,
    SalaryRecord,
    User,
)
from services.disciplinary import (
    apply_deductions,
    compute_total_pending_deduction,
    get_pending_actions,
    resolve_default_amount,
)
from services.salary.engine import SalaryEngine
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def disc_client(tmp_path):
    db_path = tmp_path / "disc.sqlite"
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
    init_salary_services(SalaryEngine(load_from_db=False), MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)
    app.include_router(disciplinary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_emp(session, employee_id="E001", name="王小明"):
    emp = Employee(
        employee_id=employee_id, name=name, base_salary=36000, is_active=True
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client, session_factory, username="disc_admin", perm=None):
    if perm is None:
        perm = ["SALARY_READ", "SALARY_WRITE"]
    with session_factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=perm,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200


# ── Service 層測試 ──────────────────────────────────────────────────────────


class TestService:
    def test_resolve_default_amount_with_config(self, disc_client):
        _, session_factory = disc_client
        cfg = MagicMock(
            warning_deduction=1500,
            minor_offense_deduction=4000,
            major_offense_deduction=8000,
        )
        assert resolve_default_amount("warning", cfg) == 1500
        assert resolve_default_amount("minor", cfg) == 4000
        assert resolve_default_amount("major", cfg) == 8000

    def test_resolve_default_amount_no_config(self):
        # fallback 業主慣例
        assert resolve_default_amount("warning", None) == 1000
        assert resolve_default_amount("minor", None) == 3000
        assert resolve_default_amount("major", None) == 0

    def test_get_pending_actions_filters_applied(self, disc_client):
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            pending = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 10),
                action_type="warning",
                deduction_amount=1000,
            )
            applied = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 2, 15),
                action_type="minor",
                deduction_amount=3000,
                applied_to_salary_id=999,  # 假裝已抵扣
            )
            session.add_all([pending, applied])
            session.commit()

            result = get_pending_actions(session, emp.id, date(2026, 5, 31))
            assert len(result) == 1
            assert result[0].action_type == "warning"

    def test_compute_total_uses_amount_or_fallback(self, disc_client):
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a_explicit = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 3, 1),
                action_type="warning",
                deduction_amount=500,  # 個別覆寫
            )
            a_default = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 3, 15),
                action_type="minor",
                deduction_amount=0,  # 用 fallback 3000
            )
            session.add_all([a_explicit, a_default])
            session.commit()

            total = compute_total_pending_deduction(
                session, emp.id, date(2026, 5, 31), None
            )
            assert total == 3500

    def test_apply_deductions_marks_applied_and_truncates(self, disc_client):
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            rec = SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=6,
                festival_bonus=2000,
                overtime_bonus=500,
            )
            session.add(rec)
            session.flush()

            a1 = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 3, 1),
                action_type="warning",
                deduction_amount=1000,
            )
            a2 = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 1),
                action_type="minor",
                deduction_amount=3000,  # 足以截斷（available=2500）
            )
            session.add_all([a1, a2])
            session.commit()

            # available=2500 → a1 扣 1000、a2 扣 1500（截斷）
            applied = apply_deductions(
                session,
                employee_id=emp.id,
                salary_record_id=rec.id,
                until_date=date(2026, 5, 31),
                available_bonus=2500,
                bonus_config=None,
            )
            assert applied == 2500
            session.commit()

            a1_after = session.query(DisciplinaryAction).get(a1.id)
            a2_after = session.query(DisciplinaryAction).get(a2.id)
            assert a1_after.applied_to_salary_id == rec.id
            assert float(a1_after.applied_amount) == 1000.0
            assert a2_after.applied_to_salary_id == rec.id
            assert float(a2_after.applied_amount) == 1500.0

    def test_apply_deductions_skips_when_no_pending(self, disc_client):
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            rec = SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6)
            session.add(rec)
            session.flush()

            applied = apply_deductions(
                session,
                employee_id=emp.id,
                salary_record_id=rec.id,
                until_date=date(2026, 5, 31),
                available_bonus=10000,
                bonus_config=None,
            )
            assert applied == 0


# ── Engine 整合測試 ─────────────────────────────────────────────────────────


class TestEngineAdjust:
    def test_adjust_festival_first_then_overtime(self, disc_client):
        """節慶優先扣完才動超額。"""
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            session.add(
                DisciplinaryAction(
                    employee_id=emp.id,
                    action_date=date(2026, 3, 1),
                    action_type="warning",
                    deduction_amount=1500,
                )
            )
            session.commit()

            engine = SalaryEngine(load_from_db=False)
            engine._bonus_config = None
            festival_after, overtime_after, deducted = (
                engine._adjust_period_totals_for_discipline(
                    session, emp, 2026, 5, festival_total=1000, overtime_total=2000
                )
            )
            # 1500 扣減：festival 1000 全扣 + overtime 扣 500
            assert festival_after == 0
            assert overtime_after == 1500
            assert deducted == 1500

    def test_adjust_caps_at_available(self, disc_client):
        """扣款 > 可用獎金時截斷到 0。"""
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            session.add(
                DisciplinaryAction(
                    employee_id=emp.id,
                    action_date=date(2026, 3, 1),
                    action_type="minor",
                    deduction_amount=10000,  # 遠超可用
                )
            )
            session.commit()

            engine = SalaryEngine(load_from_db=False)
            engine._bonus_config = None
            festival_after, overtime_after, deducted = (
                engine._adjust_period_totals_for_discipline(
                    session, emp, 2026, 5, festival_total=2000, overtime_total=500
                )
            )
            assert festival_after == 0
            assert overtime_after == 0
            assert deducted == 2500  # = available

    def test_adjust_skips_non_distribution_month(self, disc_client):
        """非發放月 totals=None，直接 pass-through。"""
        _, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            engine = SalaryEngine(load_from_db=False)
            festival_after, overtime_after, deducted = (
                engine._adjust_period_totals_for_discipline(
                    session, emp, 2026, 4, festival_total=None, overtime_total=None
                )
            )
            assert festival_after is None
            assert overtime_after is None
            assert deducted == 0


# ── API CRUD 測試 ───────────────────────────────────────────────────────────


class TestApi:
    def test_create_and_list(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            emp_id = emp.id
            session.commit()
        _login(client, session_factory)

        res = client.post(
            "/api/disciplinary-actions",
            json={
                "employee_id": emp_id,
                "action_date": "2026-04-10",
                "action_type": "warning",
                "deduction_amount": 1000,
                "reason": "未交課程紀錄",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["action_type_label"] == "警告"
        assert body["deduction_amount"] == 1000

        res2 = client.get("/api/disciplinary-actions", params={"employee_id": emp_id})
        assert res2.status_code == 200
        assert len(res2.json()["items"]) == 1

    def test_invalid_action_type_rejected(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            emp_id = emp.id
            session.commit()
        _login(client, session_factory)

        res = client.post(
            "/api/disciplinary-actions",
            json={
                "employee_id": emp_id,
                "action_date": "2026-04-10",
                "action_type": "huge_offense",
                "deduction_amount": 0,
            },
        )
        assert res.status_code == 400

    def test_update_reason_on_applied_allowed(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            rec = SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6)
            session.add(rec)
            session.flush()
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 1),
                action_type="warning",
                deduction_amount=1000,
                applied_to_salary_id=rec.id,
                applied_amount=1000,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory)

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"reason": "補充說明"},
        )
        assert res.status_code == 200
        assert res.json()["reason"] == "補充說明"

    def test_update_amount_on_applied_rejected(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            rec = SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6)
            session.add(rec)
            session.flush()
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 1),
                action_type="warning",
                deduction_amount=1000,
                applied_to_salary_id=rec.id,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory)

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"deduction_amount": 2000},
        )
        assert res.status_code == 409

    def test_delete_applied_rejected(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            rec = SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6)
            session.add(rec)
            session.flush()
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 1),
                action_type="warning",
                deduction_amount=1000,
                applied_to_salary_id=rec.id,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory)

        res = client.delete(f"/api/disciplinary-actions/{aid}")
        assert res.status_code == 409

    def test_delete_pending_allowed(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 1),
                action_type="warning",
                deduction_amount=1000,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory)

        res = client.delete(f"/api/disciplinary-actions/{aid}")
        assert res.status_code == 200
        assert res.json()["deleted"] is True

    def test_requires_salary_write_for_create(self, disc_client):
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            emp_id = emp.id
            session.commit()
        # 僅 read 權限
        _login(
            client,
            session_factory,
            username="read_only",
            perm=["SALARY_READ"],
        )
        res = client.post(
            "/api/disciplinary-actions",
            json={
                "employee_id": emp_id,
                "action_date": "2026-04-10",
                "action_type": "warning",
                "deduction_amount": 1000,
            },
        )
        assert res.status_code in (401, 403)

    def test_merit類型誤填金額_create_422(self, disc_client):
        """嘉獎/小功/大功 帶 deduction_amount > 0 → 422。"""
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            emp_id = emp.id
            session.commit()
        _login(client, session_factory, username="merit_admin1")

        for merit_type in ("commendation", "minor_merit", "major_merit"):
            res = client.post(
                "/api/disciplinary-actions",
                json={
                    "employee_id": emp_id,
                    "action_date": "2026-05-01",
                    "action_type": merit_type,
                    "deduction_amount": 500,
                },
            )
            assert (
                res.status_code == 422
            ), f"merit type={merit_type} 預期 422，實得 {res.status_code}"

    def test_merit類型填零金額_create_成功(self, disc_client):
        """嘉獎 deduction_amount=0 → 200 正常建立。"""
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            emp_id = emp.id
            session.commit()
        _login(client, session_factory, username="merit_admin2")

        res = client.post(
            "/api/disciplinary-actions",
            json={
                "employee_id": emp_id,
                "action_date": "2026-05-01",
                "action_type": "commendation",
                "deduction_amount": 0,
                "reason": "表現優良",
            },
        )
        assert res.status_code == 200
        assert res.json()["action_type_label"] == "嘉獎"

    def test_merit類型誤填金額_update_422(self, disc_client):
        """現有嘉獎記錄 PUT 帶 deduction_amount > 0 → 422。"""
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 5, 1),
                action_type="commendation",
                deduction_amount=0,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory, username="merit_admin3")

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"deduction_amount": 999},
        )
        assert res.status_code == 422

    def test_update_warning改commendation帶金額_422(self, disc_client):
        """既有 warning 記錄 PUT action_type=commendation + deduction_amount=500 → 422。
        驗證 update 端點同時改類型與金額時，以更新後的 action_type 判斷。
        """
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 5, 10),
                action_type="warning",
                deduction_amount=1000,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory, username="merit_upd_admin1")

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"action_type": "commendation", "deduction_amount": 500},
        )
        assert (
            res.status_code == 422
        ), f"warning→commendation + amount=500 預期 422，實得 {res.status_code}"

    def test_update_warning改commendation填零金額_200(self, disc_client):
        """既有 warning 記錄 PUT action_type=commendation + deduction_amount=0 → 200。
        轉換為 merit 類型且金額為 0 應允許。
        """
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 5, 10),
                action_type="warning",
                deduction_amount=1000,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory, username="merit_upd_admin2")

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"action_type": "commendation", "deduction_amount": 0},
        )
        assert (
            res.status_code == 200
        ), f"warning→commendation + amount=0 預期 200，實得 {res.status_code}"
        body = res.json()
        assert body["action_type"] == "commendation"
        assert body["action_type_label"] == "嘉獎"
        assert body["deduction_amount"] == 0

    def test_update_warning改commendation不帶金額_殘留歸零(self, disc_client):
        """既有 warning(1000) PUT 只改 action_type=commendation（不帶金額）→
        殘留 deduction_amount 應歸 0，不得留下「獎勵 + 扣款金額」的不一致列。
        （_effective_amount 對 merit 恆回 0 守住金流，此處修資料層殘留。）
        """
        client, session_factory = disc_client
        with session_factory() as session:
            emp = _add_emp(session)
            a = DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 5, 10),
                action_type="warning",
                deduction_amount=1000,
            )
            session.add(a)
            session.commit()
            aid = a.id
        _login(client, session_factory, username="merit_upd_admin3")

        res = client.put(
            f"/api/disciplinary-actions/{aid}",
            json={"action_type": "commendation"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["deduction_amount"] == 0
        with session_factory() as session:
            row = session.get(DisciplinaryAction, aid)
            assert float(row.deduction_amount or 0) == 0


def test_list_actions_query_is_bounded(disc_client):
    """T6（2026-06-29 效能健檢）：懲處列表查詢須有 SQL LIMIT 安全上限。

    disciplinary_actions 為 append-only、跨全員永久成長；list_actions 原本
    完全無界 .all()（無分頁參數、無 cap）。本測試固化「查詢帶 SQL LIMIT」。
    """
    import models.base as base_module
    from sqlalchemy import event as sa_event

    client, session_factory = disc_client
    _login(client, session_factory)
    with session_factory() as session:
        emp = _add_emp(session)
        session.add(
            DisciplinaryAction(
                employee_id=emp.id,
                action_date=date(2026, 4, 10),
                action_type="warning",
                deduction_amount=1000,
            )
        )
        session.commit()

    engine = base_module._engine
    selects: list[str] = []

    def _cap(conn, cursor, statement, parameters, context, executemany):
        st = statement.lstrip().lower()
        if st.startswith("select") and "disciplinary_actions" in st:
            selects.append(st)

    sa_event.listen(engine, "after_cursor_execute", _cap)
    try:
        resp = client.get("/api/disciplinary-actions")
    finally:
        sa_event.remove(engine, "after_cursor_execute", _cap)

    assert resp.status_code == 200, resp.text
    data_selects = [
        s for s in selects if "from disciplinary_actions" in s and "count(" not in s
    ]
    assert data_selects, "應有對 disciplinary_actions 的查詢"
    assert all("limit" in s for s in data_selects), (
        f"懲處列表查詢須帶 SQL LIMIT 安全上限（防永久成長），實際：{data_selects}"
    )
