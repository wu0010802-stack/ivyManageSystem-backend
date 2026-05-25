"""User.id → LINE user_id 解析測試。"""

from datetime import datetime
from services.notification.dispatch import _resolve_line_user_id


def test_resolve_returns_line_user_id_when_user_active_and_followed(test_db_session):
    from models.database import User

    user = User(
        username="u1",
        password_hash="x",
        line_user_id="Uxxxxx",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.flush()
    assert _resolve_line_user_id(test_db_session, user_id=user.id) == "Uxxxxx"


def test_resolve_returns_none_when_user_inactive(test_db_session):
    from models.database import User

    user = User(
        username="u2",
        password_hash="x",
        line_user_id="Uxxxxx",
        line_follow_confirmed_at=datetime.now(),
        is_active=False,
    )
    test_db_session.add(user)
    test_db_session.flush()
    assert _resolve_line_user_id(test_db_session, user_id=user.id) is None


def test_resolve_returns_none_when_no_line_user_id(test_db_session):
    from models.database import User

    user = User(
        username="u3",
        password_hash="x",
        line_user_id=None,
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.flush()
    assert _resolve_line_user_id(test_db_session, user_id=user.id) is None


def test_resolve_returns_none_when_not_followed(test_db_session):
    from models.database import User

    user = User(
        username="u4",
        password_hash="x",
        line_user_id="Uxxxxx",
        line_follow_confirmed_at=None,
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.flush()
    assert _resolve_line_user_id(test_db_session, user_id=user.id) is None


def test_resolve_returns_none_when_user_not_found(test_db_session):
    assert _resolve_line_user_id(test_db_session, user_id=99999) is None


def test_resolve_returns_none_when_user_id_is_none(test_db_session):
    assert _resolve_line_user_id(test_db_session, user_id=None) is None
