"""
回歸測試：薪資手動調整 (PUT /salaries/{id}/manual-adjust)

涵蓋：
- 時薪制員工 hourly_total 不可被歸零（#1 bug）
- _recalculate_salary_record_totals 重算後 gross_salary 仍含時薪總計
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
import api.salary as salary_module
from api.salary import router as salary_router, _recalculate_salary_record_totals
from models.database import Base, Employee, User, SalaryRecord, AuditLog
from utils.audit import AuditMiddleware
from utils.auth import hash_password


@pytest.fixture
def salary_client(tmp_path, monkeypatch):
    db_path = tmp_path / "salary-manual-adjust.sqlite"
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

    fake_salary_engine = MagicMock()
    fake_insurance_service = MagicMock()
    salary_module.init_salary_services(fake_salary_engine, fake_insurance_service)

    # AuditMiddleware 預設用 asyncio.to_thread + background task 推 audit 寫入，
    # TestClient 同步呼叫立即 query DB 會 race（task 還沒 run）。
    # 改 _schedule_audit_write 直接同步寫入，讓 audit 在 response 返回前完成。
    import utils.audit as audit_module

    monkeypatch.setattr(
        audit_module,
        "_schedule_audit_write",
        audit_module._write_audit_sync,
    )

    app = FastAPI()
    # T7: 掛 AuditMiddleware 才能驗證 manual_adjust 是否真的寫 AuditLog
    app.add_middleware(AuditMiddleware)
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_with_meeting_absence(session_factory):
    """正職員工 + 已扣減的節慶獎金（festival 1800、meeting_absence 200，raw=2000）"""
    with session_factory() as session:
        emp = Employee(
            employee_id="M001",
            name="會議扣減測試",
            base_salary=30000,
            employee_type="regular",
            is_active=True,
        )
        session.add(emp)
        session.flush()
        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=6,  # 發放月
            base_salary=30000,
            festival_bonus=1800,
            meeting_absence_deduction=200,
            gross_salary=30000,
            total_deduction=0,
            net_salary=30000,
            is_finalized=False,
        )
        session.add(record)
        user = User(
            employee_id=None,
            username="adj_admin",
            password_hash=hash_password("AdjPass123"),
            role="admin",
            permission_names=["*"],
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        return record.id


def _seed_hourly(session_factory):
    """建立時薪制員工 + 既有薪資記錄（hourly_total=24,000）。"""
    with session_factory() as session:
        emp = Employee(
            employee_id="H001",
            name="時薪測試",
            base_salary=0,
            hourly_rate=200,
            employee_type="hourly",
            is_active=True,
        )
        session.add(emp)
        session.flush()
        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=4,
            base_salary=0,
            hourly_total=24000,
            work_hours=120,
            hourly_rate=200,
            gross_salary=24000,
            total_deduction=0,
            net_salary=24000,
            is_finalized=False,
        )
        session.add(record)
        user = User(
            employee_id=None,
            username="adj_admin",
            password_hash=hash_password("AdjPass123"),
            role="admin",
            permission_names=["*"],
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        return record.id


def _login(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "adj_admin", "password": "AdjPass123"},
    )
    assert res.status_code == 200


class TestRecalculatePreservesHourlyTotal:
    def test_recalculate_includes_hourly_total(self):
        """單元測試：_recalculate 必須把 hourly_total 加回 gross_salary"""
        record = SalaryRecord(
            base_salary=0,
            hourly_total=24000,
            other_deduction=500,
        )
        _recalculate_salary_record_totals(record)
        assert record.gross_salary == 24000
        assert record.total_deduction == 500
        assert record.net_salary == 23500

    def test_edit_meeting_absence_alone_recomputes_festival_bonus(self, salary_client):
        """情境：管理員只改 meeting_absence_deduction（200→0，例：會議實際出席被誤標）。
        festival_bonus 應自動回推 raw（1800+200=2000），再以新 absence 重套：
        festival = max(0, 2000 - 0) = 2000。
        """
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "測試連動：清空 meeting_absence",
                "meeting_absence_deduction": 0,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        assert rec["meeting_absence_deduction"] == 0
        assert rec["festival_bonus"] == 2000

    def test_edit_meeting_absence_partial_recomputes(self, salary_client):
        """meeting_absence 200→100：festival 應變成 max(0, 2000-100)=1900。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "調整會議缺席扣減試算",
                "meeting_absence_deduction": 100,
            },
        )
        assert res.status_code == 200
        assert res.json()["record"]["festival_bonus"] == 1900

    def test_meeting_absence_connection_delta_counted_for_threshold(
        self, salary_client
    ):
        """回歸：meeting_absence 連動的 festival_bonus 變動量也要納入 total_abs_delta，
        否則會計可拆兩動作（降 meeting_absence → festival 連動推高）繞過 1000 元金流簽核。"""
        client, sf = salary_client
        # 種子：festival 600、meeting_absence 600（raw=1200）
        with sf() as session:
            emp = Employee(
                employee_id="MABYP",
                name="繞過測試",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            session.add(emp)
            session.flush()
            record = SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=6,
                base_salary=30000,
                festival_bonus=600,
                meeting_absence_deduction=600,
                gross_salary=30000,
                total_deduction=0,
                net_salary=30000,
                is_finalized=False,
            )
            session.add(record)
            # 給「能 manual_adjust 但不具 ACTIVITY_PAYMENT_APPROVE」的角色
            from utils.permissions import Permission

            non_approve_perms = ["SALARY_WRITE", "SALARY_READ"]
            user = User(
                employee_id=None,
                username="hr_no_approve",
                password_hash=hash_password("HrPass1234"),
                role="hr",
                permission_names=non_approve_perms,
                is_active=True,
                must_change_password=False,
            )
            session.add(user)
            session.commit()
            record_id = record.id

        # 登入 hr_no_approve（無金流簽核）
        res_login = client.post(
            "/api/auth/login",
            json={"username": "hr_no_approve", "password": "HrPass1234"},
        )
        assert res_login.status_code == 200, res_login.text

        # 把 meeting_absence 降 600 → 0（payload |delta|=600，本身不超 1000；
        # 但連動 festival 600→1200，連動 |delta|=600；總和 1200 > 1000 應觸發 403）
        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "嘗試以 meeting_absence 連動繞門檻",
                "meeting_absence_deduction": 0,
            },
        )
        assert res.status_code == 403, res.text
        assert (
            "金流簽核" in res.json()["detail"]
            or "ACTIVITY_PAYMENT_APPROVE" in res.json()["detail"]
        )

    def test_cross_request_split_accumulates_against_baseline(self, salary_client):
        """C9：無金流簽核權者對他人 record 連兩次各 +800（單次 |delta|<1000，
        各自能過舊「單次門檻」），但相對 baseline 的累積偏移已達 1600 > 1000，
        第二次應要求 ACTIVITY_PAYMENT_APPROVE → 403。封死跨請求拆筆繞過。"""
        client, sf = salary_client
        with sf() as session:
            emp = Employee(
                employee_id="SPLIT1",
                name="拆筆測試",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            session.add(emp)
            session.flush()
            record = SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=6,
                base_salary=30000,
                special_bonus=0,
                gross_salary=30000,
                total_deduction=0,
                net_salary=30000,
                is_finalized=False,
            )
            session.add(record)
            user = User(
                employee_id=None,
                username="hr_no_approve",
                password_hash=hash_password("HrPass1234"),
                role="hr",
                permission_names=["SALARY_WRITE", "SALARY_READ"],
                is_active=True,
                must_change_password=False,
            )
            session.add(user)
            session.commit()
            record_id = record.id

        res_login = client.post(
            "/api/auth/login",
            json={"username": "hr_no_approve", "password": "HrPass1234"},
        )
        assert res_login.status_code == 200, res_login.text

        # 第一次：special_bonus 0 → 800（單次 delta=800 < 1000，baseline 偏移 800）→ 放行
        res1 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "第一次加給八百", "special_bonus": 800},
        )
        assert res1.status_code == 200, res1.text
        assert res1.json()["record"]["manual_overrides"] == ["special_bonus"]

        # 第二次：special_bonus 800 → 1600（單次 delta 仍 800 < 1000，但相對 baseline 0
        # 累積偏移 = 1600 > 1000）→ 第二次應 403。
        res2 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "第二次再加八百", "special_bonus": 1600},
        )
        assert res2.status_code == 403, res2.text
        assert (
            "金流簽核" in res2.json()["detail"]
            or "ACTIVITY_PAYMENT_APPROVE" in res2.json()["detail"]
        )

        # 被擋後 DB 不應落到 1600
        with sf() as session:
            r = session.query(SalaryRecord).filter_by(id=record_id).one()
            assert r.special_bonus == 800

    def test_single_field_baseline_accumulation_allows_with_approve(
        self, salary_client
    ):
        """有金流簽核權者同樣的跨請求調整應放行（門檻只擋無權者）。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)  # admin(*) seeded
        _login(client)

        res1 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "管理員加給八百", "special_bonus": 800},
        )
        assert res1.status_code == 200, res1.text
        res2 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"adjustment_reason": "管理員再加八百", "special_bonus": 1600},
        )
        assert res2.status_code == 200, res2.text
        assert res2.json()["record"]["bonus_amount"] >= 0

    def test_edit_both_festival_and_meeting_absence_no_auto_recompute(
        self, salary_client
    ):
        """同時手動覆寫 festival_bonus 與 meeting_absence_deduction：
        管理員的 festival 為最終值，不再自動回推 raw。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "同時覆寫節慶與扣減",
                "festival_bonus": 3000,
                "meeting_absence_deduction": 100,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        # festival 維持管理員打的 3000，不再自動扣 100
        assert rec["festival_bonus"] == 3000
        assert rec["meeting_absence_deduction"] == 100

    def test_manual_adjust_does_not_zero_hourly_total(self, salary_client):
        """整合測試：時薪制員工被 manual-adjust 改任意欄位後，gross 仍含 hourly_total。"""
        client, sf = salary_client
        record_id = _seed_hourly(sf)
        _login(client)

        # 管理員加 500 元其他扣款
        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "測試時薪 gross 保留",
                "other_deduction": 500,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        # hourly_total 應仍為 24000，gross_salary 應為 24000
        assert (
            rec["gross_salary"] == 24000
        ), f"時薪制 gross_salary 不應被歸零，得到 {rec['gross_salary']}"
        assert rec["net_salary"] == 23500

    # ------------------------------------------------------------------
    # T7：AuditLog 寫入斷言（manual_adjust 必須留稽核軌跡）
    # ------------------------------------------------------------------
    def test_manual_adjust_writes_audit_row(self, salary_client):
        """每次手動調整都必須寫一筆 AuditLog，summary 含 record_id、employee_id、
        年月與變更欄位描述。Regression: 早期 audit_summary 用通用「修改薪資」
        無法事後追責。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "audit 斷言測試用",
                "meeting_absence_deduction": 0,
            },
        )
        assert res.status_code == 200

        with sf() as session:
            rows = (
                session.query(AuditLog)
                .filter(
                    AuditLog.entity_id == str(record_id),
                    AuditLog.summary.like("%手動調整薪資%"),
                )
                .order_by(AuditLog.id.desc())
                .all()
            )
            assert (
                rows
            ), "manual_adjust 未寫入任何 AuditLog（過濾 entity_id + 手動調整薪資 summary）"
            summary = rows[0].summary or ""
            assert f"#{record_id}" in summary, f"summary 缺 record_id：{summary!r}"
            # 至少含一個欄位變動描述
            assert "→" in summary, f"summary 缺欄位變動格式：{summary!r}"


class TestExtraAllowanceManualAdjust:
    """額外加給（值週/活動加班費）手填欄位的端點行為。"""

    def test_set_extra_allowance_and_label(self, salary_client):
        """填入 extra_allowance 金額 + 名目：併入 gross/net、兩欄進 manual_overrides、回應帶名目。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(
            sf
        )  # base 30000、festival 1800(不進 gross)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "補發值週費",
                "extra_allowance": 1241,
                "extra_allowance_label": "值週",
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        assert rec["extra_allowance"] == 1241
        assert rec["extra_allowance_label"] == "值週"
        # gross = base 30000 + extra 1241（festival 不進 gross）
        assert rec["gross_salary"] == 31241
        assert rec["net_salary"] == 31241
        # 金額與名目都應鎖定，重算時保留
        assert "extra_allowance" in rec["manual_overrides"]
        assert "extra_allowance_label" in rec["manual_overrides"]
