"""wipe.tables_to_wipe() 的單元測試(純 metadata,不連 DB)。"""

from __future__ import annotations

from scripts.seedgen.wipe import tables_to_wipe


def test_includes_core_business_tables():
    tables = tables_to_wipe()
    for name in [
        "students",
        "employees",
        "salary_records",
        "attendances",
        "classrooms",
    ]:
        assert name in tables


def test_excludes_preserve_set():
    tables = tables_to_wipe()
    for name in ["alembic_version", "permission_definitions", "roles"]:
        assert name not in tables


def test_excludes_skip_substrings():
    tables = tables_to_wipe()
    assert "jwt_blocklist" not in tables
    assert "rate_limit_buckets" not in tables
    assert not any(t.endswith("_refresh_tokens") for t in tables)
    assert not any(t.endswith("_cache") for t in tables)
    assert "scheduler_heartbeats" not in tables


def test_all_from_metadata_tables():
    import models.database  # noqa: F401
    from models.base import Base

    # 用 metadata.tables(dict)取全表名,避免 sorted_tables 對 employees↔
    # classrooms FK 環噴 SAWarning;對「是否為 metadata 子集」的斷言等價。
    all_names = set(Base.metadata.tables.keys())
    assert set(tables_to_wipe()).issubset(all_names)
