"""tests/test_activity_restore_lock_order.py

Restore 多課程 PostgreSQL 死鎖（code review #3，Medium）。

問題：restore_registration 取得 RegistrationCourse 後，依未排序結果逐門
`ActivityCourse ... with_for_update().first()` 鎖課程。兩筆報名分別含 [A,B]、
[B,A] 並行 restore 時可能形成 ABBA 循環等待。advisory lock 是 per-student，
不同學生共用課程的並行 restore 不被序列化，無法擋此死鎖。

修正：先收集 course_id、排序，一次以 id 排序整批鎖定所有課程（對齊
register_courses 的批次鎖策略）。

測法：spy sqlalchemy Query.with_for_update，斷言 restore 一筆雙課程報名時
ActivityCourse 只被鎖『一次』（單一批次查詢），而非逐課多次。SQLite 下
with_for_update 為 no-op，但仍會被呼叫，故可驗證鎖取得『次數/批次性』。
"""

import os
import sys

import sqlalchemy.orm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from utils.academic import resolve_current_academic_term
from tests.test_activity_restore_capacity import (  # noqa: F401
    restore_client,
    _add_admin,
    _login,
)


def _seed_rejected_two_courses(sf):
    """造一筆 rejected 報名，含兩門 active 課程（皆 enrolled）。"""
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        _add_admin(s)
        c1 = ActivityCourse(
            name="圍棋",
            price=300,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        c2 = ActivityCourse(
            name="畫畫",
            price=500,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add_all([c1, c2])
        s.flush()
        reg = ActivityRegistration(
            student_name="林小華",
            birthday="2020-03-03",
            parent_phone="0911111111",
            school_year=sy,
            semester=sem,
            is_active=False,
            match_status="rejected",
            pending_review=False,
            paid_amount=0,
            is_paid=False,
        )
        s.add(reg)
        s.flush()
        s.add_all(
            [
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=c1.id,
                    status="enrolled",
                    price_snapshot=300,
                ),
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=c2.id,
                    status="enrolled",
                    price_snapshot=500,
                ),
            ]
        )
        s.commit()
        return reg.id


def test_restore_locks_all_courses_in_single_batch(restore_client, monkeypatch):
    """雙課程 restore：ActivityCourse 應以單一批次 FOR UPDATE 鎖定（只 1 次），
    而非逐課鎖（2 次），消除 ABBA 死鎖窗口。"""
    client, sf = restore_client
    reg_id = _seed_rejected_two_courses(sf)
    _login(client)

    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    course_locks = [e for e in recorded if e is ActivityCourse]
    assert len(course_locks) == 1, (
        "雙課程 restore 應以單一批次鎖定 ActivityCourse（防 ABBA 死鎖），"
        f"實際鎖了 {len(course_locks)} 次"
    )


def test_restore_two_courses_still_processed(restore_client):
    """反回歸：批次鎖後雙課程仍正常處理（皆保持 enrolled、計時欄清空）。"""
    client, sf = restore_client
    reg_id = _seed_rejected_two_courses(sf)
    _login(client)

    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    with sf() as s:
        rcs = s.query(RegistrationCourse).filter_by(registration_id=reg_id).all()
        assert len(rcs) == 2
        for rc in rcs:
            assert rc.status == "enrolled"
            assert rc.confirm_deadline is None
