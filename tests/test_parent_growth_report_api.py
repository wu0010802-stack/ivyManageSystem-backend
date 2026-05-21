"""Tests for /api/parent/growth-reports endpoints."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.parent_portal.growth_reports import router as parent_growth_reports_router
from models.auth import User
from models.database import Base, Classroom, Guardian, Student, StudentGrowthReport
from utils.auth import create_access_token


@pytest.fixture(scope="function")
def app_client(tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    db_path = tmp_path / "parent_growth.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestingSession = sessionmaker(bind=engine, autoflush=False)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = TestingSession
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(parent_growth_reports_router, prefix="/api/parent")

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        TestingSession
    )
    client = TestClient(app)

    with TestingSession() as session:
        parent = User(
            username="parent_a",
            password_hash="$2b$12$dummy",
            role="parent",
            permission_names=[],
            is_active=True,
            token_version=0,
        )
        session.add(parent)
        session.flush()

        classroom = Classroom(name="兔兔班", is_active=True)
        session.add(classroom)
        session.flush()

        student = Student(
            student_id="S001",
            name="王小明",
            classroom_id=classroom.id,
            is_active=True,
        )
        other_student = Student(
            student_id="S099",
            name="李小華",
            classroom_id=classroom.id,
            is_active=True,
        )
        session.add_all([student, other_student])
        session.flush()

        guardian = Guardian(
            user_id=parent.id,
            student_id=student.id,
            name="王家長",
            relation="父親",
            is_primary=True,
        )
        session.add(guardian)
        session.commit()

        parent_id = parent.id
        student_id = student.id
        other_student_id = other_student.id

    parent_token = create_access_token(
        data={
            "sub": "parent_a",
            "user_id": parent_id,
            "role": "parent",
            "permission_names": [],
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {parent_token}"})

    yield client, TestingSession, student_id, other_student_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_parent_list_reports_empty(app_client):
    client, _, student_id, _ = app_client
    resp = client.get(f"/api/parent/growth-reports?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_parent_cannot_list_other_kid(app_client):
    client, _, _, other_student_id = app_client
    resp = client.get(f"/api/parent/growth-reports?student_id={other_student_id}")
    assert resp.status_code == 403, resp.text


def test_parent_lists_ready_report(app_client):
    """直接寫 DB 建一筆 ready report，parent 應看到."""
    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        session.add(
            StudentGrowthReport(
                student_id=student_id,
                period_label="2026 春季",
                period_start=date(2026, 2, 1),
                period_end=date(2026, 5, 31),
                status="ready",
                file_path="growth_reports/1/1.pdf",
            )
        )
        session.commit()
    resp = client.get(f"/api/parent/growth-reports?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "ready"


def test_parent_list_does_not_expose_admin_internal_fields(app_client):
    """F-V6-06：parent 序列化不暴露 admin 內部欄位（error_message / file_path /
    generated_by）。攻擊面：admin 對失敗 report 補 patch 後改回 status=READY
    時 error_message 殘留會經 _row_to_dict reuse 洩漏給家長。
    """
    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        session.add(
            StudentGrowthReport(
                student_id=student_id,
                period_label="x",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                status="ready",
                file_path="/srv/instance/growth_reports/1/9.pdf",
                error_message="DEBUG: psycopg2.errors.NotNullViolation table=...",
                generated_by=None,  # 跳過 FK；測試焦點是 serialization 過濾
            )
        )
        session.commit()
    resp = client.get(f"/api/parent/growth-reports?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert "error_message" not in item, f"家長端不應暴露 error_message，got {item}"
    assert "file_path" not in item, f"家長端不應暴露 file_path，got {item}"
    assert "generated_by" not in item, f"家長端不應暴露 generated_by，got {item}"


def test_parent_pending_report_excluded(app_client):
    """status=pending 報告不應出現在 parent list."""
    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        session.add(
            StudentGrowthReport(
                student_id=student_id,
                period_label="待產生",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                status="pending",
            )
        )
        session.commit()
    resp = client.get(f"/api/parent/growth-reports?student_id={student_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_parent_download_report_not_found(app_client):
    """不存在的 report_id 回 404."""
    client, _, student_id, _ = app_client
    resp = client.get(
        f"/api/parent/growth-reports/9999/download?student_id={student_id}"
    )
    assert resp.status_code == 404, resp.text


def test_parent_download_report_not_ready(app_client):
    """status=pending 的報告回 409."""
    client, session_factory, student_id, _ = app_client
    with session_factory() as session:
        r = StudentGrowthReport(
            student_id=student_id,
            period_label="待產生",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            status="pending",
        )
        session.add(r)
        session.commit()
        report_id = r.id
    resp = client.get(
        f"/api/parent/growth-reports/{report_id}/download?student_id={student_id}"
    )
    assert resp.status_code == 409, resp.text


def test_parent_cannot_download_other_kid(app_client):
    """嘗試下載他人子女報告回 403."""
    client, session_factory, student_id, other_student_id = app_client
    # 不需要對應的 report 存在；IDOR 檢查在 report 查詢前
    resp = client.get(
        f"/api/parent/growth-reports/1/download?student_id={other_student_id}"
    )
    assert resp.status_code == 403, resp.text


def test_parent_download_view_count_increments_atomically(
    app_client, tmp_path, monkeypatch
):
    """REGRESSION: 連續 3 次下載 view_count 必須 == 3 (原子化 INCR)；
    parent_first_viewed_at 只在第一次設定，後續不被覆寫（agent P2 #10）.
    """
    from datetime import datetime as _dt

    from api.portfolio import reports as reports_mod

    client, session_factory, student_id, _ = app_client

    # patch REPORT_ROOT to tmp_path 並建立假 PDF 檔
    report_root = tmp_path / "growth_reports"
    report_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reports_mod, "REPORT_ROOT", report_root)
    pdf_path = report_root / "1.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with session_factory() as session:
        session.add(
            StudentGrowthReport(
                id=1,
                student_id=student_id,
                period_label="x",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                status="ready",
                file_path=str(pdf_path),
            )
        )
        session.commit()

    first_viewed_after_call_1: list = [None]
    for i in range(3):
        resp = client.get(
            f"/api/parent/growth-reports/1/download?student_id={student_id}"
        )
        assert resp.status_code == 200, f"call {i + 1}: {resp.text}"

        with session_factory() as session:
            r = session.query(StudentGrowthReport).filter_by(id=1).first()
            if i == 0:
                first_viewed_after_call_1[0] = r.parent_first_viewed_at
                assert r.parent_first_viewed_at is not None
                assert r.parent_view_count == 1
            elif i == 1:
                # 第 2 次：first_viewed_at 不應被覆寫
                assert r.parent_first_viewed_at == first_viewed_after_call_1[0]
                assert r.parent_view_count == 2
            else:
                assert r.parent_view_count == 3
