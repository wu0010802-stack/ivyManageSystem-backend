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


def test_run_alembic_upgrade_stamps_baseline_for_legacy_schema(monkeypatch):
    from startup import migrations as migrations_module

    calls = []

    monkeypatch.setattr(migrations_module, "needs_alembic_baseline_stamp", lambda: True)
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
    monkeypatch.setattr(
        migrations_module,
        "subprocess",
        type(
            "MockSubprocess",
            (),
            {
                "run": staticmethod(
                    lambda args, cwd, check: calls.append((args, cwd, check))
                )
            },
        )(),
    )

    migrations_module.run_alembic_upgrade()

    assert [call[0][-2:] for call in calls] == [
        ["stamp", "4ddf3ebad3e8"],
        ["upgrade", "heads"],
    ]


def test_run_alembic_upgrade_skips_stamp_when_schema_is_versioned(monkeypatch):
    from startup import migrations as migrations_module

    calls = []

    monkeypatch.setattr(
        migrations_module, "needs_alembic_baseline_stamp", lambda: False
    )
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
    monkeypatch.setattr(
        migrations_module,
        "subprocess",
        type(
            "MockSubprocess",
            (),
            {
                "run": staticmethod(
                    lambda args, cwd, check: calls.append((args, cwd, check))
                )
            },
        )(),
    )

    migrations_module.run_alembic_upgrade()

    assert [call[0][-2:] for call in calls] == [
        ["upgrade", "heads"],
    ]


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
