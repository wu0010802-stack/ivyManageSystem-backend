"""驗證 /events/{id}/ack + /ack/signature 對 ack_deadline 的封鎖。

威脅：原 endpoint 完全沒檢查 event.ack_deadline；家長可在截止日後仍簽收 / 上傳簽名，
事後查詢無法分辨「準時簽」vs「逾時補簽」。

修法：
- /ack 與 /ack/signature 兩端點同步加 ack_deadline 過期檢查 → 400
- 簽名上傳時記 signature_uploaded_at；重簽會更新此欄位

Refs: 資安掃描 2026-05-07 P2。
"""

import io
import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from models.database import (
    Base,
    Classroom,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    User,
)
from utils.auth import create_access_token

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def ack_client(tmp_path, monkeypatch):
    db_path = tmp_path / "parent-ack-deadline.sqlite"
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


def _seed(session, *, ack_deadline, line_id="UD"):
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
        title="家長日簽收",
        description="測試",
        event_date=date.today() - timedelta(days=10),
        event_type="meeting",
        is_all_day=True,
        is_active=True,
        requires_acknowledgment=True,
        ack_deadline=ack_deadline,
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


class TestAckDeadlineGuard:
    def test_ack_blocked_after_deadline(self, ack_client):
        """ack_deadline 已過 → /ack 回 400"""
        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(
                s, ack_deadline=date.today() - timedelta(days=1)
            )
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        res = client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        assert res.status_code == 400, res.text
        assert "截止" in res.json()["detail"]

    def test_ack_allowed_on_deadline_day(self, ack_client):
        """deadline = today → 仍可簽（>= 邊界含當日）"""
        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(s, ack_deadline=date.today())
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        res = client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        assert res.status_code == 200, res.text

    def test_ack_allowed_when_no_deadline(self, ack_client):
        """ack_deadline=None → 永遠可簽（既有政策）"""
        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(s, ack_deadline=None)
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        res = client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        assert res.status_code == 200, res.text


class TestSignatureDeadlineGuard:
    def test_signature_upload_blocked_after_deadline(self, ack_client):
        """先建 ack，再讓 deadline 過期，最後上傳簽名 → 400"""
        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(
                s, ack_deadline=date.today() + timedelta(days=1)
            )
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        # 在 deadline 內先 ack
        ack_res = client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        assert ack_res.status_code == 200

        # 模擬 deadline 已過：直接改 DB
        with sf() as s:
            ev = s.query(SchoolEvent).filter(SchoolEvent.id == eid).first()
            ev.ack_deadline = date.today() - timedelta(days=1)
            s.commit()

        # 上傳簽名 → 400
        res = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        assert res.status_code == 400, res.text
        assert "截止" in res.json()["detail"]


class TestSignatureUploadedAtTimestamp:
    def test_signature_upload_records_timestamp(self, ack_client):
        """簽名上傳成功後 signature_uploaded_at 必須非空"""
        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(
                s, ack_deadline=date.today() + timedelta(days=7)
            )
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        res = client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        assert res.status_code == 201, res.text

        with sf() as s:
            ack = s.query(EventAcknowledgment).filter_by(event_id=eid).first()
            assert ack.signature_uploaded_at is not None

    def test_resignature_updates_timestamp(self, ack_client):
        """重簽會更新 signature_uploaded_at（而非保留首次）"""
        import time

        client, sf = ack_client
        with sf() as s:
            user, student, event = _seed(
                s, ack_deadline=date.today() + timedelta(days=7)
            )
            s.commit()
            token = _token(user)
            sid, eid = student.id, event.id

        client.post(
            f"/api/parent/events/{eid}/ack",
            json={"student_id": sid},
            cookies={"access_token": token},
        )
        client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        with sf() as s:
            t1 = (
                s.query(EventAcknowledgment)
                .filter_by(event_id=eid)
                .first()
                .signature_uploaded_at
            )

        time.sleep(0.05)
        client.post(
            f"/api/parent/events/{eid}/ack/signature?student_id={sid}",
            files={"file": ("sig2.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        with sf() as s:
            t2 = (
                s.query(EventAcknowledgment)
                .filter_by(event_id=eid)
                .first()
                .signature_uploaded_at
            )

        assert t2 > t1
