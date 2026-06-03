"""tests/consent/test_dsr_approve_execute.py — Task 12 TDD

approve_dsr_request 按 request_type 分派執行測試：
- delete → ownership 重驗 + student lifecycle → WITHDRAWN（365d GC 接手）
- delete + 非監護人 → 403
- correct → 僅 status=approved，student lifecycle 不變
- 既有 approve-status 測試對應調整（使用 correct 型別避免 lifecycle / ownership 檢查）

P2-3 PDPA Phase 2 enforcement Task 12。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.database import Base, User
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_STATUS_PENDING,
    DsrRequest,
)
from models.guardian import Guardian
from models.student_log import (
    StudentChangeLog,
)  # noqa: F401 — 確保 student_change_logs 表在 create_all 時被建立
from utils.auth import hash_password

# ============================================================
# 共用種子函式（呼叫端負責 session.commit() + session.close()）
# ============================================================


def _make_admin(session) -> User:
    """建立 admin user，flush 後回傳（呼叫端 commit）。"""
    admin = User(
        username="dsr_exec_admin",
        password_hash=hash_password("pass"),
        role="admin",
        permission_names=["*"],
    )
    session.add(admin)
    session.flush()
    return admin


def _make_student_with_guardian(session, admin_user_id: int):
    """建立 Classroom → Student(active) → Guardian(user_id=admin)，回傳 (student, guardian)。"""
    classroom = Classroom(name="測試班級", school_year=114, semester=1)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="T999",
        name="測試學生",
        classroom_id=classroom.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
        is_active=True,
        enrollment_date=date(2026, 2, 1),
    )
    session.add(student)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=admin_user_id,
        name="測試家長",
        phone="0912345678",
        deleted_at=None,
    )
    session.add(guardian)
    session.flush()
    return student, guardian


# ============================================================
# Fixture — 每個測試獨立 SQLite 檔 + TestClient
# ============================================================


@pytest.fixture
def dsr_exec_setup(tmp_path):
    """回傳 (client, session_factory, seed_data)。seed_data 由 fixture 完成。"""
    db_path = tmp_path / "dsr-exec.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    # ── Seed：admin + student + guardian（在 TestClient 啟動前完成）─────────
    session = session_factory()
    admin = _make_admin(session)
    student, guardian = _make_student_with_guardian(session, admin.id)

    # 額外 other_user（無監護關係）
    other_user = User(
        username="other_parent",
        password_hash=hash_password("pass"),
        role="parent",
        permission_names=[],
    )
    session.add(other_user)
    session.flush()

    # ghost guardian（student_id=9999，SQLite 不強制 FK）
    ghost_guardian = Guardian(
        student_id=9999,
        user_id=admin.id,
        name="鬼家長",
        deleted_at=None,
    )
    session.add(ghost_guardian)

    session.commit()

    seed = {
        "admin_id": admin.id,
        "student_id": student.id,
        "other_user_id": other_user.id,
    }
    session.close()
    # ────────────────────────────────────────────────────────────────────────

    from api.dsr_admin import router as dsr_admin_router
    from api.auth import router as auth_router

    app = FastAPI()
    app.include_router(dsr_admin_router)
    app.include_router(auth_router)

    with TestClient(app) as c:
        yield c, session_factory, seed

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _admin_login(client) -> None:
    resp = client.post(
        "/api/auth/login", json={"username": "dsr_exec_admin", "password": "pass"}
    )
    assert resp.status_code == 200


def _create_pending_dsr(session_factory, **kwargs) -> int:
    """在測試中動態新增一筆 pending DSR，回傳 id（session 已 commit + close）。"""
    session = session_factory()
    dsr = DsrRequest(status=DSR_STATUS_PENDING, **kwargs)
    session.add(dsr)
    session.commit()
    dsr_id = dsr.id
    session.close()
    return dsr_id


# ============================================================
# Task 12 核心：approve delete → lifecycle WITHDRAWN + ownership 重驗
# ============================================================


class TestApproveDeleteDispatch:
    """delete 型 DSR approve：student 轉 WITHDRAWN，Guardian ownership 重驗。"""

    def test_approve_delete_valid_guardian_transitions_student_to_withdrawn(
        self, dsr_exec_setup
    ):
        """approve delete + 申請家長確為監護人 → student.lifecycle_status = withdrawn。"""
        c, session_factory, seed = dsr_exec_setup
        admin_id = seed["admin_id"]
        student_id = seed["student_id"]

        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=admin_id,
            request_type=DSR_REQUEST_TYPE_DELETE,
            subject_entity_type="student",
            subject_entity_id=student_id,
            reason="個資刪除申請",
        )

        _admin_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "同意刪除"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        assert body["decision_note"] == "同意刪除"
        assert body["decided_at"] is not None

        # 驗證 student lifecycle 已轉 withdrawn
        verify_session = session_factory()
        updated_student = verify_session.query(Student).filter_by(id=student_id).first()
        assert updated_student is not None
        assert updated_student.lifecycle_status == LIFECYCLE_WITHDRAWN
        assert updated_student.is_active is False
        verify_session.close()

    def test_approve_delete_non_guardian_returns_403(self, dsr_exec_setup):
        """approve delete + 申請家長非監護人 → 403。"""
        c, session_factory, seed = dsr_exec_setup
        student_id = seed["student_id"]
        other_user_id = seed["other_user_id"]

        # DSR 由 other_user 提出（無 Guardian 關係）
        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=other_user_id,
            request_type=DSR_REQUEST_TYPE_DELETE,
            subject_entity_type="student",
            subject_entity_id=student_id,
            reason="個資刪除申請",
        )

        _admin_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "嘗試核准"},
        )

        assert resp.status_code == 403
        assert "監護關係" in resp.json()["detail"]

        # student lifecycle 不應改變
        verify_session = session_factory()
        untouched = verify_session.query(Student).filter_by(id=student_id).first()
        assert untouched.lifecycle_status == LIFECYCLE_ACTIVE
        verify_session.close()

    def test_approve_delete_missing_student_returns_404(self, dsr_exec_setup):
        """approve delete + Guardian 存在但 student_id=9999 不存在 → 404。"""
        c, session_factory, seed = dsr_exec_setup
        admin_id = seed["admin_id"]

        # ghost_guardian(student_id=9999, user_id=admin_id) 已在 fixture 中 seed
        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=admin_id,
            request_type=DSR_REQUEST_TYPE_DELETE,
            subject_entity_type="student",
            subject_entity_id=9999,
            reason="不存在的學生",
        )

        _admin_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "核准"},
        )

        assert resp.status_code == 404


# ============================================================
# Task 12 核心：approve correct → 僅 status，lifecycle 不變
# ============================================================


class TestApproveCorrectDispatch:
    """correct 型 DSR approve：僅 status=approved，student lifecycle 不動，new_value 不套。"""

    def test_approve_correct_sets_status_without_lifecycle_change(self, dsr_exec_setup):
        """approve correct → status=approved，student lifecycle 不變。"""
        c, session_factory, seed = dsr_exec_setup
        admin_id = seed["admin_id"]
        student_id = seed["student_id"]

        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=admin_id,
            request_type=DSR_REQUEST_TYPE_CORRECT,
            subject_entity_type="student",
            subject_entity_id=student_id,
            field_name="name",
            new_value="新姓名",
            reason="更正申請",
        )

        _admin_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "請手動更正"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["decision_note"] == "請手動更正"
        assert body["decided_at"] is not None

        # student lifecycle 不應改變（仍為 active）
        verify_session = session_factory()
        unchanged = verify_session.query(Student).filter_by(id=student_id).first()
        assert unchanged.lifecycle_status == LIFECYCLE_ACTIVE
        # new_value 不應被套用（name 仍為原值）
        assert unchanged.name == "測試學生"
        verify_session.close()


# ============================================================
# 既有 approve-status 測試調整後的版本
# 原 TestDsrAdminApproveEndpoint 使用混合 delete/correct 型別 seed，
# Task 12 新增 ownership 檢查後 delete DSR 若無對應 Student+Guardian 會 403/404。
# 此處明確使用 correct 型別以驗證純 status=approved 路徑，不觸發 lifecycle。
# ============================================================


class TestApproveStatusUpdateCorrectType:
    """approve 純 status 路徑驗證（correct 型別，不觸發 ownership 或 lifecycle）。"""

    def test_approve_correct_returns_approved_with_fields(self, dsr_exec_setup):
        c, session_factory, seed = dsr_exec_setup
        admin_id = seed["admin_id"]

        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=admin_id,
            request_type=DSR_REQUEST_TYPE_CORRECT,
            subject_entity_type="student",
            subject_entity_id=1,
            field_name="name",
            new_value="test",
            reason="更正申請",
        )

        _admin_login(c)
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "核准更正"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["decision_note"] == "核准更正"
        assert body["decided_at"] is not None
        assert body["decided_by"] is not None

    def test_approve_correct_non_pending_returns_404(self, dsr_exec_setup):
        c, session_factory, seed = dsr_exec_setup
        admin_id = seed["admin_id"]

        dsr_id = _create_pending_dsr(
            session_factory,
            user_id=admin_id,
            request_type=DSR_REQUEST_TYPE_CORRECT,
            subject_entity_type="student",
            subject_entity_id=1,
            field_name="name",
            new_value="test",
            reason="更正申請",
        )

        _admin_login(c)
        # 第一次 approve
        c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "第一次核准"},
        )
        # 再次 approve 應 404（已非 pending）
        resp = c.post(
            f"/api/admin/dsr-requests/{dsr_id}/approve",
            json={"decision_note": "第二次嘗試"},
        )
        assert resp.status_code == 404

    def test_approve_nonexistent_id_returns_404(self, dsr_exec_setup):
        c, _, _ = dsr_exec_setup
        _admin_login(c)
        resp = c.post(
            "/api/admin/dsr-requests/99999/approve",
            json={"decision_note": "不存在"},
        )
        assert resp.status_code == 404
