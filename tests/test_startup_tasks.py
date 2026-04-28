"""啟動與維運任務分流測試。"""

import os
import sys

from sqlalchemy import create_engine, inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_init_database_only_initializes_engine_and_session(monkeypatch):
    from models import base

    calls = []

    monkeypatch.setattr(
        base, "get_engine", lambda: calls.append("get_engine") or object()
    )
    monkeypatch.setattr(
        base,
        "get_session_factory",
        lambda: calls.append("get_session_factory") or object(),
    )

    base.init_database()

    assert calls == [
        "get_engine",
        "get_session_factory",
    ]


def test_run_startup_bootstrap_skips_schema_migrations(monkeypatch):
    from startup import bootstrap as bootstrap_module
    import main

    calls = []

    # patch bootstrap_module 上的名稱綁定（非 seed_module）
    monkeypatch.setattr(
        bootstrap_module, "init_database", lambda: calls.append("init_database")
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_job_titles",
        lambda: calls.append("seed_job_titles"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_default_configs",
        lambda: calls.append("seed_default_configs"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_shift_types",
        lambda: calls.append("seed_shift_types"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_default_admin",
        lambda: calls.append("seed_default_admin"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_approval_policies",
        lambda: calls.append("seed_approval_policies"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "seed_activity_settings",
        lambda: calls.append("seed_activity_settings"),
    )
    monkeypatch.setattr(
        main.salary_engine,
        "load_config_from_db",
        lambda: calls.append("load_config_from_db"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_load_line_config",
        lambda _: calls.append("_load_line_config"),
    )

    bootstrap_module.run_startup_bootstrap(main.salary_engine, main.line_service)

    assert calls == [
        "init_database",
        "seed_job_titles",
        "seed_default_configs",
        "seed_shift_types",
        "seed_default_admin",
        "seed_approval_policies",
        "seed_activity_settings",
        "load_config_from_db",
        "_load_line_config",
    ]


def test_run_startup_bootstrap_creates_ivykids_table_for_legacy_db(
    monkeypatch, tmp_path
):
    from startup import bootstrap as bootstrap_module
    import main
    import models.base as base_module
    from api import recruitment as recruitment_api

    db_path = tmp_path / "startup-bootstrap.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = None

    monkeypatch.setattr(bootstrap_module, "init_database", lambda: None)
    monkeypatch.setattr(bootstrap_module, "get_engine", lambda: engine)
    monkeypatch.setattr(bootstrap_module, "migrate_school_year_to_roc", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_class_grades", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_job_titles", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_default_configs", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_shift_types", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_default_admin", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_approval_policies", lambda: None)
    monkeypatch.setattr(bootstrap_module, "seed_activity_settings", lambda: None)
    monkeypatch.setattr(main.salary_engine, "load_config_from_db", lambda: None)
    monkeypatch.setattr(bootstrap_module, "_load_line_config", lambda _: None)
    monkeypatch.setattr(recruitment_api, "normalize_existing_months", lambda: None)

    try:
        bootstrap_module.run_startup_bootstrap(main.salary_engine, main.line_service)

        tables = set(inspect(engine).get_table_names())
        assert "recruitment_ivykids_records" in tables
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def test_run_maintenance_tasks_executes_alembic_and_permission_backfill(monkeypatch):
    from startup import bootstrap as bootstrap_module

    calls = []

    monkeypatch.setattr(
        bootstrap_module,
        "run_alembic_upgrade",
        lambda: calls.append("run_alembic_upgrade"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "migrate_permissions_rw",
        lambda: calls.append("migrate_permissions_rw"),
    )

    bootstrap_module.run_maintenance_tasks()

    assert calls == [
        "run_alembic_upgrade",
        "migrate_permissions_rw",
    ]


class _MockSubprocessResult:
    """模擬 subprocess.run 的回傳；returncode=0 表示成功，避免 _run 拋 RuntimeError。"""

    returncode = 0
    stdout = ""
    stderr = ""


def _patch_alembic_subprocess(monkeypatch, migrations_module, calls):
    """把 shutil.which 與 subprocess.run 換成可記錄呼叫的 mock。

    subprocess.run 簽名接受 **kwargs，避免 commit 3fa99e9e 之後 capture_output=True
    打到只接固定參數的 lambda 而 TypeError。
    """
    monkeypatch.setattr(
        migrations_module,
        "shutil",
        type(
            "MockShutil",
            (),
            {
                "which": staticmethod(
                    lambda name: "/mock/alembic" if name == "alembic" else None
                )
            },
        )(),
    )

    def _fake_run(args, **kwargs):
        calls.append(args)
        return _MockSubprocessResult()

    monkeypatch.setattr(
        migrations_module,
        "subprocess",
        type("MockSubprocess", (), {"run": staticmethod(_fake_run)})(),
    )


def test_run_alembic_upgrade_empty_db_creates_schema_and_stamps_heads(monkeypatch):
    """全新空 DB（無 user table、無 alembic_version）：

    呼叫 Base.metadata.create_all 建出完整 ORM schema，再 alembic stamp heads
    標記已 fully migrated（不再 upgrade），避免 baseline migration 的
    op.alter_column 對著還沒建立的 allowance_types 表跑而 UndefinedTable。
    """
    from startup import migrations as migrations_module

    calls = []
    create_all_args = []

    monkeypatch.setattr(migrations_module, "_detect_alembic_state", lambda: "empty")
    monkeypatch.setattr(
        migrations_module, "get_engine", lambda: "fake_engine_handle"
    )

    class _FakeMetadata:
        @staticmethod
        def create_all(engine):
            create_all_args.append(engine)

    monkeypatch.setattr(
        migrations_module, "Base", type("FakeBase", (), {"metadata": _FakeMetadata})
    )
    _patch_alembic_subprocess(monkeypatch, migrations_module, calls)

    migrations_module.run_alembic_upgrade()

    assert create_all_args == [
        "fake_engine_handle"
    ], "空 DB 必須先呼叫 Base.metadata.create_all() 建立完整 schema"
    assert [list(c[-2:]) for c in calls] == [
        ["stamp", "heads"]
    ], "空 DB 標 heads（不執行 baseline migration）"


def test_run_alembic_upgrade_legacy_schema_stamps_baseline_then_upgrades(monkeypatch):
    """既有 schema 但無 alembic_version（舊部署首次接上 alembic）：

    stamp baseline 後再 upgrade heads；維持原有路徑不變。
    """
    from startup import migrations as migrations_module

    calls = []
    create_all_args = []

    monkeypatch.setattr(
        migrations_module, "_detect_alembic_state", lambda: "needs_baseline"
    )
    monkeypatch.setattr(
        migrations_module, "get_engine", lambda: "fake_engine_handle"
    )

    class _FakeMetadata:
        @staticmethod
        def create_all(engine):
            create_all_args.append(engine)

    monkeypatch.setattr(
        migrations_module, "Base", type("FakeBase", (), {"metadata": _FakeMetadata})
    )
    _patch_alembic_subprocess(monkeypatch, migrations_module, calls)

    migrations_module.run_alembic_upgrade()

    assert create_all_args == [], "既有 schema 不應再次呼叫 create_all"
    assert [list(c[-2:]) for c in calls] == [
        ["stamp", "4ddf3ebad3e8"],
        ["upgrade", "heads"],
    ]


def test_run_alembic_upgrade_versioned_db_only_upgrades(monkeypatch):
    """已版控（alembic_version 存在）：直接 upgrade heads。"""
    from startup import migrations as migrations_module

    calls = []
    create_all_args = []

    monkeypatch.setattr(
        migrations_module, "_detect_alembic_state", lambda: "versioned"
    )
    monkeypatch.setattr(
        migrations_module, "get_engine", lambda: "fake_engine_handle"
    )

    class _FakeMetadata:
        @staticmethod
        def create_all(engine):
            create_all_args.append(engine)

    monkeypatch.setattr(
        migrations_module, "Base", type("FakeBase", (), {"metadata": _FakeMetadata})
    )
    _patch_alembic_subprocess(monkeypatch, migrations_module, calls)

    migrations_module.run_alembic_upgrade()

    assert create_all_args == []
    assert [list(c[-2:]) for c in calls] == [["upgrade", "heads"]]


def test_detect_alembic_state_classifies_three_modes(monkeypatch, tmp_path):
    """_detect_alembic_state 直接針對 SQLite engine 驗三態判斷。"""
    from startup import migrations as migrations_module
    from sqlalchemy import text

    # 1. 空 DB
    empty_engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    monkeypatch.setattr(migrations_module, "get_engine", lambda: empty_engine)
    assert migrations_module._detect_alembic_state() == "empty"

    # 2. 既有 schema、無 alembic_version
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    with legacy_engine.begin() as conn:
        conn.execute(text("CREATE TABLE employees (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(migrations_module, "get_engine", lambda: legacy_engine)
    assert migrations_module._detect_alembic_state() == "needs_baseline"

    # 3. 已版控
    versioned_engine = create_engine(f"sqlite:///{tmp_path / 'versioned.sqlite'}")
    with versioned_engine.begin() as conn:
        conn.execute(text("CREATE TABLE employees (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR)"))
    monkeypatch.setattr(migrations_module, "get_engine", lambda: versioned_engine)
    assert migrations_module._detect_alembic_state() == "versioned"


def test_startup_event_only_runs_bootstrap(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "run_alembic_upgrade", lambda: calls.append("alembic"))
    monkeypatch.setattr(
        main, "run_startup_bootstrap", lambda se, ls: calls.append("bootstrap")
    )

    main.on_startup()

    assert "alembic" in calls
    assert "bootstrap" in calls
