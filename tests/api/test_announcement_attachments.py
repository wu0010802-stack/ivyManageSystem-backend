"""Announcement attachment upload endpoint tests (PR #2 T3)."""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.announcements import router as announcements_router
from api.attachments import download_router as attachments_download_router
from models.database import (
    Announcement,
    Attachment,
    Base,
    Employee,
    User,
)
from utils.auth import create_access_token, hash_password

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "ann-attach.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def admin_emp(db_session):
    emp = Employee(
        employee_id="ATT-ADMIN",
        name="att_admin",
        is_active=True,
        base_salary=0,
    )
    db_session.add(emp)
    db_session.flush()
    return emp


@pytest.fixture
def admin_client(db_engine, admin_emp, db_session):
    factory = sessionmaker(bind=db_engine)

    user = User(
        username="att_admin_user",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=["*"],
        employee_id=admin_emp.id,
        is_active=True,
        token_version=0,
    )
    db_session.add(user)
    db_session.commit()

    token = create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permission_names": user.permission_names or [],
            "token_version": user.token_version or 0,
        }
    )

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = factory

    app = FastAPI()
    app.include_router(announcements_router)
    app.include_router(attachments_download_router)

    with TestClient(app) as client:
        client.cookies.set("access_token", token)
        yield client

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory


# ── Helpers ───────────────────────────────────────────────────────────────────


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%dummy\n%%EOF\n"


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_upload_png_succeeds(admin_client, db_session, admin_emp):
    """上傳有效 PNG 回 201 並建 Attachment row。"""
    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    files = {"file": ("hero.png", _png_bytes(), "image/png")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["mime_type"].startswith("image/")

    db_session.expire_all()
    rows = (
        db_session.query(Attachment)
        .filter(Attachment.owner_type == "announcement", Attachment.owner_id == a.id)
        .all()
    )
    assert len(rows) == 1


def test_upload_pdf_succeeds(admin_client, db_session, admin_emp):
    """上傳有效 PDF 回 201，thumb_url 為 None（非圖片）。"""
    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    files = {"file": ("notice.pdf", _pdf_bytes(), "application/pdf")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["mime_type"] == "application/pdf"
    assert body.get("thumb_url") is None


def test_upload_rejects_disallowed_ext(admin_client, db_session, admin_emp):
    """不接受 .exe 等非白名單格式，回 400。"""
    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    files = {"file": ("evil.exe", b"MZdata", "application/octet-stream")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400, res.text


def test_upload_rejects_fake_pdf(admin_client, db_session, admin_emp):
    """副檔名 .pdf 但內容非 %PDF magic bytes，回 400。"""
    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    files = {"file": ("fake.pdf", b"not a real pdf", "application/pdf")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400, res.text


def test_upload_enforces_5_limit(admin_client, db_session, admin_emp):
    """單一公告最多 5 個附件，第 6 個回 400 且 detail 提及 '5' 或 '上限'。"""
    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    for i in range(5):
        files = {"file": (f"p{i}.png", _png_bytes(), "image/png")}
        res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
        assert res.status_code == 201, f"第 {i + 1} 個 PNG 應成功，got {res.text}"

    files = {"file": ("extra.png", _png_bytes(), "image/png")}
    res = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    assert res.status_code == 400, res.text
    detail = res.json().get("detail", "")
    assert "5" in detail or "上限" in detail, f"detail 應提及上限，got: {detail!r}"


def test_upload_404_unknown_announcement(admin_client):
    """不存在的公告 id 回 404。"""
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    res = admin_client.post("/api/announcements/999999/attachments", files=files)
    assert res.status_code == 404, res.text


def test_delete_attachment_soft_deletes(admin_client, db_session, admin_emp):
    from models.database import Announcement, Attachment

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    up = admin_client.post(f"/api/announcements/{a.id}/attachments", files=files)
    att_id = up.json()["id"]

    res = admin_client.delete(f"/api/announcements/{a.id}/attachments/{att_id}")
    assert res.status_code == 200

    row = db_session.query(Attachment).filter(Attachment.id == att_id).first()
    db_session.refresh(row)
    assert row.deleted_at is not None


def test_delete_rejects_cross_announcement(
    admin_client, db_session, admin_emp
):
    from models.database import Announcement

    a1 = Announcement(title="A", content="C", created_by=admin_emp.id)
    a2 = Announcement(title="B", content="C", created_by=admin_emp.id)
    db_session.add_all([a1, a2])
    db_session.commit()
    files = {"file": ("p.png", _png_bytes(), "image/png")}
    up = admin_client.post(f"/api/announcements/{a1.id}/attachments", files=files)
    att_id = up.json()["id"]

    res = admin_client.delete(f"/api/announcements/{a2.id}/attachments/{att_id}")
    assert res.status_code == 404


def test_admin_can_download_announcement_attachment(
    admin_client, db_session, admin_emp
):
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    url = up.json()["url"]
    res = admin_client.get(url)
    assert res.status_code == 200


def test_download_404_for_unknown_key(admin_client):
    res = admin_client.get("/api/uploads/portfolio/2026/05/nonexistent.png")
    assert res.status_code == 404


def test_download_410_for_soft_deleted_attachment(
    admin_client, db_session, admin_emp
):
    from models.database import Announcement, Attachment

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()
    up = admin_client.post(
        f"/api/announcements/{a.id}/attachments",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    body = up.json()
    url = body["url"]
    att_id = body["id"]

    # 軟刪除
    admin_client.delete(f"/api/announcements/{a.id}/attachments/{att_id}")
    res = admin_client.get(url)
    assert res.status_code == 410
