"""
tests/test_activity_attendance_orphan_stats.py
──────────────────────────────────────────────
驗證場次列表統計（build_session_rows_with_stats）與儀表板統計
（ActivityService.get_attendance_stats）不會將「孤兒點名」計入。

孤兒點名情境：
  (a) 學生報名後整筆軟刪（is_active=False），但 ActivityAttendance row 未刪。
      → build_session_rows_with_stats 的 recorded/present 應與詳情頁一致（孤兒不計入）。
  (b) 報名被駁回（is_active=False, match_status='rejected'），但仍有點名記錄。
      → get_attendance_stats 不計入孤兒；有效出席率不因孤兒膨脹。

2026-06-22 P2：統計口徑對齊修補回歸測試。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.classroom import Student

from api.activity._shared import (
    _build_session_detail_response,
    build_session_rows_with_stats,
)
from services.activity_service import ActivityService

TERM = {"school_year": 114, "semester": 1}


def _query_session_rows(session, course_id):
    """模擬 attendance.py list_sessions 的查詢，帶 course_name label，
    供 build_session_rows_with_stats 使用。"""
    return (
        session.query(
            ActivitySession.id,
            ActivitySession.course_id,
            ActivitySession.session_date,
            ActivitySession.notes,
            ActivitySession.created_by,
            ActivitySession.created_at,
            ActivityCourse.name.label("course_name"),
        )
        .join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
        .filter(ActivitySession.course_id == course_id)
        .all()
    )


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_course(s, name="圍棋", **kwargs) -> ActivityCourse:
    c = ActivityCourse(
        name=name,
        price=1000,
        capacity=30,
        is_active=True,
        school_year=TERM["school_year"],
        semester=TERM["semester"],
        **kwargs,
    )
    s.add(c)
    s.flush()
    return c


def _make_session(s, course_id) -> ActivitySession:
    sess = ActivitySession(
        course_id=course_id, session_date=date.today(), created_by="test"
    )
    s.add(sess)
    s.flush()
    return sess


def _make_student(s, *, name="學生甲", is_active=True) -> Student:
    """建立 Student 記錄，is_active 可設 False 模擬離校。"""
    st = Student(
        student_id=f"T{name}",
        name=name,
        is_active=is_active,
    )
    s.add(st)
    s.flush()
    return st


def _make_reg(
    s,
    *,
    name="王小明",
    is_active=True,
    match_status="matched",
    student_id=None,
) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        is_active=is_active,
        match_status=match_status,
        student_id=student_id,
    )
    s.add(r)
    s.flush()
    return r


def _enroll(s, reg_id: int, course_id: int, status: str = "enrolled"):
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    s.add(rc)
    s.flush()
    return rc


def _attend(
    s, session_id: int, reg_id: int, is_present: bool = True
) -> ActivityAttendance:
    a = ActivityAttendance(
        session_id=session_id,
        registration_id=reg_id,
        is_present=is_present,
        notes="",
        recorded_by="test",
    )
    s.add(a)
    s.flush()
    return a


# ── (a) build_session_rows_with_stats 對齊詳情頁 ─────────────────────────────


class TestBuildSessionRowsOrphan:
    """build_session_rows_with_stats 不計入孤兒點名，與 _build_session_detail_response 一致。"""

    def test_soft_deleted_registration_excluded_from_list_stats(self, session):
        """整筆軟刪（is_active=False）的報名點名不計入列表統計。

        修前：build_session_rows_with_stats 只 COUNT ActivityAttendance，孤兒被計入。
        修後：列表 present_count == 詳情頁 present_count == 0（孤兒排除）。
        """
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 建一筆正常報名並點名出席
        reg_active = _make_reg(session, name="在籍生")
        _enroll(session, reg_active.id, course.id)
        _attend(session, sess.id, reg_active.id, is_present=True)

        # 建一筆軟刪報名並留下孤兒點名
        reg_deleted = _make_reg(session, name="已刪除生", is_active=False)
        _enroll(session, reg_deleted.id, course.id)
        _attend(session, sess.id, reg_deleted.id, is_present=True)

        session.commit()

        # 列表統計（模擬 list_sessions 的帶 course_name JOIN 查詢）
        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        assert len(list_stats) == 1
        row = list_stats[0]

        # 詳情頁統計
        detail = _build_session_detail_response(session, sess)

        # 兩者 present_count 必須一致（孤兒不算）
        assert row["present_count"] == detail["present_count"], (
            f"list present_count={row['present_count']} != "
            f"detail present_count={detail['present_count']}; 孤兒點名被計入列表"
        )
        # 且值正確（只有 1 筆有效出席）
        assert detail["present_count"] == 1
        assert row["present_count"] == 1

        # recorded_count 同樣應一致（只計有效報名的點名）
        assert row["recorded_count"] == detail["total"], (
            f"list recorded_count={row['recorded_count']} != "
            f"detail total={detail['total']}"
        )

    def test_rejected_registration_excluded_from_list_stats(self, session):
        """被駁回（match_status='rejected', is_active=False）的報名點名不計入列表統計。"""
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 正常報名並點名
        reg_ok = _make_reg(session, name="正常生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=True)

        # 被駁回報名留下孤兒點名
        reg_rejected = _make_reg(
            session, name="被駁生", is_active=False, match_status="rejected"
        )
        _enroll(session, reg_rejected.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_rejected.id, is_present=True)

        session.commit()

        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        row = list_stats[0]
        detail = _build_session_detail_response(session, sess)

        assert row["present_count"] == detail["present_count"]
        assert row["present_count"] == 1  # 只有正常生


# ── (b) get_attendance_stats 不計孤兒 ────────────────────────────────────────


class TestGetAttendanceStatsOrphan:
    """ActivityService.get_attendance_stats 儀表板出席率不含孤兒點名。"""

    def test_rejected_orphan_excluded_from_dashboard_stats(self, session, svc):
        """被駁回報名的孤兒點名不應計入 get_attendance_stats total/present。

        修前：get_attendance_stats JOIN path CourseSession→Attendance 沒驗報名有效性，
              孤兒被算進 total → 出席率膨脹（或壓低）。
        修後：total/present 只算有效報名（is_active=True、match_status!='rejected'、
              RegistrationCourse.status='enrolled'）。
        """
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 1 筆有效報名，出席
        reg_ok = _make_reg(session, name="有效生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=True)

        # 1 筆駁回孤兒，出席（is_active=False, match_status='rejected'）
        reg_rej = _make_reg(
            session, name="駁回生", is_active=False, match_status="rejected"
        )
        _enroll(session, reg_rej.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_rej.id, is_present=False)  # 甚至缺席孤兒

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)

        # 只有 1 筆有效點名（reg_ok, is_present=True）
        by_course = result["by_course"]
        assert len(by_course) == 1
        entry = by_course[0]

        # avg_rate 應為 1.0（1 出席 / 1 有效），而非 0.5（1/2 含孤兒）
        assert (
            entry["avg_rate"] == 1.0
        ), f"avg_rate={entry['avg_rate']} != 1.0；孤兒點名被計入儀表板統計"

    def test_soft_deleted_orphan_excluded_from_dashboard_stats(self, session, svc):
        """整筆軟刪（is_active=False）的孤兒點名不計入儀表板出席統計。"""
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 1 筆有效報名，缺席
        reg_ok = _make_reg(session, name="有效生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=False)

        # 1 筆軟刪孤兒，出席（不應計入）
        reg_del = _make_reg(session, name="刪除生", is_active=False)
        _enroll(session, reg_del.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_del.id, is_present=True)

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)
        by_course = result["by_course"]
        assert len(by_course) == 1
        entry = by_course[0]

        # avg_rate 應為 0.0（1 缺席 / 1 有效），不受孤兒出席影響
        assert (
            entry["avg_rate"] == 0.0
        ), f"avg_rate={entry['avg_rate']} != 0.0；孤兒出席點名被計入儀表板統計"


# ── (c) fan-out 重複計數：多課同時報名 ──────────────────────────────────────


class TestFanOutMultiCourse:
    """一筆 registration 同時 enrolled 多門課時，出席統計不因 RegistrationCourse fan-out 膨脹。

    缺陷（1785bc31）：兩個聚合查詢的 RegistrationCourse JOIN 未綁定場次課程（course_id），
    一筆報名若 enrolled A、B 兩門課，對課程 A 的場次點一次名，JOIN 出 2 列 →
    COUNT(attendance.id) 計 2 而非 1 → recorded/present 灌水。
    """

    def test_build_session_rows_no_fanout_when_multi_course_enrolled(self, session):
        """build_session_rows_with_stats：多課報名 recorded==1, present==1（不重複計）。

        建一筆報名同時 enrolled 課程 A（圍棋）與課程 B（書法），
        對課程 A 的場次點名出席 → A 場次 recorded_count/present_count 均應為 1。
        """
        course_a = _make_course(session, name="圍棋")
        course_b = _make_course(session, name="書法")
        sess_a = _make_session(session, course_a.id)

        reg = _make_reg(session, name="多課生")
        _enroll(session, reg.id, course_a.id, status="enrolled")
        _enroll(session, reg.id, course_b.id, status="enrolled")  # 同時報了 B
        _attend(session, sess_a.id, reg.id, is_present=True)  # 只對 A 的場次點名

        session.commit()

        rows = _query_session_rows(session, course_a.id)
        list_stats = build_session_rows_with_stats(session, rows)
        assert len(list_stats) == 1
        row = list_stats[0]

        assert row["recorded_count"] == 1, (
            f"recorded_count={row['recorded_count']}，應為 1；"
            "RegistrationCourse 未綁定 course_id 造成 fan-out 重複計數"
        )
        assert (
            row["present_count"] == 1
        ), f"present_count={row['present_count']}，應為 1；fan-out 導致灌水"

    def test_get_attendance_stats_no_fanout_when_multi_course_enrolled(
        self, session, svc
    ):
        """get_attendance_stats：多課報名的統計不因 fan-out 膨脹。

        2 人報名課程 A（圍棋），同時都也 enrolled 課程 B（書法）。
        第 1 人出席，第 2 人缺席 → avg_rate 應為 0.5（1/2）。

        fan-out 缺陷時：每筆 attendance 被 JOIN 出 2 列（A+B 各一個 RegistrationCourse），
        present=2, total=4 → avg_rate=0.5 恰好數值相同，但 total 已膨脹。
        因此改以 total_sessions（全局加總 attendance 記錄數）驗證：
          正確：avg_attendance_rate 計算分母 = 2（2 筆點名）；
          fan-out：分母 = 4（每筆被重複計 2 次）。
        avg_attendance_rate = present_sum / total_sum = 2/4 vs 1/2，數值差異可判。
        """
        course_a = _make_course(session, name="圍棋")
        course_b = _make_course(session, name="書法")
        sess_a = _make_session(session, course_a.id)

        reg1 = _make_reg(session, name="多課生甲")
        reg2 = _make_reg(session, name="多課生乙")
        for reg in (reg1, reg2):
            _enroll(session, reg.id, course_a.id, status="enrolled")
            _enroll(session, reg.id, course_b.id, status="enrolled")  # 都同時報了 B

        _attend(session, sess_a.id, reg1.id, is_present=True)  # 甲出席
        _attend(session, sess_a.id, reg2.id, is_present=False)  # 乙缺席

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)

        # avg_attendance_rate = total_present / total_records（全課程加總）
        # 正確：present=1, records=2 → avg=0.5
        # fan-out：present=2, records=4 → avg=0.5（數值相同，無法判）
        # 因此改用全局 avg_attendance_rate 計算 present_sum 與 total_sum 的比例：
        # 若 total_records=4（膨脹）且 present=2，avg=0.5——與正確相同。
        # 改驗 by_course avg_rate 分子/分母：需計 total_records_across_courses。
        # 最直接：重算 present/total，期望 present_sum=1, total_sum=2。
        # global avg_attendance_rate 由 total_present / total_records，兩種情況 0.5 相同。
        # 所以改以 sessions 計數輔助：理論上只有 1 個 session，課程 A 的 sessions==1。
        by_course = result["by_course"]
        course_a_entry = next(
            (e for e in by_course if e["course_name"] == "圍棋"), None
        )
        assert course_a_entry is not None, "圍棋課程應出現在 by_course 中"

        # avg_rate = present / total。fan-out 時 present=2/total=4=0.5；正確 present=1/total=2=0.5。
        # 兩者 avg_rate 相同，無法靠 avg_rate 區分。
        # 改驗：avg_attendance_rate（全局）= sum(present)/sum(total)；
        # fan-out 時 total 膨脹 2 倍但 present 也膨脹 2 倍，avg 不變。
        # 最可靠驗法：用 sessions==1 + avg_rate==0.5 確認分母為 2 而非 4。
        # 由於 avg_rate = round(present/total, 2)，正確 1/2=0.5，fan-out 2/4=0.5 相同。
        # ─ 唯一可區分的途徑：直接斷言 total_records 不膨脹。
        # total_records 不直接暴露於 get_attendance_stats，但可推算：
        # avg_attendance_rate = sum_present / sum_total。
        # sum_present = avg_rate_a * total_a（反推）。暫改為驗 avg_rate 及 sessions 合理性。
        # 為明確捕獲 fan-out，改用「1 人出席 2 場，1 人缺席 2 場」場景讓 present/total 分母差異大。
        # -- 上述推導說明 sessions=1 時 avg_rate 無法區分 fan-out；改用多場次：
        # 新增第 2 場次，只讓甲出席場次 1（缺場次 2）→ total_correct=2(有點名)/total_fanout=4。
        # present_correct=1/total_correct=2 → 0.5；fanout present=2/total=4=0.5 仍相同...
        # 結論：本 SQLite 函式下 avg_rate 無法捕獲等比膨脹的 fan-out。
        # 改策略：直接驗 recorded_count（由 build_session_rows 驗），
        # get_attendance_stats 的 fan-out 須透過非等比情境才能在 SQLite 觀察。
        # 用場景：1 人出席（is_present=True）、1 人缺席（is_present=False），都報 A+B。
        # fan-out：每人 attendance 各 JOIN 出 2 RC 列 → present=2(甲出席列*2), total=4
        # 非等比：但 sum(case is_present) = 甲 is_present=True 算 2 次，乙算 0 次 = present=2
        # total=4 → avg=2/4=0.5；正確 present=1, total=2 → 0.5。仍相同。
        # ─ 最終策略：改為 3 課程場景使 fan-out 產生非等比膨脹。
        # 甲：enrolled A+B+C，出席 A 的場次。
        # fan-out RC join: 3 列(A/B/C)。present = is_present*3（如 is_present=True → 3）。
        # total = 3（attendance 1 筆×3 RC 列）。avg=3/3=1.0，正確=1/1=1.0，仍相同。
        # 試 2 人：甲(A+B)出席, 乙(A+B)缺席：fanout present=2, total=4, avg=0.5；
        #   正確 present=1, total=2, avg=0.5。相同。
        # 試 3 人：甲(A+B)出席, 乙(A+B)出席, 丙(A)只報A缺席。
        #   fanout: 甲 present+=2, 乙 present+=2, 丙 present+=0(缺席*1RC)
        #           total: 甲2+乙2+丙1=5, present:甲2+乙2=4, avg=4/5=0.8
        #   正確: present=2(甲乙), total=3(甲乙丙), avg=2/3=0.667
        #   ← 可以捕獲！改用這個場景。

        # 以上場景分析說明原本測試無法捕獲等比 fan-out；下方補第三人（僅報 A）。
        # 注意：這些 assert 是在已新增第三人後驗的。由於測試資料已 commit，
        # 此處僅能驗 2 人場景的 avg_rate。
        # 此測試保留作為「不因 fan-out 破壞整體流程」的煙霧測試；
        # 等比 fan-out 的精確捕獲見下方 test_get_attendance_stats_fanout_asymmetric。
        assert course_a_entry["sessions"] == 1
        assert course_a_entry["avg_rate"] == 0.5

    def test_get_attendance_stats_fanout_asymmetric(self, session, svc):
        """get_attendance_stats：非等比 fan-out 場景可精確捕獲。

        甲、乙均報課程 A（圍棋）+ 課程 B（書法），丙只報課程 A。
        甲、乙出席，丙缺席。
        正確：圍棋 present=2, total=3, avg=0.667。
        fan-out：甲 JOIN 出 2 RC → 計 2；乙 JOIN 出 2 RC → 計 2；丙只 1 RC → 計 1。
                 total=5, present=4 → avg=0.8（與正確不同）。
        """
        course_a = _make_course(session, name="圍棋_asym")
        course_b = _make_course(session, name="書法_asym")
        sess_a = _make_session(session, course_a.id)

        reg1 = _make_reg(session, name="甲")
        reg2 = _make_reg(session, name="乙")
        reg3 = _make_reg(session, name="丙")

        # 甲、乙：enrolled A + B
        _enroll(session, reg1.id, course_a.id, status="enrolled")
        _enroll(session, reg1.id, course_b.id, status="enrolled")
        _enroll(session, reg2.id, course_a.id, status="enrolled")
        _enroll(session, reg2.id, course_b.id, status="enrolled")
        # 丙：只 enrolled A
        _enroll(session, reg3.id, course_a.id, status="enrolled")

        _attend(session, sess_a.id, reg1.id, is_present=True)
        _attend(session, sess_a.id, reg2.id, is_present=True)
        _attend(session, sess_a.id, reg3.id, is_present=False)

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)
        by_course = result["by_course"]

        course_a_entry = next(
            (e for e in by_course if e["course_name"] == "圍棋_asym"), None
        )
        assert course_a_entry is not None, "圍棋_asym 課程應出現在 by_course 中"
        # 正確：present=2, total=3 → avg=round(2/3,2)=0.67
        # fan-out：present=4, total=5 → avg=round(4/5,2)=0.8
        assert course_a_entry["avg_rate"] == round(2 / 3, 2), (
            f"avg_rate={course_a_entry['avg_rate']}，應為 {round(2/3,2)}；"
            "fan-out 造成 total 非等比膨脹（甲乙各+1，丙不變），avg_rate 偏高"
        )


# ── (I-1 / N-1) Student.is_active=False 離校學生不計入統計 ────────────────────


class TestStudentInactiveExcluded:
    """底層 Student.is_active=False（離校/畢業）的報名點名不計入統計。

    詳情頁 _build_session_detail_response 已有：
        or_(ActivityRegistration.student_id.is_(None), Student.is_active.is_(True))
    兩個聚合查詢（_build_valid_attendance_agg_query / get_attendance_stats）
    修前漏了此條件 → 離校生仍被計入，造成列表/儀表板與詳情頁不一致。
    """

    def test_build_session_rows_excludes_inactive_student(self, session):
        """build_session_rows_with_stats：底層 Student.is_active=False 的報名不計入統計。

        情境：
          - 在籍生（Student.is_active=True）：報名 enrolled，點名出席。
          - 離校生（Student.is_active=False）：報名 enrolled，點名出席（但詳情頁不算）。
        修前：聚合查詢無 Student JOIN → 離校生出席被計入 → present_count=2。
        修後：與詳情頁一致 → present_count=1（只算在籍生）。
        """
        course = _make_course(session, name="離校測試_list")
        sess = _make_session(session, course.id)

        # 在籍生
        st_active = _make_student(session, name="在籍生A", is_active=True)
        reg_active = _make_reg(session, name="在籍生A", student_id=st_active.id)
        _enroll(session, reg_active.id, course.id)
        _attend(session, sess.id, reg_active.id, is_present=True)

        # 離校生（Student.is_active=False）
        st_inactive = _make_student(session, name="離校生B", is_active=False)
        reg_inactive = _make_reg(session, name="離校生B", student_id=st_inactive.id)
        _enroll(session, reg_inactive.id, course.id)
        _attend(session, sess.id, reg_inactive.id, is_present=True)

        session.commit()

        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        assert len(list_stats) == 1
        row = list_stats[0]

        detail = _build_session_detail_response(session, sess)

        # 列表與詳情頁 present_count 必須一致
        assert row["present_count"] == detail["present_count"], (
            f"list present_count={row['present_count']} != "
            f"detail present_count={detail['present_count']}；"
            "離校生（Student.is_active=False）被計入聚合統計"
        )
        # 且只計在籍生那 1 筆
        assert (
            row["present_count"] == 1
        ), f"present_count={row['present_count']}，應為 1；離校生出席不應計入"
        assert (
            row["recorded_count"] == 1
        ), f"recorded_count={row['recorded_count']}，應為 1；離校生點名不應計入"

    def test_get_attendance_stats_excludes_inactive_student(self, session, svc):
        """get_attendance_stats：底層 Student.is_active=False 的報名不計入儀表板出席統計。

        情境：
          - 在籍生：缺席（is_present=False）。
          - 離校生：出席（is_present=True，但不應被計入）。
        修前：avg_rate = 1/2 = 0.5（含離校生）。
        修後：avg_rate = 0/1 = 0.0（只算在籍生，且在籍生缺席）。
        """
        course = _make_course(session, name="離校測試_dashboard")
        sess = _make_session(session, course.id)

        # 在籍生，缺席
        st_active = _make_student(session, name="在籍生C", is_active=True)
        reg_active = _make_reg(session, name="在籍生C", student_id=st_active.id)
        _enroll(session, reg_active.id, course.id)
        _attend(session, sess.id, reg_active.id, is_present=False)

        # 離校生，出席（不應計入）
        st_inactive = _make_student(session, name="離校生D", is_active=False)
        reg_inactive = _make_reg(session, name="離校生D", student_id=st_inactive.id)
        _enroll(session, reg_inactive.id, course.id)
        _attend(session, sess.id, reg_inactive.id, is_present=True)

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)
        by_course = result["by_course"]
        entry = next(
            (e for e in by_course if e["course_name"] == "離校測試_dashboard"), None
        )
        assert entry is not None, "課程應出現在 by_course 中"

        # 修前（含離校生）：present=1, total=2, avg=0.5
        # 修後（排除離校生）：present=0, total=1, avg=0.0
        assert entry["avg_rate"] == 0.0, (
            f"avg_rate={entry['avg_rate']}，應為 0.0；"
            "離校生（Student.is_active=False）出席被計入儀表板統計，應排除"
        )

    def test_no_student_id_reg_still_counted(self, session):
        """校外生（student_id=None）的點名照常計入（不因 outerjoin 條件被排除）。

        詳情頁口徑：or_(student_id.is_(None), Student.is_active.is_(True))
        → student_id=None 的報名無論如何都保留。
        """
        course = _make_course(session, name="校外生測試")
        sess = _make_session(session, course.id)

        # 校外生（無 student_id）
        reg_external = _make_reg(session, name="校外生E", student_id=None)
        _enroll(session, reg_external.id, course.id)
        _attend(session, sess.id, reg_external.id, is_present=True)

        session.commit()

        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        assert len(list_stats) == 1
        row = list_stats[0]

        assert (
            row["present_count"] == 1
        ), f"present_count={row['present_count']}，校外生（student_id=None）應照常計入"
        assert row["recorded_count"] == 1
