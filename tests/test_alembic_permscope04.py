"""permscope04 migration 回歸測試。

測試覆蓋（Phase 2.3 DISMISSAL_CALLS）：
1. upgrade seeding 2 個 DISMISSAL_CALLS codes 的 scope_options
2. 非 Phase 2.3 範圍的 code（STUDENTS_READ permscope01 / PORTFOLIO_READ permscope02 /
   STUDENTS_HEALTH_READ permscope03）scope_options 不被改動
3. upgrade backfill teacher role.permissions（bare DISMISSAL_CALLS_* → :own_class）
4. admin role 不受影響
5. upgrade backfill teacher user.permission_names（bare DISMISSAL_CALLS_* → :own_class）+
   token_version bump 且 STUDENTS_*:own_class（permscope01）/ PORTFOLIO_*:own_class
   （permscope02）/ STUDENTS_HEALTH_*:own_class（permscope03）原樣保留
6. wildcard '*' teacher user 不被改動
7. downgrade 只剝 DISMISSAL_CALLS 後綴（不動其他 family），同時 bump token_version

Reference: tests/test_alembic_permscope03.py（_AlembicOpStub pattern）
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
    / "20260601_permscope04_dismissal.py"
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
        "permscope04_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_prereq_tables(bind):
    """建 migration 會碰到的最小 permission_definitions / roles / users 表並插入測試資料。

    模擬 permscope01 + permscope02 + permscope03 已執行的狀態：
    - permission_definitions 已有 scope_options 欄位
      - STUDENTS_READ（permscope01 已 seed）
      - PORTFOLIO_READ（permscope02 已 seed）
      - STUDENTS_HEALTH_READ（permscope03 已 seed）
      - 2 條 DISMISSAL_CALLS（permscope04 即將 seed，目前 NULL）
      - DASHBOARD（無 scope，對照組）
    - teacher role 持有 STUDENTS_READ:own_class / PORTFOLIO_READ:own_class /
      STUDENTS_HEALTH_READ:own_class + 2 條 bare DISMISSAL_CALLS_*
    - teacher user 同上
    - admin role/user 持有 wildcard
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
        Column("scope_options", Text, nullable=True),
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

    # permission_definitions seed:
    # 前 3 條已 permscope01/02/03 seed 過（有 scope_options）
    # 2 條 DISMISSAL_CALLS 為本次 Phase 2.3 要 seed 的對象（先 NULL）
    # DASHBOARD 為對照組
    bind.execute(
        text(
            "INSERT INTO permission_definitions (code, label, group_name, is_core, scope_options) VALUES "
            "('STUDENTS_READ', '學生管理（檢視）', '學生', 1, :scoped_opts), "
            "('PORTFOLIO_READ', '學習歷程（檢視）', '學生', 1, :scoped_opts), "
            "('STUDENTS_HEALTH_READ', '學生健康（檢視）', '健康', 1, :scoped_opts), "
            "('DISMISSAL_CALLS_READ', '接送通知（檢視）', '接送', 1, NULL), "
            "('DISMISSAL_CALLS_WRITE', '接送通知（操作）', '接送', 1, NULL), "
            "('DASHBOARD', '儀表板', '系統', 1, NULL)"
        ),
        {"scoped_opts": json.dumps(["own_class", "all"])},
    )

    # roles seed:
    # teacher 已 permscope01/02/03 轉好三個 :own_class，DISMISSAL_CALLS_* 仍 bare
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
                    "PORTFOLIO_READ:own_class",
                    "STUDENTS_HEALTH_READ:own_class",
                    "DISMISSAL_CALLS_READ",
                    "DISMISSAL_CALLS_WRITE",
                ]
            ),
            "admin_perms": json.dumps(["*"]),
        },
    )

    # users seed:
    # teacher_user 同 role 設定（DISMISSAL_CALLS_* 仍 bare）
    # admin_user 持有 wildcard
    bind.execute(
        text(
            "INSERT INTO users (username, password_hash, role, permission_names, token_version) VALUES "
            "('teacher_user', 'x', 'teacher', :teacher_names, 1), "
            "('admin_user', 'x', 'admin', :admin_names, 0)"
        ),
        {
            "teacher_names": json.dumps(
                [
                    "STUDENTS_READ:own_class",
                    "PORTFOLIO_READ:own_class",
                    "STUDENTS_HEALTH_READ:own_class",
                    "DISMISSAL_CALLS_READ",
                    "DISMISSAL_CALLS_WRITE",
                ]
            ),
            "admin_names": json.dumps(["*"]),
        },
    )


# ---------------------------------------------------------------------------
# 測試
# ---------------------------------------------------------------------------


def test_upgrade_seeds_two_dismissal_codes_with_scope_options(tmp_path):
    """upgrade 後 2 條 DISMISSAL_CALLS codes 都有 scope_options=['own_class','all']。"""
    db_path = tmp_path / "permscope04_seed.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
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
    """upgrade 後 STUDENTS_READ（permscope01）/ PORTFOLIO_READ（permscope02）/
    STUDENTS_HEALTH_READ（permscope03）/ DASHBOARD 不被本次 migration 改動。"""
    db_path = tmp_path / "permscope04_other.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    module = _load_migration_module()

    with engine.begin() as conn:
        _create_prereq_tables(conn)
        module.op = _AlembicOpStub(conn)
        module.upgrade()

        for code in ("STUDENTS_READ", "PORTFOLIO_READ", "STUDENTS_HEALTH_READ"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code=:code"
                ),
                {"code": code},
            ).fetchone()
            val = row[0]
            opts = json.loads(val) if isinstance(val, str) else val
            assert opts == [
                "own_class",
                "all",
            ], f"{code}.scope_options 不應被改動，實際: {opts}"

        # DASHBOARD 應仍為 NULL（無 scope）
        row = conn.execute(
            text(
                "SELECT scope_options FROM permission_definitions WHERE code='DASHBOARD'"
            )
        ).fetchone()
        assert row[0] is None, f"DASHBOARD.scope_options 應仍為 NULL，實際: {row[0]}"

    engine.dispose()


def test_upgrade_teacher_role_dismissal_permissions_get_own_class_suffix(tmp_path):
    """upgrade 後 teacher role 的 2 條 DISMISSAL_CALLS bare → :own_class；
    STUDENTS_READ:own_class / PORTFOLIO_READ:own_class / STUDENTS_HEALTH_READ:own_class
    不變。"""
    db_path = tmp_path / "permscope04_teacher_role.sqlite"
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

        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
            assert (
                f"{code}:own_class" in perms
            ), f"teacher role 應含 {code}:own_class，實際: {perms}"
            assert (
                code not in perms
            ), f"teacher role 不應仍含 bare {code}，實際: {perms}"

        # 不可動的三項（permscope01/02/03 管轄）
        for prior in (
            "STUDENTS_READ:own_class",
            "PORTFOLIO_READ:own_class",
            "STUDENTS_HEALTH_READ:own_class",
        ):
            assert prior in perms, f"{prior} 應保留，實際: {perms}"

    engine.dispose()


def test_upgrade_admin_role_unchanged(tmp_path):
    """upgrade 後 admin role 的 wildcard permissions 不應被改動。"""
    db_path = tmp_path / "permscope04_admin_role.sqlite"
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
    """upgrade 後 teacher user permission_names 內 2 條 DISMISSAL_CALLS bare → :own_class，
    其他 :own_class 不變，token_version 從 1 bump 到 2。"""
    db_path = tmp_path / "permscope04_user_backfill.sqlite"
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

        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
            assert (
                f"{code}:own_class" in names
            ), f"teacher user 應有 {code}:own_class，實際: {names}"
            assert (
                code not in names
            ), f"teacher user 不應仍含 bare {code}，實際: {names}"

        for prior in (
            "STUDENTS_READ:own_class",
            "PORTFOLIO_READ:own_class",
            "STUDENTS_HEALTH_READ:own_class",
        ):
            assert prior in names, f"{prior} 應不變，實際: {names}"
        assert tv == 2, f"token_version 應從 1 bump 至 2，實際: {tv}"

    engine.dispose()


def test_upgrade_skips_wildcard_teacher_user(tmp_path):
    """持有 wildcard '*' 的 teacher user 不應被 backfill（已有完整存取）。"""
    db_path = tmp_path / "permscope04_wildcard.sqlite"
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
    """upgrade → downgrade 後 2 條 DISMISSAL_CALLS suffix 被剝掉，
    其他 :own_class 不動，token_version 再 bump 一次。"""
    db_path = tmp_path / "permscope04_downgrade.sqlite"
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
        assert "DISMISSAL_CALLS_READ:own_class" in names_after_upgrade
        tv_after_upgrade = user_row[1]
        assert tv_after_upgrade == 2  # 1 → 2

        # 2. downgrade
        module.downgrade()

        # 3. teacher role permissions：2 條還原 bare，其他不動
        role_row = conn.execute(
            text("SELECT permissions FROM roles WHERE code='teacher' AND is_core=1")
        ).fetchone()
        perms = json.loads(role_row[0]) if isinstance(role_row[0], str) else role_row[0]
        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
            assert code in perms, f"downgrade 後 {code} bare 應還原，實際: {perms}"
            assert (
                f"{code}:own_class" not in perms
            ), f"downgrade 後不應仍含 {code}:own_class，實際: {perms}"
        # 其他 family :own_class 應保留
        for prior in (
            "STUDENTS_READ:own_class",
            "PORTFOLIO_READ:own_class",
            "STUDENTS_HEALTH_READ:own_class",
        ):
            assert (
                prior in perms
            ), f"downgrade 不應動 {prior}（屬其他 permscope 管轄），實際: {perms}"

        # 4. teacher user：2 條還原 bare + token_version 再 bump
        user_row2 = conn.execute(
            text(
                "SELECT permission_names, token_version FROM users WHERE username='teacher_user'"
            )
        ).fetchone()
        names_after_down = (
            json.loads(user_row2[0]) if isinstance(user_row2[0], str) else user_row2[0]
        )
        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
            assert (
                code in names_after_down
            ), f"downgrade 後 {code} bare 應還原，實際: {names_after_down}"
            assert (
                f"{code}:own_class" not in names_after_down
            ), f"downgrade 後不應仍含 {code}:own_class，實際: {names_after_down}"
        for prior in (
            "STUDENTS_READ:own_class",
            "PORTFOLIO_READ:own_class",
            "STUDENTS_HEALTH_READ:own_class",
        ):
            assert (
                prior in names_after_down
            ), f"downgrade 不應動 {prior}，實際: {names_after_down}"
        assert (
            user_row2[1] == 3
        ), f"downgrade 應 bump token_version (2→3)，實際: {user_row2[1]}"

        # 5. 2 條 DISMISSAL_CALLS 的 scope_options 應被清回 NULL
        for code in ("DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code=:code"
                ),
                {"code": code},
            ).fetchone()
            assert (
                row[0] is None
            ), f"downgrade 後 {code}.scope_options 應為 NULL，實際: {row[0]}"

        # 6. 其他 family scope_options 不應被改動
        for code in ("STUDENTS_READ", "PORTFOLIO_READ", "STUDENTS_HEALTH_READ"):
            row = conn.execute(
                text(
                    "SELECT scope_options FROM permission_definitions WHERE code=:code"
                ),
                {"code": code},
            ).fetchone()
            val = row[0]
            opts = json.loads(val) if isinstance(val, str) else val
            assert opts == [
                "own_class",
                "all",
            ], f"{code}.scope_options 不應被本次 downgrade 改動，實際: {opts}"

    engine.dispose()
