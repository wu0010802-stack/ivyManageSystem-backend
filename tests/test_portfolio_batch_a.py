"""Portfolio Batch A 整合測試。

涵蓋：
- Attachments upload / thumbnail 生成 / magic bytes / 權限 / 軟刪除
- Observations CRUD / class scope / domain / rating
- Allergies CRUD / 權限分級（teacher 不可 write）
- Medication orders/logs / 自動預建 pending logs / administer / skip / immutability / correct
- Today-medication endpoint / class scope
"""

from __future__ import annotations

import io
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attachments import download_router as attachments_download_router
from api.attachments import router as attachments_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portfolio.observations import router as observations_router
from api.student_health import router as student_health_router
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentMedicationLog,
    User,
)
from models.classroom import LIFECYCLE_ACTIVE
from utils.auth import hash_password
from utils.permissions import Permission
from utils.portfolio_storage import (
    LocalStorage,
    reset_portfolio_storage,
    set_portfolio_storage,
)

# ── Test fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def storage_root(tmp_path):
    """Portfolio storage 注入 local backend 指向 tmp_path。"""
    root = tmp_path / "portfolio_uploads"
    root.mkdir()
    storage = LocalStorage(root=root)
    set_portfolio_storage(storage)
    yield root
    reset_portfolio_storage()


@pytest.fixture
def app_with_db(tmp_path, storage_root):
    """FastAPI app + SQLite in-memory，已掛全 portfolio routers。"""
    db_path = tmp_path / "portfolio.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    # SQLite FK 必須手動開啟，才會觸發 ondelete 等行為
    @event.listens_for(engine, "connect")
    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    # 手動安裝 immutability trigger（migration 會建立，但 create_all 不會）
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TRIGGER IF NOT EXISTS trg_medication_log_immutable
                BEFORE UPDATE ON student_medication_logs
                FOR EACH ROW
                WHEN OLD.administered_at IS NOT NULL OR OLD.skipped = 1
                BEGIN
                    SELECT RAISE(ABORT, '已執行 / 已跳過的餵藥紀錄不可修改');
                END;
                """))

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attachments_router)
    app.include_router(attachments_download_router)
    app.include_router(observations_router)
    app.include_router(student_health_router)

    with TestClient(app) as client:
        yield client, session_factory, engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_jpeg(width: int = 2048, height: int = 1536, color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _make_png(width: int = 100, height: int = 100) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="blue").save(buf, format="PNG")
    return buf.getvalue()


def _add_user(
    session,
    username: str,
    password: str,
    *,
    role: str = "supervisor",
    perms: int = -1,  # sentinel for 全權限（Permission.ALL 在 SQLite 上 overflow）
    employee_id: int | None = None,
) -> User:
    u = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=perms,
        is_active=True,
        employee_id=employee_id,
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _login(client: TestClient, username: str, password: str) -> None:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text


def _seed_classrooms_and_students(session) -> dict:
    """建立 2 班 + 各 1 學生；教師 A 帶 A 班、教師 B 帶 B 班。"""
    emp_a = Employee(name="教師 A", employee_id="EA", hire_date=date(2024, 1, 1))
    emp_b = Employee(name="教師 B", employee_id="EB", hire_date=date(2024, 1, 1))
    session.add_all([emp_a, emp_b])
    session.flush()

    cls_a = Classroom(name="A 班", head_teacher_id=emp_a.id, is_active=True)
    cls_b = Classroom(name="B 班", head_teacher_id=emp_b.id, is_active=True)
    session.add_all([cls_a, cls_b])
    session.flush()

    st_a = Student(
        student_id="SA01",
        name="A 班學生",
        classroom_id=cls_a.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    st_b = Student(
        student_id="SB01",
        name="B 班學生",
        classroom_id=cls_b.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add_all([st_a, st_b])
    session.commit()
    for obj in (emp_a, emp_b, cls_a, cls_b, st_a, st_b):
        session.refresh(obj)

    return {
        "emp_a": emp_a,
        "emp_b": emp_b,
        "cls_a": cls_a,
        "cls_b": cls_b,
        "st_a": st_a,
        "st_b": st_b,
    }


# ══════════════════════════════════════════════════════════════════════════
# Attachments
# ══════════════════════════════════════════════════════════════════════════


class TestAttachments:
    def test_upload_jpeg_generates_thumb_and_display(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            # 先建一個 observation 當 owner
            from models.database import StudentObservation

            obs = StudentObservation(
                student_id=seed["st_a"].id,
                observation_date=date.today(),
                narrative="觀察",
            )
            s.add(obs)
            s.commit()
            obs_id = obs.id
        _login(client, "admin", "Pass1234")

        jpg = _make_jpeg()
        r = client.post(
            "/api/attachments",
            files={"file": ("photo.jpg", jpg, "image/jpeg")},
            data={"owner_type": "observation", "owner_id": str(obs_id)},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["mime_type"] == "image/jpeg"
        assert data["url"].startswith("/api/uploads/portfolio/")
        assert data["display_url"] is not None
        assert data["thumb_url"] is not None

        # 確認三個實體檔案都存在
        assert (
            storage_root / data["url"].replace("/api/uploads/portfolio/", "")
        ).exists()
        assert (
            storage_root / data["display_url"].replace("/api/uploads/portfolio/", "")
        ).exists()
        assert (
            storage_root / data["thumb_url"].replace("/api/uploads/portfolio/", "")
        ).exists()

        # display 縮到 ≤1024, thumb 縮到 ≤256
        display_path = storage_root / data["display_url"].replace(
            "/api/uploads/portfolio/", ""
        )
        with Image.open(display_path) as img:
            assert max(img.size) == 1024
        thumb_path = storage_root / data["thumb_url"].replace(
            "/api/uploads/portfolio/", ""
        )
        with Image.open(thumb_path) as img:
            assert max(img.size) == 256

    def test_magic_bytes_rejects_fake_jpg(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            from models.database import StudentObservation

            obs = StudentObservation(
                student_id=seed["st_a"].id,
                observation_date=date.today(),
                narrative="觀察",
            )
            s.add(obs)
            s.commit()
            obs_id = obs.id
        _login(client, "admin", "Pass1234")

        fake_jpg = b"NOT_A_REAL_JPEG" * 100
        r = client.post(
            "/api/attachments",
            files={"file": ("fake.jpg", fake_jpg, "image/jpeg")},
            data={"owner_type": "observation", "owner_id": str(obs_id)},
        )
        assert r.status_code == 400
        assert "不符" in r.json()["detail"]

    def test_upload_rejects_over_size_limit(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            from models.database import StudentObservation

            obs = StudentObservation(
                student_id=seed["st_a"].id,
                observation_date=date.today(),
                narrative="觀察",
            )
            s.add(obs)
            s.commit()
            obs_id = obs.id
        _login(client, "admin", "Pass1234")

        huge = b"\xff\xd8\xff" + b"\x00" * (11 * 1024 * 1024)  # JPEG magic + 11MB
        r = client.post(
            "/api/attachments",
            files={"file": ("big.jpg", huge, "image/jpeg")},
            data={"owner_type": "observation", "owner_id": str(obs_id)},
        )
        assert r.status_code == 400
        assert "10MB" in r.json()["detail"]

    def test_soft_delete_and_download(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            from models.database import StudentObservation

            obs = StudentObservation(
                student_id=seed["st_a"].id,
                observation_date=date.today(),
                narrative="觀察",
            )
            s.add(obs)
            s.commit()
            obs_id = obs.id
        _login(client, "admin", "Pass1234")

        r = client.post(
            "/api/attachments",
            files={"file": ("p.jpg", _make_jpeg(width=512, height=384), "image/jpeg")},
            data={"owner_type": "observation", "owner_id": str(obs_id)},
        )
        att = r.json()
        # 下載（帶 auth cookie）
        r2 = client.get(att["url"])
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("image/jpeg")

        # 軟刪除
        r3 = client.delete(f"/api/attachments/{att['id']}")
        assert r3.status_code == 200

        # 下載應該 410
        r4 = client.get(att["url"])
        assert r4.status_code == 410

    def test_reject_unsupported_ext(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            from models.database import StudentObservation

            obs = StudentObservation(
                student_id=seed["st_a"].id,
                observation_date=date.today(),
                narrative="觀察",
            )
            s.add(obs)
            s.commit()
            obs_id = obs.id
        _login(client, "admin", "Pass1234")

        r = client.post(
            "/api/attachments",
            files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
            data={"owner_type": "observation", "owner_id": str(obs_id)},
        )
        assert r.status_code == 400
        assert "不支援" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════
# Observations
# ══════════════════════════════════════════════════════════════════════════


class TestObservations:
    def test_crud_flow(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            st_id = seed["st_a"].id
        _login(client, "admin", "Pass1234")

        # Create
        r = client.post(
            f"/api/students/{st_id}/observations",
            json={
                "observation_date": "2026-04-24",
                "narrative": "今日學會綁鞋帶",
                "domain": "身體動作與健康",
                "rating": 5,
                "is_highlight": True,
            },
        )
        assert r.status_code == 201, r.text
        obs = r.json()
        assert obs["is_highlight"] is True
        assert obs["rating"] == 5

        # List
        r2 = client.get(f"/api/students/{st_id}/observations")
        assert r2.status_code == 200
        payload = r2.json()
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == obs["id"]

        # Patch
        r3 = client.patch(
            f"/api/students/{st_id}/observations/{obs['id']}",
            json={"narrative": "今日學會綁鞋帶（修訂）", "rating": 4},
        )
        assert r3.status_code == 200
        assert r3.json()["rating"] == 4

        # Soft delete
        r4 = client.delete(f"/api/students/{st_id}/observations/{obs['id']}")
        assert r4.status_code == 200

        # List 看不到
        r5 = client.get(f"/api/students/{st_id}/observations")
        assert r5.json()["total"] == 0

    def test_domain_validation(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            st_id = seed["st_a"].id
        _login(client, "admin", "Pass1234")

        r = client.post(
            f"/api/students/{st_id}/observations",
            json={
                "observation_date": "2026-04-24",
                "narrative": "x",
                "domain": "不存在的領域",
            },
        )
        assert r.status_code == 422  # pydantic 422

    def test_rating_range(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "admin", "Pass1234")
            st_id = seed["st_a"].id
        _login(client, "admin", "Pass1234")

        r = client.post(
            f"/api/students/{st_id}/observations",
            json={
                "observation_date": "2026-04-24",
                "narrative": "x",
                "rating": 10,  # 超出範圍
            },
        )
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════
# Class scope filter（advisor flag #4）
# ══════════════════════════════════════════════════════════════════════════


class TestClassScope:
    def test_teacher_cannot_see_other_class_observation(
        self, app_with_db, storage_root
    ):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            # 教師 A：只帶 A 班
            _add_user(
                s,
                "teacher_a",
                "Pass1234",
                role="teacher",
                perms=(
                    Permission.PORTFOLIO_READ
                    | Permission.PORTFOLIO_WRITE
                    | Permission.STUDENTS_HEALTH_READ
                    | Permission.STUDENTS_MEDICATION_ADMINISTER
                ),
                employee_id=seed["emp_a"].id,
            )
            st_b_id = seed["st_b"].id
        _login(client, "teacher_a", "Pass1234")

        # 教師 A 想讀 B 班學生的觀察 → 403
        r = client.get(f"/api/students/{st_b_id}/observations")
        assert r.status_code == 403

        # 教師 A 想寫 B 班學生的觀察 → 403
        r2 = client.post(
            f"/api/students/{st_b_id}/observations",
            json={"observation_date": "2026-04-24", "narrative": "x"},
        )
        assert r2.status_code == 403

    def test_supervisor_can_see_all_classes(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(
                s,
                "super",
                "Pass1234",
                role="supervisor",
                perms=-1,
            )
            st_b_id = seed["st_b"].id
        _login(client, "super", "Pass1234")

        r = client.get(f"/api/students/{st_b_id}/observations")
        assert r.status_code == 200

    def test_today_medication_scoped_to_class(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        from models.database import StudentMedicationOrder

        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(
                s,
                "teacher_a",
                "Pass1234",
                role="teacher",
                perms=(
                    Permission.STUDENTS_HEALTH_READ
                    | Permission.STUDENTS_MEDICATION_ADMINISTER
                ),
                employee_id=seed["emp_a"].id,
            )
            # 兩班各一張今日 order
            for st in (seed["st_a"], seed["st_b"]):
                o = StudentMedicationOrder(
                    student_id=st.id,
                    order_date=date.today(),
                    medication_name="藥",
                    dose="1 顆",
                    time_slots=["08:30"],
                )
                s.add(o)
                s.flush()
                s.add(StudentMedicationLog(order_id=o.id, scheduled_time="08:30"))
            s.commit()
        _login(client, "teacher_a", "Pass1234")

        r = client.get("/api/portfolio/today-medication")
        assert r.status_code == 200
        payload = r.json()
        # 教師 A 只能看到 A 班
        assert payload["pending"] == 1
        assert len(payload["orders"]) == 1
        assert payload["orders"][0]["student_name"] == "A 班學生"


# ══════════════════════════════════════════════════════════════════════════
# Health / Medication
# ══════════════════════════════════════════════════════════════════════════


class TestHealthAndMedication:
    def test_allergy_crud(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "super", "Pass1234", role="supervisor", perms=-1)
            st_id = seed["st_a"].id
        _login(client, "super", "Pass1234")

        r = client.post(
            f"/api/students/{st_id}/allergies",
            json={"allergen": "花生", "severity": "severe"},
        )
        assert r.status_code == 201, r.text
        a = r.json()
        assert a["active"] is True

        r2 = client.get(f"/api/students/{st_id}/allergies")
        assert r2.json()["total"] == 1

        r3 = client.patch(
            f"/api/students/{st_id}/allergies/{a['id']}",
            json={"active": False},
        )
        assert r3.status_code == 200
        assert r3.json()["active"] is False

        # include_inactive=false 過濾掉
        r4 = client.get(f"/api/students/{st_id}/allergies")
        assert r4.json()["total"] == 0
        r5 = client.get(f"/api/students/{st_id}/allergies?include_inactive=true")
        assert r5.json()["total"] == 1

    def test_teacher_cannot_write_allergy(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(
                s,
                "teacher_a",
                "Pass1234",
                role="teacher",
                perms=(
                    Permission.STUDENTS_HEALTH_READ
                    | Permission.STUDENTS_MEDICATION_ADMINISTER
                ),
                employee_id=seed["emp_a"].id,
            )
            st_id = seed["st_a"].id
        _login(client, "teacher_a", "Pass1234")

        r = client.post(
            f"/api/students/{st_id}/allergies",
            json={"allergen": "花生", "severity": "severe"},
        )
        assert r.status_code == 403

    def test_medication_order_creates_pending_logs(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "super", "Pass1234", role="supervisor", perms=-1)
            st_id = seed["st_a"].id
        _login(client, "super", "Pass1234")

        r = client.post(
            f"/api/students/{st_id}/medication-orders",
            json={
                "order_date": date.today().isoformat(),
                "medication_name": "感冒藥",
                "dose": "1 顆",
                "time_slots": ["08:30", "12:00", "15:00"],
            },
        )
        assert r.status_code == 201, r.text
        order = r.json()
        assert len(order["logs"]) == 3
        for lg in order["logs"]:
            assert lg["status"] == "pending"
            assert lg["administered_at"] is None

    def test_time_slot_validation(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "super", "Pass1234", role="supervisor", perms=-1)
            st_id = seed["st_a"].id
        _login(client, "super", "Pass1234")

        # 無效時段
        r = client.post(
            f"/api/students/{st_id}/medication-orders",
            json={
                "order_date": date.today().isoformat(),
                "medication_name": "藥",
                "dose": "1 顆",
                "time_slots": ["8:30"],  # 應為 HH:MM
            },
        )
        assert r.status_code == 422

        # 重複時段
        r2 = client.post(
            f"/api/students/{st_id}/medication-orders",
            json={
                "order_date": date.today().isoformat(),
                "medication_name": "藥",
                "dose": "1 顆",
                "time_slots": ["08:30", "08:30"],
            },
        )
        assert r2.status_code == 422


class TestMedicationImmutability:
    """advisor flag #2: 已 administered/skipped 的 log 不可 UPDATE，修正走 /correct。"""

    def _setup_order_with_log(self, factory) -> tuple[int, int]:
        from models.database import StudentMedicationOrder

        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "super", "Pass1234", role="supervisor", perms=-1)
            order = StudentMedicationOrder(
                student_id=seed["st_a"].id,
                order_date=date.today(),
                medication_name="藥",
                dose="1 顆",
                time_slots=["08:30"],
            )
            s.add(order)
            s.flush()
            lg = StudentMedicationLog(order_id=order.id, scheduled_time="08:30")
            s.add(lg)
            s.commit()
            return order.id, lg.id

    def test_administer_then_update_is_rejected_by_trigger(
        self, app_with_db, storage_root
    ):
        client, factory, engine = app_with_db
        order_id, log_id = self._setup_order_with_log(factory)
        _login(client, "super", "Pass1234")

        r = client.post(
            f"/api/medication-logs/{log_id}/administer",
            json={"note": "吃完"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "administered"
        assert body["administered_at"] is not None

        # 直接發起第二次 administer → endpoint 層會 409
        r2 = client.post(
            f"/api/medication-logs/{log_id}/administer",
            json={"note": "重餵"},
        )
        assert r2.status_code == 409

        # 嘗試繞過 API 直接 UPDATE DB → 應被 trigger 拒絕
        with factory() as s:
            lg = (
                s.query(StudentMedicationLog)
                .filter(StudentMedicationLog.id == log_id)
                .first()
            )
            lg.note = "偷改"
            with pytest.raises(Exception) as excinfo:
                s.commit()
            assert (
                "不可修改" in str(excinfo.value)
                or "immutable" in str(excinfo.value).lower()
            )

    def test_correct_endpoint_creates_new_log(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        order_id, log_id = self._setup_order_with_log(factory)
        _login(client, "super", "Pass1234")

        r = client.post(f"/api/medication-logs/{log_id}/administer", json={})
        assert r.status_code == 200

        # 呼叫 /correct 新增修正紀錄
        r2 = client.post(
            f"/api/medication-logs/{log_id}/correct",
            json={
                "correction_reason": "實際沒吃",
                "skipped": True,
                "skipped_reason": "小孩拒吃",
            },
        )
        assert r2.status_code == 201
        corr = r2.json()
        assert corr["correction_of"] == log_id
        assert corr["status"] == "correction"
        assert corr["skipped"] is True

        # 原 log 仍保持 administered 狀態
        from models.database import StudentMedicationLog

        with factory() as s:
            orig = (
                s.query(StudentMedicationLog)
                .filter(StudentMedicationLog.id == log_id)
                .first()
            )
            assert orig.administered_at is not None
            assert orig.correction_of is None

    def test_skip_then_administer_is_rejected(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        order_id, log_id = self._setup_order_with_log(factory)
        _login(client, "super", "Pass1234")

        r = client.post(
            f"/api/medication-logs/{log_id}/skip",
            json={"skipped_reason": "家長取消"},
        )
        assert r.status_code == 200

        # 再呼叫 administer 應 409
        r2 = client.post(f"/api/medication-logs/{log_id}/administer", json={})
        assert r2.status_code == 409


# ══════════════════════════════════════════════════════════════════════════
# Medication reminder scheduler（單元邏輯，不啟動 loop）
# ══════════════════════════════════════════════════════════════════════════


class TestMedicationReminderScheduler:
    def test_count_today_medication_orders(self, app_with_db, storage_root):
        client, factory, _ = app_with_db
        from models.database import StudentMedicationOrder
        from services.medication_reminder_scheduler import (
            count_today_medication_orders,
            run_medication_reminder,
        )

        with factory() as s:
            seed = _seed_classrooms_and_students(s)
            _add_user(s, "super", "Pass1234", role="supervisor", perms=-1)
            # 今日 2 筆、昨日 1 筆
            from datetime import timedelta

            today = date.today()
            yesterday = today - timedelta(days=1)
            for st, d in [
                (seed["st_a"], today),
                (seed["st_b"], today),
                (seed["st_a"], yesterday),
            ]:
                o = StudentMedicationOrder(
                    student_id=st.id,
                    order_date=d,
                    medication_name="藥",
                    dose="1",
                    time_slots=["08:30"],
                )
                s.add(o)
            s.commit()

        assert count_today_medication_orders() == 2
        result = run_medication_reminder()
        assert result["order_count"] == 2
        assert result["date"] == date.today().isoformat()
