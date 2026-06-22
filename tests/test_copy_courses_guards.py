"""tests/test_copy_courses_guards.py — copy_courses_from_previous 安全守衛測試。

涵蓋：
- Bug A：高價課複製需 ACTIVITY_PAYMENT_APPROVE 簽核守衛（P3 authz 內控不一致）
- Bug B：目標學期同名課程已存在時逐筆跳過，不整批 500（P3 並發/縱深防禦）
"""

import os
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import (
    _public_inquiry_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityCourse, Base, User
from utils.academic import resolve_current_academic_term
from utils.activity_constants import ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD
from utils.auth import hash_password

# ─────────────────────────────────────────────────────────────────── #
# Fixture
# ─────────────────────────────────────────────────────────────────── #


@pytest.fixture
def client_factory(tmp_path):
    db_path = tmp_path / "copy_guards.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_inquiry_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(sf, *, username: str, password: str, permission_names: list[str]):
    """在資料庫建立 admin 角色使用者並回傳。"""
    with sf() as s:
        user = User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            permission_names=permission_names,
            is_active=True,
        )
        s.add(user)
        s.commit()


def _login(client, *, username: str, password: str):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, f"登入失敗: {res.json()}"
    return res


def _source_term():
    """回傳與當前學期不同的（來源, 目標）學期對。"""
    sy, sem = resolve_current_academic_term()
    other_sem = 2 if sem == 1 else 1
    return sy, other_sem, sy, sem  # source_sy, source_sem, target_sy, target_sem


# ─────────────────────────────────────────────────────────────────── #
# Bug A：高價課複製需 ACTIVITY_PAYMENT_APPROVE
# ─────────────────────────────────────────────────────────────────── #

HIGH_PRICE = ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD + 1  # 30_001


class TestCopyCoursesHighPriceGuard:
    """copy_courses_from_previous 複製高價課程時應套用與 create_course / update_course 相同的
    require_approve_for_high_price 守衛。"""

    def test_copy_high_price_course_without_approve_perm_returns_403(
        self, client_factory
    ):
        """僅具 ACTIVITY_WRITE（無 ACTIVITY_PAYMENT_APPROVE）時複製高價課程 → 403。

        修前：整批成功複製。修後：403 整批拒絕。
        """
        client, sf = client_factory
        _create_user(
            sf,
            username="writer",
            password="Pass1234!",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        )
        _login(client, username="writer", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="昂貴課程",
                    price=HIGH_PRICE,
                    sessions=10,
                    capacity=5,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": tgt_sy,
                "target_semester": tgt_sem,
            },
        )
        assert (
            res.status_code == 403
        ), f"預期 403，實際回傳 {res.status_code}: {res.json()}"

    def test_copy_high_price_course_with_approve_perm_succeeds(self, client_factory):
        """具 ACTIVITY_PAYMENT_APPROVE 時複製高價課程 → 201 成功。"""
        client, sf = client_factory
        _create_user(
            sf,
            username="approver",
            password="Pass1234!",
            permission_names=[
                "ACTIVITY_READ",
                "ACTIVITY_WRITE",
                "ACTIVITY_PAYMENT_APPROVE",
            ],
        )
        _login(client, username="approver", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="昂貴課程",
                    price=HIGH_PRICE,
                    sessions=10,
                    capacity=5,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": tgt_sy,
                "target_semester": tgt_sem,
            },
        )
        assert (
            res.status_code == 201
        ), f"預期 201，實際回傳 {res.status_code}: {res.json()}"
        assert res.json()["created"] == 1

    def test_copy_low_price_courses_without_approve_perm_succeeds(self, client_factory):
        """低價課程不觸發守衛，僅具 ACTIVITY_WRITE 即可複製。"""
        client, sf = client_factory
        _create_user(
            sf,
            username="writer2",
            password="Pass1234!",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        )
        _login(client, username="writer2", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="平價課程",
                    price=ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD,  # 邊界值（不超過）
                    sessions=10,
                    capacity=5,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": tgt_sy,
                "target_semester": tgt_sem,
            },
        )
        assert (
            res.status_code == 201
        ), f"邊界價格不應擋，實際回傳 {res.status_code}: {res.json()}"
        assert res.json()["created"] == 1


# ─────────────────────────────────────────────────────────────────── #
# Bug B：同名課程已存在時應逐筆跳過，不整批 500
# ─────────────────────────────────────────────────────────────────── #


class TestCopyCoursesNameConflictSkip:
    """目標學期已有同名 active 課程時，copy_courses_from_previous 應跳過衝突者，
    繼續建立其餘課程，而非因 unique constraint violation 整批 500。

    SQLite 測試環境不使用 advisory lock（no-op），但 savepoint 語意相同：
    衝突列回滾到 nested savepoint，其餘列正常 commit。
    """

    def test_duplicate_name_in_target_skipped_not_500(self, client_factory):
        """目標學期已有同名課程 → 該課跳過（skipped=1），其餘正常建立（created≥1），
        不回 500。

        修前（使用 session.flush() 直接撞 unique）：整批 rollback 回 500。
        修後（begin_nested savepoint）：衝突列跳過，其餘成功。
        """
        client, sf = client_factory
        _create_user(
            sf,
            username="admin",
            password="Pass1234!",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        )
        _login(client, username="admin", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()

        with sf() as s:
            # 來源學期兩門課
            s.add(
                ActivityCourse(
                    name="重複課程",
                    price=1000,
                    sessions=10,
                    capacity=10,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            s.add(
                ActivityCourse(
                    name="新課程",
                    price=800,
                    sessions=8,
                    capacity=8,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            # 目標學期已存在「重複課程」（is_active=True 會觸發 partial unique）
            s.add(
                ActivityCourse(
                    name="重複課程",
                    price=9999,
                    sessions=5,
                    capacity=5,
                    school_year=tgt_sy,
                    semester=tgt_sem,
                    is_active=True,
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": tgt_sy,
                "target_semester": tgt_sem,
            },
        )
        # 不應整批 500
        assert (
            res.status_code == 201
        ), f"預期 201，實際回傳 {res.status_code}: {res.json()}"
        data = res.json()
        # 「重複課程」已存在應被跳過
        assert data["skipped"] == 1, f"預期 skipped=1，實際 {data}"
        # 「新課程」應成功建立
        assert data["created"] == 1, f"預期 created=1，實際 {data}"

    def test_all_names_exist_returns_zero_created(self, client_factory):
        """目標學期全部同名時 → created=0 skipped=N，不 500。"""
        client, sf = client_factory
        _create_user(
            sf,
            username="admin2",
            password="Pass1234!",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        )
        _login(client, username="admin2", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()

        with sf() as s:
            for name in ["課程A", "課程B"]:
                s.add(
                    ActivityCourse(
                        name=name,
                        price=500,
                        sessions=5,
                        capacity=5,
                        school_year=src_sy,
                        semester=src_sem,
                        is_active=True,
                    )
                )
                s.add(
                    ActivityCourse(
                        name=name,
                        price=500,
                        sessions=5,
                        capacity=5,
                        school_year=tgt_sy,
                        semester=tgt_sem,
                        is_active=True,
                    )
                )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": tgt_sy,
                "target_semester": tgt_sem,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["created"] == 0
        assert data["skipped"] == 2


# ─────────────────────────────────────────────────────────────────── #
# Bug B 延伸：非 IntegrityError 例外不應被靜默吞掉（收窄守衛）
# ─────────────────────────────────────────────────────────────────── #


class TestCopyCoursesNonIntegrityErrorPropagates:
    """savepoint 內拋出非 IntegrityError（如 RuntimeError）時，例外應往外傳播，
    不被靜默計入 skipped、不回正常 201。

    修前（except Exception）：RuntimeError 被吞 → 回 201 created=0 skipped=1（靜默錯誤）。
    修後（except IntegrityError）：RuntimeError 傳播 → 回 500。
    """

    def test_runtime_error_in_flush_propagates_not_silenced(self, client_factory):
        """session.flush 拋 RuntimeError 時，端點應回 500 而非 201。"""
        client, sf = client_factory
        _create_user(
            sf,
            username="admin3",
            password="Pass1234!",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        )
        _login(client, username="admin3", password="Pass1234!")

        src_sy, src_sem, tgt_sy, tgt_sem = _source_term()

        with sf() as s:
            s.add(
                ActivityCourse(
                    name="測試課程",
                    price=500,
                    sessions=5,
                    capacity=5,
                    school_year=src_sy,
                    semester=src_sem,
                    is_active=True,
                )
            )
            s.commit()

        # 讓 session.flush 拋一個非 IntegrityError 例外（模擬 DB 連線中斷等非預期錯誤）
        original_flush = None

        def flush_raising_runtime_error(*args, **kwargs):
            raise RuntimeError("模擬非預期的資料庫錯誤")

        with patch(
            "api.activity.courses.ActivityCourse.__init__",
            side_effect=RuntimeError("模擬非預期的資料庫錯誤"),
        ):
            res = client.post(
                "/api/activity/courses/copy-from-previous",
                json={
                    "source_school_year": src_sy,
                    "source_semester": src_sem,
                    "target_school_year": tgt_sy,
                    "target_semester": tgt_sem,
                },
            )

        # 修後（except IntegrityError）：RuntimeError 不被捕捉 → 500
        # 修前（except Exception）：RuntimeError 被吞 → 201 skipped=1（錯誤的靜默行為）
        assert (
            res.status_code == 500
        ), f"非 IntegrityError 例外應傳播為 500，實際回傳 {res.status_code}: {res.json()}"
