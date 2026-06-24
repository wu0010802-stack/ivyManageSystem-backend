"""教師首頁 consecutive_absences / upcoming_birthdays 批次化（P3 N+1 修補）。

兩者的 batch 版原為 dict-comp 逐班呼叫 _single（各發 1-2 查詢），教師管多班時於
home/summary 熱路徑逐班 N+1。改為跨班一次 IN query（對齊同檔 _compute_allergy_alerts_batch
範式），per-student 計算抽純函式給單筆/批次共用，語意完全一致。
"""

from datetime import date

from sqlalchemy import event

from models.classroom import Classroom, Student, StudentAttendance
from services.portal_dashboard_service import (
    compute_consecutive_absences,
    compute_upcoming_birthdays,
)

TODAY = date(2026, 6, 24)


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


def _classroom(session, name):
    c = Classroom(name=name, school_year=2026, semester=1, is_active=True)
    session.add(c)
    session.flush()
    return c


def _student(session, cid, sid, name, birthday=None):
    s = Student(
        student_id=sid,
        name=name,
        classroom_id=cid,
        is_active=True,
        enrollment_date=date(2025, 8, 1),
        lifecycle_status="active",
        birthday=birthday,
    )
    session.add(s)
    session.flush()
    return s


def _attend(session, student_id, d, status):
    session.add(StudentAttendance(student_id=student_id, date=d, status=status))
    session.flush()


def _build(session):
    from datetime import timedelta

    ca = _classroom(session, "A班")
    cb = _classroom(session, "B班")
    cc = _classroom(session, "C班")  # 無學生

    sa1 = _student(session, ca.id, "A001", "甲生", birthday=date(2020, 6, 26))
    sa2 = _student(session, ca.id, "A002", "乙生", birthday=date(2021, 6, 24))
    sb1 = _student(session, cb.id, "B001", "丙生", birthday=date(2019, 7, 5))

    y = TODAY - timedelta(days=1)  # 06-23
    # 甲生：連續 2 天缺席（06-23, 06-22）
    _attend(session, sa1.id, y, "缺席")
    _attend(session, sa1.id, y - timedelta(days=1), "缺席")
    # 乙生：只缺 1 天（不達 threshold）
    _attend(session, sa2.id, y, "缺席")
    # 丙生：連續 3 天缺席
    _attend(session, sb1.id, y, "缺席")
    _attend(session, sb1.id, y - timedelta(days=1), "缺席")
    _attend(session, sb1.id, y - timedelta(days=2), "缺席")

    session.commit()
    return [ca.id, cb.id, cc.id]


def test_consecutive_absences_batch_matches_per_class(test_db_session):
    session = test_db_session
    cids = _build(session)

    batch = compute_consecutive_absences(session, classroom_id=cids, today=TODAY)
    assert set(batch.keys()) == set(cids)
    for cid in cids:
        assert batch[cid] == compute_consecutive_absences(
            session, classroom_id=cid, today=TODAY
        )
    # 顯式：A 班僅甲生達標（2 天）、B 班丙生（3 天）、C 班空
    assert [r["student_name"] for r in batch[cids[0]]] == ["甲生"]
    assert batch[cids[0]][0]["days"] == 2
    assert [r["student_name"] for r in batch[cids[1]]] == ["丙生"]
    assert batch[cids[1]][0]["days"] == 3
    assert batch[cids[2]] == []


def test_upcoming_birthdays_batch_matches_per_class(test_db_session):
    session = test_db_session
    cids = _build(session)

    batch = compute_upcoming_birthdays(
        session, classroom_id=cids, today=TODAY, window_days=7
    )
    assert set(batch.keys()) == set(cids)
    for cid in cids:
        assert batch[cid] == compute_upcoming_birthdays(
            session, classroom_id=cid, today=TODAY, window_days=7
        )
    # A 班：乙生(today, days0) + 甲生(days2)；B 班：丙生 7/5 超窗 → 無
    a_names = [r["student_name"] for r in batch[cids[0]]]
    assert set(a_names) == {"甲生", "乙生"}
    assert batch[cids[1]] == []


def test_consecutive_absences_batch_query_count_bounded(test_db_session):
    session = test_db_session
    from datetime import timedelta

    cids = []
    for i in range(4):
        c = _classroom(session, f"Q{i}班")
        s = _student(session, c.id, f"Q{i:03d}", f"童{i}")
        _attend(session, s.id, TODAY - timedelta(days=1), "缺席")
        _attend(session, s.id, TODAY - timedelta(days=2), "缺席")
        cids.append(c.id)
    session.commit()

    engine = session.get_bind()
    with _SelectCounter(engine) as ctr:
        compute_consecutive_absences(session, classroom_id=cids, today=TODAY)
    # 批次：學生 1 + 出勤 1 = 2；逐班會是 4 班 × 2 = 8。設 ≤ 4。
    assert ctr.count <= 4, f"consecutive_absences 批次查詢數 {ctr.count} 超標"


def test_upcoming_birthdays_batch_query_count_bounded(test_db_session):
    session = test_db_session

    cids = []
    for i in range(4):
        c = _classroom(session, f"R{i}班")
        _student(session, c.id, f"R{i:03d}", f"娃{i}", birthday=date(2020, 6, 25))
        cids.append(c.id)
    session.commit()

    engine = session.get_bind()
    with _SelectCounter(engine) as ctr:
        compute_upcoming_birthdays(
            session, classroom_id=cids, today=TODAY, window_days=7
        )
    # 批次：學生 1；逐班會是 4。設 ≤ 2。
    assert ctr.count <= 2, f"upcoming_birthdays 批次查詢數 {ctr.count} 超標"
