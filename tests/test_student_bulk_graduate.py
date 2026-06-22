"""POST /students/bulk-graduate — 批次畢業/轉出。

整班學年末一次處理；有效在讀學生原子處理，找不到/已非在讀/離園日早於入學日列入
skipped。對齊單筆 graduate_student 副作用（set_lifecycle_status + StudentChangeLog +
才藝軟刪 + 發放月薪資 stale）。用 test_db_session（swap 全域 engine 至 SQLite）確保
含 report_cache 失效在內的所有 get_session 都打測試庫、不誤寫 dev DB。
"""

import asyncio
import inspect
from datetime import date

import pytest
from fastapi import HTTPException

from api.students import bulk_graduate_students, StudentBulkGraduate
from models.classroom import (
    Classroom,
    ClassGrade,
    Student,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
)
from models.student_log import StudentChangeLog


def _run(coro):
    return asyncio.run(coro) if inspect.iscoroutine(coro) else coro


_ADMIN = {"user_id": 1, "username": "admin", "permission_names": ["*"], "role": "admin"}


def _seed(session, *, n=3, enrollment=date(2024, 9, 1)):
    grade = ClassGrade(name="大班", sort_order=3)
    session.add(grade)
    session.flush()
    room = Classroom(name="畢業班", school_year=114, semester=2, grade_id=grade.id)
    session.add(room)
    session.flush()
    ids = []
    for i in range(n):
        st = Student(
            student_id=f"G{i+1}",
            name=f"生{i+1}",
            classroom_id=room.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            enrollment_date=enrollment,
        )
        session.add(st)
        session.flush()
        ids.append(st.id)
    session.commit()
    return ids


def _call(student_ids, **kw):
    item = StudentBulkGraduate(
        student_ids=student_ids,
        graduation_date=kw.get("graduation_date", "2026-06-30"),
        status=kw.get("status", "已畢業"),
        reason=kw.get("reason"),
        notes=kw.get("notes"),
    )
    return _run(bulk_graduate_students(item=item, current_user=_ADMIN))


def test_graduates_whole_class(test_db_session):
    ids = _seed(test_db_session, n=3)
    result = _call(ids)
    assert result["graduated_count"] == 3
    assert sorted(result["succeeded_ids"]) == sorted(ids)
    assert result["skipped"] == []

    test_db_session.expire_all()
    for sid in ids:
        st = test_db_session.query(Student).get(sid)
        assert st.is_active is False
        assert st.lifecycle_status == LIFECYCLE_GRADUATED
        assert st.graduation_date == date(2026, 6, 30)
    # 每生一筆異動紀錄（事件=畢業）
    logs = test_db_session.query(StudentChangeLog).all()
    assert len(logs) == 3
    assert all(lg.event_type == "畢業" for lg in logs)


def test_transfer_out_status(test_db_session):
    ids = _seed(test_db_session, n=1)
    result = _call(ids, status="已轉出")
    assert result["graduated_count"] == 1
    test_db_session.expire_all()
    st = test_db_session.query(Student).get(ids[0])
    assert st.lifecycle_status == LIFECYCLE_TRANSFERRED
    log = test_db_session.query(StudentChangeLog).first()
    assert log.event_type == "轉出"


def test_skips_missing_inactive_and_early_date(test_db_session):
    ids = _seed(test_db_session, n=2, enrollment=date(2024, 9, 1))
    # 把第 2 個設為已非在讀
    test_db_session.expire_all()
    s2 = test_db_session.query(Student).get(ids[1])
    s2.is_active = False
    test_db_session.commit()

    # ids[0] 有效；ids[1] 已非在讀；99999 不存在
    result = _call([ids[0], ids[1], 99999])
    assert result["graduated_count"] == 1
    assert result["succeeded_ids"] == [ids[0]]
    reasons = {s["student_id"]: s["reason"] for s in result["skipped"]}
    assert reasons[ids[1]] == "已非在讀狀態"
    assert reasons[99999] == "找不到學生"


def test_skips_graduation_before_enrollment(test_db_session):
    ids = _seed(test_db_session, n=1, enrollment=date(2026, 9, 1))
    result = _call(ids, graduation_date="2026-06-30")  # 早於入學
    assert result["graduated_count"] == 0
    assert result["skipped"][0]["reason"] == "離園日期早於入學日期"
    test_db_session.expire_all()
    assert test_db_session.query(Student).get(ids[0]).is_active is True


def test_empty_ids_400(test_db_session):
    with pytest.raises(HTTPException) as exc:
        _call([])
    assert exc.value.status_code == 400


def test_bad_date_format_400(test_db_session):
    ids = _seed(test_db_session, n=1)
    with pytest.raises(HTTPException) as exc:
        _call(ids, graduation_date="2026/06/30")
    assert exc.value.status_code == 400


def test_duplicate_ids_processed_once(test_db_session):
    ids = _seed(test_db_session, n=1)
    result = _call([ids[0], ids[0]])
    assert result["graduated_count"] == 1
    assert test_db_session.query(StudentChangeLog).count() == 1


# ── 單筆 sync 拒絕（金流簽核 / 並發）→ skip 該生不整批中止 ──────────────────

_HR_NO_APPROVE = {
    "user_id": 2,
    "username": "hr1",
    "permission_names": ["STUDENTS_WRITE"],  # 無 ACTIVITY_PAYMENT_APPROVE
    "role": "hr",  # unrestricted role（過 require_unrestricted_role）
}


def _seed_paid_activity_reg(session, student_id):
    """為學生掛一筆當前學期、已繳費（paid_amount>0）的 active 才藝報名。"""
    from models.database import ActivityRegistration
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    reg = ActivityRegistration(
        student_name="付款生",
        school_year=sy,
        semester=sem,
        student_id=student_id,
        is_active=True,
        paid_amount=500,
        match_status="matched",
        pending_review=False,
    )
    session.add(reg)
    session.flush()
    return reg.id


def test_sync_403_skips_that_student_not_whole_batch(test_db_session):
    """某生有已繳費才藝報名、操作者無 ACTIVITY_PAYMENT_APPROVE → sync 回 403。
    應 skip 該生並續跑其餘（非整批中止），其餘學生正常畢業。"""
    ids = _seed(test_db_session, n=2)
    a, b = ids[0], ids[1]
    _seed_paid_activity_reg(test_db_session, a)
    test_db_session.commit()

    item = StudentBulkGraduate(
        student_ids=[a, b],
        graduation_date="2026-06-30",
        status="已畢業",
        reason=None,
        notes=None,
    )
    result = _run(bulk_graduate_students(item=item, current_user=_HR_NO_APPROVE))

    # A 被 skip（金流簽核權限），B 正常畢業——非整批 403 中止
    assert result["graduated_count"] == 1
    assert result["succeeded_ids"] == [b]
    reasons = {s["student_id"]: s["reason"] for s in result["skipped"]}
    assert a in reasons, result["skipped"]
    assert "簽核" in reasons[a] or "ACTIVITY_PAYMENT_APPROVE" in reasons[a]

    test_db_session.expire_all()
    assert test_db_session.query(Student).get(a).is_active is True, "A 不應被畢業"
    assert test_db_session.query(Student).get(b).is_active is False, "B 應正常畢業"
