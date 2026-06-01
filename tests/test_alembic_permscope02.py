"""permscope02 migration 回歸測試。

測試覆蓋（Phase 2.1 PORTFOLIO）：
1. upgrade seeding 三個 PORTFOLIO_* codes 的 scope_options
2. 非 Phase 2.1 範圍的 code（STUDENTS_*、DASHBOARD）scope_options 不被改動
3. upgrade backfill teacher role.permissions（bare PORTFOLIO_* → :own_class）
4. admin role 不受影響
5. upgrade backfill teacher user.permission_names（bare PORTFOLIO_* → :own_class）+ token_version bump
   且 STUDENTS_HEALTH_READ（非 Phase 2.1）保留 bare
6. wildcard '*' teacher user 不被改動
7. downgrade 只剝 PORTFOLIO_* 後綴（不動 STUDENTS_*:own_class），同時 bump token_version

Reference: tests/test_alembic_permscope01.py（_AlembicOpStub pattern）
"""

import importlib.util
import json
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    text,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260530_permscope02_portfolio_seed_and_backfill.py"
)


class _AlembicOpStub:
    """讓 migration 內 op.* 在測試環境下操作 SQLite test connection。"""

    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind

    def add_column(self, table_name, column):
        col_type = column.type.compile(dialect=self.bind.dialect)
        nullable = "" if column.nullable else " NOT NULL"
        self.bind.execute(
            text(
                f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}{nullable}"
            )
        )

    def drop_column(self, table_name, column_name):
        self.bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    def execute(self, sql):
        if isinstance(sql, str):
            sql = text(sql)
        self.bind.execute(sql)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "permscope02_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prereq_tables(bind):
    """建 migration 會碰到的最小 permission_definitions / roles / users 表並插入測試資料。

    模擬 permscope01 已執行的狀態：
    - permission_definitions 已有 scope_options 欄位（NULL or 已 seed STUDENTS_*）
    - teacher role/users 的 STUDENTS_* 已轉 :own_class
    """
    metadata = MetaData()

    Table(
        "permission_definitions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", Text, nullable=False, unique=True),
        Column("label", Text, nullable=False),
        Column("group_name", Text, nullable=False, server_default="自訂"),
        Column("is_core", Boolean, nullable=False, server_default="0"),
        Column("scope_options", Text, nullable=True),  # 模擬 permscope01 已新增的欄位
    )

    Table(
        "roles",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", Text, nullable=False, unique=True),
        Column("label", Text, nullable=False),
        Column("is_core", Boolean, nullable=False, server_default="0"),
        Column("permissions", Text, nullable=False, server_default="[]"),
    )

    Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("username", Text, nullable=False, unique=True),
        Column("password_hash", Text, nullable=False),
        Column("role", Text, nullable=False),
        Column("permission_names", Text, nullable=False, server_default="[]"),
        Column("token_version", Integer, nullable=True),
    )

    metadata.create_all(bind)

    # permission_definitions seed
    # STUDENTS_READ 模擬 permscope01 已 seed 的狀態（scope_options 非 NULL）
    # PORTFOLIO_* 為本次 Phase 2.1 要 seed 的對象（先 NULL）
    # DASHBOARD 為對照組
    bind.execute(
        text(
            "INSERT INTO permission_definitions (code, label, group_name, is_core, scope_options) VALUES "
            "('STUDENTS_READ', '學生管理（檢視）', '學生', 1, :students_opts), "
            "('PORTFOLIO_READ', '學習歷程（檢視）', '學生', 1, NULL), "
            "('PORTFOLIO_WRITE', '學習歷程（編輯）', '學生', 1, NULL), "
            "('PORTFOLIO_PUBLISH', '學習歷程（發布）', '學生', 1, NULL), "
            "('STUDENTS_HEALTH_READ', '學生健康（檢視）', '學生', 1, NULL), "
            "('DASHBOARD', '儀表板', '系統', 1, NULL)"
        ),
        {"students_opts": json.dumps(["own_class", "all"])},
    )

    # roles seed:
    # teacher 模擬 permscope01 已執行：STUDENTS_READ:own_class 已轉好，PORTFOLIO_* 仍 bare
    # admin 持有 wildcard
    bind.execute(
        text(
            "INSERT INTO roles (code, label, is_core, permissions) VALUES "
            "('teacher', '教師', 1, :teacher_perms), "
            "('admin', '系統管理員', 1, :admin_perms)"
        ),
        {
            "teacher_perms": json.dumps(
                [
                    "STUDENTS_READ:own_class",
                    "PORTFOLIO_READ",
                    "PORTFOLIO_WRITE",
                    "PORTFOLIO_PUBLISH",
                    "STUDENTS_HEALTH_READ",
                ]
            ),
            "admin_perms": json.dumps(["*"]),
        },
    )

    # users seed:
    # teacher_user 模擬 permscope01 已執行：STUDENTS_READ 已轉 :own_class，PORTFOLIO_READ 仍 bare
    # admin_user 持有 wildcard
    bind.execute(
        text(
            "INSERT INTO users (username, password_hash, role, permission_names, token_version) VALUES "
            "('teacher_user', 'x', 'teacher', :teacher_names, 1), "
            "('admin_user', 'x', 'admin', :admin_names, 0)"
        ),
        {
            "teacher_names": json.dumps(
                ["STUDENTS_READ:own_class", "PORTFOLIO_READ", "STUDENTS_HEALTH_READ"]
            ),
            "admin_names": json.dumps(["*"]),
        },
    )


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_upgrade_seeds_three_portfolio_codes_with_scope_options(tmp_path):
    """upgrade 後 PORTFOLIO_READ / PORTFOLIO_WRITE / PORTFOLIO_PUBLISH 有 scope_options。"""
    db_path = tmp_path / "permscope02_seed.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        for code in ("PORTFOLIO_READ", "PORTFOLIO_WRITE", "PORTFOLIO_PUBLISH"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code = :code"
                ),
                {"code": code},
            ).fetchone()
            assert row is not None, f"找不到 {code}"
            val = row[0]
            opts = json.loads(val) if isinstance(val, str) else val
            assert opts == [
                "own_class",
                "all",
            ], f"{code}.scope_options 應為 ['own_class', 'all']，實際: {opts}"

    engine.dispose()


def test_upgrade_other_codes_unchanged(tmp_path):
    """upgrade 後 STUDENTS_READ（permscope01 已 seed）與 DASHBOARD（無 scope）不被改動。"""
    db_path = tmp_path / "permscope02_other.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        # STUDENTS_READ 應仍為 ['own_class','all']（不被本次 migration 覆蓋）
        row = conn.execute(
            text(
                "SELECT scope_options FROM permission_definitions WHERE code='STUDENTS_READ'"
            )
        ).fetchone()
        val = row[0]
        opts = json.loads(val) if isinstance(val, str) else val
        assert opts == [
            "own_class",
            "all",
        ], f"STUDENTS_READ.scope_options 不應被改動，實際: {opts}"

        # DASHBOARD 應為 NULL
        row = conn.execute(
            text(
                "SELECT scope_options FROM permission_definitions WHERE code='DASHBOARD'"
            )
        ).fetchone()
        assert row[0] is None, f"DASHBOARD.scope_options 應仍為 NULL，實際: {row[0]}"

        # STUDENTS_HEALTH_READ 應為 NULL（非 Phase 2.1 範圍）
        row = conn.execute(
            text(
                "SELECT scope_options FROM permission_definitions WHERE code='STUDENTS_HEALTH_READ'"
            )
        ).fetchone()
        assert (
            row[0] is None
        ), f"STUDENTS_HEALTH_READ.scope_options 應仍為 NULL，實際: {row[0]}"

    engine.dispose()


def test_upgrade_teacher_role_portfolio_permissions_get_own_class_suffix(tmp_path):
    """upgrade 後 teacher role 的 PORTFOLIO_* bare → :own_class；其他 code 不變。"""
    db_path = tmp_path / "permscope02_teacher_role.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        row = conn.execute(
            text("SELECT permissions FROM roles WHERE code='teacher' AND is_core=1")
        ).fetchone()
        assert row is not None, "找不到 teacher role"
        perms = json.loads(row[0]) if isinstance(row[0], str) else row[0]

        for code in ("PORTFOLIO_READ", "PORTFOLIO_WRITE", "PORTFOLIO_PUBLISH"):
            assert (
                f"{code}:own_class" in perms
            ), f"teacher role 應含 {code}:own_class，實際: {perms}"
            assert (
                code not in perms
            ), f"teacher role 不應仍含 bare {code}，實際: {perms}"

        # 不可動的兩項
        assert (
            "STUDENTS_READ:own_class" in perms
        ), f"STUDENTS_READ:own_class（permscope01 已轉）應保留，實際: {perms}"
        assert (
            "STUDENTS_HEALTH_READ" in perms
        ), f"STUDENTS_HEALTH_READ bare（非 Phase 2.1）應保留，實際: {perms}"

    engine.dispose()


def test_upgrade_admin_role_unchanged(tmp_path):
    """upgrade 後 admin role 的 wildcard permissions 不應被改動。"""
    db_path = tmp_path / "permscope02_admin_role.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        row = conn.execute(
            text("SELECT permissions FROM roles WHERE code='admin' AND is_core=1")
        ).fetchone()
        assert row is not None, "找不到 admin role"
        perms = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        assert perms == ["*"], f"admin role 應仍為 ['*']，實際: {perms}"

    engine.dispose()


def test_upgrade_existing_teacher_user_backfilled(tmp_path):
    """upgrade 後 teacher user permission_names 內 PORTFOLIO_READ bare → :own_class，
    STUDENTS_HEALTH_READ 保持 bare（非 Phase 2.1），STUDENTS_READ:own_class 不變，
    token_version 從 1 bump 到 2。"""
    db_path = tmp_path / "permscope02_user_backfill.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        row = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='teacher_user'"
            )
        ).fetchone()
        assert row is not None
        names = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        tv = row[1]

        assert (
            "PORTFOLIO_READ:own_class" in names
        ), f"teacher user 應有 PORTFOLIO_READ:own_class，實際: {names}"
        assert (
            "PORTFOLIO_READ" not in names
        ), f"teacher user 不應仍含 bare PORTFOLIO_READ，實際: {names}"
        assert (
            "STUDENTS_HEALTH_READ" in names
        ), f"STUDENTS_HEALTH_READ（非 Phase 2.1）應保留 bare，實際: {names}"
        assert (
            "STUDENTS_READ:own_class" in names
        ), f"STUDENTS_READ:own_class 應不變，實際: {names}"
        assert tv == 2, f"token_version 應從 1 bump 至 2，實際: {tv}"

    engine.dispose()


def test_upgrade_skips_wildcard_teacher_user(tmp_path):
    """Teacher user with wildcard '*' must not be backfilled (already has full access)."""
    db_path = tmp_path / "permscope02_wildcard.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        # 額外插入 wildcard teacher
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, permission_names, token_version) "
                "VALUES ('twild', 'x', 'teacher', :perms, 7)"
            ),
            {"perms": json.dumps(["*"])},
        )

        module.op = _AlembicOpStub(conn)
        module.upgrade()

        row = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='twild'"
            )
        ).fetchone()

    perms = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert perms == ["*"], f"wildcard teacher 不應被改動，實際: {perms}"
    assert row[1] == 7, f"wildcard teacher token_version 不應被 bump，實際: {row[1]}"

    engine.dispose()


def test_downgrade_restores_bare_codes_and_bumps_token_version(tmp_path):
    """upgrade → downgrade 後 PORTFOLIO_* suffix 被剝掉，STUDENTS_*:own_class 不動，
    token_version 再 bump 一次（per Phase 1 review fix）。"""
    db_path = tmp_path / "permscope02_downgrade.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)

        # 1. upgrade
        module.upgrade()

        user_row = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='teacher_user'"
            )
        ).fetchone()
        names_after_upgrade = (
            json.loads(user_row[0]) if isinstance(user_row[0], str) else user_row[0]
        )
        assert "PORTFOLIO_READ:own_class" in names_after_upgrade
        tv_after_upgrade = user_row[1]
        assert tv_after_upgrade == 2  # 1 → 2

        # 2. downgrade
        module.downgrade()

        # 3. teacher role permissions：PORTFOLIO_* 還原 bare，STUDENTS_*:own_class 不動
        role_row = conn.execute(
            text("SELECT permissions FROM roles WHERE code='teacher' AND is_core=1")
        ).fetchone()
        perms = json.loads(role_row[0]) if isinstance(role_row[0], str) else role_row[0]
        for code in ("PORTFOLIO_READ", "PORTFOLIO_WRITE", "PORTFOLIO_PUBLISH"):
            assert code in perms, f"downgrade 後 {code} bare 應還原，實際: {perms}"
            assert (
                f"{code}:own_class" not in perms
            ), f"downgrade 後不應仍含 {code}:own_class，實際: {perms}"
        # STUDENTS_*:own_class 應保留
        assert (
            "STUDENTS_READ:own_class" in perms
        ), f"downgrade 不應動 STUDENTS_READ:own_class（屬 permscope01 管轄），實際: {perms}"

        # 4. teacher user：PORTFOLIO_* 還原 bare + token_version 再 bump
        user_row2 = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='teacher_user'"
            )
        ).fetchone()
        names_after_down = (
            json.loads(user_row2[0]) if isinstance(user_row2[0], str) else user_row2[0]
        )
        assert (
            "PORTFOLIO_READ" in names_after_down
        ), f"downgrade 後 PORTFOLIO_READ bare 應還原，實際: {names_after_down}"
        assert not any(
            n.startswith("PORTFOLIO_") and ":own_class" in n for n in names_after_down
        ), f"downgrade 後不應仍含 PORTFOLIO_*:own_class，實際: {names_after_down}"
        assert (
            "STUDENTS_READ:own_class" in names_after_down
        ), f"downgrade 不應動 STUDENTS_READ:own_class，實際: {names_after_down}"
        assert (
            user_row2[1] == 3
        ), f"downgrade 應 bump token_version (2→3)，實際: {user_row2[1]}"

        # 5. PORTFOLIO_* 的 scope_options 應被清回 NULL
        for code in ("PORTFOLIO_READ", "PORTFOLIO_WRITE", "PORTFOLIO_PUBLISH"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code=:code"
                ),
                {"code": code},
            ).fetchone()
            assert (
                row[0] is None
            ), f"downgrade 後 {code}.scope_options 應為 NULL，實際: {row[0]}"

        # 6. STUDENTS_READ.scope_options 不應被改動（仍有值）
        row = conn.execute(
            text(
                "SELECT scope_options FROM permission_definitions WHERE code='STUDENTS_READ'"
            )
        ).fetchone()
        val = row[0]
        opts = json.loads(val) if isinstance(val, str) else val
        assert opts == [
            "own_class",
            "all",
        ], f"STUDENTS_READ.scope_options 不應被本次 downgrade 改動，實際: {opts}"

    engine.dispose()
