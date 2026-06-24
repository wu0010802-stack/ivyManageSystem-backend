"""才藝後台報名：拒絕守衛、rematch 失敗清綁定、班級來源單一化、編輯去重補鎖。

2026-06-24 code review 四項 finding 的回歸測試：
- #1 (High)  reject 只允許 pending；非 pending / 已付款回 409（不繞 delete 的退費簽核）
- #2 (Med)   rematch 比對失敗時清掉舊 student_id/classroom_id，回到待審核
- #3 (Med)   後台 create/update 比對到在校生時，班級以 Student.classroom_id 為準（同源）
- #4 (Low/M) update 改身分時補 acquire_activity_registration_lock（與 rematch 對齊）
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Student,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "reg_guards.sqlite"
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


def _login(client, username="admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return r


def _seed(session, *, with_student=True):
    """admin + 大象班 + 圍棋課（+ 王小明在大象班）。回傳大象班 classroom_id。"""
    from models.database import User

    sy, sem = resolve_current_academic_term()
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
            is_active=True,
        )
    )
    elephant = Classroom(name="大象班", is_active=True, school_year=sy, semester=sem)
    session.add(elephant)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋", price=1200, school_year=sy, semester=sem, is_active=True
        )
    )
    if with_student:
        session.add(
            Student(
                student_id="S001",
                name="王小明",
                birthday=date(2020, 5, 10),
                classroom_id=elephant.id,
                parent_phone="0912345678",
                is_active=True,
            )
        )
    session.commit()
    return elephant.id


def _add_classroom(session, name):
    sy, sem = resolve_current_academic_term()
    c = Classroom(name=name, is_active=True, school_year=sy, semester=sem)
    session.add(c)
    session.commit()
    return c.id


# ── #1 reject 守衛 ─────────────────────────────────────────────────────────
class TestRejectGate:
    def test_reject_blocks_confirmed_matched_registration(self, client_sf):
        """已確認（matched / 非 pending）的報名不可由 reject 軟刪。"""
        client, sf = client_sf
        with sf() as s:
            cid = _seed(s)
            sy, sem = resolve_current_academic_term()
            student = s.query(Student).filter_by(name="王小明").one()
            reg = ActivityRegistration(
                student_name="王小明",
                birthday="2020-05-10",
                parent_phone="0912345678",
                school_year=sy,
                semester=sem,
                student_id=student.id,
                classroom_id=cid,
                class_name="大象班",
                is_active=True,
                pending_review=False,
                match_status="matched",
                paid_amount=0,
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "誤拒測試"},
        )
        assert res.status_code == 409, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is True
            assert reg.match_status == "matched"

    def test_reject_blocks_paid_pending_registration(self, client_sf):
        """pending 但已有繳費 → 必須走刪除/退費流程，reject 回 409。"""
        client, sf = client_sf
        with sf() as s:
            _seed(s, with_student=False)
            sy, sem = resolve_current_academic_term()
            reg = ActivityRegistration(
                student_name="待審小孩",
                birthday="2020-05-10",
                parent_phone="0912345678",
                school_year=sy,
                semester=sem,
                is_active=True,
                pending_review=True,
                match_status="pending",
                paid_amount=1200,
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "已付款測試"},
        )
        assert res.status_code == 409, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is True
            assert reg.match_status == "pending"

    def test_reject_still_allows_plain_pending(self, client_sf):
        """正常 pending（未付款）報名仍可被拒絕（不破壞既有審核流程）。"""
        client, sf = client_sf
        with sf() as s:
            _seed(s, with_student=False)
            sy, sem = resolve_current_academic_term()
            reg = ActivityRegistration(
                student_name="待審小孩",
                birthday="2020-05-10",
                parent_phone="0912345678",
                school_year=sy,
                semester=sem,
                is_active=True,
                pending_review=True,
                match_status="pending",
                paid_amount=0,
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "校外生"},
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is False
            assert reg.match_status == "rejected"


# ── #2 rematch 失敗清綁定 ───────────────────────────────────────────────────
class TestRematchClearsStaleBinding:
    def test_rematch_clears_binding_when_match_fails(self, client_sf):
        """原本 matched 的 reg，改成查無此生 → 清掉舊 student_id/classroom_id 回 pending。"""
        client, sf = client_sf
        with sf() as s:
            cid = _seed(s)
            sy, sem = resolve_current_academic_term()
            student = s.query(Student).filter_by(name="王小明").one()
            reg = ActivityRegistration(
                student_name="王小明",
                birthday="2020-05-10",
                parent_phone="0912345678",
                school_year=sy,
                semester=sem,
                student_id=student.id,
                classroom_id=cid,
                class_name="大象班",
                is_active=True,
                pending_review=False,
                match_status="matched",
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/rematch",
            json={"name": "查無此生", "birthday": "2015-01-01"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["matched"] is False

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.student_id is None, "比對失敗應清掉舊 student_id"
            assert reg.classroom_id is None, "比對失敗應清掉舊 classroom_id"
            assert reg.pending_review is True
            assert reg.match_status == "pending"


# ── #3 班級來源單一化 ───────────────────────────────────────────────────────
class TestClassroomSameSource:
    def test_admin_create_uses_matched_student_classroom(self, client_sf):
        """表單誤選長頸鹿班，但比對到的王小明在大象班 → classroom 以學生班級為準。"""
        client, sf = client_sf
        with sf() as s:
            elephant_id = _seed(s)
            _add_classroom(s, "長頸鹿班")

        _login(client)
        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "class": "長頸鹿班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_name="王小明").one()
            assert reg.student_id is not None
            assert (
                reg.classroom_id == elephant_id
            ), "應對齊學生的大象班，而非表單長頸鹿班"
            assert reg.class_name == "大象班"

    def test_admin_create_uses_form_class_for_external_student(self, client_sf):
        """校外生（比對不到）→ 沿用表單班級。"""
        client, sf = client_sf
        with sf() as s:
            _seed(s)  # 王小明在大象班
            giraffe_id = _add_classroom(s, "長頸鹿班")

        _login(client)
        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "校外小孩",
                "birthday": "2019-09-09",
                "class": "長頸鹿班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_name="校外小孩").one()
            assert reg.student_id is None
            assert reg.classroom_id == giraffe_id
            assert reg.class_name == "長頸鹿班"

    def test_admin_update_realigns_classroom_to_matched_student(self, client_sf):
        """編輯把校外生改成在校生王小明 → classroom 對齊大象班（不留表單長頸鹿班）。"""
        client, sf = client_sf
        with sf() as s:
            elephant_id = _seed(s)
            giraffe_id = _add_classroom(s, "長頸鹿班")
            sy, sem = resolve_current_academic_term()
            reg = ActivityRegistration(
                student_name="校外小孩",
                birthday="2019-09-09",
                school_year=sy,
                semester=sem,
                student_id=None,
                classroom_id=giraffe_id,
                class_name="長頸鹿班",
                is_active=True,
                pending_review=False,
                match_status="forced",
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)
        res = client.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "class": "長頸鹿班",
                "email": "",
            },
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.student_id is not None
            assert reg.classroom_id == elephant_id, "應對齊大象班"
            assert reg.class_name == "大象班"

    def test_admin_update_preserves_manual_binding_on_non_identity_edit(
        self, client_sf
    ):
        """只改 email（姓名/生日不變）不可清掉既有 manual 綁定，班級仍跟隨該生。"""
        client, sf = client_sf
        with sf() as s:
            elephant_id = _seed(s)
            sy, sem = resolve_current_academic_term()
            student = s.query(Student).filter_by(name="王小明").one()
            # 家長打錯名字、由校方人工綁定到王小明（match_status=manual）
            reg = ActivityRegistration(
                student_name="小名",
                birthday="2020-05-10",
                school_year=sy,
                semester=sem,
                student_id=student.id,
                classroom_id=elephant_id,
                class_name="大象班",
                is_active=True,
                pending_review=False,
                match_status="manual",
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id
            student_id = student.id

        _login(client)
        res = client.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "小名",
                "birthday": "2020-05-10",
                "class": "大象班",
                "email": "new@example.com",
            },
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.student_id == student_id, "未改姓名/生日不可清掉 manual 綁定"
            assert reg.classroom_id == elephant_id


# ── #4 update 改身分補 advisory lock ────────────────────────────────────────
class TestUpdateAcquiresLock:
    def test_update_acquires_lock_on_identity_change(self, client_sf, monkeypatch):
        """改 name/birthday 時須對「修改後身分」取 advisory lock（與 rematch C6 對齊）。

        SQLite lock 為 no-op，本測試聚焦 wiring（確實呼叫且帶修改後身分）。
        """
        client, sf = client_sf
        with sf() as s:
            elephant_id = _seed(s, with_student=False)
            sy, sem = resolve_current_academic_term()
            reg = ActivityRegistration(
                student_name="原名",
                birthday="2020-01-01",
                school_year=sy,
                semester=sem,
                classroom_id=elephant_id,
                class_name="大象班",
                is_active=True,
                pending_review=False,
                match_status="forced",
            )
            s.add(reg)
            s.commit()
            reg_id = reg.id

        _login(client)

        calls = []
        from utils import advisory_lock as advisory_lock_mod

        real_fn = advisory_lock_mod.acquire_activity_registration_lock

        def spy(session, **kw):
            calls.append(kw)
            return real_fn(session, **kw)

        monkeypatch.setattr(
            advisory_lock_mod, "acquire_activity_registration_lock", spy
        )
        from api.activity import registrations as reg_mod

        monkeypatch.setattr(reg_mod, "acquire_activity_registration_lock", spy)

        res = client.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "新名",
                "birthday": "2019-02-02",
                "class": "大象班",
                "email": "",
            },
        )
        assert res.status_code == 200, res.text
        assert calls, "改身分時應取 advisory lock"
        assert calls[0]["student_name"] == "新名"
        assert calls[0]["birthday"] == "2019-02-02"
