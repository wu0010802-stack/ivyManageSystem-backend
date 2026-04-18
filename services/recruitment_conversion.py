"""招生訪視 → 正式學生 轉化服務（原子）。

輸入：一筆 `recruitment_visits.id`
輸出：新建立的 `Student`（含監護人 + 關聯 recruitment_visit_id + ChangeLog「入學」）

原子性：所有寫入使用單一 session flush；呼叫端負責 commit/rollback。
任何驗證失敗拋 `RecruitmentConversionError`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from models.classroom import LIFECYCLE_ACTIVE, LIFECYCLE_ENROLLED, Student
from models.guardian import Guardian
from models.recruitment import RecruitmentVisit
from models.student_log import StudentChangeLog
from utils.academic import resolve_current_academic_term


class RecruitmentConversionError(ValueError):
    """招生轉化流程的業務錯誤。"""


@dataclass
class ConversionResult:
    student_id: int
    recruitment_visit_id: int
    change_log_id: int
    primary_guardian_id: Optional[int]


def convert_recruitment_to_student(
    session: Session,
    recruitment_visit_id: int,
    student_id_code: str,
    *,
    classroom_id: Optional[int] = None,
    enrollment_date: Optional[date] = None,
    initial_lifecycle_status: str = LIFECYCLE_ENROLLED,
    gender: Optional[str] = None,
    recorded_by: Optional[int] = None,
) -> ConversionResult:
    """從 recruitment_visit 建立正式 Student（原子操作）。

    Parameters
    ----------
    student_id_code: 使用者提供的學號（需唯一）
    initial_lifecycle_status: 建立時的生命週期狀態，預設 enrolled（已報到未開學）。
        也可為 active（已開學）。
    """
    if initial_lifecycle_status not in (LIFECYCLE_ENROLLED, LIFECYCLE_ACTIVE):
        raise RecruitmentConversionError(
            f"initial_lifecycle_status 僅允許 enrolled 或 active，收到 {initial_lifecycle_status!r}"
        )

    visit = (
        session.query(RecruitmentVisit)
        .filter(RecruitmentVisit.id == recruitment_visit_id)
        .first()
    )
    if visit is None:
        raise RecruitmentConversionError(
            f"招生訪視不存在：id={recruitment_visit_id}"
        )

    # 重複轉化檢查
    existing = (
        session.query(Student)
        .filter(Student.recruitment_visit_id == recruitment_visit_id)
        .first()
    )
    if existing is not None:
        raise RecruitmentConversionError(
            f"此招生訪視已轉化為學生（student_id={existing.id}）"
        )

    # 學號唯一性
    code = (student_id_code or "").strip()
    if not code:
        raise RecruitmentConversionError("學號不可為空")
    dup = session.query(Student).filter(Student.student_id == code).first()
    if dup is not None:
        raise RecruitmentConversionError(f"學號已存在：{code}")

    enroll_date = enrollment_date or date.today()

    student = Student(
        student_id=code,
        name=(visit.child_name or "").strip() or "未命名",
        gender=gender,
        birthday=visit.birthday,
        classroom_id=classroom_id,
        enrollment_date=enroll_date,
        lifecycle_status=initial_lifecycle_status,
        recruitment_visit_id=visit.id,
        parent_phone=visit.phone,  # 快照欄位
        address=visit.address,
        notes=(visit.notes or None),
        is_active=True,
    )
    session.add(student)
    session.flush()  # 取得 student.id

    # 從 recruitment 資料建立主要監護人（若有電話或來訪者資訊）
    primary_guardian_id: Optional[int] = None
    if (visit.phone or "").strip():
        guardian = Guardian(
            student_id=student.id,
            name=(visit.referrer or "家長").strip() or "家長",
            phone=visit.phone.strip(),
            relation="監護人",
            is_primary=True,
            is_emergency=False,
            can_pickup=True,
            sort_order=0,
        )
        session.add(guardian)
        session.flush()
        primary_guardian_id = guardian.id
        # 快照回寫到 students.parent_name/phone（相容期雙寫）
        student.parent_name = guardian.name
        student.parent_phone = guardian.phone

    # 寫入「入學」異動紀錄。
    # 刻意不呼叫 student_lifecycle.transition()：此處是「建立」而非「既有學生的狀態轉移」，
    # 學生尚未進入狀態機（沒有 from_status）。若走 transition() 會變成
    # prospect → enrolled 的偽轉移並寫出第二筆 ChangeLog。
    school_year, semester = resolve_current_academic_term()
    change_log = StudentChangeLog(
        student_id=student.id,
        school_year=school_year,
        semester=semester,
        event_type="入學",
        event_date=enroll_date,
        classroom_id=student.classroom_id,
        reason="招生轉化",
        notes=f"由招生訪視 #{visit.id} 轉化",
        recorded_by=recorded_by,
    )
    session.add(change_log)
    session.flush()

    # 更新 recruitment 狀態（enrolled 旗標）— 不變更 visit 其他欄位
    visit.enrolled = True

    return ConversionResult(
        student_id=student.id,
        recruitment_visit_id=visit.id,
        change_log_id=change_log.id,
        primary_guardian_id=primary_guardian_id,
    )
