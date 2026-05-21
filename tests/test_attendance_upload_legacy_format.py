"""回歸測試：考勤上傳的「舊格式」分支因 storage 遷移意外 NameError。

Bug Sweep Round 4 (2026-05-14)：
commit fd385e90「匯入改用 StorageBackend + BytesIO」把 `file_path = _upload_dir() / ...`
換成 `backend.save(...) + pd.read_excel(io.BytesIO(content))`，但同檔 api/attendance/
upload.py 舊格式 else 分支（line 575-579）仍呼叫 `parse_attendance_file(file_path)`。
任何缺「上班時間/下班時間」欄位的 Excel 會 fall through 觸發 NameError，被外層
`except Exception` 包成 `raise_safe_500`，admin 端看到「解析失敗」整批匯入失敗。

回歸測試：上傳一個只含「姓名/時間」（datetime 格式）的舊式 Excel，預期：
- 不會收到 500「解析失敗」
- parse_attendance_file 被呼叫且引數為 BytesIO（不是已刪除的 file_path）
"""

import io
import os
import sys
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.database import Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def upload_client(tmp_path):
    db_path = tmp_path / "att-upload-legacy.sqlite"
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
    app.include_router(auth_router)
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin(sf):
    with sf() as s:
        emp = Employee(
            employee_id="E_admin_legacy",
            name="管理員",
            base_salary=30000,
            employee_type="regular",
            is_active=True,
        )
        s.add(emp)
        s.flush()
        user = User(
            username="admin_legacy_upload",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,  # 純 admin 走 unrestricted 路徑
            is_active=True,
            must_change_password=False,
        )
        s.add(user)
        s.commit()


def _login(client: TestClient, username: str = "admin_legacy_upload"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


def _legacy_format_xlsx_bytes() -> bytes:
    """僅含「姓名/時間」欄（無「上班時間/下班時間」）的舊格式 Excel。"""
    df = pd.DataFrame(
        {
            "姓名": ["張小明"],
            "時間": ["2026-02-02 08:00:00"],
        }
    )
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


class TestLegacyFormatUploadNoNameError:
    """B1 (P1)：缺「上班時間/下班時間」的 Excel 不該因 file_path 未定義而 500。"""

    def test_legacy_format_upload_does_not_raise_nameerror(self, upload_client):
        client, sf = upload_client
        _seed_admin(sf)
        assert _login(client).status_code == 200

        captured = {}

        def _fake_parser(file_or_buffer, employee_schedules=None, **kw):
            captured["arg"] = file_or_buffer
            return ({}, pd.DataFrame(), pd.DataFrame())

        with patch(
            "services.attendance_parser.parse_attendance_file",
            side_effect=_fake_parser,
        ):
            res = client.post(
                "/api/attendance/upload",
                files={
                    "file": (
                        "legacy.xlsx",
                        _legacy_format_xlsx_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )

        # 修補前：500 + "解析失敗"（NameError: file_path is not defined）
        # 修補後：200，且 parse_attendance_file 收到 BytesIO 而非已刪除的 file_path
        assert res.status_code == 200, res.text
        assert "arg" in captured, "舊格式分支沒走到 parse_attendance_file"
        assert isinstance(
            captured["arg"], io.BytesIO
        ), f"舊格式分支應該餵 BytesIO 進 parser，實際收到 {type(captured['arg'])!r}"
