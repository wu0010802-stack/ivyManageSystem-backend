"""RED→GREEN TDD：sync_registrations_on_student_deactivate 部分失敗主動告警。

當 SAVEPOINT 內單筆軟刪失敗 → 進 failed list →
  1. ops_alert.notify_student_sync_failure 被呼叫，帶失敗 reg_id
  2. 函式仍正常回傳成功筆數（行為不變）
  3. 告警函式自身拋例外時，deactivate 仍回傳成功筆數（fail-soft，不 propagate）

全部使用 SQLite in-memory，不依賴外部 DB，不依賴 LineService 真實注入。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from models.database import Base
    from models.academic_term import AcademicTerm  # noqa: F401 — registers table

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session
    finally:
        session.close()


def _seed_two_regs(session):
    """建一個學生 + 兩筆當學期 active 報名（paid_amount=0）。回傳 (student_id, [reg_id_0, reg_id_1])。"""
    from models.database import ActivityRegistration, Classroom, Student
    from utils.academic import resolve_current_academic_term

    classroom = Classroom(name="班Alert", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S-alert-001",
        name="告警測試生",
        birthday=date(2020, 3, 1),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    sy, sem = resolve_current_academic_term()
    regs = []
    for i in range(2):
        r = ActivityRegistration(
            student_name=f"告警測試生{i}",
            class_name="班Alert",
            classroom_id=classroom.id,
            school_year=sy,
            semester=sem,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(r)
        regs.append(r)
    session.commit()
    return student.id, [r.id for r in regs]


class TestDeactivateSyncFailureAlert:
    """部分失敗時主動告警：notify_student_sync_failure 被呼叫且含正確參數。"""

    def test_alert_called_with_failed_reg_id(self, monkeypatch, sqlite_session):
        """單筆軟刪失敗 → notify_student_sync_failure 被呼叫，帶該 reg_id；回傳成功筆數 1。"""
        from services import activity_student_sync as ass
        from services import ops_alert

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_two_regs(session)
        failing_id = reg_ids[0]

        original = ass._soft_delete_single_registration

        def patched(session, reg, **kwargs):
            if reg.id == failing_id:
                raise RuntimeError("人為失敗 for alert test")
            return original(session, reg, **kwargs)

        monkeypatch.setattr(ass, "_soft_delete_single_registration", patched)

        with patch.object(ops_alert, "notify_student_sync_failure") as mock_notify:
            deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        # ① 回傳成功筆數（行為不變）
        assert deleted == 1, f"預期 1 筆成功，實際 {deleted}"

        # ② 告警函式被呼叫
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args

        # 必須帶 student_id
        assert call_kwargs.kwargs.get("student_id") == student_id or (
            len(call_kwargs.args) > 0 and call_kwargs.args[0] == student_id
        ), f"notify_student_sync_failure 未帶正確 student_id；呼叫參數={call_kwargs}"

        # 必須帶 failed_registration_ids 含失敗的 reg_id
        failed_ids = call_kwargs.kwargs.get("failed_registration_ids") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
        )
        assert (
            failed_ids is not None
        ), "notify_student_sync_failure 未傳 failed_registration_ids"
        assert (
            failing_id in failed_ids
        ), f"failed_registration_ids 應含失敗 reg_id={failing_id}；實際={failed_ids}"

    def test_no_alert_when_all_succeed(self, monkeypatch, sqlite_session):
        """全部成功時 notify_student_sync_failure 不應被呼叫。"""
        from services import activity_student_sync as ass
        from services import ops_alert

        _engine, session = sqlite_session
        student_id, _ = _seed_two_regs(session)

        with patch.object(ops_alert, "notify_student_sync_failure") as mock_notify:
            deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        assert deleted == 2
        mock_notify.assert_not_called()

    def test_alert_exception_does_not_propagate(self, monkeypatch, sqlite_session):
        """告警函式自身拋例外時，deactivate 仍正常回傳成功筆數（fail-soft）。"""
        from services import activity_student_sync as ass
        from services import ops_alert

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_two_regs(session)
        failing_id = reg_ids[1]

        original = ass._soft_delete_single_registration

        def patched(session, reg, **kwargs):
            if reg.id == failing_id:
                raise RuntimeError("人為失敗 for fail-soft test")
            return original(session, reg, **kwargs)

        monkeypatch.setattr(ass, "_soft_delete_single_registration", patched)

        # 告警本身拋例外
        with patch.object(
            ops_alert,
            "notify_student_sync_failure",
            side_effect=RuntimeError("LINE crash"),
        ):
            # 不應拋例外，應正常回傳
            deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        assert (
            deleted == 1
        ), f"告警拋例外時 deactivate 仍應回傳成功筆數 1，實際 {deleted}"
