"""admin_feed endpoint 整合測試。

每個 layer 的 fetch 細節由本檔負責 end-to-end 驗證，
不另寫 unit test（fetcher 是 endpoint 的私有實作）。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.calendar_admin import router as calendar_admin_router
from models.activity import ActivityCourse, ActivitySession
from models.appraisal import AppraisalCycle, Semester
from models.base import Base
from models.database import User
from models.employee import Employee
from models.event import Holiday, SchoolEvent, WorkdayOverride
from models.leave import LeaveRecord
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def calendar_admin_client(tmp_path):
    db_path = tmp_path / "calendar_admin.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(calendar_admin_router, prefix="/api/calendar")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, session_factory):
    """建一個全權限 admin 並登入回傳 access_token。"""
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "AdminPass1"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


def _make_employee(session_factory, name="王老師", employee_id="E0001"):
    """造員工 — 至少必填 employee_id(unique)+name。"""
    with session_factory() as s:
        emp = Employee(employee_id=employee_id, name=name)
        s.add(emp)
        s.commit()
        s.refresh(emp)
        return emp.id


def _login_with_permissions(client, session_factory, username, permissions_int):
    """造一個指定 permissions 的 user 並登入；用於跨權限矩陣測試。"""
    with session_factory() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password("Pass1234"),
                role="staff",
                permissions=permissions_int,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": "Pass1234"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


# ---------------------------------------------------------------------------
# 邊界 / 參數驗證 (6 tests)
# ---------------------------------------------------------------------------


def test_window_over_90_days_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-01-01", "to": "2026-05-01"},  # 121 天
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    # 區分自定 422（"window exceeds"）與 FastAPI 內建 validation 422
    assert "window" in r.json()["detail"].lower()


def test_to_before_from_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-31", "to": "2026-05-01"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422
    assert "to must be" in r.json()["detail"].lower()


def test_missing_from_returns_422(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"to": "2026-05-31"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422


def test_unauthenticated_returns_401(calendar_admin_client):
    client, _ = calendar_admin_client
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31"},
    )
    assert r.status_code == 401


def test_empty_window_returns_empty_items(calendar_admin_client):
    """無任何資料的 window 回 200 + items=[]"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "2099-01-01"
    assert body["to"] == "2099-01-07"
    assert body["items"] == []


def test_unknown_layer_ignored(calendar_admin_client):
    """`?layers=foo` 不報錯，當作沒指定有效 layer。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07", "layers": "foo,bar"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# event layer (Task 4)
# ---------------------------------------------------------------------------


def test_event_layer_basic(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="家長會",
                event_date=date(2026, 5, 20),
                event_type="meeting",
                is_active=True,
                requires_acknowledgment=False,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["layer"] == "event"
    assert it["title"] == "家長會"
    assert it["start"] == "2026-05-20"
    assert it["end"] == "2026-05-20"
    assert it["color"] == "#10b981"
    assert it["link"] == f"/calendar?eventId={it['id']}"


def test_event_multi_day_uses_end_date(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="校外教學",
                event_date=date(2026, 5, 20),
                end_date=date(2026, 5, 22),
                event_type="activity",
                is_active=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["start"] == "2026-05-20"
    assert items[0]["end"] == "2026-05-22"


def test_event_requires_ack_uses_ack_color(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="家長簽閱通知",
                event_date=date(2026, 5, 20),
                is_active=True,
                requires_acknowledgment=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.json()["items"][0]["color"] == "#ef4444"


def test_event_inactive_excluded(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="已停用",
                event_date=date(2026, 5, 20),
                is_active=False,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.json()["items"] == []


def test_event_multi_day_spanning_window_start(calendar_admin_client):
    """event_date 在 window 開始前但 end_date 落入 window 內 → 應被納入。

    鎖定 overlap clause 的非 NULL 分支（end_date >= from_）。
    """
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            SchoolEvent(
                title="跨月活動",
                event_date=date(2026, 4, 28),
                end_date=date(2026, 5, 2),
                is_active=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["start"] == "2026-04-28"
    assert items[0]["end"] == "2026-05-02"


# ---------------------------------------------------------------------------
# holiday layer (Task 5)
# ---------------------------------------------------------------------------


def test_holiday_layer_basic(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add_all(
            [
                Holiday(date=date(2026, 5, 1), name="勞動節"),
                WorkdayOverride(date=date(2026, 5, 16), name="補上 5/1"),
            ]
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "holiday"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    by_date = {it["start"]: it for it in items}

    assert by_date["2026-05-01"]["title"] == "勞動節"
    assert by_date["2026-05-01"]["color"] == "#f59e0b"
    assert by_date["2026-05-01"]["id"] == "holiday:2026-05-01"
    assert by_date["2026-05-01"]["link"] is None

    assert by_date["2026-05-16"]["title"] == "補上 5/1"
    assert by_date["2026-05-16"]["color"] == "#6366f1"
    assert by_date["2026-05-16"]["id"] == "workday_override:2026-05-16"
    assert by_date["2026-05-16"]["link"] is None


# ---------------------------------------------------------------------------
# leave layer (Task 6)
# ---------------------------------------------------------------------------


def test_leave_layer_approved_and_pending(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    emp_id = _make_employee(sf)
    with sf() as s:
        s.add_all(
            [
                LeaveRecord(
                    employee_id=emp_id,
                    leave_type="sick",
                    start_date=date(2026, 5, 10),
                    end_date=date(2026, 5, 11),
                    is_approved=True,
                ),
                LeaveRecord(
                    employee_id=emp_id,
                    leave_type="annual",
                    start_date=date(2026, 5, 15),
                    end_date=date(2026, 5, 15),
                    is_approved=None,  # pending
                ),
                LeaveRecord(
                    employee_id=emp_id,
                    leave_type="personal",
                    start_date=date(2026, 5, 20),
                    end_date=date(2026, 5, 20),
                    is_approved=False,  # rejected — 應被過濾
                ),
            ]
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "leave"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 2

    colors = sorted({it["color"] for it in items})
    assert colors == ["#0ea5e9", "#94a3b8"]  # approved blue + pending gray

    # title 包含員工名 + leave_type
    assert any("王老師" in it["title"] and "sick" in it["title"] for it in items)


def test_leave_without_permission_excluded(calendar_admin_client):
    """無 LEAVES_READ 的 caller 不應看到 leave layer，但能看到 event。"""
    client, sf = calendar_admin_client
    # CALENDAR (1<<2) but no LEAVES_READ (1<<5)
    tok = _login_with_permissions(client, sf, "viewer", 1 << 2)
    emp_id = _make_employee(sf)
    with sf() as s:
        s.add(
            LeaveRecord(
                employee_id=emp_id,
                leave_type="sick",
                start_date=date(2026, 5, 10),
                end_date=date(2026, 5, 10),
                is_approved=True,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "leave"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# activity layer (Task 7)
# ---------------------------------------------------------------------------


def test_activity_layer_joins_course_name(calendar_admin_client):
    """場次帶出課程名 + 「第 N 堂」（N 為該課程內依日期排序之序號，跨 window 全域）。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        course = ActivityCourse(name="陶藝班", price=2000)
        s.add(course)
        s.flush()
        # 額外塞一筆 window 外的早場次，驗證 session_no 是全域序號 (=1)
        # 落在 window 內的兩堂應為第 2、3 堂
        s.add_all(
            [
                ActivitySession(course_id=course.id, session_date=date(2026, 4, 26)),
                ActivitySession(course_id=course.id, session_date=date(2026, 5, 10)),
                ActivitySession(course_id=course.id, session_date=date(2026, 5, 17)),
            ]
        )
        s.commit()
        course_id = course.id

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "activity"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    titles = sorted(it["title"] for it in items)
    assert titles == ["陶藝班 第2堂", "陶藝班 第3堂"]
    for it in items:
        assert it["layer"] == "activity"
        assert it["color"] == "#ec4899"
        assert it["link"] == f"/activity?courseId={course_id}"
        assert it["meta"]["course_id"] == course_id


def test_activity_without_permission_excluded(calendar_admin_client):
    """無 ACTIVITY_READ (1<<27) caller 看不到 activity layer。"""
    client, sf = calendar_admin_client
    tok = _login_with_permissions(client, sf, "viewer_act", 1 << 2)  # CALENDAR only
    with sf() as s:
        course = ActivityCourse(name="繪畫", price=1500)
        s.add(course)
        s.flush()
        s.add(ActivitySession(course_id=course.id, session_date=date(2026, 5, 10)))
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "activity"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# appraisal layer (Task 8)
# ---------------------------------------------------------------------------


def test_appraisal_three_milestones_per_cycle(calendar_admin_client):
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        cycle = AppraisalCycle(
            academic_year=114,
            semester=Semester.FIRST,
            start_date=date(2026, 5, 5),
            end_date=date(2026, 5, 25),
            base_score_calc_date=date(2026, 5, 15),
            base_score=0,
        )
        s.add(cycle)
        s.commit()
        cycle_id = cycle.id

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "appraisal"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 3
    starts = sorted(it["start"] for it in items)
    assert starts == ["2026-05-05", "2026-05-15", "2026-05-25"]

    by_milestone = {it["meta"]["milestone"]: it for it in items}
    assert "開始" in by_milestone["start_date"]["title"]
    assert "結束" in by_milestone["end_date"]["title"]
    assert "基準分結算" in by_milestone["base_score_calc_date"]["title"]

    for it in items:
        assert it["color"] == "#dc2626"
        assert it["link"] == f"/appraisal?cycleId={cycle_id}"


def test_appraisal_milestone_outside_window_excluded(calendar_admin_client):
    """cycle 的 end_date 在 window 外、start_date 在 window 內 → 只下發 start。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(
            AppraisalCycle(
                academic_year=114,
                semester=Semester.FIRST,
                start_date=date(2026, 5, 10),
                end_date=date(2026, 8, 30),
                base_score_calc_date=date(2026, 6, 30),
                base_score=0,
            )
        )
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "appraisal"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["meta"]["milestone"] == "start_date"
