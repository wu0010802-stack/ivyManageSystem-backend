"""才藝候補 sweep 家長解析批次化（P2 殘留 N+1 修補）。

主要 finding（sweep 三迴圈逐列查 course.name）已於 main 由 _course_name_map 批次修掉；
殘留為 _notify_parents → _resolve_parent_user_ids_for_registration 每筆 2 查詢
（reg→student + Guardian），在背景 sweep 逐 registration 觸發。

新增 _resolve_parent_user_ids_batch：reg→student 一次 in_() + Guardian 一次 in_()，
回 {reg_id: [user_id]}，語意與逐筆 _resolve_parent_user_ids_for_registration 一致。
"""

from datetime import datetime

from sqlalchemy import event

from models.activity import ActivityRegistration
from models.database import Guardian, Student
from services.activity_service import (
    _resolve_parent_user_ids_batch,
    _resolve_parent_user_ids_for_registration,
)


class _SelectCounter:
    def __init__(self, engine):
        self._engine = engine
        self.count = 0

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on)
        return False


def _student(session, sid, name):
    s = Student(
        student_id=sid,
        name=name,
        is_active=True,
        enrollment_date=datetime(2025, 8, 1).date(),
        lifecycle_status="active",
    )
    session.add(s)
    session.flush()
    return s


def _reg(session, student_id):
    r = ActivityRegistration(student_name="報名生", student_id=student_id)
    session.add(r)
    session.flush()
    return r


def _guardian(session, student_id, user_id, name="家長", deleted=False):
    g = Guardian(
        student_id=student_id,
        user_id=user_id,
        name=name,
        is_primary=False,
        deleted_at=datetime(2026, 1, 1) if deleted else None,
    )
    session.add(g)
    session.flush()
    return g


def _build(session):
    s1 = _student(session, "S001", "甲")
    r1 = _reg(session, s1.id)
    _guardian(session, s1.id, 101)
    _guardian(session, s1.id, 102)

    s2 = _student(session, "S002", "乙")
    r2 = _reg(session, s2.id)
    _guardian(session, s2.id, 201)
    _guardian(session, s2.id, None)  # user_id None → 排除
    _guardian(session, s2.id, 999, deleted=True)  # 已刪 → 排除

    r3 = _reg(session, None)  # 無 student → []

    s4 = _student(session, "S004", "丁")
    r4 = _reg(session, s4.id)  # student 無 guardian → []

    session.commit()
    return [r1.id, r2.id, r3.id, r4.id]


def test_batch_matches_single_resolve(test_db_session):
    session = test_db_session
    reg_ids = _build(session)

    batch = _resolve_parent_user_ids_batch(session, reg_ids)

    assert set(batch.keys()) == set(reg_ids)
    for rid in reg_ids:
        # uid 順序不保證，比 set
        assert set(batch[rid]) == set(
            _resolve_parent_user_ids_for_registration(session, rid)
        )
    # 顯式驗證
    assert set(batch[reg_ids[0]]) == {101, 102}
    assert set(batch[reg_ids[1]]) == {201}  # None + deleted 排除
    assert batch[reg_ids[2]] == []  # 無 student
    assert batch[reg_ids[3]] == []  # 無 guardian


def test_batch_empty_returns_empty(test_db_session):
    assert _resolve_parent_user_ids_batch(test_db_session, []) == {}


def test_batch_query_count_independent_of_reg_count(test_db_session):
    session = test_db_session
    reg_ids = []
    for i in range(8):
        s = _student(session, f"Q{i:03d}", f"童{i}")
        r = _reg(session, s.id)
        _guardian(session, s.id, 1000 + i)
        reg_ids.append(r.id)
    session.commit()

    engine = session.get_bind()
    with _SelectCounter(engine) as ctr:
        batch = _resolve_parent_user_ids_batch(session, reg_ids)

    assert len(batch) == 8
    # reg→student 一次 + Guardian 一次 = 2 條；逐筆會是 8 × 2 = 16，設 ≤ 3。
    assert ctr.count <= 3, f"批次家長解析查詢數 {ctr.count} 超標（疑 N+1 回歸）"
