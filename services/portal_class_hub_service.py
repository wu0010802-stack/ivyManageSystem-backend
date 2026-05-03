"""今日工作台純函式：時段定義、任務歸屬、sticky_next。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, date as date_cls
from typing import Literal, Optional

from sqlalchemy.orm import Session
from models.database import Employee, Classroom, Student
from models.classroom import StudentAttendance, StudentIncident
from models.portfolio import (
    StudentMedicationOrder,
    StudentMedicationLog,
    StudentObservation,
)
from models.contact_book import StudentContactBookEntry

SlotId = Literal["morning", "forenoon", "noon", "afternoon"]


@dataclass(frozen=True)
class SlotDef:
    slot_id: SlotId
    label: str
    start: time
    end: time


SLOT_DEFINITIONS: tuple[SlotDef, ...] = (
    SlotDef("morning", "早晨", time(7, 0), time(9, 0)),
    SlotDef("forenoon", "上午", time(9, 0), time(12, 0)),
    SlotDef("noon", "午間", time(12, 0), time(14, 0)),
    SlotDef("afternoon", "下午", time(14, 0), time(18, 0)),
)


def classify_time_to_slot(t: time) -> SlotId:
    """把一個 time 歸到 4 段中的某一段。早於 07:00 → morning，晚於 18:00 → afternoon。"""
    if t < SLOT_DEFINITIONS[0].start:
        return "morning"
    for sd in SLOT_DEFINITIONS:
        if sd.start <= t < sd.end:
            return sd.slot_id
    return "afternoon"


def pick_sticky_next(candidates: list[dict], now: datetime) -> Optional[dict]:
    """從待辦候選中挑「下一件最近且未過期」。candidates 每筆需有 'due_at' 欄位。"""
    future = [c for c in candidates if c.get("due_at") and c["due_at"] >= now]
    if not future:
        return None
    return min(future, key=lambda c: c["due_at"])


def resolve_teacher_classroom(
    sess: Session, *, employee_id: int
) -> Optional[Classroom]:
    """以教師 employee_id 反查目前指派的 active 班級（若無則 None）。

    沿用既有 portal 慣例：以 Employee.classroom_id 為主鍵；若教師同時是副班導
    需擴充時，請改成查 ClassroomTeacherAssignment（v2）。
    """
    emp = sess.get(Employee, employee_id)
    if not emp or not emp.classroom_id:
        return None
    c = sess.get(Classroom, emp.classroom_id)
    if not c or not c.is_active:
        return None
    return c


def count_attendance_pending(
    sess: Session,
    *,
    classroom_id: int,
    today: date_cls,
) -> int:
    """今日尚未點名的學生數。

    判定條件：班上 active 學生中，今日無 StudentAttendance row 者。
    （`StudentAttendance.status` 為 NOT NULL default '出席'，故有 row 即視為已點名。）
    """
    students = (
        sess.query(Student)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active.is_(True),
        )
        .all()
    )
    if not students:
        return 0

    sids = [s.id for s in students]
    records = (
        sess.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id.in_(sids),
            StudentAttendance.date == today,
        )
        .all()
    )
    rec_map = {r.student_id: r for r in records}

    return sum(1 for s in students if s.id not in rec_map)


def list_pending_medications(
    sess: Session,
    *,
    classroom_id: int,
    today: date_cls,
) -> list[dict]:
    """回傳今日尚未執行的用藥（每筆 log 一個 dict），依 due_at ASC 排序。

    每筆 dict 結構：
      - 'id': StudentMedicationLog.id
      - 'order_id': order id
      - 'student_id', 'student_name'
      - 'detail': "<medication_name> <dose>"  (e.g. "退燒藥 5ml")
      - 'due_at': datetime — today + scheduled_time

    Pending 條件：log.administered_at IS NULL AND log.skipped IS False AND log.correction_of IS NULL，
    且 order.order_date == today，且 student.classroom_id == classroom_id 且 student.is_active。
    """
    rows = (
        sess.query(
            StudentMedicationLog,
            StudentMedicationOrder,
            Student,
        )
        .join(
            StudentMedicationOrder,
            StudentMedicationLog.order_id == StudentMedicationOrder.id,
        )
        .join(Student, StudentMedicationOrder.student_id == Student.id)
        .filter(
            StudentMedicationOrder.order_date == today,
            Student.classroom_id == classroom_id,
            Student.is_active.is_(True),
            StudentMedicationLog.administered_at.is_(None),
            StudentMedicationLog.skipped.is_(False),
            StudentMedicationLog.correction_of.is_(None),
        )
        .order_by(StudentMedicationLog.scheduled_time)
        .all()
    )
    out: list[dict] = []
    for log, order, student in rows:
        hh, mm = map(int, log.scheduled_time.split(":"))
        due = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm)
        out.append(
            {
                "id": log.id,
                "order_id": order.id,
                "student_id": student.id,
                "student_name": student.name,
                "detail": f"{order.medication_name} {order.dose}",
                "due_at": due,
            }
        )
    return out
