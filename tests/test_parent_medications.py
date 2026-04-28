"""家長端用藥單（Phase 2）。"""

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
    Guardian,
    Student,
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
    User,
)
from models.portfolio import (
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    MEDICATION_SOURCE_PARENT,
)
from utils.auth import create_access_token

# 1x1 PNG（最小有效 PNG，用於上傳測試）
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def med_client(tmp_path, monkeypatch):
    db_path = tmp_path / "parent-meds.sqlite"
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

    # 把 portfolio_storage 指向臨時目錄，避免污染 production 路徑
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "uploads"))
    # reset 既有的 portfolio storage cache（若已建立）
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


def _seed(session, *, line_id="U1", student_name="S1"):
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
        student_id=f"ST_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="家長",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, student


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


# ════════════════════════════════════════════════════════════════════════
# 建單流程
# ════════════════════════════════════════════════════════════════════════


class TestCreateOrder:
    def test_parent_create_writes_source_parent_and_logs(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = _seed(session)
            session.commit()
            token = _token(user)
            sid = student.id

        body = {
            "student_id": sid,
            "order_date": date.today().isoformat(),
            "medication_name": "退燒藥",
            "dose": "5ml",
            "time_slots": ["08:00", "13:00"],
            "note": "飯後服用",
        }
        resp = client.post(
            "/api/parent/medication-orders",
            json=body,
            cookies={"access_token": token},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["source"] == MEDICATION_SOURCE_PARENT
        assert data["medication_name"] == "退燒藥"
        assert data["created_by"] == user.id
        assert len(data["logs"]) == 2
        assert all(lg["status"] == "pending" for lg in data["logs"])
        assert sorted(lg["scheduled_time"] for lg in data["logs"]) == [
            "08:00",
            "13:00",
        ]

        # DB 也對齊
        with sf() as session:
            o = (
                session.query(StudentMedicationOrder)
                .filter(StudentMedicationOrder.student_id == sid)
                .first()
            )
            assert o is not None
            assert o.source == MEDICATION_SOURCE_PARENT
            assert o.created_by == user.id
            logs = (
                session.query(StudentMedicationLog)
                .filter(StudentMedicationLog.order_id == o.id)
                .all()
            )
            assert len(logs) == 2

    def test_idor_other_parent_cannot_create_for_my_child(self, med_client):
        client, sf = med_client
        with sf() as session:
            user_a, student_a = _seed(session, line_id="UA", student_name="A1")
            user_b, _ = _seed(session, line_id="UB", student_name="B1")
            session.commit()
            sid_a = student_a.id
            token_b = _token(user_b)

        body = {
            "student_id": sid_a,  # B 的 token 但聲稱 A 的小孩
            "order_date": date.today().isoformat(),
            "medication_name": "其他",
            "dose": "1g",
            "time_slots": ["09:00"],
        }
        resp = client.post(
            "/api/parent/medication-orders",
            json=body,
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 403


# ════════════════════════════════════════════════════════════════════════
# 過敏軟警告
# ════════════════════════════════════════════════════════════════════════


class TestAllergySoftWarning:
    def _seed_with_allergy(self, session, allergen: str = "Amoxicillin"):
        user, student = _seed(session)
        session.add(
            StudentAllergy(
                student_id=student.id,
                allergen=allergen,
                severity="moderate",
                reaction_symptom="紅疹",
                active=True,
            )
        )
        session.flush()
        return user, student

    def test_first_submit_with_allergen_returns_409(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = self._seed_with_allergy(session)
            session.commit()
            token = _token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid,
                "order_date": date.today().isoformat(),
                "medication_name": "Amoxicillin 250mg",  # 含 allergen
                "dose": "1顆",
                "time_slots": ["09:00"],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "ALLERGY_WARNING"
        assert any(a["allergen"] == "Amoxicillin" for a in detail["allergens"])

    def test_acknowledge_warning_passes(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = self._seed_with_allergy(session)
            session.commit()
            token = _token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid,
                "order_date": date.today().isoformat(),
                "medication_name": "Amoxicillin 250mg",
                "dose": "1顆",
                "time_slots": ["09:00"],
                "acknowledge_allergy_warning": True,
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201

    def test_unrelated_medication_skips_warning(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = self._seed_with_allergy(session, allergen="海鮮")
            session.commit()
            token = _token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid,
                "order_date": date.today().isoformat(),
                "medication_name": "退燒藥",  # 與「海鮮」不相關
                "dose": "5ml",
                "time_slots": ["09:00"],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201

    def test_inactive_allergy_does_not_block(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = _seed(session)
            session.add(
                StudentAllergy(
                    student_id=student.id,
                    allergen="Amoxicillin",
                    severity="mild",
                    active=False,
                )
            )
            session.commit()
            token = _token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid,
                "order_date": date.today().isoformat(),
                "medication_name": "Amoxicillin 250mg",
                "dose": "1顆",
                "time_slots": ["09:00"],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201


# ════════════════════════════════════════════════════════════════════════
# 列表 / 詳情 / IDOR
# ════════════════════════════════════════════════════════════════════════


class TestListAndDetail:
    def test_list_returns_only_for_specified_student(self, med_client):
        client, sf = med_client
        with sf() as session:
            user, student = _seed(session)
            session.commit()
            token = _token(user)
            sid = student.id

        # 建兩單
        for med in ["藥A", "藥B"]:
            client.post(
                "/api/parent/medication-orders",
                json={
                    "student_id": sid,
                    "order_date": date.today().isoformat(),
                    "medication_name": med,
                    "dose": "1g",
                    "time_slots": ["09:00"],
                },
                cookies={"access_token": token},
            )

        resp = client.get(
            f"/api/parent/medication-orders?student_id={sid}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_detail_idor_other_parent_403(self, med_client):
        client, sf = med_client
        with sf() as session:
            user_a, student_a = _seed(session, line_id="UA", student_name="A1")
            user_b, _ = _seed(session, line_id="UB", student_name="B1")
            session.commit()
            token_a = _token(user_a)
            token_b = _token(user_b)
            sid_a = student_a.id

        # A 建單
        rsp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid_a,
                "order_date": date.today().isoformat(),
                "medication_name": "X",
                "dose": "1",
                "time_slots": ["09:00"],
            },
            cookies={"access_token": token_a},
        )
        order_id = rsp.json()["id"]

        # B 嘗試讀
        resp = client.get(
            f"/api/parent/medication-orders/{order_id}",
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 403


# ════════════════════════════════════════════════════════════════════════
# 附件
# ════════════════════════════════════════════════════════════════════════


class TestPhotoUpload:
    def _create_order(self, client, sf):
        with sf() as session:
            user, student = _seed(session, line_id="UP", student_name="P1")
            session.commit()
            token = _token(user)
            sid = student.id
            uid = user.id

        rsp = client.post(
            "/api/parent/medication-orders",
            json={
                "student_id": sid,
                "order_date": date.today().isoformat(),
                "medication_name": "X",
                "dose": "1",
                "time_slots": ["09:00"],
            },
            cookies={"access_token": token},
        )
        return token, rsp.json()["id"], uid

    def test_upload_photo_creates_attachment(self, med_client):
        client, sf = med_client
        token, order_id, uid = self._create_order(client, sf)

        resp = client.post(
            f"/api/parent/medication-orders/{order_id}/photos",
            files={"file": ("rx.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["mime_type"].startswith("image/")
        assert "/api/parent/uploads/portfolio/" in data["url"]

        with sf() as session:
            atts = (
                session.query(Attachment)
                .filter(
                    Attachment.owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER,
                    Attachment.owner_id == order_id,
                )
                .all()
            )
            assert len(atts) == 1
            assert atts[0].uploaded_by == uid

    def test_other_parent_cannot_upload_to_my_order(self, med_client):
        client, sf = med_client
        token, order_id, _ = self._create_order(client, sf)
        with sf() as session:
            other_user, _ = _seed(session, line_id="UX", student_name="X1")
            session.commit()
            other_token = _token(other_user)

        resp = client.post(
            f"/api/parent/medication-orders/{order_id}/photos",
            files={"file": ("rx.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": other_token},
        )
        assert resp.status_code == 403

    def test_reject_disallowed_extension(self, med_client):
        client, sf = med_client
        token, order_id, _ = self._create_order(client, sf)
        resp = client.post(
            f"/api/parent/medication-orders/{order_id}/photos",
            files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")},
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_delete_photo_soft_deletes(self, med_client):
        client, sf = med_client
        token, order_id, _ = self._create_order(client, sf)
        resp = client.post(
            f"/api/parent/medication-orders/{order_id}/photos",
            files={"file": ("rx.png", io.BytesIO(_TINY_PNG), "image/png")},
            cookies={"access_token": token},
        )
        att_id = resp.json()["id"]

        resp2 = client.delete(
            f"/api/parent/medication-orders/{order_id}/photos/{att_id}",
            cookies={"access_token": token},
        )
        assert resp2.status_code == 200

        with sf() as session:
            att = session.query(Attachment).filter(Attachment.id == att_id).first()
            assert att.deleted_at is not None
