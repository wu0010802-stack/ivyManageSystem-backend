"""啟動與維運任務分流測試。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_init_database_only_initializes_engine_and_session(monkeypatch):
    from models import base

    calls = []

    monkeypatch.setattr(base, "get_engine", lambda: calls.append("get_engine") or object())
    monkeypatch.setattr(base, "get_session_factory", lambda: calls.append("get_session_factory") or object())

    base.init_database()

    assert calls == [
        "get_engine",
        "get_session_factory",
    ]


def test_run_startup_bootstrap_skips_schema_migrations(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "init_database", lambda: calls.append("init_database"))
    monkeypatch.setattr(main, "seed_job_titles", lambda: calls.append("seed_job_titles"))
    monkeypatch.setattr(main, "seed_default_configs", lambda: calls.append("seed_default_configs"))
    monkeypatch.setattr(main, "seed_shift_types", lambda: calls.append("seed_shift_types"))
    monkeypatch.setattr(main, "seed_default_admin", lambda: calls.append("seed_default_admin"))
    monkeypatch.setattr(main, "seed_approval_policies", lambda: calls.append("seed_approval_policies"))
    monkeypatch.setattr(main, "seed_activity_settings", lambda: calls.append("seed_activity_settings"))
    monkeypatch.setattr(main.salary_engine, "load_config_from_db", lambda: calls.append("load_config_from_db"))
    monkeypatch.setattr(main, "_load_line_config", lambda: calls.append("_load_line_config"))

    main.run_startup_bootstrap()

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


def test_run_maintenance_tasks_executes_alembic_and_permission_backfill(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "run_alembic_upgrade", lambda: calls.append("run_alembic_upgrade"))
    monkeypatch.setattr(main, "migrate_permissions_rw", lambda: calls.append("migrate_permissions_rw"))

    main.run_maintenance_tasks()

    assert calls == [
        "run_alembic_upgrade",
        "migrate_permissions_rw",
    ]


def test_run_alembic_upgrade_stamps_baseline_for_legacy_schema(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "needs_alembic_baseline_stamp", lambda: True)
    monkeypatch.setattr(main.shutil, "which", lambda name: "/mock/alembic" if name == "alembic" else None)
    monkeypatch.setattr(main.subprocess, "run", lambda args, cwd, check: calls.append((args, cwd, check)))

    main.run_alembic_upgrade()

    assert [call[0][-2:] for call in calls] == [
        ["stamp", "4ddf3ebad3e8"],
        ["upgrade", "head"],
    ]


def test_run_alembic_upgrade_skips_stamp_when_schema_is_versioned(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "needs_alembic_baseline_stamp", lambda: False)
    monkeypatch.setattr(main.shutil, "which", lambda name: "/mock/alembic" if name == "alembic" else None)
    monkeypatch.setattr(main.subprocess, "run", lambda args, cwd, check: calls.append((args, cwd, check)))

    main.run_alembic_upgrade()

    assert [call[0][-2:] for call in calls] == [
        ["upgrade", "head"],
    ]


def test_startup_event_only_runs_bootstrap(monkeypatch):
    import main

    calls = []

    monkeypatch.setattr(main, "run_startup_bootstrap", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(main, "run_maintenance_tasks", lambda: calls.append("maintenance"))

    main.on_startup()

    assert calls == ["bootstrap"]
