"""DB 基礎建設 schema-drift 偵測（設計審查 2026-06-25 主題 B）。"""

from __future__ import annotations

import re
from pathlib import Path

from startup.infra_check import (
    CRITICAL_DB_FUNCTIONS,
    CRITICAL_DB_ROLES,
    CRITICAL_DB_TRIGGERS,
    CRITICAL_PARTIAL_UNIQUE_INDEXES,
    check_db_infra_present,
    compute_missing_infra,
)

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _migrations_blob() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _VERSIONS.glob("*.py"))


# ── 純函式 ──────────────────────────────────────────────────────────────


def test_compute_missing_all_present():
    missing = compute_missing_infra(
        CRITICAL_DB_FUNCTIONS,
        CRITICAL_DB_TRIGGERS,
        CRITICAL_DB_ROLES,
        CRITICAL_PARTIAL_UNIQUE_INDEXES,
        tables_policy_without_force=[],
    )
    assert missing == []


def test_compute_missing_reports_gaps():
    missing = compute_missing_infra(
        found_funcs=[f for f in CRITICAL_DB_FUNCTIONS if f != "parent_owns_attachment"],
        found_triggers=CRITICAL_DB_TRIGGERS,
        found_roles=[r for r in CRITICAL_DB_ROLES if r != "ivy_parent_role"],
        found_indexes=CRITICAL_PARTIAL_UNIQUE_INDEXES,
        tables_policy_without_force=["student_medication_logs"],
    )
    assert "function:parent_owns_attachment" in missing
    assert "role:ivy_parent_role" in missing
    assert "rls_not_forced:student_medication_logs" in missing
    assert len(missing) == 3


# ── 假 PG session（驗查詢→告警路徑）──────────────────────────────────────


class _FakeScalars:
    def __init__(self, vals):
        self._vals = vals

    def all(self):
        return self._vals


class _FakeResult:
    def __init__(self, vals):
        self._vals = vals

    def scalars(self):
        return _FakeScalars(self._vals)


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _FakePgSession:
    bind = _Bind()

    def __init__(self, results_by_marker):
        self._r = results_by_marker

    def execute(self, stmt, params=None):
        q = str(stmt)
        for marker, vals in self._r.items():
            if marker in q:
                return _FakeResult(vals)
        return _FakeResult([])


def test_check_present_all_ok_no_alert(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append(msg),
    )
    sess = _FakePgSession(
        {
            "pg_proc": list(CRITICAL_DB_FUNCTIONS),
            "pg_trigger": list(CRITICAL_DB_TRIGGERS),
            "pg_roles": list(CRITICAL_DB_ROLES),
            "pg_indexes": list(CRITICAL_PARTIAL_UNIQUE_INDEXES),
            "pg_policies": [],  # 無「有 policy 卻沒 FORCE」的表
        }
    )
    assert check_db_infra_present(sess) == []
    assert captured == []


def test_check_missing_pushes_sentry(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        "utils.sentry_init.capture_message",
        lambda msg, level="warning": captured.append(msg),
    )
    sess = _FakePgSession({})  # fresh create_all+stamp：所有查詢回 []
    missing = check_db_infra_present(sess)
    assert len(missing) == (
        len(CRITICAL_DB_FUNCTIONS)
        + len(CRITICAL_DB_TRIGGERS)
        + len(CRITICAL_DB_ROLES)
        + len(CRITICAL_PARTIAL_UNIQUE_INDEXES)
    )
    assert len(captured) == 1
    assert "基礎建設缺漏" in captured[0]


def test_check_detects_policy_without_force(monkeypatch):
    """有 parent_% policy 卻沒 FORCE RLS（owner 可繞過）→ 須報 rls_not_forced。"""
    monkeypatch.setattr(
        "utils.sentry_init.capture_message", lambda msg, level="warning": None
    )
    sess = _FakePgSession(
        {
            "pg_proc": list(CRITICAL_DB_FUNCTIONS),
            "pg_trigger": list(CRITICAL_DB_TRIGGERS),
            "pg_roles": list(CRITICAL_DB_ROLES),
            "pg_indexes": list(CRITICAL_PARTIAL_UNIQUE_INDEXES),
            "pg_policies": ["students", "guardians"],  # 這兩表有 policy 卻沒 FORCE
        }
    )
    missing = check_db_infra_present(sess)
    assert missing == ["rls_not_forced:guardians", "rls_not_forced:students"]


def test_non_pg_returns_empty():
    """SQLite/dev（非 PG）→ 不偵測、回 []、不阻擋啟動。"""

    class _SqliteDialect:
        name = "sqlite"

    class _SqliteBind:
        dialect = _SqliteDialect()

    class _SqliteSession:
        bind = _SqliteBind()

        def execute(self, *a, **k):
            raise AssertionError("非 PG 不應查 pg_catalog")

    assert check_db_infra_present(_SqliteSession()) == []


# ── 防腐：掃 migration 反推，漏登 / 過時即紅（根治此前漏登 24 policy 的成因）──────


def test_registered_names_exist_in_migrations():
    """每個登記的 function/trigger/role/index 名都必須真實出現在某 migration
    （否則清單過時 → 永遠誤報缺漏）。"""
    blob = _migrations_blob()
    for name in (
        *CRITICAL_DB_FUNCTIONS,
        *CRITICAL_DB_TRIGGERS,
        *CRITICAL_DB_ROLES,
        *CRITICAL_PARTIAL_UNIQUE_INDEXES,
    ):
        assert name in blob, f"偵測清單物件 {name} 未出現在任何 migration（過時？）"


def test_no_unregistered_security_definer_function_or_immutable_trigger():
    """掃 migration 的 CREATE OR REPLACE FUNCTION 與 immutability TRIGGER，凡未登記者即
    fail——根治此前『新增 op.execute infra 卻沒進偵測清單 → 偵測有盲區』的漂移成因。
    （RLS policy 為 f-string 迴圈產生無法純文字列舉，改以 ivy_parent_role+parent_owns_attachment
    精確物件 + 結構性 FORCE 檢查涵蓋，故不在此 assert policy 名。）"""
    blob = _migrations_blob()
    # CREATE OR REPLACE FUNCTION <name>（限 ASCII 識別字，避開中文註解誤匹配）
    fn_names = set(re.findall(r"CREATE OR REPLACE FUNCTION\s+([a-z_][a-z0-9_]*)", blob))
    unlisted_fns = fn_names - set(CRITICAL_DB_FUNCTIONS)
    assert not unlisted_fns, (
        f"以下 migration DB function 未登記於 CRITICAL_DB_FUNCTIONS（偵測盲區）："
        f"{sorted(unlisted_fns)}；請補登記（或確認非關鍵）。"
    )
    trg_names = set(re.findall(r"CREATE TRIGGER\s+([a-z_][a-z0-9_]*)", blob))
    unlisted_trgs = trg_names - set(CRITICAL_DB_TRIGGERS)
    assert not unlisted_trgs, (
        f"以下 migration trigger 未登記於 CRITICAL_DB_TRIGGERS（偵測盲區）："
        f"{sorted(unlisted_trgs)}；請補登記。"
    )
