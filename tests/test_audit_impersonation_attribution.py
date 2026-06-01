import pytest
from models.audit import AuditLog
from starlette.requests import Request
from utils.auth import create_access_token
from utils.audit import _extract_impersonation_from_header


def test_auditlog_has_impersonation_columns():
    cols = AuditLog.__table__.columns.keys()
    assert "impersonated_by" in cols
    assert "impersonated_by_name" in cols


def _req_with_cookie(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"access_token={token}".encode())],
    }
    return Request(scope)


def test_extract_impersonation_from_token():
    token = create_access_token(
        {
            "user_id": 5,
            "employee_id": 5,
            "role": "teacher",
            "name": "老師A",
            "impersonated_by": 1,
            "impersonated_by_name": "王小明",
            "impersonation_mode": "write",
        }
    )
    by, name = _extract_impersonation_from_header(_req_with_cookie(token))
    assert by == 1
    assert name == "王小明"


def test_extract_impersonation_none_for_normal_token():
    token = create_access_token(
        {"user_id": 5, "employee_id": 5, "role": "teacher", "name": "老師A"}
    )
    by, name = _extract_impersonation_from_header(_req_with_cookie(token))
    assert by is None
    assert name is None


def test_write_under_impersonation_stamps_admin(test_db_session):
    """write-mode 模擬 token → AuditLog row 帶 impersonated_by == admin user_id。

    做法：直接呼叫 write_audit_in_session（同交易同步寫入），繞過
    AuditMiddleware fire-and-forget 背景 thread 的競態問題。
    只要證明「impersonation token → 持久化 AuditLog row 帶 admin 歸屬」即可。

    token claims：
      user_id=10 / name="老師甲" （AuditLog.user_id / username）
      impersonated_by=1 / impersonated_by_name="系統管理員"（AuditLog 兩個 impersonation 欄位）
      impersonation_mode="write"（write 模式；唯讀守衛不攔 write）
    """
    from utils.audit import write_audit_in_session

    admin_user_id = 1
    admin_name = "系統管理員"
    teacher_user_id = 10

    token = create_access_token(
        {
            "user_id": teacher_user_id,
            "employee_id": 10,
            "role": "teacher",
            "name": "老師甲",
            "impersonated_by": admin_user_id,
            "impersonated_by_name": admin_name,
            "impersonation_mode": "write",
        }
    )

    # 建立帶有 write-mode impersonation cookie 的最小 Starlette Request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/portal/my-overtimes",
        "query_string": b"",
        "headers": [(b"cookie", f"access_token={token}".encode())],
    }
    request = Request(scope)

    # 在測試 session 內同步寫入 AuditLog（不等背景 thread）
    write_audit_in_session(
        test_db_session,
        request,
        action="CREATE",
        entity_type="overtime",
        summary="新增加班（模擬寫入測試）",
        entity_id="1",
    )
    test_db_session.commit()

    # 查最新一筆 overtime audit row
    row = (
        test_db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "overtime")
        .order_by(AuditLog.id.desc())
        .first()
    )

    assert row is not None, "AuditLog row 未寫入"
    assert row.user_id == teacher_user_id, f"user_id 應為老師，got {row.user_id}"
    assert (
        row.impersonated_by == admin_user_id
    ), f"impersonated_by 應為 admin user_id={admin_user_id}，got {row.impersonated_by}"
    assert (
        row.impersonated_by_name == admin_name
    ), f"impersonated_by_name 應為 '{admin_name}'，got {row.impersonated_by_name}"
