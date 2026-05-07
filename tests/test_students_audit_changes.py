"""驗證 PUT /api/students/{id} 與 POST /api/students/bulk-transfer 寫入
request.state.audit_changes，讓 AuditMiddleware 能記下家長/緊急聯絡人/班級
等敏感欄位變更的具體 before→after。

威脅：原本兩條端點完全沒設 audit_changes，AuditMiddleware 只留下「動作=UPDATE」
標籤，無法事後溯回誰把家長電話從 A 改成 B、誰把學生轉到哪個班。

Refs: 邏輯漏洞 audit 2026-05-07 P1。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students_audit.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    # 暴露 audit_changes 供 assertion：在 endpoint 處理完後從 request.state 抓出
    captured = {"audit_changes": None, "audit_entity_id": None}

    @app.middleware("http")
    async def capture_audit(request, call_next):
        response = await call_next(request)
        captured["audit_changes"] = getattr(request.state, "audit_changes", None)
        captured["audit_entity_id"] = getattr(request.state, "audit_entity_id", None)
        return response

    with TestClient(app) as client:
        yield client, session_factory, captured

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session, permissions: int):
    u = User(
        username="admin_audit",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username="admin_audit"):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


def _seed_classroom(session, name="大班A"):
    c = Classroom(name=name, is_active=True)
    session.add(c)
    session.flush()
    return c


def _seed_student(session, name="王小明", classroom_id=None, **kwargs):
    s = Student(
        student_id=kwargs.pop("student_id", "S001"),
        name=name,
        classroom_id=classroom_id,
        is_active=True,
        parent_name=kwargs.pop("parent_name", "王父"),
        parent_phone=kwargs.pop("parent_phone", "0900-111-222"),
        **kwargs,
    )
    session.add(s)
    session.flush()
    return s


WRITE_PERMS = int(Permission.STUDENTS_WRITE) | int(Permission.STUDENTS_READ)


class TestPutStudentAuditChanges:
    def test_update_records_changed_fields_only(self, students_client):
        client, sf, captured = students_client
        with sf() as s:
            _create_admin(s, permissions=WRITE_PERMS)
            cls_a = _seed_classroom(s, "大班A")
            student = _seed_student(s, classroom_id=cls_a.id)
            s.commit()
            sid = student.id

        assert _login(client).status_code == 200
        res = client.put(
            f"/api/students/{sid}",
            json={
                "parent_phone": "0922-333-444",  # 改了
                "parent_name": "王父",  # 沒改（仍同值）
                "address": "新地址 100 號",  # 改了（原本 None）
            },
        )
        assert res.status_code == 200, res.text

        ac = captured["audit_changes"]
        assert ac is not None, "PUT /students/{id} 必須設 request.state.audit_changes"
        assert "parent_phone" in ac
        assert ac["parent_phone"]["before"] == "0900-111-222"
        assert ac["parent_phone"]["after"] == "0922-333-444"
        assert "address" in ac
        assert ac["address"]["before"] is None
        assert ac["address"]["after"] == "新地址 100 號"
        # 沒改的欄位不應出現在 diff
        assert "parent_name" not in ac
        # entity_id 帶到
        assert captured["audit_entity_id"] == sid

    def test_update_no_diff_when_no_change(self, students_client):
        client, sf, captured = students_client
        with sf() as s:
            _create_admin(s, permissions=WRITE_PERMS)
            cls_a = _seed_classroom(s, "大班A")
            student = _seed_student(s, classroom_id=cls_a.id)
            s.commit()
            sid = student.id

        assert _login(client).status_code == 200
        # 送同樣的值
        res = client.put(
            f"/api/students/{sid}",
            json={"parent_name": "王父", "parent_phone": "0900-111-222"},
        )
        assert res.status_code == 200, res.text

        # 沒實質變動 → audit_changes 應為 None
        ac = captured["audit_changes"]
        assert ac is None, f"無實質變動時不應產生 diff，但拿到 {ac}"


class TestBulkTransferAuditChanges:
    def test_bulk_transfer_records_per_student_changes(self, students_client):
        client, sf, captured = students_client
        with sf() as s:
            _create_admin(s, permissions=WRITE_PERMS)
            cls_a = _seed_classroom(s, "大班A")
            cls_b = _seed_classroom(s, "大班B")
            s1 = _seed_student(s, name="生A", student_id="S100", classroom_id=cls_a.id)
            s2 = _seed_student(s, name="生B", student_id="S101", classroom_id=cls_a.id)
            s.commit()
            target_id = cls_b.id
            s_ids = [s1.id, s2.id]

        assert _login(client).status_code == 200
        res = client.post(
            "/api/students/bulk-transfer",
            json={"student_ids": s_ids, "target_classroom_id": target_id},
        )
        assert res.status_code == 200, res.text

        ac = captured["audit_changes"]
        assert ac is not None, "bulk-transfer 必須設 request.state.audit_changes"
        assert ac["action"] == "bulk_transfer"
        assert ac["target_classroom_id"] == target_id
        assert ac["moved_count"] == 2
        transfers = {t["student_id"]: t for t in ac["transfers"]}
        assert set(transfers.keys()) == set(s_ids)

    def test_bulk_transfer_no_audit_when_all_already_in_target(self, students_client):
        """所有學生本來就在目標班 → moved_count=0 → 不設 audit_changes。"""
        client, sf, captured = students_client
        with sf() as s:
            _create_admin(s, permissions=WRITE_PERMS)
            cls_b = _seed_classroom(s, "大班B")
            s1 = _seed_student(s, name="生A", student_id="S200", classroom_id=cls_b.id)
            s.commit()
            target_id = cls_b.id
            s_ids = [s1.id]

        assert _login(client).status_code == 200
        res = client.post(
            "/api/students/bulk-transfer",
            json={"student_ids": s_ids, "target_classroom_id": target_id},
        )
        assert res.status_code == 200, res.text

        # moved_count=0 → 不該設 audit_changes
        ac = captured["audit_changes"]
        assert ac is None
