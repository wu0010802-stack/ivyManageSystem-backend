"""tests/test_record_formatters.py — utils/record_formatters 純函式測試。

assessment_to_dict / incident_to_dict 把 SQLAlchemy 模型扁平化成可序列化 dict。
用 SimpleNamespace 假裝 ORM 物件，避免拉 DB / Base。
"""

from datetime import datetime
from types import SimpleNamespace

from utils.record_formatters import assessment_to_dict, incident_to_dict

# ── helpers ──────────────────────────────────────────────────────────


def _make_student(name="王小明", student_id="S001", classroom_id=3):
    return SimpleNamespace(
        name=name,
        student_id=student_id,
        classroom_id=classroom_id,
    )


def _make_assessment(**overrides):
    base = dict(
        id=11,
        student_id=22,
        semester="113-1",
        assessment_type="formative",
        domain="language",
        rating="A",
        content="表現很好",
        suggestions="繼續加油",
        assessment_date=datetime(2026, 5, 17, 9, 0),
        recorded_by=99,
        related_incident_id=None,
        created_at=datetime(2026, 5, 17, 10, 0),
        updated_at=datetime(2026, 5, 17, 11, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_incident(**overrides):
    base = dict(
        id=77,
        student_id=22,
        incident_type="injury",
        severity="minor",
        occurred_at=datetime(2026, 5, 16, 14, 30),
        description="跌倒擦傷",
        action_taken="冰敷",
        parent_notified=True,
        parent_notified_at=datetime(2026, 5, 16, 14, 45),
        recorded_by=99,
        created_at=datetime(2026, 5, 16, 15, 0),
        updated_at=datetime(2026, 5, 16, 16, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── assessment_to_dict ───────────────────────────────────────────────


class TestAssessmentToDict:
    def test_happy_path_basic_fields(self):
        a = _make_assessment()
        s = _make_student()
        result = assessment_to_dict(a, s)

        assert result["id"] == 11
        assert result["student_id"] == 22
        assert result["student_name"] == "王小明"
        assert result["student_no"] == "S001"
        assert result["classroom_id"] == 3
        assert result["semester"] == "113-1"
        assert result["assessment_type"] == "formative"
        assert result["domain"] == "language"
        assert result["rating"] == "A"
        assert result["content"] == "表現很好"
        assert result["suggestions"] == "繼續加油"
        assert result["recorded_by"] == 99
        assert result["related_incident_id"] is None
        assert result["related_incident"] is None

    def test_datetime_fields_serialized_to_isoformat(self):
        a = _make_assessment()
        s = _make_student()
        result = assessment_to_dict(a, s)
        assert result["assessment_date"] == "2026-05-17T09:00:00"
        assert result["created_at"] == "2026-05-17T10:00:00"

    def test_include_updated_at_false_by_default(self):
        result = assessment_to_dict(_make_assessment(), _make_student())
        assert "updated_at" not in result

    def test_include_updated_at_true_adds_key(self):
        result = assessment_to_dict(
            _make_assessment(), _make_student(), include_updated_at=True
        )
        assert result["updated_at"] == "2026-05-17T11:00:00"

    def test_none_student_makes_student_fields_none(self):
        result = assessment_to_dict(_make_assessment(), None)
        assert result["student_name"] is None
        assert result["student_no"] is None
        assert result["classroom_id"] is None

    def test_none_datetime_fields_serialize_to_none(self):
        a = _make_assessment(assessment_date=None, created_at=None, updated_at=None)
        result = assessment_to_dict(a, _make_student(), include_updated_at=True)
        assert result["assessment_date"] is None
        assert result["created_at"] is None
        assert result["updated_at"] is None

    def test_related_incident_included(self):
        a = _make_assessment(related_incident_id=77)
        ri = SimpleNamespace(
            id=77,
            incident_type="behavior",
            occurred_at=datetime(2026, 5, 15, 10, 30),
        )
        result = assessment_to_dict(a, _make_student(), related_incident=ri)
        assert result["related_incident_id"] == 77
        assert result["related_incident"] == {
            "id": 77,
            "incident_type": "behavior",
            "occurred_at": "2026-05-15T10:30:00",
        }

    def test_related_incident_with_none_occurred_at(self):
        a = _make_assessment(related_incident_id=77)
        ri = SimpleNamespace(id=77, incident_type="behavior", occurred_at=None)
        result = assessment_to_dict(a, _make_student(), related_incident=ri)
        assert result["related_incident"]["occurred_at"] is None


# ── incident_to_dict ─────────────────────────────────────────────────


class TestIncidentToDict:
    def test_happy_path_basic_fields(self):
        i = _make_incident()
        s = _make_student()
        result = incident_to_dict(i, s)

        assert result["id"] == 77
        assert result["student_id"] == 22
        assert result["student_name"] == "王小明"
        assert result["student_no"] == "S001"
        assert result["classroom_id"] == 3
        assert result["incident_type"] == "injury"
        assert result["severity"] == "minor"
        assert result["description"] == "跌倒擦傷"
        assert result["action_taken"] == "冰敷"
        assert result["parent_notified"] is True
        assert result["recorded_by"] == 99

    def test_datetime_fields_serialized_to_isoformat(self):
        result = incident_to_dict(_make_incident(), _make_student())
        assert result["occurred_at"] == "2026-05-16T14:30:00"
        assert result["parent_notified_at"] == "2026-05-16T14:45:00"
        assert result["created_at"] == "2026-05-16T15:00:00"

    def test_include_updated_at_false_by_default(self):
        result = incident_to_dict(_make_incident(), _make_student())
        assert "updated_at" not in result

    def test_include_updated_at_true_adds_key(self):
        result = incident_to_dict(
            _make_incident(), _make_student(), include_updated_at=True
        )
        assert result["updated_at"] == "2026-05-16T16:00:00"

    def test_none_student_makes_student_fields_none(self):
        result = incident_to_dict(_make_incident(), None)
        assert result["student_name"] is None
        assert result["student_no"] is None
        assert result["classroom_id"] is None

    def test_none_datetime_fields_serialize_to_none(self):
        i = _make_incident(
            occurred_at=None,
            parent_notified_at=None,
            created_at=None,
            updated_at=None,
        )
        result = incident_to_dict(i, _make_student(), include_updated_at=True)
        assert result["occurred_at"] is None
        assert result["parent_notified_at"] is None
        assert result["created_at"] is None
        assert result["updated_at"] is None

    def test_parent_notified_false_passes_through(self):
        i = _make_incident(parent_notified=False, parent_notified_at=None)
        result = incident_to_dict(i, _make_student())
        assert result["parent_notified"] is False
        assert result["parent_notified_at"] is None
