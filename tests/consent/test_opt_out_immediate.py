"""tests/consent/test_opt_out_immediate.py — opt-out 即時撤回（P2-3）。

覆蓋 spec §3.2a：
- granular scope（line_push）opt-out 後 consent_check 即時回 False（快取已 invalidate）。
- service_essential opt-out → 400（不可停止基礎服務同意）。
- 從未簽過任何 consent 的 scope opt-out → 400「無可撤回」。
- opt-out 後 DsrRequest.status == approved（自助撤回不進 pending queue）。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import models.base as base_module
from api.parent_portal import parent_router
from api.parent_portal._dependencies import get_parent_db
from models.consent import (
    CONSENT_SCOPE_LINE_PUSH,
    CONSENT_SCOPE_PHOTO_PUBLISH,
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.database import Base, Guardian, Student, User
from models.dsr import (
    DSR_REQUEST_TYPE_OPT_OUT,
    DSR_STATUS_APPROVED,
    DsrRequest,
)
from services.consent.checker import consent_check
from tests._parent_rls_test_utils import make_sqlite_parent_db_override
from utils.auth import create_access_token
from utils.cache_layer import reset_cache_for_testing
from utils.taipei_time import now_taipei_naive

# ── 快取隔離（每個 test 從乾淨快取開始）────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# ── Client fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def opt_out_client(tmp_path):
    db_path = tmp_path / "opt-out-immediate.sqlite"
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

    app = FastAPI()
    app.include_router(parent_router)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _seed_policy(session) -> PolicyVersion:
    pv = PolicyVersion(
        version="2026.test.optout",
        effective_at=now_taipei_naive(),
        document_path="/policies/2026-optout-test.pdf",
    )
    session.add(pv)
    session.flush()
    return pv


def _seed_parent(session, *, username: str, line_id: str) -> User:
    user = User(
        username=username,
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id=line_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    student = Student(
        student_id=f"S_{line_id}",
        name="測試學生",
        lifecycle_status="active",
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="測試家長",
        relation="母親",
        is_primary=True,
    )
    session.add(guardian)
    session.commit()
    return session.merge(user)


def _seed_consent(
    session_factory, user_id: int, policy_version_id: int, scope: str, consented: bool
) -> None:
    with session_factory() as session:
        log = ParentConsentLog(
            user_id=user_id,
            policy_version_id=policy_version_id,
            scope=scope,
            consented=consented,
            consented_at=now_taipei_naive(),
        )
        session.add(log)
        session.commit()


def _parent_token(user: User) -> str:
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


# ── 測試：granular scope 即時撤回 + 快取 invalidate ─────────────────────────


def test_opt_out_granular_immediate_and_cache_invalidated(opt_out_client):
    """家長先簽 line_push 同意 → opt-out line_push → consent_check 即時回 False。

    驗證「即時」意義：先 warm 快取（consent_check 回 True），opt-out 後再查，
    若 invalidate_consent_cache 沒呼叫，快取仍為 True 並打臉斷言。
    """
    client, sf = opt_out_client

    # 1. Seed：policy + 家長 + line_push consented=True
    with sf() as session:
        pv = _seed_policy(session)
        user = _seed_parent(
            session, username="parent_optout_immediate", line_id="U_OPT_IMM"
        )
        user_id = user.id
        pv_id = pv.id

    _seed_consent(sf, user_id, pv_id, CONSENT_SCOPE_LINE_PUSH, consented=True)

    # 2. 暖快取：consent_check 第一次 → True（快取住）
    with sf() as session:
        result_before = consent_check(session, user_id, CONSENT_SCOPE_LINE_PUSH)
    assert result_before is True, "前置：line_push 同意後 consent_check 應回 True"

    # 3. 發 opt-out 請求
    with sf() as session:
        user_obj = session.get(User, user_id)
        token = _parent_token(user_obj)

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_LINE_PUSH, "reason": "不再需要推播"},
    )
    assert r.status_code == 200, f"opt-out 應成功：{r.text}"
    body = r.json()
    assert body["scope"] == CONSENT_SCOPE_LINE_PUSH
    assert body["request_type"] == DSR_REQUEST_TYPE_OPT_OUT
    assert (
        body["status"] == DSR_STATUS_APPROVED
    ), "granular opt-out 應即時核准（approved），不進 pending queue"

    # 4. 即時驗證：consent_check 不等 TTL，立即回 False（快取已 invalidate）
    with sf() as session:
        result_after = consent_check(session, user_id, CONSENT_SCOPE_LINE_PUSH)
    assert result_after is False, (
        "opt-out 後 consent_check 應立即回 False；"
        "若仍為 True 表示 invalidate_consent_cache 未被呼叫"
    )


def test_opt_out_granular_dsr_status_is_approved(opt_out_client):
    """opt-out granular scope 後 DsrRequest.status == approved（法律備案）。"""
    client, sf = opt_out_client

    with sf() as session:
        pv = _seed_policy(session)
        user = _seed_parent(session, username="parent_optout_dsr", line_id="U_OPT_DSR")
        user_id = user.id
        pv_id = pv.id

    _seed_consent(sf, user_id, pv_id, CONSENT_SCOPE_PHOTO_PUBLISH, consented=True)

    with sf() as session:
        user_obj = session.get(User, user_id)
        token = _parent_token(user_obj)

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_PHOTO_PUBLISH},
    )
    assert r.status_code == 200, r.text

    with sf() as session:
        dsr_row = (
            session.query(DsrRequest)
            .filter(
                DsrRequest.user_id == user_id,
                DsrRequest.request_type == DSR_REQUEST_TYPE_OPT_OUT,
            )
            .first()
        )
        assert dsr_row is not None
        assert (
            dsr_row.status == DSR_STATUS_APPROVED
        ), f"DsrRequest.status 應為 approved，實際為 {dsr_row.status}"
        assert dsr_row.scope == CONSENT_SCOPE_PHOTO_PUBLISH


def test_opt_out_service_essential_rejected_400(opt_out_client):
    """service_essential scope opt-out → 400（基礎服務同意不可停止）。"""
    client, sf = opt_out_client

    with sf() as session:
        _seed_policy(session)
        user = _seed_parent(
            session, username="parent_optout_essential", line_id="U_OPT_ESS"
        )
        user_id = user.id

    with sf() as session:
        user_obj = session.get(User, user_id)
        token = _parent_token(user_obj)

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_SERVICE_ESSENTIAL},
    )
    assert (
        r.status_code == 400
    ), f"service_essential opt-out 應回 400，實際：{r.status_code}"
    assert (
        "基礎服務" in r.json()["detail"] or "刪除申請" in r.json()["detail"]
    ), f"錯誤訊息應指引刪除申請，實際：{r.json()['detail']}"


def test_opt_out_no_prior_consent_rejected_400(opt_out_client):
    """從未對 scope 簽過任何 consent 的家長 opt-out → 400「無可撤回」。

    撤回不存在的同意無意義，需擋住避免寫入混亂的 consented=False log。
    """
    client, sf = opt_out_client

    with sf() as session:
        _seed_policy(session)
        user = _seed_parent(
            session, username="parent_optout_nolog", line_id="U_OPT_NIL"
        )
        user_id = user.id

    with sf() as session:
        user_obj = session.get(User, user_id)
        token = _parent_token(user_obj)

    # photo_publish：該家長從未有任何 consent log
    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_PHOTO_PUBLISH},
    )
    assert (
        r.status_code == 400
    ), f"無 consent 紀錄 opt-out 應回 400，實際：{r.status_code}"
    assert (
        "無可撤回" in r.json()["detail"]
    ), f"錯誤訊息應含「無可撤回」，實際：{r.json()['detail']}"


def test_opt_out_consent_log_written_with_consented_false(opt_out_client):
    """opt-out 後 ParentConsentLog 應新增一筆 consented=False。"""
    client, sf = opt_out_client

    with sf() as session:
        pv = _seed_policy(session)
        user = _seed_parent(session, username="parent_optout_log", line_id="U_OPT_LOG")
        user_id = user.id
        pv_id = pv.id

    _seed_consent(sf, user_id, pv_id, CONSENT_SCOPE_LINE_PUSH, consented=True)

    with sf() as session:
        user_obj = session.get(User, user_id)
        token = _parent_token(user_obj)

    r = client.post(
        "/api/parent/me/opt-out",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": CONSENT_SCOPE_LINE_PUSH},
    )
    assert r.status_code == 200, r.text

    with sf() as session:
        logs = (
            session.query(ParentConsentLog)
            .filter(
                ParentConsentLog.user_id == user_id,
                ParentConsentLog.scope == CONSENT_SCOPE_LINE_PUSH,
            )
            .order_by(ParentConsentLog.consented_at)
            .all()
        )
        assert len(logs) == 2, f"應有 2 筆 log（原同意 + 撤回），實際：{len(logs)}"
        assert logs[0].consented is True, "第一筆應為 consented=True"
        assert logs[1].consented is False, "第二筆（opt-out 寫入）應為 consented=False"
        # policy_version_id 沿用原同意版本
        assert (
            logs[1].policy_version_id == pv_id
        ), "撤回 log 的 policy_version_id 應沿用原同意的版本"
