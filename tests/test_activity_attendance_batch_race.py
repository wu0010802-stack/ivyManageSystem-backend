"""
tests/test_activity_attendance_batch_race.py
─────────────────────────────────────────────
P2 併發修補測試：批次點名「snapshot 過期」→ IntegrityError 整批回滾。

情境模擬：
  - 管理端 batch_update_attendance、教師端 portal_batch_update_attendance
  - existing_map 建立時 DB 還無此 (session_id, registration_id) 列
  - 在 existing_map 建立後、session.add() 之前，另一請求已 commit 同一列
  - → 較慢者 add 撞 uq_activity_attendance_session_reg

修補前（RED）：撞唯一約束 → IntegrityError / HTTP 500，整批回滾。
修補後（GREEN）：不 500、回 {"ok": True, ...}，且該列被更新成 body 帶入的 is_present。

實作策略：
  - 用 TestClient + monkeypatch base_module._engine/_SessionFactory（對齊
    test_activity_attendance_audit.py 的整合測試慣例）
  - 先呼叫 PUT endpoint，在其「建完 existing_map 後、add() 前」hook 上
    用另一個 session 插入同一列並 commit（模擬併發請求已先落地）
  - 實作上：monkeypatch api/activity/attendance.py 裡的 ActivityAttendance
    constructor（或更可靠的方式：在 session.add() 前攔截，直接往 DB 插入衝突列）
  - 採最可移植方式：monkeypatch query_valid_session_registrations 使其在
    每次呼叫時額外 pre-insert 衝突列（不依賴時序、SQLite PG 皆可）。

  更精準的做法：透過 SQLAlchemy session event 在 before_flush 攔截；
  但為保持可讀性，改採 monkeypatch 方式：在端點的 existing_map 建立查詢
  回傳之前，由測試往 DB 偷偷插入一筆已 commit 的衝突列，使 session 內
  identity map 快取看不到（其他 session committed）而查詢快取已過期。

  最簡潔可移植做法：在測試端，endpoint call 前先 commit 一筆 attendance，
  然後 monkeypatch query() 讓它回空（simulate stale snapshot）→ 端點走 add
  分支 → 修前 500，修後成功更新。

  Monkeypatch 目標：
    models.activity.ActivityAttendance（同名 attribute 在 session.query 裡）
  實際上 patch session.query via 自訂 Session subclass：
    重寫 query() ——對 ActivityAttendance 過濾 `existing_map` 查詢回 []，
    讓端點誤以為沒有舊紀錄（stale snapshot），走 add 分支。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session as _SASession
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.database import get_session  # noqa: F401
from api.activity import router as activity_router
from api.portal.activity import router as portal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from tests.test_activity_pos import _create_admin, _login, _setup_reg
from utils.academic import resolve_current_academic_term

# ── Fixture：整合測試環境（對齊 test_activity_attendance_audit.py） ───────────


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "batch_race.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    # Portal router 需掛在 /api/portal 前綴下才對得上
    from fastapi import APIRouter

    portal_wrap = APIRouter(prefix="/api/portal")
    portal_wrap.include_router(portal_router)
    app.include_router(portal_wrap)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ── 共用 setup ──────────────────────────────────────────────────────────────


def _setup_scene(sf):
    """建立課程、報名、場次，回傳 (course_id, session_id, reg_id)。"""
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, student_name="小明", course_name="圍棋")
        course_id = (
            s.query(ActivityCourse).filter(ActivityCourse.name == "圍棋").first().id
        )
        act_session = ActivitySession(
            course_id=course_id,
            session_date=date(2026, 6, 1),
            created_by="test",
        )
        s.add(act_session)
        s.flush()
        session_id = act_session.id
        reg_id = reg.id
        s.commit()
    return course_id, session_id, reg_id


def _pre_insert_attendance(sf, session_id: int, reg_id: int, is_present: bool = True):
    """模擬「另一個已 commit 的請求」先把 attendance 寫入 DB。"""
    with sf() as s:
        att = ActivityAttendance(
            session_id=session_id,
            registration_id=reg_id,
            is_present=is_present,
            notes="先來的請求",
            recorded_by="other_teacher",
        )
        s.add(att)
        s.commit()


# ── 測試輔助：Stale-snapshot session factory ──────────────────────────────────


def _make_stale_session_factory(real_sf, session_id: int, reg_id: int):
    """
    回傳一個 session factory，其產生的 Session 對
    `ActivityAttendance.session_id == session_id AND
     ActivityAttendance.registration_id IN [reg_id]`
    的查詢永遠回傳 []（模擬 existing_map 快照過期）。
    其餘查詢正常走真實 DB。
    """

    class StaleSession(_SASession):
        """繼承 Session，只攔截 existing_map 那次 ActivityAttendance 查詢。"""

        _stale_done: bool = False

        def query(self, *entities, **kwargs):
            q = super().query(*entities, **kwargs)
            # 只攔截第一次對 ActivityAttendance 的查詢（即 existing_map 建立）
            if (
                not self._stale_done
                and len(entities) == 1
                and entities[0] is ActivityAttendance
            ):
                self._stale_done = True

                class _EmptyQuery:
                    """假 query：filter / all 都回空，模擬 stale snapshot。"""

                    def filter(self, *a, **kw):
                        return self

                    def all(self):
                        return []

                return _EmptyQuery()
            return q

    return sessionmaker(bind=real_sf.kw["bind"], class_=StaleSession)


# ── 管理端：batch_update_attendance ──────────────────────────────────────────


class TestAdminBatchRace:
    """管理端 PUT /api/activity/attendance/sessions/{id}/records 併發修補。"""

    def test_admin_batch_race_red_before_fix(self, client, monkeypatch):
        """
        RED：修前——存在 stale existing_map + DB 已有衝突列 → 500。

        此測試在修補後應變為 PASS（GREEN），因為修補後不再 500。
        注意：由於這個測試檔是在修補後才寫入並執行，所以它直接測試修補後行為（GREEN）。
        驗證：即使 existing_map 為空（stale snapshot）但 DB 已有衝突列，
              端點不 500，回 {"ok": True, ...}，且 is_present 被更新。
        """
        c, sf = client
        _, session_id, reg_id = _setup_scene(sf)

        # 先用另一 session 插入衝突列（模擬另一請求已 commit）
        _pre_insert_attendance(sf, session_id, reg_id, is_present=True)

        # 換上 stale session factory：讓端點的 existing_map 看不到剛插入的列
        stale_sf = _make_stale_session_factory(sf, session_id, reg_id)
        monkeypatch.setattr(base_module, "_SessionFactory", stale_sf)

        _login(c)
        resp = c.put(
            f"/api/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {
                        "registration_id": reg_id,
                        "is_present": False,
                        "notes": "後來的請求",
                    }
                ]
            },
        )

        # GREEN（修後）：不 500
        assert (
            resp.status_code == 200
        ), f"預期 200，實際 {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["ok"] is True
        assert data["updated"] >= 1

        # 確認 DB 內 is_present 被更新為 False（後來請求的值）
        with sf() as s:
            att = (
                s.query(ActivityAttendance)
                .filter_by(session_id=session_id, registration_id=reg_id)
                .one()
            )
            assert (
                att.is_present is False
            ), f"預期 is_present=False（後來請求的值），實際 {att.is_present}"

    def test_admin_batch_race_other_rows_not_lost(self, client, monkeypatch):
        """
        GREEN：併發衝突只影響衝突列；同批其他列正常寫入，不因單列衝突整批回滾。
        """
        c, sf = client
        _, session_id, reg_id = _setup_scene(sf)

        # 再建一個額外報名（第二個 reg_id）
        with sf() as s:
            sy, sem = resolve_current_academic_term()
            course_id = (
                s.query(ActivityCourse).filter(ActivityCourse.name == "圍棋").first().id
            )
            reg2 = ActivityRegistration(
                student_name="小花",
                birthday="2019-05-01",
                class_name="大班",
                is_active=True,
                school_year=sy,
                semester=sem,
            )
            s.add(reg2)
            s.flush()
            s.add(
                RegistrationCourse(
                    registration_id=reg2.id,
                    course_id=course_id,
                    status="enrolled",
                    price_snapshot=1500,
                )
            )
            s.flush()
            reg2_id = reg2.id
            s.commit()

        # 只對 reg_id 預先插入衝突列；reg2_id 不插
        _pre_insert_attendance(sf, session_id, reg_id, is_present=True)

        stale_sf = _make_stale_session_factory(sf, session_id, reg_id)
        monkeypatch.setattr(base_module, "_SessionFactory", stale_sf)

        _login(c)
        resp = c.put(
            f"/api/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {"registration_id": reg_id, "is_present": False, "notes": "後來"},
                    {"registration_id": reg2_id, "is_present": True, "notes": "新增"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["updated"] == 2  # 兩筆都算成功

        with sf() as s:
            # reg_id：更新為 False
            att1 = (
                s.query(ActivityAttendance)
                .filter_by(session_id=session_id, registration_id=reg_id)
                .one()
            )
            assert att1.is_present is False
            # reg2_id：新增成功
            att2 = (
                s.query(ActivityAttendance)
                .filter_by(session_id=session_id, registration_id=reg2_id)
                .one_or_none()
            )
            assert att2 is not None, "reg2_id 的點名記錄不應因 reg_id 衝突而遺失"
            assert att2.is_present is True


# ── 教師端（Portal）：portal_batch_update_attendance ──────────────────────────


class TestPortalBatchRace:
    """教師端 PUT /api/portal/activity/attendance/sessions/{id}/records 併發修補。"""

    def test_portal_batch_race_conflict_resolved(self, client, monkeypatch):
        """
        GREEN：教師端同樣的 stale-snapshot 情境不 500，正確更新衝突列。
        """
        c, sf = client
        _, session_id, reg_id = _setup_scene(sf)

        _pre_insert_attendance(sf, session_id, reg_id, is_present=True)

        stale_sf = _make_stale_session_factory(sf, session_id, reg_id)
        monkeypatch.setattr(base_module, "_SessionFactory", stale_sf)

        _login(c)
        resp = c.put(
            f"/api/portal/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {
                        "registration_id": reg_id,
                        "is_present": False,
                        "notes": "portal後來",
                    }
                ]
            },
        )
        assert (
            resp.status_code == 200
        ), f"Portal 端預期 200，實際 {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["ok"] is True

        with sf() as s:
            att = (
                s.query(ActivityAttendance)
                .filter_by(session_id=session_id, registration_id=reg_id)
                .one()
            )
            assert att.is_present is False
