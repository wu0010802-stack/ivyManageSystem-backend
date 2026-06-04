"""services/recruitment_timeline.py — 招生→學生 歷程時間軸（union 招生事件 + 學生異動）。"""

from __future__ import annotations

from datetime import datetime as _dt

from sqlalchemy.orm import Session

from models.classroom import Student
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from models.student_log import StudentChangeLog
from schemas.recruitment_timeline import TimelineEvent


class TimelineNotFound(Exception):
    """visit 不存在。"""


def build_visit_timeline(session: Session, *, visit_id: int) -> list[TimelineEvent]:
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise TimelineNotFound(f"visit {visit_id} not found")

    rec_events = (
        session.query(RecruitmentEventLog)
        .filter_by(recruitment_visit_id=visit_id)
        .all()
    )
    student = (
        session.query(Student).filter(Student.recruitment_visit_id == visit_id).first()
    )
    student_events = []
    if student is not None:
        student_events = (
            session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        )

    events: list[TimelineEvent] = []
    for e in rec_events:
        events.append(
            TimelineEvent(
                source="recruitment",
                event_type=e.event_type,
                from_stage=e.from_stage,
                to_stage=e.to_stage,
                actor_user_id=e.actor_user_id,
                reason=e.reason,
                created_at=e.created_at,
            )
        )
    for e in student_events:
        ts = (
            e.event_date
            if hasattr(e.event_date, "hour")
            else _dt.combine(e.event_date, _dt.min.time())
        )
        events.append(
            TimelineEvent(
                source="student",
                event_type=e.event_type,
                from_stage=None,
                to_stage=None,
                actor_user_id=e.recorded_by,
                reason=e.reason,
                created_at=ts,
            )
        )

    events.sort(key=lambda x: x.created_at)
    return events
