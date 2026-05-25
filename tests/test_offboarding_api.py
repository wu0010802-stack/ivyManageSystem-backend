"""驗證 router 註冊 + endpoint path 存在（後續 task 補完整 case）。

Task 8 smoke test：
- test_router_registered: api.offboarding 模組可 import，router prefix=/offboarding，
  可掛入 FastAPI app，且 main.py 有對應 include_router 呼叫。
- Task 9+ 補完整 endpoint case（此時才有路徑可 HTTP 驗證）。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.offboarding import router as offboarding_router
from models.database import Base


@pytest.fixture
def client(tmp_path):
    """SQLite in-memory + TestClient（auth + offboarding router）。"""
    db_path = tmp_path / "offboarding-smoke.sqlite"
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
    app.include_router(offboarding_router, prefix="/api")

    with TestClient(app) as c:
        yield c

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_router_registered(client):
    """router 已掛載：模組可 import，router prefix 正確，app 建立無異常。

    Task 8 只建 router 殼（無 endpoint），因此不做 HTTP path 存在性驗證。
    HTTP endpoint 驗證由 Task 9+ 補入（test_preview_* / test_process_* 等）。

    驗證三件事：
    1. `from api.offboarding import router` 無 ImportError（fixture 的 import 已驗證）
    2. router.prefix == "/offboarding"
    3. client fixture 可正常建立（app.include_router 不拋例外）
    """
    # 1 & 2: router 可 import 且 prefix 正確
    assert (
        offboarding_router.prefix == "/offboarding"
    ), f"router prefix 錯誤：{offboarding_router.prefix!r}，期望 '/offboarding'"

    # 3: main.py 有正確 include offboarding_router（grep 驗證）
    import importlib.util
    import pathlib

    main_py = pathlib.Path(__file__).parent.parent / "main.py"
    content = main_py.read_text()
    assert (
        "from api.offboarding import router as offboarding_router" in content
    ), "main.py 缺少 offboarding_router import"
    assert (
        "app.include_router(offboarding_router" in content
    ), "main.py 缺少 app.include_router(offboarding_router...)"

    # 4: client fixture 正常建立（proxy：GET /docs 應 200，確認 app 正常啟動）
    resp = client.get("/docs")
    assert resp.status_code == 200, f"app 啟動異常，/docs 回 {resp.status_code}"
