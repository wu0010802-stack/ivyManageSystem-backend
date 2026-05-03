"""教師工作台彙整 endpoint。"""

import logging
from datetime import datetime, date as date_cls
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_session
from services.portal_class_hub_service import (
    SLOT_DEFINITIONS,
    classify_time_to_slot,
    count_attendance_pending,
    count_contact_book_pending,
    count_incidents_today,
    count_observation_pending,
    list_pending_medications,
    pick_sticky_next,
    resolve_teacher_classroom,
)
from utils.auth import get_current_user
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter()


# ============ Pydantic Models ============


class ClassHubTaskSample(BaseModel):
    student_id: int
    student_name: str
    detail: Optional[str] = None
    due_at: Optional[datetime] = None


class ClassHubTask(BaseModel):
    kind: Literal[
        "attendance",
        "medication",
        "observation",
        "incident",
        "contact_book",
    ]
    count: int
    due_at: Optional[datetime] = None
    samples: list[ClassHubTaskSample] = []
    action_mode: Literal["sheet", "page", "inline_button"]


class ClassHubSlotData(BaseModel):
    slot_id: Literal["morning", "forenoon", "noon", "afternoon"]
    tasks: list[ClassHubTask] = []


class ClassHubCounts(BaseModel):
    attendance_pending: int = 0
    medications_pending: int = 0
    observations_pending: int = 0
    incidents_today: int = 0
    contact_books_pending: int = 0


class ClassHubStickyTask(BaseModel):
    kind: str
    student_name: Optional[str] = None
    detail: str
    due_at: datetime
    deep_link: str


class ClassHubTodayResponse(BaseModel):
    classroom_id: int
    classroom_name: str
    fetched_at: datetime
    sticky_next: Optional[ClassHubStickyTask] = None
    counts: ClassHubCounts
    slots: list[ClassHubSlotData]


# ============ Endpoint ============


@router.get("/class-hub/today", response_model=ClassHubTodayResponse)
def get_class_hub_today(
    current_user: dict = Depends(get_current_user),
    sess: Session = Depends(get_session),
) -> ClassHubTodayResponse:
    """教師今日工作台彙整：4 段時段卡 + sticky_next + counts。

    若教師沒有指派班級，回傳 classroom_id=0 的空殼結構（前端顯示空狀態）。
    """
    today = date_cls.today()
    now = datetime.now()
    employee_id = current_user.get("employee_id")

    # 權限遮罩；-1 (或全位元) 表示管理員擁有全部權限
    perms = int(current_user.get("permissions", 0) or 0)

    def has(p: int) -> bool:
        return perms < 0 or (perms & p) != 0

    # 無 employee_id 或無班級 → 空殼
    classroom = (
        resolve_teacher_classroom(sess, employee_id=employee_id)
        if employee_id
        else None
    )
    if classroom is None:
        return ClassHubTodayResponse(
            classroom_id=0,
            classroom_name="",
            fetched_at=now,
            sticky_next=None,
            counts=ClassHubCounts(),
            slots=[
                ClassHubSlotData(slot_id=sd.slot_id, tasks=[])
                for sd in SLOT_DEFINITIONS
            ],
        )

    # 蒐集各類待辦（依權限過濾）
    attn_pending = (
        count_attendance_pending(sess, classroom_id=classroom.id, today=today)
        if has(Permission.STUDENTS_READ)
        else 0
    )
    medications = (
        list_pending_medications(sess, classroom_id=classroom.id, today=today)
        if has(Permission.STUDENTS_HEALTH_READ)
        else []
    )
    obs_pending = (
        count_observation_pending(sess, classroom_id=classroom.id, today=today)
        if has(Permission.PORTFOLIO_READ)
        else 0
    )
    incidents = (
        count_incidents_today(sess, classroom_id=classroom.id, today=today)
        if has(Permission.STUDENTS_READ)
        else 0
    )
    contact_pending = (
        count_contact_book_pending(sess, classroom_id=classroom.id, today=today)
        if has(Permission.PORTFOLIO_READ)
        else 0
    )

    counts = ClassHubCounts(
        attendance_pending=attn_pending,
        medications_pending=len(medications),
        observations_pending=obs_pending,
        incidents_today=incidents,
        contact_books_pending=contact_pending,
    )

    # 組裝 slots（依 spec §4.4 任務→時段對應）
    slot_tasks: dict[str, list[ClassHubTask]] = {
        sd.slot_id: [] for sd in SLOT_DEFINITIONS
    }

    # 學生點名 → 早晨
    if attn_pending > 0:
        slot_tasks["morning"].append(
            ClassHubTask(
                kind="attendance",
                count=attn_pending,
                action_mode="sheet",
            )
        )

    # 用藥 → 依 due_at 落入哪個時段，每筆 1 個 task
    for med in medications:
        slot_id = classify_time_to_slot(med["due_at"].time())
        slot_tasks[slot_id].append(
            ClassHubTask(
                kind="medication",
                count=1,
                due_at=med["due_at"],
                samples=[
                    ClassHubTaskSample(
                        student_id=med["student_id"],
                        student_name=med["student_name"],
                        detail=med["detail"],
                        due_at=med["due_at"],
                    )
                ],
                action_mode="sheet",
            )
        )

    # 課堂觀察 → 上午
    if obs_pending > 0:
        slot_tasks["forenoon"].append(
            ClassHubTask(
                kind="observation",
                count=obs_pending,
                action_mode="page",
            )
        )

    # 事件紀錄 → 上午（含「+ 新增」入口，count 即使為 0 也顯示；需 STUDENTS_READ）
    if has(Permission.STUDENTS_READ):
        slot_tasks["forenoon"].append(
            ClassHubTask(
                kind="incident",
                count=incidents,
                action_mode="inline_button",
            )
        )

    # 聯絡簿 → 下午
    if contact_pending > 0:
        slot_tasks["afternoon"].append(
            ClassHubTask(
                kind="contact_book",
                count=contact_pending,
                action_mode="page",
            )
        )

    slots = [
        ClassHubSlotData(slot_id=sd.slot_id, tasks=slot_tasks[sd.slot_id])
        for sd in SLOT_DEFINITIONS
    ]

    # sticky_next：v1 僅 medication 有 due_at；其他類型若未來加入排程時間，於此擴充。
    sticky_candidates = [
        {
            "kind": "medication",
            "student_name": med["student_name"],
            "detail": med["detail"],
            "due_at": med["due_at"],
            "deep_link": f"/portal/class-hub?sheet=medication&id={med['id']}",
        }
        for med in medications
    ]
    sticky_raw = pick_sticky_next(sticky_candidates, now)
    sticky_next = ClassHubStickyTask(**sticky_raw) if sticky_raw else None

    return ClassHubTodayResponse(
        classroom_id=classroom.id,
        classroom_name=classroom.name,
        fetched_at=now,
        sticky_next=sticky_next,
        counts=counts,
        slots=slots,
    )
