"""Password history replay prevention tests。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base
from models.auth import PasswordHistory, User
from utils.auth import hash_password
from utils.password_history import (
    PASSWORD_HISTORY_DEPTH,
    assert_not_recently_used,
    record,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ph.sqlite'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    user = User(
        username="alice",
        password_hash=hash_password("Initial-Password-1"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        token_version=0,
    )
    s.add(user)
    s.commit()
    yield s, user
    s.close()


def test_assert_not_recently_used_passes_empty_history(db):
    s, user = db
    assert_not_recently_used(s, user.id, "Brand-New-Password-1")


def test_assert_not_recently_used_raises_on_exact_match(db):
    s, user = db
    h1 = hash_password("Old-Password-One-1")
    record(s, user.id, h1)
    with pytest.raises(HTTPException) as ei:
        assert_not_recently_used(s, user.id, "Old-Password-One-1")
    assert ei.value.status_code == 400


def test_assert_not_recently_used_only_checks_last_N(db):
    """超過 PASSWORD_HISTORY_DEPTH 的舊紀錄不應命中。"""
    import time

    s, user = db
    oldest = "Oldest-Password-1234"
    record(s, user.id, hash_password(oldest))
    for i in range(PASSWORD_HISTORY_DEPTH):
        time.sleep(0.1)
        record(s, user.id, hash_password(f"Filler-Password-{i:04d}"))
    # 最舊的應可重用
    assert_not_recently_used(s, user.id, oldest)


def test_assert_not_recently_used_isolates_users(db):
    s, user = db
    h1 = hash_password("User-A-Password-1")
    record(s, user.id, h1)
    # 對另一 user_id 應該不擋
    assert_not_recently_used(s, 9999, "User-A-Password-1")
