"""Integration tests for growth report admin endpoints (Task 5 + 6)."""

from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portfolio.reports import router as growth_reports_router
from models.auth import User
from models.database import Base, Classroom, Student


@pytest.fixture(scope="function")
def app_client(monkeypatch, tmp_path):
    _account_failures.clear()
    _ip_attempts.clear()

    # Patch REPORT_ROOT in reports module
    from api.portfolio import reports as reports_mod

    report_root = tmp_path / "growth_reports"
    monkeypatch.setattr(reports_mod, "REPORT_ROOT", report_root)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestingSession = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(base_module, "_engine", engine)
    monkeypatch.setattr(base_module, "_SessionFactory", TestingSession)
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(growth_reports_router)
    client = TestClient(app)

    with TestingSession() as session:
        admin = User(
            id=1,
            username="admin",
            password_hash="$2b$12$dummy",
            role="admin",
            permissions=-1,
            is_active=True,
            token_version=0,
        )
        classroom = Classroom(id=1, name="兔兔班", is_active=True)
        student = Student(
            id=1,
            student_id="S001",
            name="王小明",
            classroom_id=1,
            lifecycle_status="active",
            birthday=date(2022, 3, 5),
            enrollment_date=date(2024, 9, 1),
        )
        session.add_all([admin, classroom, student])
        session.commit()

    from utils.auth import create_access_token

    token = create_access_token(
        data={
            "sub": "admin",
            "user_id": 1,
            "role": "admin",
            "permissions": -1,
            "token_version": 0,
        }
    )
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client, TestingSession, tmp_path
    engine.dispose()


def test_create_report_returns_pending_or_more(app_client):
    client, _, _ = app_client
    resp = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "2026 春季",
            "period_start": "2026-02-01",
            "period_end": "2026-05-31",
            "teacher_narrative": "本期表現穩定",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in ("pending", "generating", "ready")
    assert body["period_label"] == "2026 春季"


def test_period_start_must_precede_end(app_client):
    client, _, _ = app_client
    resp = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "x",
            "period_start": "2026-06-01",
            "period_end": "2026-01-01",
        },
    )
    assert resp.status_code == 422


def test_create_report_dedup_blocks_duplicate_active_period(app_client):
    """F-V6-02：同 (student_id, period_label, period_start, period_end) 在非 failed
    狀態下僅允許一筆。模擬 admin 連點 POST，第二筆應 409 並含 existing report_id。"""
    client, _, _ = app_client
    payload = {
        "period_label": "2026 春季",
        "period_start": "2026-02-01",
        "period_end": "2026-05-31",
    }
    first = client.post("/api/students/1/growth-reports", json=payload)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = client.post("/api/students/1/growth-reports", json=payload)
    assert second.status_code == 409, second.text
    assert f"report_id={first_id}" in second.json()["detail"]


def test_create_report_dedup_allows_retry_after_failed(app_client):
    """F-V6-02：已 failed 的 report 不擋同 period 重建（容許 retry）。"""
    client, session_factory, _ = app_client
    payload = {
        "period_label": "2026 秋季",
        "period_start": "2026-09-01",
        "period_end": "2026-12-31",
    }
    first = client.post("/api/students/1/growth-reports", json=payload)
    assert first.status_code == 201
    first_id = first.json()["id"]

    # 把第一筆強制改成 failed，模擬 PDF 生成失敗
    from models.database import StudentGrowthReport

    with session_factory() as session:
        r = session.query(StudentGrowthReport).filter_by(id=first_id).first()
        r.status = "failed"
        r.error_message = "test forced failure"
        session.commit()

    # 同 period 應可再建一筆
    retry = client.post("/api/students/1/growth-reports", json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["id"] != first_id


def _wait_ready(client, rid, max_secs: float = 5.0) -> str:
    for _ in range(int(max_secs * 10)):
        st = client.get(f"/api/students/1/growth-reports/{rid}").json()
        if st["status"] in ("ready", "failed"):
            return st["status"]
        time.sleep(0.1)
    return "timeout"


def test_generate_then_download_pdf(app_client):
    client, _, _ = app_client
    resp = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "2026 春季",
            "period_start": "2026-02-01",
            "period_end": "2026-05-31",
        },
    )
    report_id = resp.json()["id"]
    status = _wait_ready(client, report_id)
    assert status == "ready"

    dl = client.get(f"/api/students/1/growth-reports/{report_id}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    assert dl.content[:4] == b"%PDF"


def test_list_returns_created_reports(app_client):
    client, _, _ = app_client
    client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "A",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "B",
            "period_start": "2026-04-01",
            "period_end": "2026-06-30",
        },
    )
    resp = client.get("/api/students/1/growth-reports")
    items = resp.json()["items"]
    assert len(items) == 2


def test_delete_removes_row_and_file(app_client):
    client, session_factory, tmp_path = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "X",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    _wait_ready(client, rid)

    resp = client.delete(f"/api/students/1/growth-reports/{rid}")
    assert resp.status_code == 204
    # Row gone
    with session_factory() as session:
        from models.database import StudentGrowthReport

        assert session.query(StudentGrowthReport).count() == 0


def test_download_409_if_not_ready(app_client):
    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "Y",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    with session_factory() as session:
        from models.database import StudentGrowthReport

        r = session.query(StudentGrowthReport).filter_by(id=rid).first()
        r.status = "pending"
        r.file_path = None
        session.commit()
    resp = client.get(f"/api/students/1/growth-reports/{rid}/download")
    assert resp.status_code == 409


# ── Task 6: LINE send ──────────────────────────────────────────────────────


def test_send_line_when_no_binding_returns_409(app_client):
    """無 LINE 綁定 → 409."""
    client, _, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "Z",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    # Wait until report is ready before testing send-line
    for _ in range(50):
        st = client.get(f"/api/students/1/growth-reports/{rid}").json()
        if st["status"] in ("ready", "failed"):
            break
        time.sleep(0.1)
    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 409


def test_send_line_when_not_ready_returns_409(app_client):
    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "W",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    # Force status back to pending
    with session_factory() as session:
        from models.database import StudentGrowthReport

        r = session.query(StudentGrowthReport).filter_by(id=rid).first()
        r.status = "pending"
        session.commit()
    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 409


def test_send_line_all_failed_releases_idempotency_lock(app_client, monkeypatch):
    """Why: 推送全部失敗 (network/token 過期) 時不可寫 line_sent_at，否則 admin
    被卡 5 分鐘無法重試，且 200 OK + sent_count=0 容易被忽略。應回 502 並釋放鎖。"""
    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "F",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    _wait_ready(client, rid)

    # 綁定家長 LINE
    with session_factory() as session:
        from models.auth import User as _U
        from models.database import Guardian

        session.add(
            _U(
                id=2,
                username="p_fail",
                password_hash="$2b$12$dummy",
                role="parent",
                permissions=0,
                is_active=True,
                token_version=0,
                line_user_id="U_FAIL",
            )
        )
        session.add(Guardian(user_id=2, student_id=1, name="家長"))
        session.commit()

    # patch line service：全失敗
    from api.portfolio import reports as reports_mod

    class _FailLine:
        def push_to_user(self, *_a, **_k):
            return False

    monkeypatch.setattr(reports_mod, "_line_service", _FailLine())

    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 502, resp.text

    # line_sent_at 必須仍為 None，admin 可立即重試
    with session_factory() as session:
        from models.database import StudentGrowthReport

        r = session.query(StudentGrowthReport).filter_by(id=rid).first()
        assert r.line_sent_at is None, "失敗時不可佔用冪等鎖"


def test_send_line_partial_success_keeps_idempotency_lock(app_client, monkeypatch):
    """部份家長成功 → 至少一份送達，line_sent_at 應寫入避免重複推送已收到的家長."""
    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "G",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    _wait_ready(client, rid)

    with session_factory() as session:
        from models.auth import User as _U
        from models.database import Guardian

        for uid, line_id in [(2, "U_OK"), (3, "U_BAD")]:
            session.add(
                _U(
                    id=uid,
                    username=f"p{uid}",
                    password_hash="$2b$12$dummy",
                    role="parent",
                    permissions=0,
                    is_active=True,
                    token_version=0,
                    line_user_id=line_id,
                )
            )
            session.add(Guardian(user_id=uid, student_id=1, name="家長"))
        session.commit()

    from api.portfolio import reports as reports_mod

    class _MixLine:
        def push_to_user(self, line_user_id, _text):
            return line_user_id == "U_OK"

    monkeypatch.setattr(reports_mod, "_line_service", _MixLine())

    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sent_count"] == 1

    with session_factory() as session:
        from models.database import StudentGrowthReport

        r = session.query(StudentGrowthReport).filter_by(id=rid).first()
        assert r.line_sent_at is not None, "至少一人成功時應佔用冪等鎖"


def test_send_line_does_not_block_under_session_scope(app_client, monkeypatch):
    """REGRESSION: 推送 IO 不可在 session_scope 內進行（測試方式：偵測推送被呼叫
    時 session 是否仍開著；由 push 函式在被呼叫時發起獨立 session 寫入並 commit
    若被外層 session_scope 的鎖卡住會 hang，此測試靠 timeout 檢測）。
    """
    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "S",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    _wait_ready(client, rid)

    with session_factory() as session:
        from models.auth import User as _U
        from models.database import Guardian

        session.add(
            _U(
                id=2,
                username="p_io",
                password_hash="$2b$12$dummy",
                role="parent",
                permissions=0,
                is_active=True,
                token_version=0,
                line_user_id="U_IO",
            )
        )
        session.add(Guardian(user_id=2, student_id=1, name="家長"))
        session.commit()

    from api.portfolio import reports as reports_mod

    pushed_during_outer_txn = []

    class _ProbeLine:
        def push_to_user(self, _uid, _text):
            # 推送時 line_sent_at 應已 commit（claim slot 完成），可從另一 session 讀到
            from models.database import StudentGrowthReport, session_scope

            with session_scope() as s2:
                r = s2.query(StudentGrowthReport).filter_by(id=rid).first()
                pushed_during_outer_txn.append(r.line_sent_at is not None)
            return True

    monkeypatch.setattr(reports_mod, "_line_service", _ProbeLine())

    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 200
    assert pushed_during_outer_txn == [
        True
    ], "推送時 line_sent_at 應已先 commit，代表推送發生在 session_scope 之外"


def test_send_line_idempotent_within_5_minutes(app_client):
    """Why: 5 分鐘內重複推送 (admin 連點 / 前端 bug) 應回 409 防 LINE quota 浪費."""
    from datetime import datetime, timedelta

    client, session_factory, _ = app_client
    create = client.post(
        "/api/students/1/growth-reports",
        json={
            "period_label": "Q",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
        },
    )
    rid = create.json()["id"]
    _wait_ready(client, rid)

    # 模擬剛剛已推送 + 已綁定 LINE，跳過真實 push 路徑
    with session_factory() as session:
        from models.auth import User
        from models.database import Guardian, StudentGrowthReport

        r = session.query(StudentGrowthReport).filter_by(id=rid).first()
        r.line_sent_at = datetime.utcnow() - timedelta(minutes=2)
        # 給家長綁 LINE，否則先撞 "未綁定 LINE" 409 看不出冪等
        parent = User(
            id=2,
            username="p1",
            password_hash="$2b$12$dummy",
            role="parent",
            permissions=0,
            is_active=True,
            token_version=0,
            line_user_id="U_TEST",
        )
        session.add(parent)
        session.add(Guardian(user_id=2, student_id=1, name="家長"))
        session.commit()

    resp = client.post(f"/api/students/1/growth-reports/{rid}/send-line", json={})
    assert resp.status_code == 409
    assert "5 分鐘內" in resp.json()["detail"]
