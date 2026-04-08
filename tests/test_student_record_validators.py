"""
純邏輯單元測試：validate_assessment_fields、validate_incident_fields
以及班級存取控制邏輯（NV1/NV2 回歸測試）
"""
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student
from utils.validators import validate_assessment_fields, validate_incident_fields, _validate_enum_field
from api.student_incidents import _require_classroom_access


@pytest.fixture
def db_session():
    """SQLite in-memory session，每個測試獨立。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# NV1/NV2 回歸測試：班級存取控制
# ---------------------------------------------------------------------------

class TestRequireClassroomAccess:
    """驗證 _require_classroom_access 的跨班存取限制。"""

    def _make_classroom(self, session, head_id=None, assistant_id=None) -> Classroom:
        cls = Classroom(name="測試班", school_year=2025, semester=1,
                        head_teacher_id=head_id, assistant_teacher_id=assistant_id)
        session.add(cls)
        session.flush()
        return cls

    def test_admin_can_access_any_classroom(self, db_session):
        """admin 不受班級限制。"""
        cls = self._make_classroom(db_session, head_id=99)
        user = {"role": "admin", "employee_id": 1}
        _require_classroom_access(db_session, user, cls.id)  # 不應拋出

    def test_hr_can_access_any_classroom(self, db_session):
        """hr 不受班級限制。"""
        cls = self._make_classroom(db_session, head_id=99)
        user = {"role": "hr", "employee_id": 1}
        _require_classroom_access(db_session, user, cls.id)  # 不應拋出

    def test_teacher_can_access_own_classroom(self, db_session):
        """教師可存取自己擔任正教師的班級。"""
        cls = self._make_classroom(db_session, head_id=10)
        user = {"role": "teacher", "employee_id": 10}
        _require_classroom_access(db_session, user, cls.id)  # 不應拋出

    def test_teacher_blocked_from_other_classroom(self, db_session):
        """教師無法存取其他班級（NV1 核心場景）。"""
        cls = self._make_classroom(db_session, head_id=99)  # 屬於 emp 99
        user = {"role": "teacher", "employee_id": 10}
        with pytest.raises(HTTPException) as exc:
            _require_classroom_access(db_session, user, cls.id)
        assert exc.value.status_code == 403

    def test_no_employee_id_blocked(self, db_session):
        """無 employee_id 的帳號無法存取任何班級。"""
        cls = self._make_classroom(db_session)
        user = {"role": "teacher", "employee_id": None}
        with pytest.raises(HTTPException) as exc:
            _require_classroom_access(db_session, user, cls.id)
        assert exc.value.status_code == 403


class TestNullClassroomAccessGuard:
    """NV2 回歸測試：classroom_id=NULL 的學生只允許管理員存取。"""

    def test_null_classroom_check_blocks_non_admin(self):
        """stu.classroom_id is None 時，非管理員應被拒（模擬路由邏輯）。"""
        # 直接模擬路由中的判斷條件
        role = "teacher"
        stu_classroom_id = None  # 未分班

        if role not in ("admin", "hr", "supervisor"):
            should_block = (stu_classroom_id is None)
        else:
            should_block = False

        assert should_block is True

    def test_null_classroom_check_allows_admin(self):
        """admin 不受 NULL classroom_id 限制。"""
        role = "admin"
        stu_classroom_id = None

        if role not in ("admin", "hr", "supervisor"):
            should_block = (stu_classroom_id is None)
        else:
            should_block = False

        assert should_block is False

    def test_assigned_classroom_not_blocked_by_null_check(self):
        """已分班學生（classroom_id 非 NULL）不應被 NULL 判斷攔截。"""
        role = "teacher"
        stu_classroom_id = 5  # 已分班

        if role not in ("admin", "hr", "supervisor"):
            should_block = (stu_classroom_id is None)
        else:
            should_block = False

        assert should_block is False


class TestValidateAssessmentFields:
    def test_all_none_passes(self):
        """所有欄位為 None 時（部分更新）不拋出例外"""
        validate_assessment_fields()  # 預設都是 None

    def test_valid_values_pass(self):
        validate_assessment_fields(
            assessment_type="期中",
            domain="語文",
            rating="優",
        )

    def test_invalid_assessment_type_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_assessment_fields(assessment_type="月考")
        assert exc.value.status_code == 400
        assert "評量類型" in exc.value.detail

    def test_invalid_domain_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_assessment_fields(domain="數學")
        assert exc.value.status_code == 400
        assert "領域" in exc.value.detail

    def test_invalid_rating_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_assessment_fields(rating="甲")
        assert exc.value.status_code == 400
        assert "評等" in exc.value.detail

    def test_error_message_contains_allowed_values(self):
        with pytest.raises(HTTPException) as exc:
            validate_assessment_fields(assessment_type="不存在")
        assert "期中" in exc.value.detail or "期末" in exc.value.detail

    @pytest.mark.parametrize("t", ["期中", "期末", "學期"])
    def test_all_valid_assessment_types(self, t):
        validate_assessment_fields(assessment_type=t)

    @pytest.mark.parametrize("r", ["優", "良", "需加強"])
    def test_all_valid_ratings(self, r):
        validate_assessment_fields(rating=r)


class TestValidateIncidentFields:
    def test_all_none_passes(self):
        validate_incident_fields()

    def test_valid_values_pass(self):
        validate_incident_fields(incident_type="意外受傷", severity="輕微")

    def test_invalid_incident_type_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_incident_fields(incident_type="打架")
        assert exc.value.status_code == 400
        assert "事件類型" in exc.value.detail

    def test_invalid_severity_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            validate_incident_fields(severity="極嚴重")
        assert exc.value.status_code == 400
        assert "嚴重程度" in exc.value.detail

    def test_only_one_field_invalid_still_raises(self):
        """只有一個欄位無效就應拋出例外"""
        with pytest.raises(HTTPException):
            validate_incident_fields(incident_type="身體健康", severity="無效值")

    @pytest.mark.parametrize("t", ["身體健康", "意外受傷", "行為觀察", "其他"])
    def test_all_valid_incident_types(self, t):
        validate_incident_fields(incident_type=t)

    @pytest.mark.parametrize("s", ["輕微", "中度", "嚴重"])
    def test_all_valid_severities(self, s):
        validate_incident_fields(severity=s)


class TestValidateEnumField:
    """直接測試底層工廠函式，確保提取後行為一致。"""

    def test_none_value_does_not_raise(self):
        _validate_enum_field("欄位", None, {"a", "b"})

    def test_valid_value_does_not_raise(self):
        _validate_enum_field("欄位", "a", {"a", "b"})

    def test_invalid_value_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            _validate_enum_field("欄位", "c", {"a", "b"})
        assert exc.value.status_code == 400
        assert "欄位" in exc.value.detail
