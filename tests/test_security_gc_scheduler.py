"""security_gc_scheduler 安全網測試（不可逆刪除正確性）。

security_gc 排程含多個不可逆 GC。本檔鎖住兩個個資/資安敏感刪除函式的安全屬性，
避免日後 regression 誤刪「仍須保留」的資料：

- `_gc_recruitment_geocode_cache`：招生地址 90 天 retention（個資法 §19）。
  **安全關鍵**：`resolved_at` 為 NULL（pending/failed）的 row 永不刪。
- `gc_staff_refresh_tokens`：員工端 refresh token GC（Spec F §3.5）。
  **安全關鍵**：未過期 / 近 7 天內 revoke 的 token 不刪
  （別砍掉仍在用的 session；近期撤銷保留供稽核）。

每個「會刪」屬性都配一個「保留」見證（grace 窗、NULL resolved_at），證明測試非 vacuous。
時間基準用 `now_taipei_naive()`：geocode GC 內部雖用 utcnow()，但本檔所有偏移皆 ≥ 數天，
8 小時時區差不影響 89/91 天邊界判定。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base, User
from models.recruitment import RecruitmentGeocodeCache
from models.staff_refresh_token import StaffRefreshToken
from services import security_gc_scheduler as sched
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    s = SessionFactory()
    yield s
    s.close()
    engine.dispose()


# ── _gc_recruitment_geocode_cache：招生地址 90 天 retention ──────────────────


def _add_geocode(session, *, address, resolved_at, status="resolved"):
    session.add(
        RecruitmentGeocodeCache(
            address=address,
            status=status,
            resolved_at=resolved_at,
        )
    )
    session.commit()


class TestRecruitmentGeocodeCacheGC:
    def test_resolved_long_ago_is_deleted(self, session):
        _add_geocode(
            session,
            address="台北市A路",
            resolved_at=now_taipei_naive() - timedelta(days=200),
        )
        deleted = sched._gc_recruitment_geocode_cache(session)
        session.commit()
        assert deleted == 1
        assert session.query(RecruitmentGeocodeCache).count() == 0

    def test_recently_resolved_is_kept(self, session):
        _add_geocode(
            session,
            address="台北市B路",
            resolved_at=now_taipei_naive() - timedelta(days=5),
        )
        deleted = sched._gc_recruitment_geocode_cache(session)
        session.commit()
        assert deleted == 0
        assert session.query(RecruitmentGeocodeCache).count() == 1

    def test_unresolved_null_is_never_deleted_even_if_old(self, session):
        """安全關鍵：pending/failed（resolved_at IS NULL）即使建立很久也不刪。"""
        session.add(
            RecruitmentGeocodeCache(
                address="台北市C路", status="pending", resolved_at=None
            )
        )
        session.commit()
        deleted = sched._gc_recruitment_geocode_cache(session)
        session.commit()
        assert deleted == 0
        assert session.query(RecruitmentGeocodeCache).count() == 1

    def test_boundary_89_days_kept_91_days_deleted(self, session):
        _add_geocode(
            session,
            address="台北市D路",
            resolved_at=now_taipei_naive() - timedelta(days=89),
        )
        _add_geocode(
            session,
            address="台北市E路",
            resolved_at=now_taipei_naive() - timedelta(days=91),
        )
        deleted = sched._gc_recruitment_geocode_cache(session)
        session.commit()
        assert deleted == 1
        remaining = {r.address for r in session.query(RecruitmentGeocodeCache).all()}
        assert remaining == {"台北市D路"}

    def test_mixed_only_old_resolved_deleted(self, session):
        _add_geocode(
            session,
            address="old-resolved",
            resolved_at=now_taipei_naive() - timedelta(days=120),
        )
        _add_geocode(
            session,
            address="recent-resolved",
            resolved_at=now_taipei_naive() - timedelta(days=3),
        )
        session.add(
            RecruitmentGeocodeCache(
                address="pending-old", status="pending", resolved_at=None
            )
        )
        session.commit()
        deleted = sched._gc_recruitment_geocode_cache(session)
        session.commit()
        assert deleted == 1
        remaining = {r.address for r in session.query(RecruitmentGeocodeCache).all()}
        assert remaining == {"recent-resolved", "pending-old"}


# ── gc_staff_refresh_tokens：員工 refresh token 7 天 grace GC ────────────────


def _add_token(session, *, token_hash, expires_at, revoked_at=None, user_id=1):
    session.add(
        StaffRefreshToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            revoked_at=revoked_at,
        )
    )
    session.commit()


@pytest.fixture
def staff_session(session):
    session.add(
        User(
            id=1,
            username="staff",
            password_hash="x",
            role="teacher",
            is_active=True,
            token_version=0,
        )
    )
    session.commit()
    return session


def _patch_scope(session, monkeypatch):
    @contextmanager
    def _scope():
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    monkeypatch.setattr(sched, "session_scope", _scope)


class TestStaffRefreshTokenGC:
    def test_active_token_is_kept(self, staff_session, monkeypatch):
        """安全關鍵：未過期、未撤銷的 token 不刪（別砍掉仍在用的 session）。"""
        _add_token(
            staff_session,
            token_hash="active",
            expires_at=now_taipei_naive() + timedelta(days=30),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 0
        assert staff_session.query(StaffRefreshToken).count() == 1

    def test_long_expired_token_is_deleted(self, staff_session, monkeypatch):
        _add_token(
            staff_session,
            token_hash="expired",
            expires_at=now_taipei_naive() - timedelta(days=30),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 1
        assert staff_session.query(StaffRefreshToken).count() == 0

    def test_recently_expired_within_grace_is_kept(self, staff_session, monkeypatch):
        """互証非 vacuous：2 天前過期仍在 7 天 grace 內 → 不刪。"""
        _add_token(
            staff_session,
            token_hash="expired_recent",
            expires_at=now_taipei_naive() - timedelta(days=2),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 0
        assert staff_session.query(StaffRefreshToken).count() == 1

    def test_long_revoked_token_is_deleted(self, staff_session, monkeypatch):
        _add_token(
            staff_session,
            token_hash="revoked",
            expires_at=now_taipei_naive() + timedelta(days=30),
            revoked_at=now_taipei_naive() - timedelta(days=30),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 1
        assert staff_session.query(StaffRefreshToken).count() == 0

    def test_recently_revoked_within_grace_is_kept(self, staff_session, monkeypatch):
        """安全關鍵：近 7 天內撤銷的 token 保留供稽核。"""
        _add_token(
            staff_session,
            token_hash="revoked_recent",
            expires_at=now_taipei_naive() + timedelta(days=30),
            revoked_at=now_taipei_naive() - timedelta(days=2),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 0
        assert staff_session.query(StaffRefreshToken).count() == 1

    def test_mixed_only_stale_deleted(self, staff_session, monkeypatch):
        _add_token(
            staff_session,
            token_hash="m_active",
            expires_at=now_taipei_naive() + timedelta(days=30),
        )
        _add_token(
            staff_session,
            token_hash="m_expired",
            expires_at=now_taipei_naive() - timedelta(days=30),
        )
        _add_token(
            staff_session,
            token_hash="m_revoked",
            expires_at=now_taipei_naive() + timedelta(days=30),
            revoked_at=now_taipei_naive() - timedelta(days=30),
        )
        _patch_scope(staff_session, monkeypatch)
        assert sched.gc_staff_refresh_tokens() == 2
        remaining = {t.token_hash for t in staff_session.query(StaffRefreshToken).all()}
        assert remaining == {"m_active"}


# ── _gc_report_snapshots：報表快照表 1 天 retention（只增不減快取表防膨脹）─────


def _add_report_snapshot(session, *, cache_key, expires_at):
    from models.report_cache import ReportSnapshot

    session.add(
        ReportSnapshot(
            cache_key=cache_key,
            category="finance",
            payload="{}",
            expires_at=expires_at,
        )
    )
    session.commit()


class TestReportSnapshotGC:
    def test_long_expired_is_deleted(self, session):
        _add_report_snapshot(
            session,
            cache_key="k_old",
            expires_at=now_taipei_naive() - timedelta(days=10),
        )
        deleted = sched._gc_report_snapshots(session)
        session.commit()
        from models.report_cache import ReportSnapshot

        assert deleted == 1
        assert session.query(ReportSnapshot).count() == 0

    def test_recently_expired_within_retention_is_kept(self, session):
        # 過期但未逾 1 天 retention → 保留（避免剛失效即被刪）
        _add_report_snapshot(
            session,
            cache_key="k_grace",
            expires_at=now_taipei_naive() - timedelta(hours=2),
        )
        deleted = sched._gc_report_snapshots(session)
        session.commit()
        from models.report_cache import ReportSnapshot

        assert deleted == 0
        assert session.query(ReportSnapshot).count() == 1

    def test_not_yet_expired_is_kept(self, session):
        _add_report_snapshot(
            session,
            cache_key="k_fresh",
            expires_at=now_taipei_naive() + timedelta(hours=1),
        )
        deleted = sched._gc_report_snapshots(session)
        session.commit()
        from models.report_cache import ReportSnapshot

        assert deleted == 0
        assert session.query(ReportSnapshot).count() == 1
