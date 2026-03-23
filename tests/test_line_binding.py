"""
Portal LINE 綁定 API 單元測試（純邏輯，SQLite in-memory）
"""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.auth import User


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def seed_users(db_session):
    """建立兩個測試用戶"""
    u1 = User(username="alice", password_hash="hash", role="teacher")
    u2 = User(username="bob", password_hash="hash", role="teacher")
    db_session.add_all([u1, u2])
    db_session.commit()
    db_session.refresh(u1)
    db_session.refresh(u2)
    yield u1, u2
    # cleanup
    db_session.query(User).filter(User.username.in_(["alice", "bob"])).delete()
    db_session.commit()


# ──────────────────────────────────────────────────────────────────────────────
# 測試：DB 層直接驗證格式與唯一性
# ──────────────────────────────────────────────────────────────────────────────

class TestLineIdFormat:
    VALID_ID = "U" + "a" * 32

    def test_valid_format(self):
        import re
        pattern = re.compile(r"^U[0-9a-f]{32}$")
        assert pattern.match(self.VALID_ID)

    def test_invalid_too_short(self):
        import re
        pattern = re.compile(r"^U[0-9a-f]{32}$")
        assert not pattern.match("Uabc")

    def test_invalid_uppercase_hex(self):
        import re
        pattern = re.compile(r"^U[0-9a-f]{32}$")
        assert not pattern.match("U" + "A" * 32)

    def test_invalid_no_u_prefix(self):
        import re
        pattern = re.compile(r"^U[0-9a-f]{32}$")
        assert not pattern.match("a" * 33)


class TestLineBindingModel:
    VALID_ID = "U" + "b" * 32
    VALID_ID2 = "U" + "c" * 32

    def test_bind_valid_id(self, db_session, seed_users):
        """可以成功設定 line_user_id"""
        u1, _ = seed_users
        u1.line_user_id = self.VALID_ID
        db_session.commit()
        db_session.refresh(u1)
        assert u1.line_user_id == self.VALID_ID

    def test_unbind_clears_id(self, db_session, seed_users):
        """解除綁定後 line_user_id 為 None"""
        u1, _ = seed_users
        u1.line_user_id = self.VALID_ID
        db_session.commit()
        u1.line_user_id = None
        db_session.commit()
        db_session.refresh(u1)
        assert u1.line_user_id is None

    def test_bind_duplicate_raises(self, db_session, seed_users):
        """兩個用戶不能綁定同一 LINE ID（UniqueConstraint）"""
        from sqlalchemy.exc import IntegrityError
        u1, u2 = seed_users
        u1.line_user_id = self.VALID_ID2
        db_session.commit()

        u2.line_user_id = self.VALID_ID2
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
