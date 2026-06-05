"""
測試軟刪 endpoint 呼叫 mark_soft_delete 後 request.state.audit_summary 含「軟刪」字眼。

四個測試對象：
1. DELETE /api/attachments/{id}            — api/attachments.py
2. DELETE /api/students/guardians/{id}     — api/students.py
3. DELETE /api/portal/contact-book/{id}/photos/{att_id}   — api/portal/contact_book.py
4. DELETE /{entry_id}/replies/{reply_id}   — api/parent_portal/contact_book.py

策略：patch 掉 DB session 及相依 helper，直接呼叫 async endpoint 函式，
確認 request.state.audit_summary 以「軟刪」開頭，audit_delete_kind == "soft"。
Tests intentionally fail before endpoint changes; pass after mark_soft_delete is wired in.
"""

from __future__ import annotations

import asyncio


def _run_maybe_async(_result):
    """B2 async→def 遷移相容：handler 轉同步 def 後不再是 coroutine；
    僅 coroutine 才走 asyncio.run，否則直接回傳同步結果。"""
    import inspect as _inspect

    if _inspect.iscoroutine(_result):
        return asyncio.run(_result)
    return _result


import sys
import os
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── helpers ──────────────────────────────────────────────────────────────────


def _fake_request():
    """FastAPI-like request 帶空 state。"""
    return SimpleNamespace(
        method="DELETE",
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/test"),
    )


def _fake_admin():
    """最小 admin 使用者 dict（無 employee_id，僅 role=admin）。

    permission_names=['*'] 是 Phase 2.1 後 portfolio_access wrapper 的 code= 入參
    走 resolve_grant 路徑時要 admin 通行所必須（wildcard → ('all') scope）；
    舊測試僅靠 role-based fallback，但已 migrate 的 router 帶 code= 後不再走 role 短路。
    """
    return {
        "user_id": 1,
        "username": "admin",
        "role": "admin",
        "employee_id": 1,
        "permission_names": ["*"],
    }


@contextmanager
def _fake_session_scope(fake_session):
    """contextmanager 相容 session_scope() 用法。"""
    yield fake_session


# ── Test 1: api/attachments.py ────────────────────────────────────────────────


def test_attachment_soft_delete_sets_soft_delete_summary():
    """DELETE /api/attachments/{id} → request.state.audit_summary 應以「軟刪」開頭。"""
    # 建立 fake Attachment
    att = SimpleNamespace(
        id=1,
        owner_type="observation",
        owner_id=10,
        deleted_at=None,
    )

    # 建立 fake session
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = att

    # 建立 fake StudentObservation（用於 _resolve_owner_student_id）
    obs = SimpleNamespace(id=10, student_id=99)

    def _query_side_effect(model):
        """根據 model 回傳不同 mock chain。"""
        mock_chain = MagicMock()
        if hasattr(model, "__tablename__") and model.__tablename__ == "attachments":
            mock_chain.filter.return_value.first.return_value = att
        elif (
            hasattr(model, "__tablename__")
            and model.__tablename__ == "student_observations"
        ):
            mock_chain.filter.return_value.first.return_value = obs
        elif hasattr(model, "__tablename__") and model.__tablename__ == "students":
            student = SimpleNamespace(id=99, classroom_id=1, lifecycle_status="active")
            mock_chain.filter.return_value.first.return_value = student
        return mock_chain

    fake_session.query.side_effect = _query_side_effect
    fake_session.flush = MagicMock()

    request = _fake_request()
    current_user = _fake_admin()

    import api.attachments as att_module
    from api.attachments import delete_attachment

    with patch.object(
        att_module,
        "session_scope",
        side_effect=lambda: _fake_session_scope(fake_session),
    ):
        with patch("utils.portfolio_access.assert_student_access") as mock_access:
            mock_access.return_value = SimpleNamespace(id=99)
            result = _run_maybe_async(
                delete_attachment(
                    attachment_id=1,
                    request=request,
                    current_user=current_user,
                )
            )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"


# ── Test 2: api/students.py (guardian) ───────────────────────────────────────


def test_guardian_soft_delete_sets_soft_delete_summary():
    """DELETE /api/students/guardians/{id} → request.state.audit_summary 應以「軟刪」開頭。"""
    guardian = SimpleNamespace(
        id=5,
        student_id=99,
        name="王媽媽",
        deleted_at=None,
        is_primary=True,
    )
    student = SimpleNamespace(id=99)

    fake_session = MagicMock()

    def _query_side_effect(model):
        mock_chain = MagicMock()
        if hasattr(model, "__tablename__") and model.__tablename__ == "guardians":
            mock_chain.filter.return_value.first.return_value = guardian
        elif hasattr(model, "__tablename__") and model.__tablename__ == "students":
            mock_chain.filter.return_value.first.return_value = student
        return mock_chain

    fake_session.query.side_effect = _query_side_effect
    fake_session.commit = MagicMock()
    fake_session.rollback = MagicMock()
    fake_session.close = MagicMock()

    request = _fake_request()
    current_user = _fake_admin()

    import api.students as stu_module
    from api.students import delete_guardian

    with patch.object(stu_module, "get_session", return_value=fake_session):
        with patch.object(stu_module, "_sync_primary_guardian_to_student"):
            result = _run_maybe_async(
                delete_guardian(
                    guardian_id=5,
                    request=request,
                    current_user=current_user,
                )
            )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"


# ── Test 3: api/portal/contact_book.py (delete_photo) ────────────────────────


def test_portal_contact_book_delete_photo_sets_soft_delete_summary():
    """DELETE /portal/contact-book/{id}/photos/{att_id} → summary 以「軟刪」開頭。"""
    entry = SimpleNamespace(id=10, classroom_id=1, deleted_at=None)
    att = SimpleNamespace(id=20, deleted_at=None)
    emp = SimpleNamespace(id=7)

    fake_session = MagicMock()

    def _query_side_effect(model):
        mock_chain = MagicMock()
        tname = getattr(model, "__tablename__", "")
        if tname == "student_contact_book_entries":
            mock_chain.filter.return_value.first.return_value = entry
        elif tname == "attachments":
            mock_chain.filter.return_value.filter.return_value.filter.return_value.first.return_value = (
                att
            )
            mock_chain.filter.return_value.filter.return_value.first.return_value = att
            mock_chain.filter.return_value.first.return_value = att
        elif tname == "employees":
            mock_chain.filter.return_value.first.return_value = emp
        return mock_chain

    fake_session.query.side_effect = _query_side_effect
    fake_session.commit = MagicMock()
    fake_session.close = MagicMock()

    request = _fake_request()
    current_user = {
        "user_id": 1,
        "username": "admin",
        "role": "admin",
        "employee_id": 7,
        # 真 admin token 帶 wildcard；contact_book delete_photo 改用
        # is_unrestricted(code=PORTFOLIO_WRITE) 後，mock 須帶 permission_names 才能
        # 反映生產（否則被當無權限 → _assert_classroom_owned 403）。
        "permission_names": ["*"],
    }

    import api.portal.contact_book as cb_module
    from api.portal.contact_book import delete_photo

    with patch.object(cb_module, "get_session", return_value=fake_session):
        with patch.object(cb_module, "_get_employee", return_value=emp):
            result = delete_photo(
                entry_id=10,
                attachment_id=20,
                request=request,
                current_user=current_user,
            )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"


# ── Test 4: api/parent_portal/contact_book.py (delete_reply) ──────────────────


def test_parent_contact_book_delete_reply_sets_soft_delete_summary():
    """DELETE /parent/contact-book/{entry_id}/replies/{reply_id} → summary 以「軟刪」開頭。"""
    entry = SimpleNamespace(id=10)
    reply = SimpleNamespace(
        id=3,
        entry_id=10,
        guardian_user_id=42,
        deleted_at=None,
    )

    fake_session = MagicMock()
    fake_session.flush = MagicMock()

    request = _fake_request()
    current_user = {"user_id": 42, "username": "parent", "role": "parent"}

    import api.parent_portal.contact_book as pcb_module
    from api.parent_portal.contact_book import delete_reply

    with patch.object(pcb_module, "_get_entry_for_parent", return_value=entry):
        fake_session.query.return_value.filter.return_value.filter.return_value.first.return_value = (
            reply
        )
        fake_session.query.return_value.filter.return_value.first.return_value = reply

        result = delete_reply(
            entry_id=10,
            reply_id=3,
            request=request,
            current_user=current_user,
            session=fake_session,
        )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"


# ── Test 5: api/employees.py (delete_employee) ────────────────────────────────


def test_employee_deactivate_sets_soft_delete_audit():
    """DELETE /employees/{id} → request.state.audit_summary 應含「軟刪 員工」且 audit_delete_kind == 'soft'"""
    employee = SimpleNamespace(
        id=7,
        name="王小明",
        is_active=True,
        resign_date=None,
        employee_id="EMP007",
    )

    def _query_side_effect(model):
        mock_chain = MagicMock()
        tname = getattr(model, "__tablename__", "")
        if tname == "employees":
            mock_chain.filter.return_value.first.return_value = employee
        elif tname == "salary_records":
            mock_chain.filter.return_value.filter.return_value.update.return_value = 0
        return mock_chain

    fake_session = MagicMock()
    fake_session.query.side_effect = _query_side_effect
    fake_session.commit = MagicMock()
    fake_session.rollback = MagicMock()
    fake_session.close = MagicMock()

    request = _fake_request()
    current_user = _fake_admin()

    import api.employees as emp_module
    from api.employees import delete_employee

    with patch.object(emp_module, "get_session", return_value=fake_session):
        result = _run_maybe_async(
            delete_employee(
                employee_id=7,
                request=request,
                current_user=current_user,
            )
        )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert (
        "員工" in request.state.audit_summary
    ), f"summary should contain '員工', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"


# ── Test 6: api/auth.py (update_user is_active=False) ────────────────────────


def test_user_deactivate_sets_soft_delete_audit():
    """PUT /users/{id} with is_active=False → request.state.audit_summary 應含「軟刪 使用者帳號」且 audit_delete_kind == 'soft'"""
    user = SimpleNamespace(
        id=3,
        username="jdoe",
        role="teacher",
        is_active=True,
        permission_names=["EMPLOYEES_READ"],
        token_version=1,
        employee_id=None,
    )

    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = user
    fake_session.commit = MagicMock()
    fake_session.rollback = MagicMock()
    fake_session.close = MagicMock()

    request = _fake_request()
    current_user = _fake_admin()

    import api.auth as auth_module
    from api.auth import update_user

    # Minimal payload: only is_active=False
    data = SimpleNamespace(
        role=None,
        permission_names=None,
        is_active=False,
    )

    with patch.object(auth_module, "get_session", return_value=fake_session):
        with patch.object(auth_module, "_assert_can_manage_user"):
            with patch.object(
                auth_module, "get_role_default_permissions", return_value=[]
            ):
                result = update_user(
                    user_id=3,
                    data=data,
                    request=request,
                    current_user=current_user,
                )

    assert hasattr(
        request.state, "audit_summary"
    ), "request.state.audit_summary 未設定 — mark_soft_delete 未被呼叫"
    assert request.state.audit_summary.startswith(
        "軟刪"
    ), f"summary should start with '軟刪', got: {request.state.audit_summary!r}"
    assert (
        "使用者帳號" in request.state.audit_summary
    ), f"summary should contain '使用者帳號', got: {request.state.audit_summary!r}"
    assert getattr(request.state, "audit_delete_kind", None) == "soft"
