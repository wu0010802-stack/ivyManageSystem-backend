"""services/recruitment_funnel.py — 招生漏斗狀態機 + 寫入入口。

純函式（derive_stage / can_transition / is_destructive）位於檔頭，
orchestrator `transition_visit()` 在後續 task 補。
"""

from __future__ import annotations

import re
from typing import Literal, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

Stage = Literal["visited", "deposited", "enrolled", "active"]
STAGES: tuple[Stage, ...] = ("visited", "deposited", "enrolled", "active")


class _VisitLike(Protocol):
    has_deposit: bool


class _StudentLike(Protocol):
    lifecycle_status: str


def derive_stage(visit: _VisitLike, student: Optional[_StudentLike]) -> Stage:
    """從 (visit, student) 推導 4 階段。

    規則：student 存在性優先（avoid dual source of truth）。
    """
    if student is not None:
        return "active" if student.lifecycle_status == "active" else "enrolled"
    return "deposited" if visit.has_deposit else "visited"


def can_transition(from_stage: Stage, to_stage: Stage) -> bool:
    """Phase A：任意拖。保留位置給未來收緊規則（例：禁止跨多段躍進）。"""
    return True


_DESTRUCTIVE_FROM: frozenset[Stage] = frozenset({"enrolled", "active"})


def is_destructive(from_stage: Stage, to_stage: Stage) -> bool:
    """destructive = 從 enrolled/active 退回任何前段。"""
    if from_stage not in _DESTRUCTIVE_FROM:
        return False
    order = {s: i for i, s in enumerate(STAGES)}
    return order[to_stage] < order[from_stage]


# ── 學號產生 ────────────────────────────────────────────────────────────────

_STUDENT_ID_RE = re.compile(r"^(\d{3})-([A-Za-z0-9_-]+)-(\d{2,})$")


def next_student_id_code(session: Session, school_year: int, class_code: str) -> str:
    """產 {year}-{class_code}-{NN}（NN 兩位數零填，同年同班遞增）。

    Postgres 上以 pg_advisory_xact_lock 防並發撞號（lock 範圍涵蓋整個 transaction，
    commit/rollback 時自動釋放）。SQLite/其他 dialect 無此 function — 跳過 lock；
    測試時用 in-memory SQLite 單連線本就無並發。
    """
    from models.classroom import Student  # 延遲 import 避免循環

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        lock_key = hash((school_year, class_code)) % (2**31)
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    prefix = f"{school_year}-{class_code}-"
    rows = (
        session.query(Student.student_id)
        .filter(Student.student_id.like(f"{prefix}%"))
        .all()
    )
    max_seq = 0
    for (sid,) in rows:
        m = _STUDENT_ID_RE.match(sid or "")
        if m and m.group(1) == str(school_year) and m.group(2) == class_code:
            max_seq = max(max_seq, int(m.group(3)))
    return f"{prefix}{max_seq + 1:02d}"


# ── 還原守衛 ─────────────────────────────────────────────────────────────────


class RecruitmentFunnelError(ValueError):
    """Funnel 業務錯誤（caller catch → HTTP 400）。"""

    def __init__(self, message: str, code: str = "FUNNEL_ERROR"):
        super().__init__(message)
        self.code = code


# 下游業務白名單 — 任一存在則無法 revert convert
# 格式：(模組路徑, 類別名, FK 欄位名, 友善標籤)
# 注意：只列確認有 student_id 欄位的 model。
# StudentFeePayment / StudentFeeRefund 透過 record_id 間接關聯，不在此列。
# StudentMedicationLog 透過 order_id 間接關聯，不在此列。
# GuardianBindingCode 以 guardian_id 關聯，不在此列。
_REVERT_BLOCKERS: list[tuple[str, str, str, str]] = [
    ("models.classroom", "StudentAttendance", "student_id", "出席紀錄"),
    ("models.fees", "StudentFeeRecord", "student_id", "繳費資料"),
    ("models.classroom", "StudentAssessment", "student_id", "評量"),
    ("models.classroom", "StudentIncident", "student_id", "獎懲紀錄"),
    ("models.portfolio", "StudentObservation", "student_id", "觀察"),
    ("models.portfolio", "StudentAllergy", "student_id", "過敏資料"),
    ("models.portfolio", "StudentMedicationOrder", "student_id", "餵藥單"),
    ("models.portfolio", "StudentMeasurement", "student_id", "體溫/體重紀錄"),
    ("models.portfolio", "StudentMilestone", "student_id", "里程碑"),
]


from dataclasses import dataclass, field
from datetime import datetime
from utils.taipei_time import now_taipei_naive


@dataclass
class TransitionResult:
    visit_id: int
    from_stage: Stage
    to_stage: Stage
    student_id: Optional[int]
    event_log_id: int
    warnings: list[str] = field(default_factory=list)


def _load_visit_locked(session: Session, visit_id: int):
    """讀 visit row。Postgres 用 SELECT FOR UPDATE 鎖；其他 dialect 跳過鎖。"""
    from models.recruitment import RecruitmentVisit

    q = session.query(RecruitmentVisit).filter(RecruitmentVisit.id == visit_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        q = q.with_for_update()
    return q.first()


def _load_student_by_visit(session: Session, visit_id: int):
    from models.classroom import Student

    return (
        session.query(Student).filter(Student.recruitment_visit_id == visit_id).first()
    )


def _write_event_log(
    session: Session,
    *,
    visit_id: int,
    event_type: str,
    from_stage: Optional[Stage],
    to_stage: Stage,
    student_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    from models.recruitment import RecruitmentEventLog

    log = RecruitmentEventLog(
        recruitment_visit_id=visit_id,
        event_type=event_type,
        from_stage=from_stage,
        to_stage=to_stage,
        student_id=student_id,
        actor_user_id=actor_user_id,
        reason=reason,
        metadata_json=metadata,
        created_at=now_taipei_naive(),
    )
    session.add(log)
    session.flush()
    return log.id


def _do_toggle_deposit(session, visit, *, to_stage: Stage, actor_user_id):
    """visited ↔ deposited 互相切換 has_deposit 旗標。"""
    visit.has_deposit = to_stage == "deposited"
    event_type = "deposit_added" if to_stage == "deposited" else "deposit_removed"
    from_stage_str: Stage = "visited" if to_stage == "deposited" else "deposited"
    log_id = _write_event_log(
        session,
        visit_id=visit.id,
        event_type=event_type,
        from_stage=from_stage_str,
        to_stage=to_stage,
        actor_user_id=actor_user_id,
    )
    return None, log_id  # (student_id, log_id)


def _do_convert(session, visit, *, classroom_id, actor_user_id):
    """deposited → enrolled：呼叫 convert_recruitment_to_student（會寫 event log + ChangeLog）。"""
    from services.recruitment_conversion import (
        convert_recruitment_to_student,
        RecruitmentConversionError,
    )
    from models.recruitment import RecruitmentEventLog

    if classroom_id is None:
        raise RecruitmentFunnelError(
            "已預繳→已報到 需要 classroom_id",
            code="CONVERT_NEED_CLASSROOM",
        )
    try:
        result = convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            student_id_code=None,  # 走自動產號路徑
            classroom_id=classroom_id,
            recorded_by=actor_user_id,
        )
    except RecruitmentConversionError as exc:
        # Bug #20：deposited→enrolled 並發 race（既有/並發報名已先建立 Student）時，
        # convert 會拋 RecruitmentConversionError。此處包成 RecruitmentFunnelError
        # 讓 API try 區塊能 catch（否則冒泡成 500）。CONVERT_CONFLICT 由 API 映射為
        # 409（衝突），其餘 funnel 業務錯誤維持 400。
        raise RecruitmentFunnelError(str(exc), code="CONVERT_CONFLICT") from exc
    # convert 內部已寫 funnel event log（converted）— 撈出 id
    last_log = (
        session.query(RecruitmentEventLog)
        .filter_by(recruitment_visit_id=visit.id, event_type="converted")
        .order_by(RecruitmentEventLog.id.desc())
        .first()
    )
    return result.student_id, last_log.id


def _do_activate(session, visit, student, *, actor_user_id):
    """enrolled → active: lifecycle 升級。"""
    from utils.student_lifecycle import set_lifecycle_status

    # 走統一入口寫 lifecycle，補上全站 AuditLog（RecruitmentEventLog 僅漏斗自身軌跡，
    # 不進統一稽核）。enrolled↔active 皆非終態，terminal_entered_at / PII GC 不受影響。
    set_lifecycle_status(session, student, "active", actor_user_id=actor_user_id)
    log_id = _write_event_log(
        session,
        visit_id=visit.id,
        event_type="activated",
        from_stage="enrolled",
        to_stage="active",
        student_id=student.id,
        actor_user_id=actor_user_id,
    )
    return student.id, log_id


def _do_revert_convert(session, visit, student, *, actor_user_id, reason):
    """enrolled → deposited: 刪 Student（含 Guardian、ChangeLog），flip visit.enrolled=False。

    呼叫前提：assert_student_revertable() 已通過。
    """
    from models.guardian import Guardian
    from models.student_log import StudentChangeLog

    assert_student_revertable(session, student.id)
    student_id = student.id

    session.query(Guardian).filter(Guardian.student_id == student_id).delete(
        synchronize_session=False
    )
    session.query(StudentChangeLog).filter(
        StudentChangeLog.student_id == student_id
    ).delete(synchronize_session=False)
    session.delete(student)
    visit.enrolled = False

    log_id = _write_event_log(
        session,
        visit_id=visit.id,
        event_type="revert_converted",
        from_stage="enrolled",
        to_stage="deposited",
        student_id=None,
        actor_user_id=actor_user_id,
        reason=reason,
        metadata={"deleted_student_id": student_id},
    )
    return None, log_id  # student deleted


def _do_revert_activate(session, visit, student, *, actor_user_id, reason):
    """active → enrolled: lifecycle 降級；若已有 attendance 則 warning（不擋）。"""
    from models.classroom import StudentAttendance

    warnings: list[str] = []
    has_attendance = (
        session.query(StudentAttendance)
        .filter(StudentAttendance.student_id == student.id)
        .limit(1)
        .first()
    )
    if has_attendance:
        warnings.append("student_has_attendance_after_active")
    from utils.student_lifecycle import set_lifecycle_status

    set_lifecycle_status(session, student, "enrolled", actor_user_id=actor_user_id)
    log_id = _write_event_log(
        session,
        visit_id=visit.id,
        event_type="revert_activated",
        from_stage="active",
        to_stage="enrolled",
        student_id=student.id,
        actor_user_id=actor_user_id,
        reason=reason,
    )
    return student.id, log_id, warnings


def transition_visit(
    session: Session,
    visit_id: int,
    to_stage: Stage,
    actor_user_id: Optional[int],
    *,
    classroom_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> TransitionResult:
    """單一 atomic stage transition。

    流程：lock visit → derive from_stage → 規則檢查 → dispatch sub-action → 寫 event log → flush。
    Commit/rollback 由 caller 負責。
    """
    visit = _load_visit_locked(session, visit_id)
    if visit is None:
        raise RecruitmentFunnelError(
            f"招生訪視不存在：id={visit_id}", code="VISIT_NOT_FOUND"
        )
    student = _load_student_by_visit(session, visit_id)
    from_stage = derive_stage(visit, student)

    if from_stage == to_stage:
        raise RecruitmentFunnelError(f"已在 {to_stage} 階段", code="STAGE_ALREADY")
    if is_destructive(from_stage, to_stage) and not (reason and reason.strip()):
        raise RecruitmentFunnelError(
            "destructive 操作需提供 reason", code="REASON_REQUIRED"
        )

    warnings: list[str] = []

    # === Dispatch ===
    # Task 6: visited ↔ deposited
    if {from_stage, to_stage} == {"visited", "deposited"}:
        student_id, log_id = _do_toggle_deposit(
            session,
            visit,
            to_stage=to_stage,
            actor_user_id=actor_user_id,
        )

    # Task 8: deposited → enrolled
    elif from_stage == "deposited" and to_stage == "enrolled":
        student_id, log_id = _do_convert(
            session,
            visit,
            classroom_id=classroom_id,
            actor_user_id=actor_user_id,
        )

    # Task 9: enrolled ↔ active
    elif from_stage == "enrolled" and to_stage == "active":
        student_id, log_id = _do_activate(
            session,
            visit,
            student,
            actor_user_id=actor_user_id,
        )
    elif from_stage == "active" and to_stage == "enrolled":
        student_id, log_id, ws = _do_revert_activate(
            session,
            visit,
            student,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        warnings.extend(ws)

    # Task 10: enrolled → deposited / visited
    elif from_stage == "enrolled" and to_stage in ("deposited", "visited"):
        student_id, log_id = _do_revert_convert(
            session,
            visit,
            student,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        if to_stage == "visited":
            # 再走 deposited → visited
            visit.has_deposit = False
            log_id = _write_event_log(
                session,
                visit_id=visit.id,
                event_type="deposit_removed",
                from_stage="deposited",
                to_stage="visited",
                actor_user_id=actor_user_id,
                reason=reason,
            )

    # Task 10: active → deposited / visited (chain through active→enrolled first)
    elif from_stage == "active" and to_stage in ("deposited", "visited"):
        # 先 active → enrolled
        student_id, log_id, ws = _do_revert_activate(
            session,
            visit,
            student,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        warnings.extend(ws)
        # 重新 load student（剛 lifecycle 變 enrolled）
        student2 = _load_student_by_visit(session, visit.id)
        _, log_id = _do_revert_convert(
            session,
            visit,
            student2,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        student_id = None
        if to_stage == "visited":
            visit.has_deposit = False
            log_id = _write_event_log(
                session,
                visit_id=visit.id,
                event_type="deposit_removed",
                from_stage="deposited",
                to_stage="visited",
                actor_user_id=actor_user_id,
                reason=reason,
            )

    else:
        # R4-7：非法/未實作的跨段轉換改拋 RecruitmentFunnelError（caller catch → 400），
        # 原 NotImplementedError 未被 caller catch → 冒泡成 500。
        raise RecruitmentFunnelError(
            f"不支援的轉換 {from_stage} → {to_stage}",
            code="ILLEGAL_TRANSITION",
        )

    return TransitionResult(
        visit_id=visit.id,
        from_stage=from_stage,
        to_stage=to_stage,
        student_id=student_id,
        event_log_id=log_id,
        warnings=warnings,
    )


def assert_student_revertable(session: Session, student_id: int) -> None:
    """檢查 student 是否有下游業務記錄；任一存在則 raise RecruitmentFunnelError。

    caller 應在 destructive revert 前呼叫此函式，防止業務資料孤兒化。
    """
    import importlib

    for module_path, class_name, fk_col, label in _REVERT_BLOCKERS:
        try:
            module = importlib.import_module(module_path)
            model = getattr(module, class_name, None)
        except ImportError:
            continue
        if model is None:
            continue
        column = getattr(model, fk_col, None)
        if column is None:
            continue
        exists = session.query(model).filter(column == student_id).limit(1).first()
        if exists is not None:
            raise RecruitmentFunnelError(
                f"該學生已有業務資料（{label}），請走退學流程而非退回 funnel",
                code="REVERT_STUDENT_HAS_DATA",
            )
