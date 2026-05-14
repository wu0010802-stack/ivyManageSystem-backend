"""共用核准守衛 helper 單元測試。

涵蓋三個新 helper：
- is_self_approval
- assert_approver_eligible
- collect_months_from_date_range
"""

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import ApprovalPolicy, Base, User
from utils.approval_helpers import (
    assert_approver_eligible,
    collect_months_from_date_range,
    is_self_approval,
)


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "approval-guards.sqlite"
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

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


# ──────────────────────────────────────────────
# is_self_approval
# ──────────────────────────────────────────────


def test_is_self_approval_same_employee_returns_true():
    approver = {"id": 1, "employee_id": 42, "role": "supervisor"}
    assert is_self_approval(approver, 42) is True


def test_is_self_approval_different_employee_returns_false():
    approver = {"id": 1, "employee_id": 42, "role": "supervisor"}
    assert is_self_approval(approver, 99) is False


def test_is_self_approval_admin_without_employee_id_returns_false():
    """純管理員（無 employee_id）不會構成自我核准風險。"""
    approver = {"id": 1, "role": "admin"}  # no employee_id key
    assert is_self_approval(approver, 42) is False


def test_is_self_approval_employee_id_none_returns_false():
    approver = {"id": 1, "employee_id": None, "role": "admin"}
    assert is_self_approval(approver, 42) is False


# ──────────────────────────────────────────────
# assert_approver_eligible
# ──────────────────────────────────────────────


def _seed_user_with_role(session, employee_id: int, role: str):
    session.add(
        User(
            username=f"u{employee_id}",
            password_hash="x",
            role=role,
            is_active=True,
            employee_id=employee_id,
        )
    )
    session.flush()


def _seed_policy(session, doc_type: str, submitter_role: str, approver_roles: str):
    session.add(
        ApprovalPolicy(
            doc_type=doc_type,
            submitter_role=submitter_role,
            approver_roles=approver_roles,
            is_active=True,
        )
    )
    session.flush()


def test_assert_approver_eligible_policy_match_returns_submitter_role(db_session):
    _seed_user_with_role(db_session, employee_id=10, role="teacher")
    _seed_policy(
        db_session,
        doc_type="leave",
        submitter_role="teacher",
        approver_roles="supervisor,admin",
    )

    result = assert_approver_eligible(
        db_session,
        doc_type="leave",
        doc_label="請假",
        submitter_employee_id=10,
        approver_role="supervisor",
    )
    assert result == "teacher"


def test_assert_approver_eligible_policy_mismatch_raises_403(db_session):
    _seed_user_with_role(db_session, employee_id=10, role="teacher")
    _seed_policy(
        db_session, doc_type="leave", submitter_role="teacher", approver_roles="admin"
    )

    with pytest.raises(HTTPException) as exc:
        assert_approver_eligible(
            db_session,
            doc_type="leave",
            doc_label="請假",
            submitter_employee_id=10,
            approver_role="supervisor",
        )
    assert exc.value.status_code == 403
    assert "supervisor" in exc.value.detail
    assert "teacher" in exc.value.detail
    assert "請假" in exc.value.detail


def test_assert_approver_eligible_no_policy_admin_fallback(db_session):
    """政策未設定時，admin 仍可兜底通過。"""
    _seed_user_with_role(db_session, employee_id=10, role="teacher")
    # no policy seeded for "leave"

    result = assert_approver_eligible(
        db_session,
        doc_type="leave",
        doc_label="請假",
        submitter_employee_id=10,
        approver_role="admin",
    )
    assert result == "teacher"


def test_assert_approver_eligible_no_policy_non_admin_raises(db_session):
    _seed_user_with_role(db_session, employee_id=10, role="teacher")
    # no policy seeded

    with pytest.raises(HTTPException) as exc:
        assert_approver_eligible(
            db_session,
            doc_type="leave",
            doc_label="請假",
            submitter_employee_id=10,
            approver_role="supervisor",
        )
    assert exc.value.status_code == 403


def test_assert_approver_eligible_user_not_found_defaults_to_teacher(db_session):
    """員工沒有對應 User 帳號時，依 _get_submitter_role 規則預設 teacher。"""
    _seed_policy(
        db_session,
        doc_type="overtime",
        submitter_role="teacher",
        approver_roles="supervisor",
    )

    result = assert_approver_eligible(
        db_session,
        doc_type="overtime",
        doc_label="加班",
        submitter_employee_id=999,  # no User row
        approver_role="supervisor",
    )
    assert result == "teacher"


# ──────────────────────────────────────────────
# collect_months_from_date_range
# ──────────────────────────────────────────────


def test_collect_months_same_day():
    result = collect_months_from_date_range(date(2026, 5, 14), date(2026, 5, 14))
    assert result == {(2026, 5)}


def test_collect_months_same_month_range():
    result = collect_months_from_date_range(date(2026, 5, 1), date(2026, 5, 31))
    assert result == {(2026, 5)}


def test_collect_months_cross_month_within_year():
    result = collect_months_from_date_range(date(2026, 3, 15), date(2026, 6, 5))
    assert result == {(2026, 3), (2026, 4), (2026, 5), (2026, 6)}


def test_collect_months_cross_year():
    """跨年（12 月 → 隔年 1 月）— 月份遞增必須正確處理 12→1 的進位。"""
    result = collect_months_from_date_range(date(2025, 11, 20), date(2026, 2, 10))
    assert result == {(2025, 11), (2025, 12), (2026, 1), (2026, 2)}


def test_collect_months_multi_year():
    result = collect_months_from_date_range(date(2024, 12, 31), date(2026, 1, 1))
    assert (2024, 12) in result
    assert (2025, 6) in result
    assert (2026, 1) in result
    assert len(result) == 14  # 2024-12, 2025-01..12, 2026-01 = 14
