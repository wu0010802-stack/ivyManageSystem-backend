"""家長端事件簽收手寫簽名圖（Phase 2）。"""

import io
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
from api.parent_portal import parent_router
from models.database import (
    Attachment,
    Base,
    Classroom,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    User,
)
from models.portfolio import ATTACHMENT_OWNER_EVENT_ACK
from utils.auth import create_access_token

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def ack_client(tmp_path, monkeypatch):
    db_path = tmp_path / "parent-ack.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "uploads"))
    import utils.portfolio_storage as ps_mod

    if hasattr(ps_mod, "_portfolio_storage"):
        ps_mod._portfolio_storage = None

    app = FastAPI()
    app.include_router(parent_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_event_for_parent(session, *, line_id="UE", title="家長日"):
    user = User(
        username=f"p_{line_id}",
        password_hash="!LINE",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = Classroom(name=f"C-{line_id}", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id=f"ST_{line_id}",
        name=f"Stu_{line_id}",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="家長",
            relation="父親",
            is_primary=True,
        )
    )
    event = SchoolEvent(
        title=title,
        description="測試",
        event_date=date.today(),
        event_type="meeting",
        is_all_day=True,
        is_active=True,
        requires_acknowledgment=True,
    )
    session.add(event)
    session.flush()
    return user, student, event


def _token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


class TestSignatureUpload:
    def test_signature_set_after_ack(self, ack_client):
        client, sf = ack_client
        with sf() as session:
            user, student, event = _seed_event_for_parent(session)
            session.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        # Step 1: ack（取得 ack_id）
        ack_resp = client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid, "signature_name": "王小明"},
            cookies={"access_token": token},
        )
        assert ack_resp.status_code == 200
        ack_id = ack_resp.json()["ack_id"]
        assert ack_id

        # Step 2: 簽名上傳
        sig_resp = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        assert sig_resp.status_code == 201, sig_resp.text
        data = sig_resp.json()
        assert data["ack_id"] == ack_id
        assert data["signature_attachment_id"] is not None
        assert "/api/parent/uploads/portfolio/" in data["url"]

        with sf() as session:
            ack = (
                session.query(EventAcknowledgment)
                .filter(EventAcknowledgment.id == ack_id)
                .first()
            )
            assert ack.signature_attachment_id is not None

    def test_signature_replace_softdeletes_old(self, ack_client):
        client, sf = ack_client
        with sf() as session:
            user, student, event = _seed_event_for_parent(session)
            session.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        r1 = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig1.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        att1 = r1.json()["signature_attachment_id"]
        r2 = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig2.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        att2 = r2.json()["signature_attachment_id"]
        assert att1 != att2

        with sf() as session:
            old = session.query(Attachment).filter(Attachment.id == att1).first()
            new = session.query(Attachment).filter(Attachment.id == att2).first()
            assert old.deleted_at is not None
            assert new.deleted_at is None
            assert new.owner_type == ATTACHMENT_OWNER_EVENT_ACK

    def test_signature_without_ack_returns_404(self, ack_client):
        client, sf = ack_client
        with sf() as session:
            user, student, event = _seed_event_for_parent(session)
            session.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        # 直接傳簽名而沒先 ack
        resp = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        assert resp.status_code == 404

    def test_idor_other_parent_cannot_sign_for_my_child(self, ack_client):
        client, sf = ack_client
        with sf() as session:
            user_a, student_a, event = _seed_event_for_parent(session, line_id="UA")
            user_b, _, _ = _seed_event_for_parent(session, line_id="UB")
            session.commit()
            token_a = _token(user_a)
            token_b = _token(user_b)
            sid_a, eid = student_a.id, event.id

        client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid_a},
            cookies={"access_token": token_a},
        )
        # B 嘗試對 A 的小孩傳簽名 → 403
        resp = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid_a}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 403

    def test_oversize_signature_rejected(self, ack_client):
        client, sf = ack_client
        with sf() as session:
            user, student, event = _seed_event_for_parent(session)
            session.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )

        # > 200KB PNG（用大量 bytes 模擬，magic bytes header 仍是 PNG）
        big = _TINY_PNG[:8] + b"\x00" * (250 * 1024)  # 保留 PNG header
        resp = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("big.png", io.BytesIO(big), "image/png")},
            cookies={"access_token": token},
        )
        assert resp.status_code == 400
