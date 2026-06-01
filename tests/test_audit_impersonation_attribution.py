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


@pytest.mark.xfail(
    reason="待 Task 4/5：需 impersonate 端點簽發 token + 唯讀守衛放行 write"
)
def test_write_under_impersonation_stamps_admin():
    # 用 write-mode impersonation token 打「會被 audit 的」portal 寫入端點
    # （必須是 ENTITY_PATTERNS 內有列的路徑，例如 POST /api/portal/my-overtimes，
    #  否則 _parse_entity_type 回 None → middleware 短路 → 無 audit row）。
    # 查最新 AuditLog row：user_id==老師、impersonated_by==admin、impersonated_by_name==admin名。
    raise NotImplementedError
