"""
tests/test_classroom_promote.py — 班級跨學年升班邏輯測試

測試範圍：
- _should_advance_grade()：純函式，判斷是否為跨學年升班
- _resolve_next_grade_id()：純函式，解析目標年級
- _term_start_date()：純函式，計算學期開始日期
- promote_classrooms_to_academic_year：整合測試（SQLite in-memory）
"""

import os
import sys
import asyncio
from datetime import date
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.employee import Employee
from models.auth import User
from models.classroom import Classroom, Student, ClassGrade


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def grade_data(session):
    """三層年級：大班 sort_order=1（畢業）← 中班 sort_order=2 ← 小班 sort_order=3。

    _resolve_next_grade_id 尋找 sort_order - 1，
    故小班(3) → 中班(2) → 大班(1) → None（畢業）。
    """
    da = ClassGrade(name="大班", sort_order=1)
    zhong = ClassGrade(name="中班", sort_order=2)
    xiao = ClassGrade(name="小班", sort_order=3)
    session.add_all([da, zhong, xiao])
    session.commit()
    return {"大班": da, "中班": zhong, "小班": xiao}


@pytest.fixture
def source_classrooms(session, grade_data):
    """114學年度下學期三個班級，各配一位學生。"""
    teacher = Employee(employee_id="T001", name="王老師", position="幼兒園教師")
    session.add(teacher)
    session.flush()

    c_da = Classroom(
        name="大班A", class_code="DA", school_year=114, semester=2,
        grade_id=grade_data["大班"].id, head_teacher_id=teacher.id,
    )
    c_zhong = Classroom(
        name="中班A", class_code="ZA", school_year=114, semester=2,
        grade_id=grade_data["中班"].id, head_teacher_id=teacher.id,
    )
    c_xiao = Classroom(
        name="小班A", class_code="XA", school_year=114, semester=2,
        grade_id=grade_data["小班"].id,
    )
    session.add_all([c_da, c_zhong, c_xiao])
    session.flush()

    s_da = Student(student_id="SD01", name="大班生", classroom_id=c_da.id)
    s_zhong = Student(student_id="SZ01", name="中班生", classroom_id=c_zhong.id)
    s_xiao = Student(student_id="SX01", name="小班生", classroom_id=c_xiao.id)
    session.add_all([s_da, s_zhong, s_xiao])
    session.commit()

    return {
        "teacher": teacher,
        "大班A": c_da,
        "中班A": c_zhong,
        "小班A": c_xiao,
        "s_da": s_da,
        "s_zhong": s_zhong,
        "s_xiao": s_xiao,
    }


# ---------------------------------------------------------------------------
# 純函式：_should_advance_grade
# ---------------------------------------------------------------------------

class TestShouldAdvanceGrade:
    from api.classrooms import _should_advance_grade

    def test_semester2_to_semester1_next_year_advances(self):
        from api.classrooms import _should_advance_grade
        assert _should_advance_grade(114, 2, 115, 1) is True

    def test_same_year_no_advance(self):
        from api.classrooms import _should_advance_grade
        assert _should_advance_grade(114, 2, 114, 1) is False

    def test_sem1_to_sem2_same_year_no_advance(self):
        from api.classrooms import _should_advance_grade
        assert _should_advance_grade(114, 1, 114, 2) is False

    def test_sem1_to_sem1_next_year_no_advance(self):
        from api.classrooms import _should_advance_grade
        assert _should_advance_grade(114, 1, 115, 1) is False


# ---------------------------------------------------------------------------
# 純函式：_resolve_next_grade_id
# ---------------------------------------------------------------------------

class TestResolveNextGradeId:
    def _grade_map(self, *grades):
        return {g.id: g for g in grades}

    def test_not_advancing_keeps_same_grade(self):
        """同學期（不升年）應保留原年級。"""
        from api.classrooms import _resolve_next_grade_id
        da = ClassGrade(id=1, name="大班", sort_order=1)
        classroom = Classroom(grade_id=1)
        grade_map = self._grade_map(da)
        result = _resolve_next_grade_id(
            classroom, grade_map,
            source_school_year=114, source_semester=1,
            target_school_year=114, target_semester=2,
        )
        assert result == da.id

    def test_advancing_middle_grade_to_senior(self):
        """中班升年後應升至大班。"""
        from api.classrooms import _resolve_next_grade_id
        da = ClassGrade(id=1, name="大班", sort_order=1)
        zhong = ClassGrade(id=2, name="中班", sort_order=2)
        classroom = Classroom(grade_id=zhong.id)
        grade_map = self._grade_map(da, zhong)
        result = _resolve_next_grade_id(
            classroom, grade_map,
            source_school_year=114, source_semester=2,
            target_school_year=115, target_semester=1,
        )
        assert result == da.id

    def test_advancing_senior_grade_returns_none(self):
        """大班升年後無下一個年級 → None（畢業）。"""
        from api.classrooms import _resolve_next_grade_id
        da = ClassGrade(id=1, name="大班", sort_order=1)
        classroom = Classroom(grade_id=da.id)
        grade_map = self._grade_map(da)
        result = _resolve_next_grade_id(
            classroom, grade_map,
            source_school_year=114, source_semester=2,
            target_school_year=115, target_semester=1,
        )
        assert result is None

    def test_no_grade_id_returns_none(self):
        """classroom 無年級時直接回傳 None。"""
        from api.classrooms import _resolve_next_grade_id
        classroom = Classroom(grade_id=None)
        result = _resolve_next_grade_id(
            classroom, {},
            source_school_year=114, source_semester=2,
            target_school_year=115, target_semester=1,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 純函式：_term_start_date
# ---------------------------------------------------------------------------

class TestTermStartDate:
    def test_semester_1_starts_august_1st(self):
        from api.classrooms import _term_start_date
        assert _term_start_date(114, 1) == date(2025, 8, 1)

    def test_semester_2_starts_february_1st_next_year(self):
        from api.classrooms import _term_start_date
        assert _term_start_date(114, 2) == date(2026, 2, 1)


# ---------------------------------------------------------------------------
# 整合測試：promote_classrooms_to_academic_year
# ---------------------------------------------------------------------------

class TestPromoteAcademicYear:
    """透過 endpoint 函式測試升班業務邏輯。"""

    def _run(self, session, payload: dict):
        from api.classrooms import promote_classrooms_to_academic_year, ClassroomPromoteAcademicYear
        item = ClassroomPromoteAcademicYear(**payload)
        current_user = {"username": "admin", "user_id": 1, "permissions": -1}
        session.close = MagicMock()
        with patch("api.classrooms.get_session", return_value=session):
            return asyncio.run(
                promote_classrooms_to_academic_year(item=item, current_user=current_user)
            )

    def _base_payload(self, source_classroom_id, target_name, grade_data,
                      target_grade_id=None, copy_teachers=True, move_students=True):
        return {
            "source_school_year": 114,
            "source_semester": 2,
            "target_school_year": 115,
            "target_semester": 1,
            "classrooms": [{
                "source_classroom_id": source_classroom_id,
                "target_name": target_name,
                "target_grade_id": target_grade_id,
                "copy_teachers": copy_teachers,
                "move_students": move_students,
            }],
        }

    def test_same_source_target_raises_400(self, session, grade_data, source_classrooms):
        """來源與目標學期相同應回傳 400。"""
        with pytest.raises(HTTPException) as exc_info:
            from api.classrooms import promote_classrooms_to_academic_year, ClassroomPromoteAcademicYear
            item = ClassroomPromoteAcademicYear(
                source_school_year=114, source_semester=2,
                target_school_year=114, target_semester=2,
                classrooms=[{"source_classroom_id": source_classrooms["中班A"].id,
                              "target_name": "中班A新", "target_grade_id": grade_data["中班"].id}],
            )
            session.close = MagicMock()
            with patch("api.classrooms.get_session", return_value=session):
                asyncio.run(promote_classrooms_to_academic_year(
                    item=item, current_user={"username": "admin"}
                ))
        assert exc_info.value.status_code == 400

    def test_missing_source_classroom_raises_404(self, session, grade_data, source_classrooms):
        """找不到來源班級應回傳 404。"""
        payload = self._base_payload(
            source_classroom_id=99999, target_name="不存在班",
            grade_data=grade_data, target_grade_id=grade_data["中班"].id,
        )
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, payload)
        assert exc_info.value.status_code == 404

    def test_creates_new_classroom_and_moves_students(self, session, grade_data, source_classrooms):
        """中班升班到大班：建立新班、搬移學生。"""
        payload = self._base_payload(
            source_classroom_id=source_classrooms["中班A"].id,
            target_name="大班B",
            grade_data=grade_data,
            target_grade_id=grade_data["大班"].id,
            move_students=True,
        )
        result = self._run(session, payload)

        assert result["created_count"] == 1
        assert result["moved_student_count"] == 1
        assert result["graduated_count"] == 0

        new_cls = session.query(Classroom).filter(
            Classroom.name == "大班B", Classroom.school_year == 115
        ).first()
        assert new_cls is not None
        assert new_cls.grade_id == grade_data["大班"].id

        session.refresh(source_classrooms["s_zhong"])
        assert source_classrooms["s_zhong"].classroom_id == new_cls.id

    def test_graduates_senior_students(self, session, grade_data, source_classrooms):
        """大班升班 → 學生畢業（is_active=False、status=已畢業）。"""
        # 大班沒有下一個年級，所以 will_graduate=True
        payload = {
            "source_school_year": 114,
            "source_semester": 2,
            "target_school_year": 115,
            "target_semester": 1,
            "classrooms": [{
                "source_classroom_id": source_classrooms["大班A"].id,
                "target_name": None,  # 畢業班不需要新班名
                "target_grade_id": None,
                "copy_teachers": True,
                "move_students": True,
            }],
        }
        result = self._run(session, payload)

        assert result["graduated_count"] == 1
        assert result["created_count"] == 0

        session.refresh(source_classrooms["s_da"])
        assert source_classrooms["s_da"].is_active is False
        assert source_classrooms["s_da"].status == "已畢業"

    def test_duplicate_target_names_in_request_raises_409(self, session, grade_data, source_classrooms):
        """同一請求中目標班級名稱重複應回傳 409。"""
        payload = {
            "source_school_year": 114,
            "source_semester": 2,
            "target_school_year": 115,
            "target_semester": 1,
            "classrooms": [
                {
                    "source_classroom_id": source_classrooms["中班A"].id,
                    "target_name": "同名班",
                    "target_grade_id": grade_data["大班"].id,
                },
                {
                    "source_classroom_id": source_classrooms["小班A"].id,
                    "target_name": "同名班",  # 重複
                    "target_grade_id": grade_data["中班"].id,
                },
            ],
        }
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, payload)
        assert exc_info.value.status_code == 409

    def test_no_student_move_when_move_students_false(self, session, grade_data, source_classrooms):
        """move_students=False 時學生不應被搬移。"""
        payload = self._base_payload(
            source_classroom_id=source_classrooms["中班A"].id,
            target_name="大班C",
            grade_data=grade_data,
            target_grade_id=grade_data["大班"].id,
            move_students=False,
        )
        result = self._run(session, payload)

        assert result["moved_student_count"] == 0
        session.refresh(source_classrooms["s_zhong"])
        assert source_classrooms["s_zhong"].classroom_id == source_classrooms["中班A"].id

    def test_copy_teachers_false_new_classroom_has_no_teacher(self, session, grade_data, source_classrooms):
        """copy_teachers=False 時新班級不應繼承師資。"""
        payload = self._base_payload(
            source_classroom_id=source_classrooms["中班A"].id,
            target_name="大班D",
            grade_data=grade_data,
            target_grade_id=grade_data["大班"].id,
            copy_teachers=False,
        )
        self._run(session, payload)

        new_cls = session.query(Classroom).filter(
            Classroom.name == "大班D"
        ).first()
        assert new_cls.head_teacher_id is None

    def test_copy_teachers_true_new_classroom_inherits_teacher(self, session, grade_data, source_classrooms):
        """copy_teachers=True 時新班級應繼承班導師。"""
        payload = self._base_payload(
            source_classroom_id=source_classrooms["中班A"].id,
            target_name="大班E",
            grade_data=grade_data,
            target_grade_id=grade_data["大班"].id,
            copy_teachers=True,
        )
        self._run(session, payload)

        new_cls = session.query(Classroom).filter(Classroom.name == "大班E").first()
        assert new_cls.head_teacher_id == source_classrooms["teacher"].id

    def test_existing_active_classroom_conflict_raises_409(self, session, grade_data, source_classrooms):
        """目標學期已存在同名活躍班級應回傳 409。"""
        existing = Classroom(
            name="大班F", class_code="DF", school_year=115, semester=1,
            grade_id=grade_data["大班"].id, is_active=True,
        )
        session.add(existing)
        session.commit()

        payload = self._base_payload(
            source_classroom_id=source_classrooms["中班A"].id,
            target_name="大班F",  # 衝突
            grade_data=grade_data,
            target_grade_id=grade_data["大班"].id,
        )
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, payload)
        assert exc_info.value.status_code == 409
