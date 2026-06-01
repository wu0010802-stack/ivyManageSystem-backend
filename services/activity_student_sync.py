"""services/activity_student_sync.py — 才藝報名與學生主檔同步（F2 第七階段抽出）。

從 api/activity/_shared.py 抽出 4 個 helper：
- _match_student_id — 公開報名（姓名+生日）匹配在籍學生
- _match_student_with_parent_phone — 三欄比對（姓名+生日+家長手機）
- sync_registrations_on_student_transfer — 學生轉班時同步當學期啟用報名
- sync_registrations_on_student_deactivate — 學生離園/退學時軟刪當學期啟用報名 + 自動沖帳

api/activity/_shared.py 保留 re-export 維持 students.py / public.py / registrations.py 既有
import surface。

依賴設計：
- _normalize_phone：transit 依賴自 schemas.activity_public，避免重複定義
- has_payment_approve / SYSTEM_RECONCILE_METHOD：另一個 service 提供，本檔僅使用
- TAIPEI_TZ：本檔自有副本（與 _shared.py 一致），避免循環匯入
"""

import logging
from datetime import date as _date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from models.database import (
    ActivityPaymentRecord,
    ActivityRegistration,
    Classroom,
    RegistrationCourse,
    Student,
)
from schemas.activity_public import _normalize_phone
from services.activity_daily_snapshot import _require_daily_close_unlocked
from services.activity_payment_guards import has_payment_approve
from services.activity_service import OCCUPYING_STATUSES, activity_service
from utils.academic import resolve_current_academic_term

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
SYSTEM_RECONCILE_METHOD = "系統補齊"


def _match_student_id(session, name: str, birthday: str) -> Optional[int]:
    """public 報名時以 (name, birthday) 嘗試匹配 students.id。

    同時匹配到多個學生則回 None（避免錯誤關聯）。
    """
    try:
        bday = _date.fromisoformat(birthday)
    except (ValueError, TypeError):
        return None

    q = session.query(Student.id).filter(
        Student.name == name.strip(),
        Student.birthday == bday,
        Student.is_active.is_(True),
    )
    rows = q.limit(2).all()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _match_student_with_parent_phone(
    session, name: str, birthday: str, parent_phone: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    """三欄比對（姓名 + 生日 + 家長手機）取得在籍學生。

    - phone 同時與 Student.parent_phone、Student.emergency_contact_phone 比對
      （任一正規化後相符即匹配）
    - 先以 (name, birthday, is_active=True) 篩出候選（通常 0-2 筆），再
      Python 端正規化比對 phone，避開 SQL regex 全表掃描
    - 多筆匹配（歧義）→ 回 (None, None)，讓上游進入 pending_review
    - 無匹配 → 回 (None, None)
    - 成功 → 回 (student_id, classroom_id)
    """
    normalized_input = _normalize_phone(parent_phone)
    if not normalized_input:
        return (None, None)

    try:
        bday = _date.fromisoformat(birthday)
    except (ValueError, TypeError):
        return (None, None)

    candidates = (
        session.query(
            Student.id,
            Student.classroom_id,
            Student.parent_phone,
            Student.emergency_contact_phone,
        )
        .filter(
            Student.name == name.strip(),
            Student.birthday == bday,
            Student.is_active.is_(True),
        )
        .limit(10)
        .all()
    )

    matches: list[tuple[int, Optional[int]]] = []
    for sid, classroom_id, pp, ep in candidates:
        if (
            _normalize_phone(pp) == normalized_input
            or _normalize_phone(ep) == normalized_input
        ):
            matches.append((sid, classroom_id))

    if len(matches) == 1:
        return matches[0]
    return (None, None)


def sync_registrations_on_student_transfer(
    session, student_id: int, new_classroom_id: Optional[int]
) -> int:
    """學生轉班時，同步更新該生當前學期仍啟用的 ActivityRegistration 班級資訊。

    - 只處理 is_active=True 的報名（rejected / 軟刪除的不動）
    - 只處理當前學期（不回頭改歷史，歷史才藝名單應保持原樣）
    - classroom_id 改寫為 new_classroom_id；class_name 改為新班級的 Classroom.name（當前）
      若 new_classroom_id 為 None 或查不到班級，只更新 classroom_id，保留原 class_name 字串
    - 回傳更新筆數
    """
    sy, sem = resolve_current_academic_term()

    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == student_id,
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        .all()
    )
    if not regs:
        return 0

    new_classroom_name: Optional[str] = None
    if new_classroom_id is not None:
        new_classroom = (
            session.query(Classroom).filter(Classroom.id == new_classroom_id).first()
        )
        if new_classroom:
            new_classroom_name = new_classroom.name

    for r in regs:
        r.classroom_id = new_classroom_id
        if new_classroom_name:
            r.class_name = new_classroom_name

    return len(regs)


def sync_registrations_on_student_deactivate(
    session, student_id: int, *, current_user: Optional[dict] = None
) -> int:
    """學生畢業 / 退學 / 刪除時，軟刪該生當前學期啟用中 ActivityRegistration。

    - 把 is_active 設為 False；保留原 match_status（供後台稽核）
    - 只處理當前學期（歷史學期的報名維持原狀，仍可供報表追溯）
    - **若 paid_amount > 0**：自動寫一筆「系統補齊」退費沖帳紀錄並清零，
      並以 logger.warning 留痕提醒管理員處理實體退款；避免幽靈金額留存
    - **金流守衛**：若有任何 paid_amount > 0 且呼叫者未具 ACTIVITY_PAYMENT_APPROVE
      則 403。避免具 STUDENTS_WRITE/STUDENTS_LIFECYCLE_WRITE 但無金流簽核者
      透過學生狀態變更繞過活動退費端點的金流控管。
      呼叫端在 pure-system 場景（如背景任務、無 user 上下文）可省略 current_user，
      但生產 API handler 必須傳入；省略視為內部呼叫。
    - 回傳影響筆數
    """
    sy, sem = resolve_current_academic_term()

    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == student_id,
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        .all()
    )
    today = datetime.now(TAIPEI_TZ).date()
    has_paid = any((r.paid_amount or 0) > 0 for r in regs)
    if has_paid:
        _require_daily_close_unlocked(session, today)
        if current_user is not None and not has_payment_approve(current_user):
            paid_total = sum(r.paid_amount or 0 for r in regs)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"該生有 {sum(1 for r in regs if (r.paid_amount or 0) > 0)} 筆"
                    f"已繳費才藝報名（合計 NT${paid_total:,}）。"
                    "離園/刪除學生會自動沖帳全額退費，需具備『才藝課收款簽核』權限"
                    "（ACTIVITY_PAYMENT_APPROVE）。請改由具該權限者執行，或先至活動退費端點"
                    "個別處理退款後再刪除學生。"
                ),
            )
    deleted = 0
    failed: list[tuple[int, str]] = []
    for r in regs:
        try:
            with session.begin_nested():
                _soft_delete_single_registration(
                    session, r, student_id=student_id, today=today
                )
            deleted += 1
        except Exception as exc:
            failed.append((r.id, str(exc)))
            logger.exception(
                "學生離園同步軟刪單筆失敗，已 SAVEPOINT 回滾：reg_id=%s student_id=%s",
                r.id,
                student_id,
            )
    if failed:
        logger.warning(
            "學生離園同步軟刪共 %d 筆失敗（成功 %d 筆）：%s",
            len(failed),
            deleted,
            failed,
        )
    return deleted


def _soft_delete_single_registration(
    session, reg: ActivityRegistration, *, student_id: int, today: _date
) -> None:
    """單筆軟刪邏輯（必要時自動沖帳並寫退費紀錄）。

    呼叫端應包在 ``session.begin_nested()`` SAVEPOINT 內，這裡任何例外會
    把該筆 reg 的所有變更（含 ActivityPaymentRecord）回滾，但不污染外層 session。
    """
    current_paid = reg.paid_amount or 0
    if current_paid > 0:
        session.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="refund",
                amount=current_paid,
                payment_date=today,
                payment_method=SYSTEM_RECONCILE_METHOD,
                notes="（學生離園同步軟刪自動沖帳）",
                operator="system",
            )
        )
        reg.paid_amount = 0
        reg.is_paid = False
        logger.warning(
            "學生離園同步軟刪報名自動沖帳：reg_id=%s student_id=%s refunded=NT$%d，"
            "請管理員跟進實體退款",
            reg.id,
            student_id,
            current_paid,
        )
        activity_service.log_change(
            session,
            reg.id,
            reg.student_name,
            "學生離園自動沖帳",
            f"學生離園同步軟刪，系統寫退費紀錄 NT${current_paid}，請跟進實體退款",
            "system",
        )

    # 軟刪前先收集本筆報名佔位（enrolled / promoted_pending）的課程，
    # 軟刪後逐課遞補候補第一位（對齊 delete_registration 的釋放名額流程）。
    # 先收集再遞補，避免在迭代中改狀態影響容量判定。
    occupying_course_ids = [
        rc.course_id
        for rc in (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)),
            )
            .all()
        )
    ]

    reg.is_active = False
    session.flush()  # 先讓 is_active=False 生效，容量查詢才看得到釋放的名額

    for course_id in occupying_course_ids:
        activity_service._auto_promote_first_waitlist(session, course_id)
