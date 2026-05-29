"""tests/test_audit_forensic.py — Ch1 AuditLog forensic readiness."""

import hashlib
import secrets

from models.audit import AuditLog


def test_audit_log_model_has_ua_hash_and_session_id():
    """AuditLog ORM 模型必須暴露 user_agent_hash 與 session_id 兩欄。"""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert "user_agent_hash" in cols, f"missing user_agent_hash, got {cols}"
    assert "session_id" in cols, f"missing session_id, got {cols}"


def test_audit_logs_table_has_session_id_index():
    """session_id 必須有 index（forensic 查詢 'find all activity of same session')."""
    indexes = list(AuditLog.__table__.indexes)
    indexed_columns = {col.name for idx in indexes for col in idx.columns}
    assert (
        "session_id" in indexed_columns
    ), f"no index covering session_id column; indexed columns: {indexed_columns}"


def _build_request_with_token(token: str, ua: str | None = None):
    """Helper: 造一個帶 Authorization header 的假 Request。"""
    from starlette.requests import Request

    headers = [(b"authorization", f"Bearer {token}".encode())]
    if ua:
        headers.append((b"user-agent", ua.encode()))
    scope = {"type": "http", "headers": headers}
    return Request(scope)


def test_extract_session_id_returns_jti_from_token():
    from utils.audit import _extract_session_id_from_request
    from utils.auth import create_access_token

    explicit_jti = secrets.token_urlsafe(16)
    token = create_access_token({"user_id": 1, "name": "alice", "jti": explicit_jti})
    request = _build_request_with_token(token)

    session_id = _extract_session_id_from_request(request)
    assert session_id == explicit_jti


def test_extract_session_id_returns_none_when_no_header():
    from utils.audit import _extract_session_id_from_request
    from starlette.requests import Request

    request = Request({"type": "http", "headers": []})
    assert _extract_session_id_from_request(request) is None


def test_extract_session_id_returns_none_on_bad_token():
    from utils.audit import _extract_session_id_from_request

    request = _build_request_with_token("not-a-jwt-blah")
    assert _extract_session_id_from_request(request) is None


def test_compute_ua_hash_returns_sha256_prefix():
    from utils.audit import _compute_ua_hash

    request = _build_request_with_token("dummy-token", ua="TestUA/1.0")
    expected = hashlib.sha256(b"TestUA/1.0").hexdigest()[:32]
    assert _compute_ua_hash(request) == expected


def test_compute_ua_hash_returns_none_when_no_header():
    from utils.audit import _compute_ua_hash
    from starlette.requests import Request

    request = Request({"type": "http", "headers": []})
    assert _compute_ua_hash(request) is None


def test_write_explicit_audit_persists_ua_hash_and_session_id(test_db_session):
    """呼叫 write_explicit_audit 後 audit_logs row 含 ua_hash + session_id。"""
    from models.audit import AuditLog
    from utils.audit import write_explicit_audit
    from utils.auth import create_access_token

    explicit_jti = secrets.token_urlsafe(16)
    token = create_access_token({"user_id": 1, "name": "alice", "jti": explicit_jti})
    request = _build_request_with_token(token, ua="TestUA/1.0 (purpose=forensic-test)")

    write_explicit_audit(
        request,
        action="READ",
        entity_type="student",
        summary="forensic test write",
        entity_id="42",
        dedup=False,
    )

    # write_explicit_audit 走 fire-and-forget；無 event loop 時 fallback 同步寫入。
    row = (
        test_db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "student", AuditLog.entity_id == "42")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.session_id == explicit_jti
    expected_ua_hash = hashlib.sha256(
        b"TestUA/1.0 (purpose=forensic-test)"
    ).hexdigest()[:32]
    assert row.user_agent_hash == expected_ua_hash


# ──────────────────────────────────────────────────────────────────────────
# PR-A Tasks 4-6 — 為什麼這裡沒有對應的 smoke test
# ──────────────────────────────────────────────────────────────────────────
#
# Plan 原排 Tasks 4-6 為 contact_book / growth_reports / parent_downloads
# 三個 GET endpoint 補 write_explicit_audit。Pre-flight 檢查發現這三條路徑
# 已在 main 既有 commit 補上：
#
#   - api/parent_portal/contact_book.py:315   (get_detail, dedup=True)
#   - api/parent_portal/growth_reports.py:128 (parent_download_report, dedup=False)
#   - api/parent_portal/parent_downloads.py:192 (download_parent_portfolio, dedup=False)
#
# Task 2 的 test_write_explicit_audit_persists_ua_hash_and_session_id 已證實
# `write_explicit_audit()` 會把 user_agent_hash / session_id / ip_address 等
# forensic 欄位寫入 audit_logs；本 PR 加的 2 欄因此自動覆蓋上述三個既有
# callsite，不需要再各別寫一次 TestClient 等價測試（會徒增 fixture 重複而
# 無實質 coverage 增益）。
#
# 若日後需把 forensic 欄位驗證收緊到「每個 endpoint」級別，建議用 codegen
# 或 grep-gate 在 CI 層擋；不要在 tests/ 層每加一個 endpoint 就複製一份
# 同樣 shape 的 TestClient 測試。
#
# Task 3 (medication) 有寫 integration test 是因為它是「新加 audit 呼叫」，
# 必須驗證該 endpoint 實際上會留 audit 軌跡；Tasks 4-6 屬於「機制驗證」而非
# 「新增 callsite 驗證」，故僅留此說明。
