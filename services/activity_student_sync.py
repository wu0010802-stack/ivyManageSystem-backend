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
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import CompileError

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


def _deactivate_term_filter(sy: int, sem: int):
    """學生離園時要取消的 active 報名學期條件：當前學期「及之後」+ NULL-term。

    school_year / semester 皆為 Integer（民國學年 / 1=上 2=下），可直接排序比較。
    - 未來學期 active 報名一併取消，避免續佔名額或被候補/付款流程處理；
    - 歷史學期（嚴格早於當前）保留供報表追溯，故用 >= 而非全部；
    - NULL-term（school_year 或 semester 任一為 NULL）屬異常資料，正常報名流程一律帶
      當前學期；但 NULL-term 的已繳費 active 報名若不取消，離園不會自動沖帳 → 幽靈
      金額/名額殘留，故一併納入軟刪（杜絕無人沖帳的幽靈報名）。對齊歷史 migration
      20260417 的回填條件 `school_year IS NULL OR semester IS NULL`：須同時涵蓋
      「school_year 有值但 semester NULL」這型 partial-null，否則離園後仍 active。
    """
    return or_(
        ActivityRegistration.school_year.is_(None),
        ActivityRegistration.semester.is_(None),
        ActivityRegistration.school_year > sy,
        and_(
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester >= sem,
        ),
    )


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


def find_active_dup_for_student(
    session,
    *,
    student_id: Optional[int],
    school_year,
    semester,
    exclude_reg_id: Optional[int] = None,
):
    """回傳同一在籍學生（student_id）同學期已存在的「他筆」active 報名（或 None）。

    不變量：同 (student_id NOT NULL, school_year, semester) 至多一筆 is_active 報名。
    供各寫入路徑在解析出 non-null student_id、即將綁定前做 check-then-act 守衛。

    為什麼需要（且 DB unique index 擋不住）：
    `uq_activity_regs_student_term_active` 的鍵是
    (student_name, birthday, school_year, semester, parent_phone) —— 含 parent_phone、
    不含 student_id。同一在籍學生有兩支官方電話（Student.parent_phone /
    Student.emergency_contact_phone）時，_match_student_with_parent_phone 任一支都會
    解析到同一 student_id，但 parent_phone 不同 → 不撞 unique index、也躲過以 phone
    為鍵的 existing 去重 → 同 student_id 同學期長出兩筆 active 報名（容量重複佔用、
    在籍人頭灌水、POS 對帳分裂）。

    呼叫端須先以 ``acquire_activity_registration_lock(name, birthday, term)`` 取得
    advisory lock 序列化並發（兩支電話的同一學生 name+birthday 相同 → 同一把鎖），
    再呼叫本函式 check；命中時自行決定 400 / silent-success。
    student_id 為 None（校外生 / 未匹配）不適用本守衛，回 None。
    exclude_reg_id：更新既有報名時排除自身。
    """
    if student_id is None:
        return None
    q = session.query(ActivityRegistration).filter(
        ActivityRegistration.student_id == student_id,
        ActivityRegistration.school_year == school_year,
        ActivityRegistration.semester == semester,
        ActivityRegistration.is_active.is_(True),
    )
    if exclude_reg_id is not None:
        q = q.filter(ActivityRegistration.id != exclude_reg_id)
    return q.first()


def sync_registrations_on_student_transfer(
    session, student_id: int, new_classroom_id: Optional[int]
) -> int:
    """學生轉班時，同步更新該生當前學期仍啟用的 ActivityRegistration 班級資訊。

    - 只處理 is_active=True 的報名（rejected / 軟刪除的不動）
    - 只處理當前學期（不回頭改歷史，歷史才藝名單應保持原樣）
    - classroom_id 改寫為 new_classroom_id；class_name 改為新班級的 Classroom.name（當前）
      若 new_classroom_id 為 None 或查不到班級，class_name 一併設為 None，確保兩欄位一致
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
        r.class_name = new_classroom_name

    return len(regs)


def sync_registrations_on_student_deactivate(
    session, student_id: int, *, current_user: Optional[dict] = None
) -> int:
    """學生畢業 / 退學 / 刪除時，軟刪該生當前學期啟用中 ActivityRegistration。

    - 把 is_active 設為 False；保留原 match_status（供後台稽核）
    - 處理當前學期「及之後」+ NULL-term（含未來學期 active 報名，避免續佔名額或
      被候補/付款流程處理；NULL-term 異常報名一併沖帳避免幽靈金額）；歷史學期報名
      維持原狀，仍可供報表追溯。詳見 _deactivate_term_filter
    - row lock（FOR UPDATE）+ populate_existing 取鎖內最新 paid_amount，與並發
      POS 收款序列化，杜絕用 stale 值覆寫（lost update）。鎖序：daily-close
      advisory 先、row lock 後（全 caller 統一，避免 deadlock）
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
    today = datetime.now(TAIPEI_TZ).date()

    base_filter = (
        ActivityRegistration.student_id == student_id,
        ActivityRegistration.is_active.is_(True),
        _deactivate_term_filter(sy, sem),
    )

    # ── 鎖序協議（全 caller 統一）：daily-close advisory 先、row lock 後 ──
    # POS checkout 走 advisory(payment_date) → row lock。本路徑若先 row lock 再取
    # advisory 會鎖序倒置 deadlock。故先「無鎖預讀」判斷本批是否有已繳費以決定是否
    # 取 advisory；刻意用純量 SUM 預讀、不載入 ORM 物件，使後續 populate_existing 的
    # FOR UPDATE 成為這些 reg 的（強制刷新）載入，取得鎖內最新值。
    prelim_paid_total = (
        session.query(func.coalesce(func.sum(ActivityRegistration.paid_amount), 0))
        .filter(*base_filter)
        .scalar()
    ) or 0
    prelim_has_paid = prelim_paid_total > 0
    if prelim_has_paid:
        _require_daily_close_unlocked(session, today)

    # ── row lock（advisory 之後）+ populate_existing：取得鎖內最新 paid_amount ──
    # 不加 populate_existing 時，若 reg 已在 session identity-map（同步流程先前讀過），
    # 查詢會回傳 stale 物件 → 自動沖帳用舊值覆寫並發 POS 已寫入的 paid_amount（lost
    # update）。SQLite 測試環境不支援 FOR UPDATE，降級為無鎖但仍刷新。
    # order_by(id)：與 POS `_lock_regs` 一致以 id 升冪取 row lock，使全系統 row-lock
    # 取鎖序確定一致，杜絕「同一學生多筆 reg 同時被 POS checkout 與離園 sync 鎖到、
    # 兩邊取鎖序相反」的 row-vs-row deadlock。
    reg_query = (
        session.query(ActivityRegistration)
        .filter(*base_filter)
        .order_by(ActivityRegistration.id)
    )
    try:
        regs = reg_query.populate_existing().with_for_update().all()
    except (CompileError, NotImplementedError):
        regs = reg_query.populate_existing().all()

    has_paid = any((r.paid_amount or 0) > 0 for r in regs)
    if has_paid and not prelim_has_paid:
        # 預讀無付款、取得 row lock 後卻出現付款（並發 POS 收款落在預讀與取鎖之間）。
        # 此刻尚未持 daily-close advisory，不可在 row lock 後補取（鎖序倒置 deadlock）。
        # 轉 409 要求重試：重試時預讀會看到付款 → 先取 advisory 再走完整流程。
        raise HTTPException(
            status_code=409,
            detail="偵測到並發才藝收款，請稍候重試離園/刪除操作",
        )
    if has_paid:
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
        failed_reg_ids = [reg_id for reg_id, _err in failed]
        reason = (
            f"共 {len(failed)} 筆 savepoint 回滾：{failed[0][1]}"
            if len(failed) == 1
            else f"共 {len(failed)} 筆 savepoint 回滾"
        )
        try:
            from services.ops_alert import notify_student_sync_failure

            notify_student_sync_failure(
                student_id=student_id,
                failed_registration_ids=failed_reg_ids,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "學生離園同步告警發送失敗（不影響主流程）：student_id=%s failed_ids=%s",
                student_id,
                failed_reg_ids,
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
