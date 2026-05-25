"""migration notif01 up/down 對 SQLite 跑（symmetric reversibility）。"""

import importlib.util
import pytest
from pathlib import Path
from sqlalchemy import inspect, text


def _load_notif01_module():
    """動態載入 notif01 migration module（不靠 sys.modules）。"""
    for path in Path("alembic/versions").glob("*notif01*.py"):
        spec = importlib.util.spec_from_file_location("notif01_mod", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise FileNotFoundError("notif01 migration not found")


def test_notif01_upgrade_renames_table_creates_logs_and_backfills_prefix(
    test_db_session, monkeypatch
):
    """upgrade: rename + backfill + 建 notification_logs；downgrade 反向。

    conftest.test_db_session 用 Base.metadata.create_all 建全部 ORM 表。
    Task 3 model 之後 NotificationPreference.__tablename__ 為 "notification_preferences"
    (Task 4 model 更新對齊 migration)，所以 create_all 建出的是「migration 後」狀態。

    本測試要模擬 notif01 之前的 DB 狀態：
      - 必須有 parent_notification_preferences（舊表名）
      - 不可有 notification_preferences（新表名）
      - 不可有 notification_logs（新表）
    因此 setup 階段 DROP 新表 + 用 raw SQL 建舊表名，再跑 upgrade。
    """
    from models.notification_log import NotificationLog
    from models.parent_notification import ParentNotificationPreference

    # 還原 pre-notif01 狀態：DROP 新表、用 raw SQL 建舊表名
    # ParentNotificationPreference 的 __tablename__ 在 Task 4 已對齊 migration
    # 為 "notification_preferences"，所以 drop 的是新表
    NotificationLog.__table__.drop(test_db_session.bind, checkfirst=True)
    ParentNotificationPreference.__table__.drop(test_db_session.bind, checkfirst=True)
    test_db_session.commit()

    test_db_session.execute(text("""
        CREATE TABLE parent_notification_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type VARCHAR(40) NOT NULL,
            channel VARCHAR(10) NOT NULL DEFAULT 'line',
            enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        )
        """))
    test_db_session.execute(
        text(
            "INSERT INTO parent_notification_preferences "
            "(user_id, event_type, channel, enabled) "
            "VALUES (999, 'message_received', 'line', 1)"
        )
    )
    test_db_session.commit()

    # 用 alembic op 綁到 test session 的 connection
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import alembic.op as alembic_op_module

    conn = test_db_session.bind.connect()
    ctx = MigrationContext.configure(conn)
    op = Operations(ctx)

    # monkeypatch alembic.op 全域方法為當前 op 實例
    for method in (
        "rename_table",
        "execute",
        "create_index",
        "create_table",
        "drop_index",
        "drop_table",
        "get_bind",
    ):
        if hasattr(op, method):
            monkeypatch.setattr(alembic_op_module, method, getattr(op, method))

    mod = _load_notif01_module()

    # 跑 upgrade
    mod.upgrade()
    conn.commit()  # SQLite 直接 commit
    inspector = inspect(test_db_session.bind)
    tables = inspector.get_table_names()
    assert "notification_preferences" in tables
    assert "notification_logs" in tables
    assert "parent_notification_preferences" not in tables

    # 驗 backfill: row 的 event_type 已加前綴
    result = conn.execute(
        text("SELECT event_type FROM notification_preferences WHERE user_id=999")
    ).fetchone()
    assert result is not None
    assert result[0] == "parent.message_received"

    # 跑 downgrade
    mod.downgrade()
    conn.commit()
    inspector = inspect(test_db_session.bind)
    tables = inspector.get_table_names()
    assert "notification_logs" not in tables
    assert "parent_notification_preferences" in tables
    assert "notification_preferences" not in tables

    result = conn.execute(
        text("SELECT event_type FROM parent_notification_preferences WHERE user_id=999")
    ).fetchone()
    assert result[0] == "message_received"

    conn.close()
