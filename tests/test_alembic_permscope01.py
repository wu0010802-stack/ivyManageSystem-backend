"""permscope01 migration 回歸測試。

測試覆蓋：
1. upgrade 新增 scope_options 欄位到 permission_definitions
2. upgrade seeding 三個 STUDENTS_* codes 的 scope_options
3. 非 scope-aware 的 code（PORTFOLIO_READ）scope_options 應為 NULL
4. upgrade backfill teacher role.permissions（bare → :own_class）
5. admin role 不受影響
6. upgrade backfill teacher user.permission_names（bare → :own_class）+ token_version bump
7. downgrade 還原 bare codes + 移除 scope_options 欄位

Reference: tests/test_alembic_pretent001.py（_AlembicOpStub pattern）
"""

import importlib.util
import json
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "20260529_permscope01_permission_scope_options.py"
)


class _AlembicOpStub:
    """讓 migration 內 op.* 在測試環境下操作 SQLite test connection。"""

    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind

    def add_column(self, table_name, column):
        """ALTER TABLE ... ADD COLUMN（SQLite 3.35+ 支援）。"""
        col_type = column.type.compile(dialect=self.bind.dialect)
        nullable = "" if column.nullable else " NOT NULL"
        self.bind.execute(
            text(
                f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}{nullable}"
            )
        )

    def drop_column(self, table_name, column_name):
        """ALTER TABLE ... DROP COLUMN（SQLite 3.35+）。"""
        self.bind.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))

    def execute(self, sql):
        """執行 SQL 字串或 TextClause。"""
        if isinstance(sql, str):
            sql = text(sql)
        self.bind.execute(sql)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "permscope01_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prereq_tables(bind):
    """建 migration 會碰到的最小 permission_definitions / roles / users 表並插入測試資料。"""
    metadata = MetaData()

    Table(
        "permission_definitions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", Text, nullable=False, unique=True),
        Column("label", Text, nullable=False),
        Column("group_name", Text, nullable=False, server_default="自訂"),
        Column("is_core", Boolean, nullable=False, server_default="0"),
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
    bind.execute(
        text(
            "INSERT INTO permission_definitions (code, label, group_name, is_core) VALUES "
            "('STUDENTS_READ', '學生管理（檢視）', '學生', 1), "
            "('STUDENTS_WRITE', '學生管理（編輯）', '學生', 1), "
            "('STUDENTS_LIFECYCLE_WRITE', '學生狀態管理', '學生', 1), "
            "('PORTFOLIO_READ', '學習歷程（檢視）', '學生', 1), "
            "('DASHBOARD', '儀表板', '系統', 1)"
        )
    )

    # roles seed: teacher (is_core=1) with STUDENTS_READ + PORTFOLIO_READ (bare)
    # admin (is_core=1) with wildcard
    bind.execute(
        text(
            "INSERT INTO roles (code, label, is_core, permissions) VALUES "
            "('teacher', '教師', 1, :teacher_perms), "
            "('admin', '系統管理員', 1, :admin_perms)"
        ),
        {
            "teacher_perms": json.dumps(["STUDENTS_READ", "PORTFOLIO_READ"]),
            "admin_perms": json.dumps(["*"]),
        },
    )

    # users seed: teacher user with bare STUDENTS_READ, admin user with wildcard
    bind.execute(
        text(
            "INSERT INTO users (username, password_hash, role, permission_names, token_version) VALUES "
            "('teacher_user', 'x', 'teacher', :teacher_names, 0), "
            "('admin_user', 'x', 'admin', :admin_names, 0)"
        ),
        {
            "teacher_names": json.dumps(["STUDENTS_READ", "PORTFOLIO_READ"]),
            "admin_names": json.dumps(["*"]),
        },
    )


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_upgrade_adds_scope_options_column(tmp_path):
    """upgrade 後 permission_definitions 有 scope_options 欄位。"""
    db_path = tmp_path / "permscope01_column.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        inspector = inspect(conn)
        cols = {c["name"] for c in inspector.get_columns("permission_definitions")}
        assert "scope_options" in cols, "permission_definitions 應有 scope_options 欄位"

    engine.dispose()


def test_upgrade_seeds_three_students_codes_with_scope_options(tmp_path):
    """upgrade 後 STUDENTS_READ / STUDENTS_WRITE / STUDENTS_LIFECYCLE_WRITE 有 scope_options。"""
    db_path = tmp_path / "permscope01_seed.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        for code in ("STUDENTS_READ", "STUDENTS_WRITE", "STUDENTS_LIFECYCLE_WRITE"):
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


def test_upgrade_other_codes_have_null_scope_options(tmp_path):
    """upgrade 後 PORTFOLIO_READ / DASHBOARD 的 scope_options 應為 NULL。"""
    db_path = tmp_path / "permscope01_null.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        for code in ("PORTFOLIO_READ", "DASHBOARD"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code = :code"
                ),
                {"code": code},
            ).fetchone()
            assert row is not None, f"找不到 {code}"
            assert row[0] is None, f"{code}.scope_options 應為 NULL，實際: {row[0]}"

    engine.dispose()


def test_upgrade_teacher_role_permissions_get_own_class_suffix(tmp_path):
    """upgrade 後 teacher role 的 STUDENTS_READ bare → STUDENTS_READ:own_class；PORTFOLIO_READ 不變。"""
    db_path = tmp_path / "permscope01_teacher_role.sqlite"
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

        assert (
            "STUDENTS_READ:own_class" in perms
        ), f"teacher role 應含 STUDENTS_READ:own_class，實際: {perms}"
        assert (
            "STUDENTS_READ" not in perms
        ), f"teacher role 不應含 bare STUDENTS_READ，實際: {perms}"
        assert (
            "PORTFOLIO_READ" in perms
        ), f"PORTFOLIO_READ（非 scope-aware）應保留，實際: {perms}"

    engine.dispose()


def test_upgrade_admin_role_permissions_unchanged(tmp_path):
    """upgrade 後 admin role 的 permissions 不應含 :own_class suffix。"""
    db_path = tmp_path / "permscope01_admin_role.sqlite"
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
        assert not any(
            ":own_class" in p for p in perms
        ), f"admin role 不應含 :own_class，實際: {perms}"

    engine.dispose()


def test_upgrade_existing_teacher_user_backfilled(tmp_path):
    """upgrade 後 teacher user permission_names bare → :own_class + token_version bump。"""
    db_path = tmp_path / "permscope01_user_backfill.sqlite"
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
            "STUDENTS_READ:own_class" in names
        ), f"teacher 應有 STUDENTS_READ:own_class，實際: {names}"
        assert (
            "STUDENTS_READ" not in names
        ), f"teacher 不應含 bare STUDENTS_READ，實際: {names}"
        assert "PORTFOLIO_READ" in names, f"PORTFOLIO_READ 應保留，實際: {names}"
        assert tv == 1, f"token_version 應從 0 bump 至 1，實際: {tv}"

    engine.dispose()


def test_downgrade_restores_bare_codes(tmp_path):
    """upgrade → downgrade 後 scope suffix 被剝掉，scope_options 欄位消失。"""
    db_path = tmp_path / "permscope01_downgrade.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)

        # 1. upgrade
        module.upgrade()

        # 2. 確認 upgrade 結果正確
        user_row = conn.execute(
            text("SELECT permission_names FROM users WHERE username='teacher_user'")
        ).fetchone()
        names_after_upgrade = (
            json.loads(user_row[0]) if isinstance(user_row[0], str) else user_row[0]
        )
        assert "STUDENTS_READ:own_class" in names_after_upgrade

        # 3. downgrade
        module.downgrade()

        # 4. scope_options 欄位應消失
        inspector = inspect(conn)
        cols = {c["name"] for c in inspector.get_columns("permission_definitions")}
        assert "scope_options" not in cols, "downgrade 後 scope_options 欄位應被移除"

        # 5. teacher role permissions 應還原 bare codes
        role_row = conn.execute(
            text("SELECT permissions FROM roles WHERE code='teacher' AND is_core=1")
        ).fetchone()
        perms = json.loads(role_row[0]) if isinstance(role_row[0], str) else role_row[0]
        assert (
            "STUDENTS_READ" in perms
        ), f"downgrade 後 STUDENTS_READ bare 應還原，實際: {perms}"
        assert not any(
            ":own_class" in p for p in perms
        ), f"downgrade 後不應含 :own_class，實際: {perms}"

        # 6. teacher user permission_names 應還原 bare codes
        user_row2 = conn.execute(
            text("SELECT permission_names FROM users WHERE username='teacher_user'")
        ).fetchone()
        names_after_down = (
            json.loads(user_row2[0]) if isinstance(user_row2[0], str) else user_row2[0]
        )
        assert (
            "STUDENTS_READ" in names_after_down
        ), f"downgrade 後 STUDENTS_READ bare 應還原，實際: {names_after_down}"
        assert not any(
            ":own_class" in n for n in names_after_down
        ), f"downgrade 後不應含 :own_class，實際: {names_after_down}"

    engine.dispose()


def test_upgrade_skips_wildcard_teacher_user(tmp_path):
    """Teacher user with wildcard '*' must not be backfilled (already has full access)."""
    db_path = tmp_path / "permscope01_wildcard.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        # seed wildcard teacher（額外插入，不依賴 _create_prereq_tables 的 admin_user）
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


def test_downgrade_bumps_token_version_on_teacher_users(tmp_path):
    """Downgrade must also bump token_version so post-upgrade JWTs are invalidated on rollback."""
    db_path = tmp_path / "permscope01_downgrade_tv.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        # 插入一個已含 scope suffix 的 teacher user（模擬 upgrade 後狀態）
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, permission_names, token_version) "
                "VALUES ('t3', 'x', 'teacher', :perms, 5)"
            ),
            {"perms": json.dumps(["STUDENTS_READ:own_class"])},
        )

        module.op = _AlembicOpStub(conn)
        # 先 upgrade（新增 scope_options 欄位，downgrade 才能 drop）
        module.upgrade()
        # 再 downgrade
        module.downgrade()

        row = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='t3'"
            )
        ).fetchone()

    perms = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert perms == ["STUDENTS_READ"], f"downgrade 後 suffix 應被剝掉，實際: {perms}"
    # upgrade 時 t3 的 token_version 從 5→6，downgrade 時再 bump 6→7
    assert (
        row[1] == 7
    ), f"downgrade 應 bump token_version (5→6 upgrade, 6→7 downgrade)，實際: {row[1]}"

    engine.dispose()
