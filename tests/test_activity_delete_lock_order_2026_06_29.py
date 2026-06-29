"""tests/test_activity_delete_lock_order_2026_06_29.py

第三輪才藝 review F2（中高）：停用課程/用品的 check-then-disable TOCTOU。

delete_course / delete_supply 流程為：
  1. SELECT 課程/用品（無鎖）
  2. count 該項的有效報名引用
  3. count == 0 → is_active = False; commit

報名端（register_courses / public_register）取 ActivityCourse 時是
`with_for_update()` 行鎖。但停用端的 SELECT 不鎖列 → 並發報名可在 count 與
commit 之間插入 RegistrationCourse，最終留下「有效報名引用已停用課程」，繞過
409 守衛。

修正：停用端對 ActivityCourse / ActivitySupply 的 SELECT 改 with_for_update，
在 count 之前鎖定該列。如此與報名端共用同一列鎖序列化：
  - 停用先拿到鎖 → 報名端 FOR UPDATE WHERE is_active=True 會等到 commit 後讀到
    is_active=False → 該課不在 locked_courses → 400 找不到課程；
  - 報名先拿到鎖 → 停用端等報名 commit 後 count>0 → 409。

測法（對齊 test_activity_restore_lock_order.py）：spy
sqlalchemy.orm.Query.with_for_update，斷言 delete 期間 ActivityCourse /
ActivitySupply 各被鎖定一次。SQLite 下 FOR UPDATE 為 no-op，但仍會被呼叫，
故可驗證「鎖確實存在」。
"""

import os
import sys

import sqlalchemy.orm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import ActivityCourse, ActivitySupply
from tests.test_activity_crud_fixes import (  # noqa: F401
    client_factory,
    _setup_admin,
)


def _spy_for_update(monkeypatch):
    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)
    return recorded


def test_delete_course_locks_course_row(client_factory, monkeypatch):
    """停用課程時應對 ActivityCourse 取 FOR UPDATE 行鎖（消 check-then-disable
    TOCTOU），而非無鎖 SELECT。"""
    client, sf = client_factory
    _setup_admin(sf, client)

    res = client.post("/api/activity/courses", json={"name": "圍棋", "price": 1200})
    assert res.status_code == 201, res.text
    course_id = res.json()["id"]

    recorded = _spy_for_update(monkeypatch)
    res = client.delete(f"/api/activity/courses/{course_id}")
    assert res.status_code == 200, res.text

    course_locks = [e for e in recorded if e is ActivityCourse]
    assert len(course_locks) >= 1, (
        "停用課程應對 ActivityCourse 取 FOR UPDATE 行鎖以序列化並發報名，"
        f"實際鎖了 {len(course_locks)} 次"
    )


def test_delete_supply_locks_supply_row(client_factory, monkeypatch):
    """停用用品時應對 ActivitySupply 取 FOR UPDATE 行鎖。"""
    client, sf = client_factory
    _setup_admin(sf, client)

    res = client.post("/api/activity/supplies", json={"name": "畫具組", "price": 300})
    assert res.status_code == 201, res.text
    supply_id = res.json()["id"]

    recorded = _spy_for_update(monkeypatch)
    res = client.delete(f"/api/activity/supplies/{supply_id}")
    assert res.status_code == 200, res.text

    supply_locks = [e for e in recorded if e is ActivitySupply]
    assert len(supply_locks) >= 1, (
        "停用用品應對 ActivitySupply 取 FOR UPDATE 行鎖以序列化並發報名，"
        f"實際鎖了 {len(supply_locks)} 次"
    )


def test_delete_course_with_active_registration_still_409(client_factory, monkeypatch):
    """反回歸：加鎖後，仍有有效報名引用的課程停用照樣回 409。"""
    from models.database import ActivityRegistration, RegistrationCourse
    from utils.academic import resolve_current_academic_term

    client, sf = client_factory
    _setup_admin(sf, client)

    res = client.post("/api/activity/courses", json={"name": "圍棋", "price": 1200})
    assert res.status_code == 201, res.text
    course_id = res.json()["id"]

    sy, sem = resolve_current_academic_term()
    with sf() as s:
        reg = ActivityRegistration(
            student_name="林小華",
            birthday="2020-03-03",
            parent_phone="0911111111",
            school_year=sy,
            semester=sem,
            is_active=True,
            match_status="manual",
            pending_review=False,
            paid_amount=0,
            is_paid=False,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course_id,
                status="enrolled",
                price_snapshot=1200,
            )
        )
        s.commit()

    res = client.delete(f"/api/activity/courses/{course_id}")
    assert res.status_code == 409, res.text
