import sys
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 讓 tests 可以 import backend 模組
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── SQLite 相容性修補（必須在所有模型 import 前執行）─────────────────────────
# models/__init__.py 在 SalaryEngine 匯入鏈中被觸發，導致 models/appraisal.py 先被
# 載入；因此修補必須在 conftest.py 最頂端（任何 import 前）執行。
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON

# 1. JSONB → JSON（appraisal_events.attachments 欄位）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


# 2. BigInteger → Integer（讓 SQLite 主鍵自動遞增）
class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]
# ─────────────────────────────────────────────────────────────────────────────

from services.salary_engine import SalaryEngine
from services.attendance_parser import AttendanceResult
import models.base as base_module
from models.database import Base

# 載入考核系統 fixtures — M1 重構：暫時停用，待 M2 重寫 conftest_appraisal.py
# pytest_plugins = ["tests.conftest_appraisal"]


@pytest.fixture
def engine():
    """SalaryEngine 實例（不從 DB 載入）"""
    return SalaryEngine(load_from_db=False)


@pytest.fixture
def sample_attendance():
    """標準考勤資料"""
    return AttendanceResult(
        employee_name="測試員工",
        total_days=22,
        normal_days=20,
        late_count=2,
        early_leave_count=1,
        missing_punch_in_count=1,
        missing_punch_out_count=0,
        total_late_minutes=45,
        total_early_minutes=15,
        details=[],
    )


@pytest.fixture
def sample_employee():
    """正職員工資料"""
    return {
        "employee_id": "E001",
        "name": "王小明",
        "title": "幼兒園教師",
        "position": "幼兒園教師",
        "employee_type": "regular",
        "base_salary": 30000,
        "hourly_rate": 0,
        "insurance_salary": 30000,
        "dependents": 0,
        "hire_date": "2025-01-01",
    }


@pytest.fixture
def sample_classroom_context():
    """班級上下文（班導，大班）"""
    return {
        "role": "head_teacher",
        "grade_name": "大班",
        "current_enrollment": 27,
        "has_assistant": True,
        "is_shared_assistant": False,
    }


class QueryCounter:
    """計數 SQLAlchemy engine 發出的查詢次數，用於 N+1 回歸測試。

    使用方式：
        def test_list_avoids_n_plus_one(counter, session):
            with counter:
                result = list_employees_with_classroom(session)
            assert counter.count <= 2, f"expected ≤ 2 queries, got {counter.count}"
    """

    def __init__(self, engine):
        self.engine = engine
        self.count = 0
        self.statements: list[str] = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1
        self.statements.append(statement)

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, exc_type, exc, tb):
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.fixture
def query_counter():
    """回傳 QueryCounter 工廠（call with engine）。

    因為測試可能使用不同 engine（in-memory SQLite vs staging PG），
    這個 fixture 讓測試自己傳入 engine。
    """
    return QueryCounter


@pytest.fixture
def test_db_session(tmp_path):
    """共用 SQLite in-memory 測試 DB fixture。

    建立 SQLite 引擎、swap 全域 engine/_SessionFactory，
    以 Base.metadata.create_all 建立全部 ORM 表，
    yield 測試用 session，測試結束後還原全域狀態。

    適用：需要實際 DB 操作的 model / CRUD 測試。
    """
    db_path = tmp_path / "test.sqlite"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    test_session_factory = sessionmaker(bind=test_engine)

    # 把 dispatch 的 after_commit / after_rollback 重綁到 test factory
    # 不重綁的話 hook 會綁在 production factory，test commit 不觸發
    try:
        from services.notification import dispatch as _dispatch

        _dispatch.install_session_hooks(test_session_factory)
    except ImportError:
        pass  # dispatch 模組未建（Task 6 之前；現在已不可能但保留 graceful fallback）

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = test_engine
    base_module._SessionFactory = test_session_factory

    Base.metadata.create_all(test_engine)

    session = test_session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    try:
        from services.notification import dispatch as _dispatch

        _dispatch._HOOKS_INSTALLED.discard(test_session_factory)
    except ImportError:
        pass
    test_engine.dispose()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每個 test 進場前 + 收尾後都清 Settings lru_cache。

    進場 reset 保證 test 從乾淨 cache 開始（避免上個 test 的 cache 殘留 + monkeypatch
    在進入 test 函式時設好 env，需要 reset 才能讓 settings 看到新值）。
    收尾 reset 避免污染後續 test。
    """
    from config import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True, scope="session")
def _csrf_testclient_origin_allowlist():
    """TestClient 預設 Origin http://testserver 注入 cors_origins。

    TestClient 預設 Origin 是 http://testserver，dev fallback 中不含此值。
    待 Task 2 落 CSRFOriginCheckMiddleware 檢查 Origin header 必在 cors_origins
    白名單後，將擋下 1433 mutation test lines 跨 189 file。預先注入此 fixture
    避免 Task 2 落地時出現大規模短暫紅燈。

    此 commit 落地時 middleware 尚未存在，fixture 純預設定 cors_origins 不影響
    既有行為（baseline 5492/0 不變）。

    Spec: docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md §4
    Plan: docs/superpowers/plans/2026-05-28-csrf-origin-middleware.md Task 1
    """
    from config import settings

    original = list(settings.network.cors_origins or [])
    if "http://testserver" not in original:
        settings.network.cors_origins = original + ["http://testserver"]
    yield
    settings.network.cors_origins = original
