"""Track E — qa-loop round2（2026-06-29）lifecycle / parent 三缺口（1 P2 + 2 P3）。

P2 招生 funnel 復活終態學生（recruitment_funnel._do_activate）：derive_stage 對任何非 active
student 回 'enrolled'（含終態 graduated/transferred），_do_activate 直接 set_lifecycle_status
('active') 繞過 transition() 的 ALLOWED_TRANSITIONS 終態守衛。修法：加 is_transition_allowed 守衛。

P3 終態子女聯絡簿仍可家長簽收/回覆（contact_book._get_entry_for_parent）：reply/ack/delete_reply
共用的 _get_entry_for_parent 未帶 for_write=True → 家長對已離校子女仍能 POST 新回覆/簽收。
修法：寫端點傳 for_write=True，讀路徑保留 False。

P3 PII GC 漏抹孤兒家長 User 的 LINE PII（pii_retention_scheduler）：抹 Guardian 後孤兒 User
仍保留 display_name/line_user_id。修法：抹完 guardian 後抹「已無 guardian 指向」的 User LINE PII。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from models.database import Student, Guardian, User
from models.contact_book import StudentContactBookEntry
from utils.auth import hash_password
import services.recruitment_funnel as funnel_svc
from services.recruitment_funnel import _do_activate, RecruitmentFunnelError
from services.student_lifecycle import is_transition_allowed
import api.parent_portal.contact_book as cb
import services.pii_retention_scheduler as gc_mod


def _mk_student(session, lifecycle, terminal_at=None):
    s = Student(
        student_id=f"ST{lifecycle[:3]}",
        name="測試生",
        lifecycle_status=lifecycle,
        terminal_entered_at=terminal_at,
    )
    session.add(s)
    session.commit()
    return s


# ── P2：funnel 不可復活終態學生 ──────────────────────────────────────────────


def test_do_activate_rejects_graduated(test_db_session):
    s = _mk_student(test_db_session, "graduated")
    with pytest.raises(RecruitmentFunnelError) as exc:
        _do_activate(test_db_session, MagicMock(), s, actor_user_id=1)
    assert exc.value.code == "TERMINAL_STUDENT"
    test_db_session.refresh(s)
    assert s.lifecycle_status == "graduated", "終態學生不應被復活"


def test_do_activate_rejects_transferred(test_db_session):
    s = _mk_student(test_db_session, "transferred")
    with pytest.raises(RecruitmentFunnelError):
        _do_activate(test_db_session, MagicMock(), s, actor_user_id=1)


def test_withdrawn_to_active_still_allowed():
    """復學（withdrawn→active）不被守衛誤殺。"""
    assert is_transition_allowed("withdrawn", "active") is True
    assert is_transition_allowed("graduated", "active") is False
    assert is_transition_allowed("transferred", "active") is False


# ── P3：終態子女聯絡簿寫入守衛 ───────────────────────────────────────────────


def _setup_parent_entry(session, lifecycle):
    parent = User(
        username="parent_x",
        password_hash=hash_password("p"),
        role="parent",
        is_active=True,
        line_user_id="Uparentline001",
        display_name="王小明的家長",
    )
    session.add(parent)
    s = _mk_student(session, lifecycle)
    session.add(Guardian(student_id=s.id, user_id=None, name="家長"))
    session.commit()
    # guardian.user_id 綁到 parent
    g = session.query(Guardian).filter(Guardian.student_id == s.id).first()
    g.user_id = parent.id
    entry = StudentContactBookEntry(
        student_id=s.id,
        classroom_id=1,
        log_date=datetime(2026, 1, 10).date(),
        published_at=datetime(2026, 1, 10),
    )
    session.add(entry)
    session.commit()
    return parent, s, entry


def test_contact_book_write_blocks_terminal_child(test_db_session):
    parent, s, entry = _setup_parent_entry(test_db_session, "graduated")
    # 寫路徑（for_write=True）→ 403；讀路徑（for_write=False）→ 仍可取得
    with pytest.raises(HTTPException) as exc:
        cb._get_entry_for_parent(
            test_db_session, user_id=parent.id, entry_id=entry.id, for_write=True
        )
    assert exc.value.status_code == 403
    got = cb._get_entry_for_parent(
        test_db_session, user_id=parent.id, entry_id=entry.id, for_write=False
    )
    assert got.id == entry.id, "讀歷史聯絡簿仍應允許"


# ── P3：PII GC 抹孤兒家長 User 的 LINE PII ───────────────────────────────────


def test_pii_gc_redacts_orphan_parent_user_line_pii(test_db_session, monkeypatch):
    monkeypatch.setattr(gc_mod, "dry_run_enabled", lambda: False)
    parent = User(
        username="parent_line_Uorphan",
        password_hash=hash_password("p"),
        role="parent",
        is_active=True,
        line_user_id="Uorphan001",
        display_name="李大華（家長真名）",
    )
    test_db_session.add(parent)
    # 終態 >365 天的學生 + 綁此家長的 guardian
    s = _mk_student(test_db_session, "graduated", terminal_at=datetime(2020, 1, 1))
    test_db_session.add(Guardian(student_id=s.id, user_id=None, name="李大華"))
    test_db_session.commit()
    g = test_db_session.query(Guardian).filter(Guardian.student_id == s.id).first()
    g.user_id = parent.id
    test_db_session.commit()

    gc_mod._run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    refreshed = test_db_session.query(User).filter(User.id == parent.id).first()
    assert refreshed.line_user_id is None, "孤兒家長 User 的 line_user_id 應被抹除"
    assert refreshed.display_name is None, "孤兒家長 User 的 display_name 應被抹除"
