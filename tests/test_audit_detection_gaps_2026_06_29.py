"""Track D — qa-loop round2（2026-06-29）audit 偵測/forensic 三缺口。

P2-1：_extract_session_id_from_request 只讀 Authorization header 的 Bearer jti，不讀 cookie；
但前端早已改 httpOnly cookie access_token、不帶 Authorization header → prod 主流量的
audit session_id 恆 NULL，forensic「同一 session 所有操作」查詢永遠查不到。siblings
_extract_user_from_header / _extract_impersonation_from_header 都先讀 cookie 再讀 header。

P2-2：is_high_risk_event / filter_high_risk 的「權限變更」分支硬要求 entity_type=='user'，
但 PUT /api/roles/{code}（改整個 role 的權限集，bump 該 role 所有成員 token_version）寫的
audit 是 entity_type='role' → 改 role 提權（最高槓桿）不進紅點清單也不發 LINE 告警。

P3：get_high_risk_audits / ack_all_audits 用 datetime.now(timezone.utc)（aware UTC）算 since，
但 audit created_at 是 now_taipei_naive()（naive 台北）→ aware/naive 比較 + ~8h 偏移；sibling
get_audit_logs 已正確用 now_taipei_naive()。
"""

from __future__ import annotations

import sqlalchemy as sa
from starlette.requests import Request

from models.audit import AuditLog
from services.audit_high_risk import is_high_risk_event, filter_high_risk
from utils.audit import _extract_session_id_from_request
from utils.auth import create_access_token, decode_token_for_audit
from utils.taipei_time import now_taipei_naive
import api.audit as audit_api

# ── P2-1：session_id 從 cookie 取 ────────────────────────────────────────────


def _req(headers):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/x",
            "headers": headers,
            "client": ("1.2.3.4", 0),
            "query_string": b"",
        }
    )


def test_session_id_extracted_from_cookie():
    token = create_access_token({"user_id": 1, "role": "admin"})
    jti = decode_token_for_audit(token).get("jti")
    assert jti, "access token 應帶 jti"
    req = _req([(b"cookie", f"access_token={token}".encode())])
    assert _extract_session_id_from_request(req) == jti


def test_session_id_still_reads_bearer_header():
    """向下相容：仍支援 Authorization Bearer（Swagger / 舊 client）。"""
    token = create_access_token({"user_id": 2, "role": "admin"})
    jti = decode_token_for_audit(token).get("jti")
    req = _req([(b"authorization", f"Bearer {token}".encode())])
    assert _extract_session_id_from_request(req) == jti


# ── P2-2：role entity 的權限變更算高風險 ─────────────────────────────────────


def test_role_permission_update_is_high_risk():
    """改整個 role 權限集（entity_type='role'）應判為高風險。"""
    assert is_high_risk_event("UPDATE", "更新角色 teacher 權限", "role") is True


def test_user_permission_update_still_high_risk():
    """既有 entity_type='user' 行為不變。"""
    assert is_high_risk_event("UPDATE", "修改使用者 (role: hr → admin)", "user") is True


def test_normal_role_update_without_keyword_not_high_risk():
    """role entity 但 summary 無權限關鍵字 → 不誤報。"""
    assert is_high_risk_event("UPDATE", "更新標籤顏色", "role") is False


def test_filter_high_risk_includes_role_permission_change(test_db_session):
    row = AuditLog(
        action="UPDATE",
        entity_type="role",
        summary="更新角色 teacher 權限（新增 EMPLOYEES_WRITE）",
        username="admin",
        created_at=now_taipei_naive(),
    )
    test_db_session.add(row)
    test_db_session.commit()
    since = now_taipei_naive().replace(year=2000)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id in [r.id for r in results]


# ── P3：時間窗用 naive 台北 ──────────────────────────────────────────────────


def test_get_high_risk_since_is_naive_taipei(test_db_session, monkeypatch):
    """get_high_risk_audits 算的 since 應為 naive（與 audit created_at 一致），非 aware UTC。"""
    captured = {}
    orig = audit_api.filter_high_risk

    def _spy(query, *, since, only_unack=True):
        captured["since"] = since
        return orig(query, since=since, only_unack=only_unack)

    monkeypatch.setattr(audit_api, "filter_high_risk", _spy)
    # 直接呼叫端點函式時 limit 的預設是 FastAPI Query 物件，須明傳。
    audit_api.get_high_risk_audits(
        days=7, unack_only=True, limit=50, current_user={"user_id": 1}
    )
    assert captured["since"].tzinfo is None, "since 應為 naive 台北時間"
