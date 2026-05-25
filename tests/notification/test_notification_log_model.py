"""NotificationLog model schema 與 default 行為測試。"""

from datetime import datetime

from models.database import NotificationLog


def test_notification_log_table_name():
    assert NotificationLog.__tablename__ == "notification_logs"


def test_notification_log_required_columns_present():
    cols = {c.name for c in NotificationLog.__table__.columns}
    required = {
        "id",
        "recipient_user_id",
        "event_type",
        "sender_id",
        "title",
        "body",
        "payload_json",
        "source_entity_type",
        "source_entity_id",
        "deep_link",
        "channels_attempted",
        "channels_succeeded",
        "channels_failed",
        "read_at",
        "created_at",
    }
    assert required.issubset(cols), f"缺欄: {required - cols}"


def test_notification_log_id_is_primary_key_autoincrement():
    id_col = NotificationLog.__table__.columns["id"]
    assert id_col.primary_key is True
    assert id_col.autoincrement is True


def test_notification_log_recipient_required():
    recipient = NotificationLog.__table__.columns["recipient_user_id"]
    assert recipient.nullable is False


def test_notification_log_create_with_defaults(test_db_session):
    row = NotificationLog(
        recipient_user_id=1,
        event_type="leave.approved",
        title="t",
        body="b",
    )
    test_db_session.add(row)
    test_db_session.flush()
    assert row.id is not None
    assert row.payload_json == {}
    assert row.channels_attempted == []
    assert row.channels_succeeded == []
    assert row.channels_failed == []
    assert row.read_at is None
    assert isinstance(row.created_at, datetime)
