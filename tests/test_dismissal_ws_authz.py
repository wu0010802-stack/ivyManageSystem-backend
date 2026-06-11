"""
tests/test_dismissal_ws_authz.py — Portal 接送通知 WS 端點授權回歸測試

背景（2026-06-04 bug）：
    portal WS `/api/ws/portal/dismissal-calls` 原本硬卡 `role == "teacher"`，
    但同一份資料的 REST 端點 `/api/portal/dismissal-calls` 是 permission-based
    （require_permission(DISMISSAL_CALLS_READ)）。導致 admin（有此權限、直接檢視
    教師端）拿得到 REST 資料（200）卻被 WS handshake 拒絕（pre-accept close 4007 →
    uvicorn 回 403 → 瀏覽器 1006），前端重試 5 次後退化成 polling =「一打開就連不上」。

    修法：WS 授權對齊 REST——gate 在 DISMISSAL_CALLS_READ 權限，訂閱範圍對齊
    is_unrestricted scope（:all → admin channel 收全校；:own_class → 自班）。

本測試鎖住「WS 授權 == REST 授權」的對等性，避免日後又在 WS 端加上比 REST 更嚴的
角色守衛而重蹈覆轍。直接呼叫 endpoint coroutine 並以 mock WebSocket 斷言 accept/close
與 subscribe 的 channel。
"""

import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import WebSocketDisconnect

import api.dismissal_ws as dws
from api.dismissal_ws import (
    portal_dismissal_ws,
    admin_dismissal_ws,
    _ADMIN_CHANNEL,
    _classroom_channel,
)
from utils.ws_hub import WS_CLOSE_FORBIDDEN
from utils import ws_connection_limiter


@pytest.fixture(autouse=True)
def _reset_limiter():
    ws_connection_limiter.reset_for_tests()
    yield
    ws_connection_limiter.reset_for_tests()


def _make_ws():
    """最小化 WS mock：accept 後 receive_text 立即 disconnect，讓主循環即時結束。"""
    ws = MagicMock()
    ws.cookies = {"access_token": "dummy"}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))
    return ws


def _close_codes(ws):
    """收集所有 ws.close(code=...) 被呼叫到的 code。"""
    return [c.kwargs.get("code") for c in ws.close.call_args_list]


def _run_endpoint(payload, classroom_ids=None):
    """以指定 verify_ws_token payload 跑 portal_dismissal_ws，回傳 (ws, backend)。"""
    ws = _make_ws()
    backend = MagicMock()
    backend.subscribe = MagicMock()
    backend.unsubscribe = MagicMock()
    with (
        patch.object(dws, "verify_ws_token", return_value=payload),
        patch.object(dws, "get_broadcast", return_value=backend),
        patch.object(
            dws, "_get_teacher_classroom_ids", return_value=(classroom_ids or [])
        ),
    ):
        asyncio.run(portal_dismissal_ws(ws))
    return ws, backend


# ── 案例 1：非教師角色但持有 DISMISSAL_CALLS_READ（admin 直接檢視）應可連 ──────────


def test_admin_with_permission_accepts_and_subscribes_admin_channel():
    """admin（wildcard 權限 → :all scope）連 portal WS 應 accept 並訂閱 admin channel，
    不得 pre-accept close 4007。這是回歸的核心：REST 200 的帳號 WS 不該 403。"""
    payload = {
        "user_id": 1,
        "role": "admin",
        "employee_id": None,
        "permission_names": ["*"],
    }
    ws, backend = _run_endpoint(payload)

    ws.accept.assert_called_once()
    assert WS_CLOSE_FORBIDDEN not in _close_codes(ws), "admin 不應被 4007 拒絕"
    backend.subscribe.assert_any_call(_ADMIN_CHANNEL, ws)


# ── 案例 2：一般教師（:own_class）維持自班訂閱（不得回歸）──────────────────────


def test_teacher_own_class_accepts_and_subscribes_own_classrooms():
    payload = {
        "user_id": 2,
        "role": "teacher",
        "employee_id": 50,
        "permission_names": ["DISMISSAL_CALLS_READ:own_class"],
    }
    ws, backend = _run_endpoint(payload, classroom_ids=[10, 20])

    ws.accept.assert_called_once()
    assert WS_CLOSE_FORBIDDEN not in _close_codes(ws)
    backend.subscribe.assert_any_call(_classroom_channel(10), ws)
    backend.subscribe.assert_any_call(_classroom_channel(20), ws)
    # 不該訂到全校 admin channel
    subscribed = [c.args[0] for c in backend.subscribe.call_args_list]
    assert _ADMIN_CHANNEL not in subscribed


# ── 案例 3：無 DISMISSAL_CALLS_READ 權限者仍須被拒（不得過度放寬）──────────────


def test_role_without_permission_is_rejected():
    """hr 預設不含 DISMISSAL_CALLS_READ；即使角色在 portal 範圍也須 4007 拒絕，
    證明修法只對齊 REST、未把門開太大。"""
    payload = {
        "user_id": 3,
        "role": "hr",
        "employee_id": None,
        "permission_names": ["EMPLOYEES_READ"],
    }
    ws, backend = _run_endpoint(payload)

    ws.accept.assert_not_called()
    assert WS_CLOSE_FORBIDDEN in _close_codes(ws)
    backend.subscribe.assert_not_called()


# ── 管理端 WS（admin_dismissal_ws）scope 對齊 REST 回歸 ───────────────────────────
#
# 背景（Finding A，2026-06-05）：管理端 WS `/api/ws/admin/dismissal-calls` 原本只檢查
#   role in (admin,hr,supervisor) + bare has_permission(STUDENTS_READ)，
# 但同資源的 REST 全園列表 `/api/dismissal-calls` 走 assert_all_scope(STUDENTS_READ)，
# 要求 :all scope。因 STUDENTS_READ 是 scope-aware，bare has_permission 對
# 'STUDENTS_READ:own_class' 仍回 True → 一個被刻意限為 own_class 的管理角色
# 在 REST 被 403、卻能從 WS 訂閱 dismissal.admin 收全校學生姓名/班級/備註（跨班 PII 洩漏）。
# 修法：admin WS 改用 is_unrestricted(payload, code=STUDENTS_READ) 對齊 REST 的 :all 要求。


def _run_admin_endpoint(payload):
    """以指定 verify_ws_token payload 跑 admin_dismissal_ws，回傳 (ws, backend)。"""
    ws = _make_ws()
    backend = MagicMock()
    backend.subscribe = MagicMock()
    backend.unsubscribe = MagicMock()
    with (
        patch.object(dws, "verify_ws_token", return_value=payload),
        patch.object(dws, "get_broadcast", return_value=backend),
    ):
        asyncio.run(admin_dismissal_ws(ws))
    return ws, backend


def test_admin_ws_rejects_supervisor_scoped_to_own_class():
    """Finding A 核心：role=supervisor 但 STUDENTS_READ:own_class 的帳號，
    REST assert_all_scope 會 403；admin WS 不得只憑 role 白名單 + bare 權限放行，
    否則訂閱 dismissal.admin 收全校 PII。必須 4007 拒絕、不得 accept/subscribe。"""
    payload = {
        "user_id": 7,
        "role": "supervisor",
        "employee_id": 99,
        "permission_names": ["STUDENTS_READ:own_class"],
    }
    ws, backend = _run_admin_endpoint(payload)

    ws.accept.assert_not_called()
    assert WS_CLOSE_FORBIDDEN in _close_codes(ws), "own_class 管理角色須被 4007 拒絕"
    backend.subscribe.assert_not_called()


def test_admin_ws_accepts_wildcard_admin():
    """零行為變更驗證：admin（wildcard → :all）仍 accept 並訂閱 admin channel。"""
    payload = {
        "user_id": 8,
        "role": "admin",
        "employee_id": None,
        "permission_names": ["*"],
    }
    ws, backend = _run_admin_endpoint(payload)

    ws.accept.assert_called_once()
    assert WS_CLOSE_FORBIDDEN not in _close_codes(ws)
    backend.subscribe.assert_any_call(_ADMIN_CHANNEL, ws)


def test_admin_ws_accepts_supervisor_with_bare_all_scope():
    """零行為變更驗證：預設 supervisor（bare STUDENTS_READ，等價 :all）仍 accept。
    確保修法只擋 :own_class，不誤傷標準管理角色。"""
    payload = {
        "user_id": 9,
        "role": "supervisor",
        "employee_id": 88,
        "permission_names": ["STUDENTS_READ"],
    }
    ws, backend = _run_admin_endpoint(payload)

    ws.accept.assert_called_once()
    assert WS_CLOSE_FORBIDDEN not in _close_codes(ws)
    backend.subscribe.assert_any_call(_ADMIN_CHANNEL, ws)
