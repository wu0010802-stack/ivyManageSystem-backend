"""考核當期狀態彙整 service。

facade：`aggregate_cycle_status(session, cycle, employee_ids=None)`
回傳 `list[ParticipantStatus]`，每位 participant 對應一筆，內含 4 個子聚合：
出缺勤、班級留校率、才藝報名率、懲處紀錄。

設計原則：
- 純函式風格；session 由 caller 注入，內部**不** commit / refresh / close。
- bulk query 避免 N+1；attendance / disciplinary 用單一 group_by 一次撈完。
- `suggested_score_delta` 初版保守給 Decimal('0')，避免在 UI 上產生未經
  calibrate 的扣分建議，待業主確認標準後再上權重。
- 時間窗：`[cycle.start_date, min(cycle.end_date, today)]`，避免把未來的
  缺勤/懲處算進來。
- `is_excluded=True` 的 participant 直接跳過（與 engine 一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Integer, case, func, or_
from sqlalchemy.orm import Session

from models.activity import ActivityRegistration
from models.appraisal import AppraisalCycle, AppraisalParticipant, RoleGroup
from models.attendance import Attendance
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.disciplinary import DisciplinaryAction
from models.employee import Employee
from utils.academic import semester_enum_to_int

# ===== Aggregate dataclasses =====


@dataclass
class AttendanceAggregate:
    employee_id: int
    late_count: int = 0
    early_leave_count: int = 0
    missing_punch_count: int = 0  # missing_in + missing_out
    leave_days: int = 0  # status='absent'
    # TODO: 待業主給定權重後 calibrate 建議扣分
    suggested_score_delta: Decimal = Decimal("0")


@dataclass
class DisciplinaryActionItem:
    id: int
    action_date: date
    action_type: str
    deduction_amount: Optional[Decimal] = None
    reason: Optional[str] = None


@dataclass
class DisciplinaryAggregate:
    employee_id: int
    warning_count: int = 0
    minor_count: int = 0
    major_count: int = 0
    actions: list[DisciplinaryActionItem] = field(default_factory=list)
    # TODO: 警告/小過/大過 → 扣分映射待業主確認
    suggested_score_delta: Decimal = Decimal("0")


@dataclass
class ClassRetentionAggregate:
    employee_id: int
    classroom_id: Optional[int] = None
    classroom_name: Optional[str] = None
    initial_count: int = 0
    final_count: int = 0
    retention_rate: Decimal = Decimal("0")  # 0-100，2 位小數
    # TODO: 留校率 → 加減分映射待業主確認（Excel 對應 col 12 RETURNING_RATE_0315）
    suggested_score_delta: Decimal = Decimal("0")


@dataclass
class ActivityRateAggregate:
    employee_id: int
    classroom_id: Optional[int] = None
    enrolled_students: int = 0  # 該班 active 學生數
    registered_for_activity: int = 0  # 該班報名才藝課人數（去重 student_id）
    activity_rate: Decimal = Decimal("0")  # 0-100，2 位小數
    # TODO: 才藝參加率 → 加分映射待業主確認（Excel 對應 col 14 AFTER_CLASS_RATE）
    suggested_score_delta: Decimal = Decimal("0")


@dataclass
class ParticipantStatus:
    participant_id: Optional[int]
    employee_id: int
    employee_name: str
    role_group: str  # value of RoleGroup enum
    classroom_id: Optional[int]
    attendance: AttendanceAggregate
    retention: ClassRetentionAggregate
    activity: ActivityRateAggregate
    disciplinary: DisciplinaryAggregate
    is_participant: bool = True
    hire_months_in_cycle: Optional[Decimal] = None


# ===== Sub-aggregators =====


def _aggregate_attendance(
    session: Session, employee_ids: list[int], start: date, end: date
) -> dict[int, AttendanceAggregate]:
    """bulk query Attendance；每 employee 一筆 AttendanceAggregate。

    用 CASE WHEN ... THEN 1 ELSE 0 END 加總四個 bool 欄位 + status='absent'，
    避免依賴 dialect 對 bool→int 的 implicit cast。
    """
    result = {eid: AttendanceAggregate(employee_id=eid) for eid in employee_ids}
    if not employee_ids:
        return result
    late_expr = func.sum(case((Attendance.is_late.is_(True), 1), else_=0)).label("late")
    early_expr = func.sum(
        case((Attendance.is_early_leave.is_(True), 1), else_=0)
    ).label("early")
    missing_expr = func.sum(
        case((Attendance.is_missing_punch_in.is_(True), 1), else_=0)
        + case((Attendance.is_missing_punch_out.is_(True), 1), else_=0)
    ).label("missing")
    absent_expr = func.sum(case((Attendance.status == "absent", 1), else_=0)).label(
        "absent"
    )
    rows = (
        session.query(
            Attendance.employee_id,
            late_expr,
            early_expr,
            missing_expr,
            absent_expr,
        )
        .filter(Attendance.employee_id.in_(employee_ids))
        .filter(Attendance.attendance_date >= start)
        .filter(Attendance.attendance_date <= end)
        .group_by(Attendance.employee_id)
        .all()
    )
    for eid, late, early, missing, absent in rows:
        agg = result[eid]
        agg.late_count = int(late or 0)
        agg.early_leave_count = int(early or 0)
        agg.missing_punch_count = int(missing or 0)
        agg.leave_days = int(absent or 0)
    return result


def _aggregate_class_retention(
    session: Session,
    employee_to_classroom: dict[int, Optional[int]],
    classroom_name_by_id: dict[int, str],
    start: date,
    end: date,
) -> dict[int, ClassRetentionAggregate]:
    """以 Student.enrollment_date / withdrawal_date / lifecycle_status 動態
    算期初/期末 active 學生數。

    期初判定（學生在 start 當天屬於該班且仍在學）：
      - classroom_id == cid
      - enrollment_date IS NOT NULL AND enrollment_date <= start
      - (withdrawal_date IS NULL OR withdrawal_date > start)
      - (graduation_date IS NULL OR graduation_date > start)

    期末判定（在 end 當天還在該班 active）：
      - classroom_id == cid
      - lifecycle_status == 'active'
      - enrollment_date IS NOT NULL AND enrollment_date <= end
      - (withdrawal_date IS NULL OR withdrawal_date > end)
      - (graduation_date IS NULL OR graduation_date > end)

    Note: 期末加上 lifecycle_status == 'active' 是因為退學/畢業/休學/轉出在
    生命週期上有獨立旗標，光看日期會把休學中的學生算為「留住」。
    """
    result: dict[int, ClassRetentionAggregate] = {}
    for emp_id, cid in employee_to_classroom.items():
        if cid is None:
            result[emp_id] = ClassRetentionAggregate(employee_id=emp_id)
            continue
        initial_q = (
            session.query(func.count(Student.id))
            .filter(Student.classroom_id == cid)
            .filter(Student.enrollment_date.isnot(None))
            .filter(Student.enrollment_date <= start)
            .filter(
                or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > start)
            )
            .filter(
                or_(Student.graduation_date.is_(None), Student.graduation_date > start)
            )
        )
        initial = int(initial_q.scalar() or 0)
        final_q = (
            session.query(func.count(Student.id))
            .filter(Student.classroom_id == cid)
            .filter(Student.lifecycle_status == LIFECYCLE_ACTIVE)
            .filter(Student.enrollment_date.isnot(None))
            .filter(Student.enrollment_date <= end)
            .filter(
                or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > end)
            )
            .filter(
                or_(Student.graduation_date.is_(None), Student.graduation_date > end)
            )
        )
        final = int(final_q.scalar() or 0)
        rate = (
            (Decimal(final) / Decimal(initial) * 100).quantize(Decimal("0.01"))
            if initial > 0
            else Decimal("0")
        )
        result[emp_id] = ClassRetentionAggregate(
            employee_id=emp_id,
            classroom_id=cid,
            classroom_name=classroom_name_by_id.get(cid),
            initial_count=initial,
            final_count=final,
            retention_rate=rate,
        )
    return result


def _aggregate_activity_rate(
    session: Session,
    employee_to_classroom: dict[int, Optional[int]],
    school_year_int: int,
    semester_int: int,
) -> dict[int, ActivityRateAggregate]:
    """該班 active 學生 vs 該班學期內報名才藝課（去重 student_id）。

    分母用「該班 active 學生數」而非 ActivityRegistration 累計報名數，
    避免同一學生報多堂被重複計算。
    """
    result: dict[int, ActivityRateAggregate] = {}
    classroom_ids = list({c for c in employee_to_classroom.values() if c is not None})
    if not classroom_ids:
        for eid in employee_to_classroom:
            result[eid] = ActivityRateAggregate(employee_id=eid)
        return result
    enrolled_rows = (
        session.query(Student.classroom_id, func.count(Student.id))
        .filter(Student.classroom_id.in_(classroom_ids))
        .filter(Student.lifecycle_status == LIFECYCLE_ACTIVE)
        .group_by(Student.classroom_id)
        .all()
    )
    enrolled_by_class = {cid: int(cnt) for cid, cnt in enrolled_rows}
    reg_rows = (
        session.query(
            ActivityRegistration.classroom_id,
            func.count(func.distinct(ActivityRegistration.student_id)),
        )
        .filter(ActivityRegistration.classroom_id.in_(classroom_ids))
        .filter(ActivityRegistration.school_year == school_year_int)
        .filter(ActivityRegistration.semester == semester_int)
        .filter(ActivityRegistration.student_id.isnot(None))
        .filter(ActivityRegistration.is_active.is_(True))
        .group_by(ActivityRegistration.classroom_id)
        .all()
    )
    reg_by_class = {cid: int(cnt) for cid, cnt in reg_rows}
    for emp_id, cid in employee_to_classroom.items():
        if cid is None:
            result[emp_id] = ActivityRateAggregate(employee_id=emp_id)
            continue
        enrolled = enrolled_by_class.get(cid, 0)
        registered = reg_by_class.get(cid, 0)
        rate = (
            (Decimal(registered) / Decimal(enrolled) * 100).quantize(Decimal("0.01"))
            if enrolled > 0
            else Decimal("0")
        )
        result[emp_id] = ActivityRateAggregate(
            employee_id=emp_id,
            classroom_id=cid,
            enrolled_students=enrolled,
            registered_for_activity=registered,
            activity_rate=rate,
        )
    return result


def _aggregate_disciplinary(
    session: Session, employee_ids: list[int], start: date, end: date
) -> dict[int, DisciplinaryAggregate]:
    result = {eid: DisciplinaryAggregate(employee_id=eid) for eid in employee_ids}
    if not employee_ids:
        return result
    rows = (
        session.query(DisciplinaryAction)
        .filter(DisciplinaryAction.employee_id.in_(employee_ids))
        .filter(DisciplinaryAction.action_date >= start)
        .filter(DisciplinaryAction.action_date <= end)
        .order_by(DisciplinaryAction.action_date.desc())
        .all()
    )
    for row in rows:
        agg = result[row.employee_id]
        atype = (row.action_type or "").lower()
        agg.actions.append(
            DisciplinaryActionItem(
                id=row.id,
                action_date=row.action_date,
                action_type=row.action_type,
                deduction_amount=row.deduction_amount,
                reason=getattr(row, "reason", None),
            )
        )
        if atype == "warning":
            agg.warning_count += 1
        elif atype == "minor":
            agg.minor_count += 1
        elif atype == "major":
            agg.major_count += 1
    return result


# ===== Facade =====


def aggregate_cycle_status(
    session: Session,
    cycle: AppraisalCycle,
    employee_ids: Optional[list[int]] = None,
) -> list[ParticipantStatus]:
    """彙整 cycle 期間每位 participant 的四個指標。

    Args:
        session: 來自 caller 的 SQLAlchemy session（不會被 commit / close）
        cycle: AppraisalCycle 實體（必須已 persist）
        employee_ids: 若給定則只彙整這些 employee（用於前端只看單人狀態）

    Returns:
        list[ParticipantStatus]，跳過 is_excluded=True 的 participant，
        順序依 AppraisalParticipant.id 升冪。

    時間窗：`[cycle.start_date, min(cycle.end_date, today)]`
    """
    participants = (
        session.query(AppraisalParticipant)
        .filter_by(cycle_id=cycle.id, is_excluded=False)
        .order_by(AppraisalParticipant.id)
        .all()
    )
    if employee_ids is not None:
        emp_set = set(employee_ids)
        participants = [p for p in participants if p.employee_id in emp_set]
    if not participants:
        return []
    employee_ids_list = [p.employee_id for p in participants]
    employees = {
        e.id: e.name
        for e in session.query(Employee)
        .filter(Employee.id.in_(employee_ids_list))
        .all()
    }
    employee_to_classroom = {p.employee_id: p.classroom_id for p in participants}
    classroom_ids = list({c for c in employee_to_classroom.values() if c is not None})
    classroom_name_by_id = (
        {
            c.id: c.name
            for c in session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        }
        if classroom_ids
        else {}
    )
    start = cycle.start_date
    end = min(cycle.end_date, date.today())
    att_map = _aggregate_attendance(session, employee_ids_list, start, end)
    ret_map = _aggregate_class_retention(
        session, employee_to_classroom, classroom_name_by_id, start, end
    )
    act_map = _aggregate_activity_rate(
        session,
        employee_to_classroom,
        cycle.academic_year,
        semester_enum_to_int(cycle.semester),
    )
    dis_map = _aggregate_disciplinary(session, employee_ids_list, start, end)
    out: list[ParticipantStatus] = []
    for p in participants:
        out.append(
            ParticipantStatus(
                participant_id=p.id,
                employee_id=p.employee_id,
                employee_name=employees.get(p.employee_id, f"emp#{p.employee_id}"),
                role_group=(
                    p.role_group.value
                    if hasattr(p.role_group, "value")
                    else str(p.role_group)
                ),
                classroom_id=p.classroom_id,
                attendance=att_map[p.employee_id],
                retention=ret_map[p.employee_id],
                activity=act_map[p.employee_id],
                disciplinary=dis_map[p.employee_id],
                is_participant=True,
                hire_months_in_cycle=p.hire_months_in_cycle,
            )
        )
    return out


def aggregate_all_active_employees_status(
    session: Session,
    cycle: AppraisalCycle,
) -> list[ParticipantStatus]:
    """彙整 cycle 期間所有 is_active=True 員工的四指標。

    已加入 cycle 的 participant 標 is_participant=True 並帶 participant_id；
    未加入的標 is_participant=False、participant_id=None，role_group / classroom_id
    由 employee_inference helpers 推斷。is_excluded=True 的 participant 不出現。
    """
    from services.appraisal.employee_inference import (
        infer_classroom_id,
        infer_role_group,
    )

    employees = (
        session.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
    )
    if not employees:
        return []
    emp_by_id = {e.id: e for e in employees}
    employee_ids = list(emp_by_id.keys())

    # 取既有 participants（in cycle, 排除 is_excluded=True）
    participants_rows = (
        session.query(AppraisalParticipant)
        .filter_by(cycle_id=cycle.id, is_excluded=False)
        .all()
    )
    participant_by_emp = {p.employee_id: p for p in participants_rows}

    # employee → classroom_id / role_group：participant 優先 override；非 participant 用 Employee 推斷
    employee_to_classroom: dict[int, Optional[int]] = {}
    employee_to_role: dict[int, RoleGroup] = {}
    for eid in employee_ids:
        emp = emp_by_id[eid]
        p = participant_by_emp.get(eid)
        if p is not None:
            employee_to_classroom[eid] = p.classroom_id
            employee_to_role[eid] = p.role_group
        else:
            employee_to_classroom[eid] = infer_classroom_id(emp)
            employee_to_role[eid] = infer_role_group(emp)

    classroom_ids = list({c for c in employee_to_classroom.values() if c is not None})
    classroom_name_by_id = (
        {
            c.id: c.name
            for c in session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        }
        if classroom_ids
        else {}
    )

    start = cycle.start_date
    end = min(cycle.end_date, date.today())
    att_map = _aggregate_attendance(session, employee_ids, start, end)
    ret_map = _aggregate_class_retention(
        session, employee_to_classroom, classroom_name_by_id, start, end
    )
    act_map = _aggregate_activity_rate(
        session,
        employee_to_classroom,
        cycle.academic_year,
        semester_enum_to_int(cycle.semester),
    )
    dis_map = _aggregate_disciplinary(session, employee_ids, start, end)

    out: list[ParticipantStatus] = []
    for eid in employee_ids:
        emp = emp_by_id[eid]
        p = participant_by_emp.get(eid)
        role = employee_to_role[eid]
        out.append(
            ParticipantStatus(
                participant_id=p.id if p else None,
                employee_id=eid,
                employee_name=emp.name,
                role_group=role.value if hasattr(role, "value") else str(role),
                classroom_id=employee_to_classroom[eid],
                attendance=att_map[eid],
                retention=ret_map[eid],
                activity=act_map[eid],
                disciplinary=dis_map[eid],
                is_participant=p is not None,
                hire_months_in_cycle=p.hire_months_in_cycle if p else None,
            )
        )
    # 排序：已加入考核者在前，再依員工姓名
    out.sort(key=lambda s: (not s.is_participant, s.employee_name))
    return out


__all__ = [
    "ActivityRateAggregate",
    "AttendanceAggregate",
    "ClassRetentionAggregate",
    "DisciplinaryActionItem",
    "DisciplinaryAggregate",
    "ParticipantStatus",
    "aggregate_all_active_employees_status",
    "aggregate_cycle_status",
]
