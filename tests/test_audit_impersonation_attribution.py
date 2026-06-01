import pytest
import models.base as base_module
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.audit import AuditLog
from models.database import Base, Guardian
from models.auth import User
from starlette.requests import Request
from utils.auth import create_access_token, hash_password
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


# ─── FIX 1 端點整合測試 ───────────────────────────────────────────────────────


@pytest.fixture
def binding_client(tmp_path):
    """建立 in-memory SQLite + TestClient，掛 guardians admin router。

    走 TestClient.post() 才能真正執行 create_binding_code 內部的
    inline AuditLog(...) 建構（而非繞過它的 write_audit_in_session shortcut）。
    """
    from api.parent_portal import admin_router
    from api.auth import _account_failures, _ip_attempts

    db_path = tmp_path / "guardian-audit-test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(admin_router)

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_binding_code_audit_stamps_impersonation(binding_client):
    """POST /api/guardians/{id}/binding-code 以 write-mode 模擬 token 呼叫時，
    AuditLog 的 impersonated_by / impersonated_by_name 應帶 admin 身份，
    而非 None（P1 fix 驗證）。
    """
    client, session_factory = binding_client

    admin_user_id = 42  # 虛構 admin user_id 寫入 token claim
    admin_name = "王小明"

    # 建立 hr user（role=hr，有 GUARDIANS_WRITE）
    with session_factory() as session:
        hr_user = User(
            username="hr_impersonated",
            password_hash=hash_password("Pass1234!"),
            role="hr",
            permission_names=["GUARDIANS_WRITE"],
            is_active=True,
            must_change_password=False,
            token_version=0,
        )
        session.add(hr_user)
        session.flush()
        hr_user_id = hr_user.id

        # 建立 guardian（需要 student_id → 先建學生）
        from models.classroom import Student

        student = Student(
            student_id="S9901",
            name="測試學生",
            lifecycle_status="active",
            is_active=True,
        )
        session.add(student)
        session.flush()

        guardian = Guardian(
            student_id=student.id,
            name="測試家長",
            relation="母親",
        )
        session.add(guardian)
        session.flush()
        guardian_id = guardian.id
        session.commit()

    # 建立帶 impersonation claim 的 write-mode token（模擬 admin 冒充 hr）
    token = create_access_token(
        {
            "user_id": hr_user_id,
            "employee_id": None,
            "role": "hr",
            "name": "HR 甲",
            "permission_names": ["GUARDIANS_WRITE"],
            "token_version": 0,
            "impersonated_by": admin_user_id,
            "impersonated_by_name": admin_name,
            "impersonation_mode": "write",
        }
    )

    resp = client.post(
        f"/api/guardians/{guardian_id}/binding-code",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    # 查 AuditLog：inline AuditLog(...) 在 session.commit() 前同步寫入
    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.entity_type == "guardian_binding")
            .order_by(AuditLog.id.desc())
            .first()
        )
    assert row is not None, "AuditLog row 未寫入"
    assert (
        row.impersonated_by == admin_user_id
    ), f"impersonated_by 應為 {admin_user_id}，got {row.impersonated_by}"
    assert (
        row.impersonated_by_name == admin_name
    ), f"impersonated_by_name 應為 '{admin_name}'，got {row.impersonated_by_name}"


def test_binding_code_audit_no_impersonation_leaves_none(binding_client):
    """一般（非模擬）token 呼叫時，AuditLog 的 impersonated_by 應為 None。"""
    client, session_factory = binding_client

    with session_factory() as session:
        hr_user2 = User(
            username="hr_normal",
            password_hash=hash_password("Pass1234!"),
            role="hr",
            permission_names=["GUARDIANS_WRITE"],
            is_active=True,
            must_change_password=False,
            token_version=0,
        )
        session.add(hr_user2)
        session.flush()
        hr_user2_id = hr_user2.id

        from models.classroom import Student

        student2 = Student(
            student_id="S9902",
            name="測試學生2",
            lifecycle_status="active",
            is_active=True,
        )
        session.add(student2)
        session.flush()

        guardian2 = Guardian(
            student_id=student2.id,
            name="測試家長2",
            relation="父親",
        )
        session.add(guardian2)
        session.flush()
        guardian2_id = guardian2.id
        session.commit()

    # 一般登入 token（無 impersonation claim）
    normal_token = create_access_token(
        {
            "user_id": hr_user2_id,
            "role": "hr",
            "name": "HR 乙",
            "permission_names": ["GUARDIANS_WRITE"],
            "token_version": 0,
        }
    )

    resp = client.post(
        f"/api/guardians/{guardian2_id}/binding-code",
        cookies={"access_token": normal_token},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"

    with session_factory() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.entity_type == "guardian_binding")
            .order_by(AuditLog.id.desc())
            .first()
        )
    assert row is not None, "AuditLog row 未寫入"
    assert (
        row.impersonated_by is None
    ), f"非模擬呼叫 impersonated_by 應為 None，got {row.impersonated_by}"
