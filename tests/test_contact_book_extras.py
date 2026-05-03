"""聯絡簿便利端點測試：unpublished / copy-from-yesterday / apply-template / batch-publish。"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from api.portal.contact_book import init_contact_book_line_service
from models.contact_book import ContactBookTemplate
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentContactBookEntry,
    User,
)
from services.contact_book_service import (
    apply_template_fields,
    copy_yesterday_to_today,
)
from utils.auth import create_access_token
from utils.permissions import Permission

# ════════════════════════════════════════════════════════════════════════
# 純函式單元測試（apply_template_fields）
# ════════════════════════════════════════════════════════════════════════


class TestApplyTemplateFields:
    def test_only_fill_blank_skips_filled(self):
        entry = StudentContactBookEntry(
            student_id=1,
            classroom_id=1,
            log_date=date(2026, 5, 2),
            mood="happy",  # 已填，不該蓋
            teacher_note=None,  # 空，可填
        )
        changed = apply_template_fields(
            entry,
            {"mood": "tired", "teacher_note": "今日表現好"},
            only_fill_blank=True,
        )
        assert "teacher_note" in changed
        assert "mood" not in changed
        assert entry.mood == "happy"
        assert entry.teacher_note == "今日表現好"

    def test_force_overwrite(self):
        entry = StudentContactBookEntry(
            student_id=1,
            classroom_id=1,
            log_date=date(2026, 5, 2),
            mood="happy",
        )
        changed = apply_template_fields(
            entry,
            {"mood": "tired"},
            only_fill_blank=False,
        )
        assert "mood" in changed
        assert entry.mood == "tired"

    def test_skip_template_field_with_none_value_when_fill_blank(self):
        """only_fill_blank=True 時，範本欄位 None 值不應動 entry。"""
        entry = StudentContactBookEntry(
            student_id=1, classroom_id=1, log_date=date(2026, 5, 2), mood=None
        )
        changed = apply_template_fields(
            entry, {"mood": None, "teacher_note": "x"}, only_fill_blank=True
        )
        assert "mood" not in changed
        assert "teacher_note" in changed
        assert entry.mood is None
        assert entry.teacher_note == "x"


# ════════════════════════════════════════════════════════════════════════
# Integration tests
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def cbx_client(tmp_path):
    db_path = tmp_path / "cb-extras.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    line_service = MagicMock()
    line_service.should_push_to_parent.return_value = None  # 不 push
    init_contact_book_line_service(line_service)

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as c:
        yield c, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()
    init_contact_book_line_service(None)


def _seed(session) -> tuple[User, Employee, Classroom, list[Student]]:
    """建立教師（身兼 head_teacher） + 一個班 + 兩位學生。"""
    emp = Employee(employee_id="T01", name="王老師", is_active=True, base_salary=30000)
    session.add(emp)
    session.flush()
    classroom = Classroom(name="A班", is_active=True, head_teacher_id=emp.id)
    session.add(classroom)
    session.flush()
    write_perm = int(Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value)
    user = User(
        username="t1",
        password_hash="!",
        role="teacher",
        employee_id=emp.id,
        permissions=write_perm,
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()
    s1 = Student(
        student_id="S1", name="小明", classroom_id=classroom.id, is_active=True
    )
    s2 = Student(
        student_id="S2", name="小美", classroom_id=classroom.id, is_active=True
    )
    session.add_all([s1, s2])
    session.flush()
    return user, emp, classroom, [s1, s2]


def _token(user: User, emp_id: int) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": emp_id,
            "role": "teacher",
            "name": user.username,
            "permissions": int(
                Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
            ),
            "token_version": user.token_version or 0,
        }
    )


# ── copy-from-yesterday ───────────────────────────────────────────────────


class TestCopyFromYesterday:
    def test_copy_yesterday_creates_drafts(self, cbx_client):
        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, s2] = _seed(session)
            yest = date(2026, 5, 1)
            today = date(2026, 5, 2)
            session.add_all(
                [
                    StudentContactBookEntry(
                        student_id=s1.id,
                        classroom_id=c.id,
                        log_date=yest,
                        mood="happy",
                        teacher_note="昨天小明",
                    ),
                    StudentContactBookEntry(
                        student_id=s2.id,
                        classroom_id=c.id,
                        log_date=yest,
                        mood="tired",
                        teacher_note="昨天小美",
                    ),
                ]
            )
            session.commit()
            tk = _token(user, emp.id)
            cid = c.id

        rsp = client.post(
            "/api/portal/contact-book/copy-from-yesterday",
            json={"classroom_id": cid, "target_date": today.isoformat()},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        assert rsp.json()["created"] == 2

        with sf() as session:
            today_entries = (
                session.query(StudentContactBookEntry)
                .filter(StudentContactBookEntry.log_date == today)
                .all()
            )
            assert len(today_entries) == 2
            notes = sorted(e.teacher_note for e in today_entries)
            assert notes == ["昨天小明", "昨天小美"]
            # 全部都是草稿
            assert all(e.published_at is None for e in today_entries)

    def test_copy_skips_existing(self, cbx_client):
        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, s2] = _seed(session)
            yest = date(2026, 5, 1)
            today = date(2026, 5, 2)
            session.add_all(
                [
                    StudentContactBookEntry(
                        student_id=s1.id, classroom_id=c.id, log_date=yest, mood="happy"
                    ),
                    StudentContactBookEntry(
                        student_id=s2.id, classroom_id=c.id, log_date=yest, mood="tired"
                    ),
                    # 小明今日已有 entry
                    StudentContactBookEntry(
                        student_id=s1.id,
                        classroom_id=c.id,
                        log_date=today,
                        teacher_note="已存在",
                    ),
                ]
            )
            session.commit()
            tk = _token(user, emp.id)
            cid = c.id

        rsp = client.post(
            "/api/portal/contact-book/copy-from-yesterday",
            json={"classroom_id": cid, "target_date": today.isoformat()},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200
        # 只新增小美一筆
        assert rsp.json()["created"] == 1


# ── unpublished list ──────────────────────────────────────────────────────


class TestUnpublishedList:
    def test_list_only_unpublished(self, cbx_client):
        from datetime import datetime as _dt

        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, s2] = _seed(session)
            today = date(2026, 5, 2)
            e1 = StudentContactBookEntry(
                student_id=s1.id, classroom_id=c.id, log_date=today, mood="happy"
            )
            e2 = StudentContactBookEntry(
                student_id=s2.id,
                classroom_id=c.id,
                log_date=today,
                mood="tired",
                published_at=_dt.now(),
            )
            session.add_all([e1, e2])
            session.commit()
            tk = _token(user, emp.id)
            cid = c.id

        rsp = client.get(
            f"/api/portal/contact-book/unpublished?classroom_id={cid}&log_date={today.isoformat()}",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        items = rsp.json()["items"]
        assert len(items) == 1
        assert items[0]["student_name"] == "小明"


# ── apply-template ─────────────────────────────────────────────────────────


class TestApplyTemplateEndpoint:
    def test_apply_template_only_fills_blank(self, cbx_client):
        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, s2] = _seed(session)
            today = date(2026, 5, 2)
            e1 = StudentContactBookEntry(
                student_id=s1.id,
                classroom_id=c.id,
                log_date=today,
                mood="happy",  # 已填
            )
            e2 = StudentContactBookEntry(
                student_id=s2.id, classroom_id=c.id, log_date=today
            )  # 全空
            session.add_all([e1, e2])
            session.flush()
            tpl = ContactBookTemplate(
                name="基礎範本",
                scope="personal",
                owner_user_id=user.id,
                fields={"mood": "tired", "teacher_note": "今日表現良好"},
                is_archived=False,
            )
            session.add(tpl)
            session.commit()
            tk = _token(user, emp.id)
            entry_ids = [e1.id, e2.id]
            tpl_id = tpl.id

        rsp = client.post(
            "/api/portal/contact-book/apply-template",
            json={
                "template_id": tpl_id,
                "entry_ids": entry_ids,
                "only_fill_blank": True,
            },
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text

        with sf() as session:
            ents = (
                session.query(StudentContactBookEntry)
                .filter(StudentContactBookEntry.id.in_(entry_ids))
                .all()
            )
            by_id = {e.id: e for e in ents}
            # e1 mood 應保留 happy
            assert by_id[entry_ids[0]].mood == "happy"
            assert by_id[entry_ids[0]].teacher_note == "今日表現良好"
            # e2 兩個欄位都被填
            assert by_id[entry_ids[1]].mood == "tired"
            assert by_id[entry_ids[1]].teacher_note == "今日表現良好"

    def test_apply_template_rejects_published(self, cbx_client):
        from datetime import datetime as _dt

        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, _] = _seed(session)
            today = date(2026, 5, 2)
            e1 = StudentContactBookEntry(
                student_id=s1.id,
                classroom_id=c.id,
                log_date=today,
                published_at=_dt.now(),
            )
            session.add(e1)
            session.flush()
            tpl = ContactBookTemplate(
                name="x",
                scope="personal",
                owner_user_id=user.id,
                fields={"teacher_note": "x"},
                is_archived=False,
            )
            session.add(tpl)
            session.commit()
            tk = _token(user, emp.id)
            ids = [e1.id]
            tpl_id = tpl.id

        rsp = client.post(
            "/api/portal/contact-book/apply-template",
            json={"template_id": tpl_id, "entry_ids": ids},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 400


# ── batch-publish ──────────────────────────────────────────────────────────


class TestBatchPublish:
    def test_batch_publish_all_drafts(self, cbx_client):
        client, sf = cbx_client
        with sf() as session:
            user, emp, c, [s1, s2] = _seed(session)
            today = date(2026, 5, 2)
            e1 = StudentContactBookEntry(
                student_id=s1.id,
                classroom_id=c.id,
                log_date=today,
                teacher_note="今日 1",
            )
            e2 = StudentContactBookEntry(
                student_id=s2.id,
                classroom_id=c.id,
                log_date=today,
                teacher_note="今日 2",
            )
            session.add_all([e1, e2])
            session.commit()
            tk = _token(user, emp.id)
            ids = [e1.id, e2.id]

        rsp = client.post(
            "/api/portal/contact-book/batch-publish",
            json={"entry_ids": ids},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()
        assert body["success_count"] == 2
        assert all(r["status"] == "ok" for r in body["results"])

        with sf() as session:
            ents = (
                session.query(StudentContactBookEntry)
                .filter(StudentContactBookEntry.id.in_(ids))
                .all()
            )
            assert all(e.published_at is not None for e in ents)
