"""tests/test_students_records_endpoint.py

驗證學生紀錄聚合服務 `services/student_records_timeline.list_timeline()`。
此服務為 `/api/students/records` 的核心；端點僅為薄包裝。
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    Classroom,
    Student,
    StudentAssessment,
    StudentIncident,
)
from models.student_log import StudentChangeLog
from services.student_records_timeline import list_timeline


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
def seeded(session):
    """建立兩個班級、三位學生與三類紀錄共多筆。"""
    c1 = Classroom(name="太陽班", school_year=114, semester=2)
    c2 = Classroom(name="月亮班", school_year=114, semester=2)
    session.add_all([c1, c2])
    session.flush()

    s1 = Student(
        student_id="S1",
        name="小花",
        lifecycle_status=LIFECYCLE_ACTIVE,
        classroom_id=c1.id,
        is_active=True,
        enrollment_date=date(2026, 2, 1),
    )
    s2 = Student(
        student_id="S2",
        name="小明",
        lifecycle_status=LIFECYCLE_ACTIVE,
        classroom_id=c1.id,
        is_active=True,
        enrollment_date=date(2026, 2, 1),
    )
    s3 = Student(
        student_id="S3",
        name="小華",
        lifecycle_status=LIFECYCLE_ACTIVE,
        classroom_id=c2.id,
        is_active=True,
        enrollment_date=date(2026, 2, 1),
    )
    session.add_all([s1, s2, s3])
    session.flush()

    # 事件紀錄：s1 三月 2 日上午 10:00；s2 三月 5 日下午 14:00；s3 三月 8 日
    session.add_all(
        [
            StudentIncident(
                student_id=s1.id,
                incident_type="行為觀察",
                severity="輕微",
                occurred_at=datetime(2026, 3, 2, 10, 0),
                description="與同學爭玩具",
                parent_notified=True,
                parent_notified_at=datetime(2026, 3, 2, 11, 0),
            ),
            StudentIncident(
                student_id=s2.id,
                incident_type="意外受傷",
                severity="中度",
                occurred_at=datetime(2026, 3, 5, 14, 0),
                description="跌倒擦傷膝蓋",
                parent_notified=False,
            ),
            StudentIncident(
                student_id=s3.id,
                incident_type="身體健康",
                severity="輕微",
                occurred_at=datetime(2026, 3, 8, 9, 0),
                description="早上發燒",
                parent_notified=True,
            ),
        ]
    )

    # 評量：s1 三月 6 日；s2 二月 28 日（同學期但較舊）
    session.add_all(
        [
            StudentAssessment(
                student_id=s1.id,
                semester="114-2",
                assessment_type="期中",
                domain="語文",
                rating="優",
                content="表達清晰",
                assessment_date=date(2026, 3, 6),
            ),
            StudentAssessment(
                student_id=s2.id,
                semester="114-2",
                assessment_type="期中",
                domain="認知",
                rating="良",
                content="數學有進步",
                assessment_date=date(2026, 2, 28),
            ),
        ]
    )

    # 異動：s1 入學 2/1；s2 休學 3/10；s3 入學 2/1、轉班 3/12
    session.add_all(
        [
            StudentChangeLog(
                student_id=s1.id,
                school_year=114,
                semester=2,
                event_type="入學",
                event_date=date(2026, 2, 1),
                classroom_id=c1.id,
                reason="新生",
            ),
            StudentChangeLog(
                student_id=s2.id,
                school_year=114,
                semester=2,
                event_type="休學",
                event_date=date(2026, 3, 10),
                classroom_id=c1.id,
                reason="家庭因素",
            ),
            StudentChangeLog(
                student_id=s3.id,
                school_year=114,
                semester=2,
                event_type="入學",
                event_date=date(2026, 2, 1),
                classroom_id=c2.id,
            ),
            StudentChangeLog(
                student_id=s3.id,
                school_year=114,
                semester=2,
                event_type="轉班",
                event_date=date(2026, 3, 12),
                classroom_id=c2.id,
                from_classroom_id=c1.id,
                to_classroom_id=c2.id,
            ),
        ]
    )
    session.commit()
    return {"c1": c1, "c2": c2, "s1": s1, "s2": s2, "s3": s3}


class TestAllTypes:
    def test_returns_all_three_types_by_default(self, session, seeded):
        result = list_timeline(session)
        types = {it["record_type"] for it in result["items"]}
        assert types == {"incident", "assessment", "change_log"}
        # 共 3 事件 + 2 評量 + 4 異動 = 9
        assert result["total"] == 9
        assert len(result["items"]) == 9

    def test_orders_newest_first(self, session, seeded):
        result = list_timeline(session)
        timestamps = [it["occurred_at"] for it in result["items"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_items_have_required_fields(self, session, seeded):
        result = list_timeline(session)
        for item in result["items"]:
            assert "record_type" in item
            assert "record_id" in item
            assert "occurred_at" in item
            assert "student_id" in item
            assert "student_name" in item
            assert "classroom_id" in item
            assert "classroom_name" in item
            assert "summary" in item
            assert "payload" in item


class TestTypeFilter:
    def test_single_type(self, session, seeded):
        result = list_timeline(session, types=["incident"])
        assert result["total"] == 3
        assert all(it["record_type"] == "incident" for it in result["items"])

    def test_multiple_types(self, session, seeded):
        result = list_timeline(session, types=["incident", "change_log"])
        types = {it["record_type"] for it in result["items"]}
        assert types == {"incident", "change_log"}
        assert result["total"] == 7  # 3 + 4

    def test_unknown_type_ignored(self, session, seeded):
        # 未知 type 應被靜默忽略，回傳空或已知部分
        result = list_timeline(session, types=["bogus"])
        assert result["total"] == 0


class TestDateRangeFilter:
    def test_date_from_excludes_earlier(self, session, seeded):
        # 僅保留 3/5 之後（含）
        result = list_timeline(session, date_from=date(2026, 3, 5))
        # incidents: 3/5, 3/8 → 2
        # assessments: 3/6 → 1
        # change_logs: 3/10, 3/12 → 2
        assert result["total"] == 5

    def test_date_to_excludes_later(self, session, seeded):
        result = list_timeline(session, date_to=date(2026, 3, 5))
        # incidents: 3/2, 3/5 → 2
        # assessments: 2/28 → 1
        # change_logs: 2/1, 2/1 → 2（入學兩筆）
        assert result["total"] == 5

    def test_date_range_inclusive(self, session, seeded):
        result = list_timeline(
            session, date_from=date(2026, 3, 1), date_to=date(2026, 3, 10)
        )
        # incidents: 3/2, 3/5, 3/8 → 3
        # assessments: 3/6 → 1
        # change_logs: 3/10 → 1
        assert result["total"] == 5


class TestClassroomFilter:
    def test_classroom_filter_incidents_assessments_via_student(self, session, seeded):
        # c1 = 太陽班 (s1, s2)
        result = list_timeline(session, classroom_id=seeded["c1"].id)
        # incidents of s1/s2 = 2
        # assessments of s1/s2 = 2
        # change_logs whose classroom_id=c1 = 2（s1 入學、s2 休學）
        assert result["total"] == 6

    def test_classroom_filter_c2(self, session, seeded):
        result = list_timeline(session, classroom_id=seeded["c2"].id)
        # incidents of s3 = 1
        # assessments of s3 = 0
        # change_logs whose classroom_id=c2 = 2（s3 入學、轉班）
        assert result["total"] == 3


class TestStudentFilter:
    def test_student_filter(self, session, seeded):
        result = list_timeline(session, student_id=seeded["s1"].id)
        assert {it["record_type"] for it in result["items"]} == {
            "incident",
            "assessment",
            "change_log",
        }
        assert result["total"] == 3


class TestTermFilter:
    def test_term_only_affects_change_log(self, session, seeded):
        # 非本學期：114 學年 1 學期 → change_logs 會 0，但事件/評量不受影響
        result = list_timeline(session, school_year=114, semester=1)
        types = {it["record_type"] for it in result["items"]}
        assert "change_log" not in types
        # 事件 3 + 評量 2 = 5
        assert result["total"] == 5

    def test_term_current_includes_change_logs(self, session, seeded):
        result = list_timeline(session, school_year=114, semester=2)
        assert result["total"] == 9


class TestPagination:
    def test_first_page(self, session, seeded):
        result = list_timeline(session, page=1, page_size=4)
        assert result["page"] == 1
        assert result["page_size"] == 4
        assert result["total"] == 9
        assert len(result["items"]) == 4

    def test_last_page_partial(self, session, seeded):
        result = list_timeline(session, page=3, page_size=4)
        # 9 筆，page 3 剩 1 筆
        assert len(result["items"]) == 1

    def test_empty_page(self, session, seeded):
        result = list_timeline(session, page=99, page_size=4)
        assert result["items"] == []


class TestPayloadShape:
    def test_incident_payload_has_severity_and_parent_notified(self, session, seeded):
        result = list_timeline(session, types=["incident"])
        item = result["items"][0]
        assert "severity" in item
        assert "parent_notified" in item
        assert item["payload"]["incident_type"]
        assert "action_taken" in item["payload"]

    def test_assessment_payload_has_domain_and_rating(self, session, seeded):
        result = list_timeline(session, types=["assessment"])
        item = result["items"][0]
        assert item["payload"].get("domain") in ("語文", "認知")
        assert item["payload"].get("rating") in ("優", "良")
        assert "semester" in item["payload"]

    def test_change_log_payload_has_event_and_classrooms(self, session, seeded):
        result = list_timeline(session, types=["change_log"])
        # 取轉班那筆
        transfer_items = [
            it for it in result["items"] if it["payload"].get("event_type") == "轉班"
        ]
        assert transfer_items
        payload = transfer_items[0]["payload"]
        assert payload.get("from_classroom_id")
        assert payload.get("to_classroom_id")


class TestTieBreakStability:
    def test_same_date_stable_order(self, session, seeded):
        # 兩筆 s3 入學 (2/1) 與 s1 入學 (2/1) 同日，應穩定排序
        result = list_timeline(
            session,
            types=["change_log"],
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 1),
        )
        assert len(result["items"]) == 2
        # 穩定排序鍵：(ts desc, record_type, record_id desc) → id 較大者在前
        ids = [it["record_id"] for it in result["items"]]
        assert ids == sorted(ids, reverse=True)
