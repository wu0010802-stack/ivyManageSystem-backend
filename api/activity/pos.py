"""
api/activity/pos.py — 課後才藝 POS 快速收銀端點

三個端點：
  GET  /pos/outstanding-by-student   依學生聚合未結清報名
  POST /pos/checkout                 一次原子性結帳多筆報名（含行級鎖 + 冪等性）
  GET  /pos/daily-summary            今日日結摘要（分付款方式/類型）

安全保護：
- 行級鎖（SELECT ... FOR UPDATE）避免併發覆寫 paid_amount
- idempotency_key 冪等支援，重複提交不會產生多筆記錄
- 金額 / 日期 / 備註長度邊界驗證
- Rate limiter 防止短時間濫呼叫
"""

import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import CompileError, IntegrityError, OperationalError

from models.database import (
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistration,
    get_session,
)
from services.activity_service import activity_service
from services.report_cache_service import report_cache_service
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter

from ._shared import (
    TAIPEI_TZ,
    MIN_REFUND_REASON_LENGTH,
    _batch_calc_total_amounts,
    _build_registration_filter_query,
    _compute_is_paid,
    _derive_payment_status,
    _fetch_reg_course_details,
    _fetch_reg_course_names,
    _fetch_reg_supplies,
    _invalidate_activity_dashboard_caches,
    _require_daily_close_unlocked,
    compute_daily_snapshot,
    require_refund_reason,
    require_approve_for_large_refund,
    validate_payment_date,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 單個品項與實收金額上限（NT$999,999 / NT$9,999,999）— 避免誤輸入或整型誇張值
_MAX_ITEM_AMOUNT = 999_999
_MAX_TENDERED = 9_999_999
# 單次結帳總額上限 NT$1,000,000 — 避免前端繞過大額確認造成誤輸入巨額
_MAX_CHECKOUT_TOTAL = 1_000_000

# 冪等 key 有效視窗（秒）：此期間內同 key 視為重試
_IDEMPOTENCY_WINDOW_SECONDS = 600

# 冪等 key 格式：POS-IDK-<32 字元英數>
_IDK_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# 收據編號同日唯一檢查重試次數（uuid 碰撞極低，但保險起見）
_RECEIPT_NO_RETRIES = 5

# Rate limiter：同 IP 每分鐘最多 60 次 checkout（約 1 張/秒，足以應付連按）
_pos_checkout_limiter = SlidingWindowLimiter(
    max_calls=60,
    window_seconds=60,
    name="pos_checkout",
    error_detail="結帳操作過於頻繁，請稍後再試",
).as_dependency()


# ── Pydantic schemas ───────────────────────────────────────────────────────


class POSCheckoutItem(BaseModel):
    registration_id: int = Field(..., gt=0)
    amount: int = Field(
        ...,
        gt=0,
        le=_MAX_ITEM_AMOUNT,
        description="本次此筆收取金額（正整數，上限 NT$999,999）",
    )


class POSCheckoutRequest(BaseModel):
    items: List[POSCheckoutItem] = Field(..., min_length=1, max_length=10)
    payment_method: Literal["現金", "轉帳", "其他"] = "現金"
    payment_date: date
    tendered: Optional[int] = Field(
        None,
        ge=0,
        le=_MAX_TENDERED,
        description="客戶實付（僅現金有意義，上限 NT$9,999,999）",
    )
    notes: str = Field("", max_length=200)
    type: Literal["payment", "refund"] = "payment"
    idempotency_key: Optional[str] = Field(
        None,
        description="冪等 key，同 key 在 10 分鐘內重送視為重試，回傳先前結果",
    )

    @field_validator("payment_date")
    @classmethod
    def _validate_payment_date(cls, v: date) -> date:
        return validate_payment_date(v)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _IDK_PATTERN.match(v):
            raise ValueError("idempotency_key 格式不合（需 8-64 英數/底線/連字號）")
        return v

    def refund_notes_cleaned(self) -> str:
        """回傳 cleaned notes（僅 type=refund 時使用；handler 額外呼叫
        require_refund_reason 做最終閘門）。"""
        return (self.notes or "").strip()


# ── 常數 ─────────────────────────────────────────────────────────────────

_VALID_METHODS = {"現金", "轉帳", "其他"}


def _make_receipt_no() -> str:
    """產生收據編號 POS-YYYYMMDD-XXXXXXXXXXXX（12 字元 hex，碰撞機率 ~10^-14）"""
    today_str = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
    return f"POS-{today_str}-{uuid.uuid4().hex[:12].upper()}"


def _build_notes(receipt_no: str, user_note: str) -> str:
    """組合 notes：保留 [receipt_no] 標記供舊版解析相容，其餘為使用者備註。

    idempotency_key 已獨立成 ActivityPaymentRecord.idempotency_key 欄位，不再放 notes。
    """
    parts = [f"[{receipt_no}]"]
    note = (user_note or "").strip()
    if note:
        parts.append(note)
    return " ".join(parts)


def _strip_system_tags(raw_notes: str) -> str:
    """從 notes 字串剝除 [POS-YYYYMMDD-XXX] 與舊版 [IDK:xxx] 標記，取出純使用者備註。"""
    s = re.sub(r"\[POS-\d{8}-[A-Fa-f0-9]+\]", "", raw_notes or "")
    s = re.sub(r"\[IDK:[A-Za-z0-9_-]+\]", "", s)
    return s.strip()


def _parse_receipt_response_from_record(
    session, record: ActivityPaymentRecord
) -> Optional[dict]:
    """用已存在的 ActivityPaymentRecord 重建 checkout response（冪等重試用）。

    僅依賴 DB 資料，不讀 request body — 確保重試時 response 穩定，
    與第一次呼叫時的狀態一致。tendered/change 不儲存，故回 None。
    """
    # 優先用 receipt_no 欄位；空值時回退抽取 notes 標記（舊紀錄相容）
    receipt_no = record.receipt_no
    notes = record.notes or ""
    if not receipt_no:
        m = re.search(r"\[(POS-\d{8}-[A-Fa-f0-9]+)\]", notes)
        if not m:
            return None
        receipt_no = m.group(1)

    # 該收據對應的所有付款記錄（同 receipt_no 代表一張收據）
    # 用欄位 + index 查詢；不再依賴 notes LIKE 以免受使用者備註污染
    same_recs = (
        session.query(ActivityPaymentRecord)
        .filter(ActivityPaymentRecord.receipt_no == receipt_no)
        .order_by(ActivityPaymentRecord.id.asc())
        .all()
    )
    # Fallback：舊資料尚未 backfill receipt_no 時，回退到 notes 比對
    if not same_recs:
        same_recs = (
            session.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.notes.like(f"%[{receipt_no}]%"))
            .order_by(ActivityPaymentRecord.id.asc())
            .all()
        )
    if not same_recs:
        return None

    reg_ids = [r.registration_id for r in same_recs]
    reg_by_id = {
        r.id: r
        for r in session.query(ActivityRegistration)
        .filter(ActivityRegistration.id.in_(reg_ids))
        .all()
    }
    total_map = _batch_calc_total_amounts(session, reg_ids)
    course_map = _fetch_reg_course_details(session, reg_ids)
    supply_map = _fetch_reg_supplies(session, reg_ids)

    total_charged = sum(r.amount for r in same_recs)
    response_items = []
    for r in same_recs:
        reg = reg_by_id.get(r.registration_id)
        if reg is None:
            continue
        total_amount = total_map.get(reg.id, 0) or 0
        response_items.append(
            {
                "registration_id": reg.id,
                "student_name": reg.student_name,
                "class_name": reg.class_name or "",
                "amount_applied": r.amount,
                "new_paid_amount": reg.paid_amount or 0,
                "total_amount": total_amount,
                "new_payment_status": _derive_payment_status(
                    reg.paid_amount or 0, total_amount
                ),
                "courses": course_map.get(reg.id, []),
                "supplies": supply_map.get(reg.id, []),
            }
        )

    first = same_recs[0]
    user_note = _strip_system_tags(notes)

    return {
        "receipt_no": receipt_no,
        "type": first.type,
        "total": total_charged,
        "tendered": None,
        "change": None,
        "payment_method": first.payment_method or "",
        "payment_date": first.payment_date.isoformat() if first.payment_date else None,
        "operator": first.operator or "",
        "notes": user_note,
        "created_at": (
            first.created_at.isoformat(timespec="seconds") if first.created_at else None
        ),
        "items": response_items,
        "idempotent_replay": True,
    }


def _find_idempotent_hit(
    session, idempotency_key: str
) -> Optional[ActivityPaymentRecord]:
    """查詢相同 idempotency_key 的紀錄。

    Why: DB 層 UniqueConstraint 已將 idempotency_key 設為永久全域唯一。
    過去這個 helper 額外用 10 分鐘視窗過濾，導致兩個衝突：
    (1) window 外同 key 重送 → helper 找不到 → 繼續 INSERT → UNIQUE 拋
        IntegrityError → catch 再查一次仍找不到 → 客戶端 500
    (2) window 內同 key 重送但應視為重試也 OK，但 window 邏輯本身是冗餘
    改為全域查詢：DB 語意（永久唯一）與 replay 語意一致，同 key 永遠回同結果。
    """
    return (
        session.query(ActivityPaymentRecord)
        .filter(ActivityPaymentRecord.idempotency_key == idempotency_key)
        .order_by(ActivityPaymentRecord.id.asc())
        .first()
    )


# ── 端點 1：依學生聚合未結清報名 ─────────────────────────────────────────


OVERDUE_DAYS_THRESHOLD = 14


@router.get("/pos/outstanding-by-student")
async def outstanding_by_student(
    q: Optional[str] = Query(
        None,
        max_length=50,
        description="關鍵字模糊搜尋（姓名 / 班級 / 家長手機）；留空則列出全部",
    ),
    limit: int = Query(100, ge=1, le=500),
    filter: Literal["outstanding", "refundable"] = Query(
        "outstanding",
        description="outstanding=未結清(paid<total)，refundable=已繳>0(供退費使用)",
    ),
    classroom: Optional[str] = Query(
        None, max_length=50, description="精確班級名稱過濾（下拉選單用）"
    ),
    overdue_only: bool = Query(
        False,
        description=f"只列報名超過 {OVERDUE_DAYS_THRESHOLD} 天仍未結清的『逾期』項目",
    ),
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """依 (student_name, birthday) 聚合報名紀錄（限學期）。

    filter=outstanding：未結清（繳費模式）
    filter=refundable ：已繳 > 0（退費模式）
    搜尋 q 為空時回傳該學期全部符合其他條件的項目（預設瀏覽清單）。
    overdue_only=True 僅在 outstanding 模式下生效。
    """
    from datetime import datetime, timedelta

    from utils.academic import resolve_academic_term_filters

    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(school_year, semester)
        query = session.query(ActivityRegistration).filter(
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        keyword = (q or "").strip()
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(
                or_(
                    ActivityRegistration.student_name.ilike(like),
                    ActivityRegistration.class_name.ilike(like),
                    ActivityRegistration.parent_phone.ilike(like),
                )
            )
        if classroom:
            query = query.filter(ActivityRegistration.class_name == classroom)
        if overdue_only and filter == "outstanding":
            cutoff = datetime.now() - timedelta(days=OVERDUE_DAYS_THRESHOLD)
            query = query.filter(ActivityRegistration.created_at < cutoff)
        regs = (
            query.order_by(
                ActivityRegistration.student_name.asc(),
                ActivityRegistration.birthday.asc(),
                ActivityRegistration.created_at.asc(),
            )
            .limit(2000)
            .all()
        )
        if not regs:
            return {"groups": []}

        reg_ids = [r.id for r in regs]
        total_map = _batch_calc_total_amounts(session, reg_ids)
        course_map = _fetch_reg_course_details(session, reg_ids)
        supply_map = _fetch_reg_supplies(session, reg_ids)

        # 依 filter 過濾
        outstanding_regs = []
        for reg in regs:
            total = total_map.get(reg.id, 0) or 0
            paid = reg.paid_amount or 0
            if filter == "refundable":
                if paid > 0:
                    outstanding_regs.append(reg)
            else:  # outstanding
                if total > 0 and paid < total:
                    outstanding_regs.append(reg)

        # 依 (student_name, birthday) 分組
        groups: dict = defaultdict(list)
        for reg in outstanding_regs:
            key = (reg.student_name, reg.birthday or "")
            groups[key].append(reg)

        result_groups = []
        for (student_name, birthday), group_regs in groups.items():
            registrations_payload = []
            group_total = 0
            for reg in group_regs:
                total = total_map.get(reg.id, 0) or 0
                paid = reg.paid_amount or 0
                owed = max(0, total - paid)
                # 繳費模式：group 合計 = 欠費；退費模式：group 合計 = 已繳
                group_total += paid if filter == "refundable" else owed
                registrations_payload.append(
                    {
                        "id": reg.id,
                        "total_amount": total,
                        "paid_amount": paid,
                        "owed": owed,
                        "class_name": reg.class_name or "",
                        "courses": course_map.get(reg.id, []),
                        "supplies": supply_map.get(reg.id, []),
                        "created_at": (
                            reg.created_at.isoformat() if reg.created_at else None
                        ),
                    }
                )
            class_name = group_regs[-1].class_name or ""
            result_groups.append(
                {
                    "student_key": f"{student_name}|{birthday}",
                    "student_name": student_name,
                    "birthday": birthday or "",
                    "class_name": class_name,
                    "group_owed_total": group_total,
                    "registrations": registrations_payload,
                }
            )

        result_groups.sort(key=lambda g: (-g["group_owed_total"], g["student_name"]))
        return {"groups": result_groups[:limit]}
    finally:
        session.close()


# ── 端點 2：POS 一次性結帳（原子 transaction + 行級鎖 + 冪等） ──────────


def _lock_regs(session, reg_ids: list):
    """對 registration 取得行級鎖。SQLite 不支援 FOR UPDATE，在測試時自動降級。"""
    query = session.query(ActivityRegistration).filter(
        ActivityRegistration.id.in_(reg_ids),
        ActivityRegistration.is_active.is_(True),
    )
    try:
        # PostgreSQL / MySQL：row-level lock
        return query.with_for_update().all()
    except (CompileError, OperationalError, NotImplementedError):
        # SQLite（單元測試）降級為無鎖
        return query.all()


@router.post("/pos/checkout", status_code=201)
async def pos_checkout(
    body: POSCheckoutRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
    _rl: None = Depends(_pos_checkout_limiter),
):
    """POS 一次性結帳：在單一 transaction 中為多筆報名建立繳費/退費記錄。

    任何一筆驗證或寫入失敗，整批 rollback，保證帳務一致性。
    支援 idempotency_key 冪等重試。
    """
    if body.payment_method not in _VALID_METHODS:
        raise HTTPException(
            status_code=400, detail=f"不支援的付款方式：{body.payment_method}"
        )

    operator = (current_user.get("username") or "").strip()
    if not operator:
        # 依賴層原本就該保證有 username；到這裡代表權限設定異常，拒絕寫入避免匿名交易
        raise HTTPException(status_code=401, detail="無法識別操作人員")

    session = get_session()
    try:
        # ── 冪等性檢查 ──────────────────────────────────────────
        if body.idempotency_key:
            existing = _find_idempotent_hit(session, body.idempotency_key)
            if existing is not None:
                replay = _parse_receipt_response_from_record(session, existing)
                if replay is not None:
                    logger.info(
                        "POS checkout idempotent replay: key=%s operator=%s",
                        body.idempotency_key,
                        operator,
                    )
                    return replay

        # ── 已簽核日守衛：拒絕 payment_date 落在已 daily-close 的日期 ──
        # Why: snapshot 已凍結，補寫會讓 reconciliation 與實際 DB 永久失準。
        _require_daily_close_unlocked(session, body.payment_date)

        # ── 退費專屬守衛：notes 必填原因 + 大額閾值審批 ────────────
        if body.type == "refund":
            cleaned_reason = require_refund_reason(body.notes)
            body.notes = cleaned_reason
            # 第一道：本次收據合計（不同 reg 的退費合在一張收據）超門檻即需簽核
            total_refund_amount = sum(it.amount for it in body.items)
            require_approve_for_large_refund(total_refund_amount, current_user)

        reg_ids = [item.registration_id for item in body.items]
        if len(set(reg_ids)) != len(reg_ids):
            raise HTTPException(status_code=400, detail="結帳項目含重複的報名 ID")

        # ── 行級鎖住所有要修改的 registrations ──────────────────
        regs = _lock_regs(session, reg_ids)
        reg_by_id = {r.id: r for r in regs}
        missing = [rid for rid in reg_ids if rid not in reg_by_id]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"找不到或已停用的報名 ID：{missing}",
            )

        # ── 第二道：每 reg 累積退費簽核（須在 _lock_regs 之後）─────────
        # 鎖之後才查 prior_refunded，避免兩個併發小額退費各自看到相同舊累積值；
        # 並且封死「同 registration 連開多張小收據繞過第一道收據合計門檻」的拆單路徑。
        # Why: 第一道（收據合計）只擋同收據內金額大；同 reg 用兩張小收據之間沒有檢查。
        if body.type == "refund":
            prior_rows = (
                session.query(
                    ActivityPaymentRecord.registration_id,
                    func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
                )
                .filter(
                    ActivityPaymentRecord.registration_id.in_(reg_ids),
                    ActivityPaymentRecord.type == "refund",
                    ActivityPaymentRecord.voided_at.is_(None),
                )
                .group_by(ActivityPaymentRecord.registration_id)
                .all()
            )
            prior_refund_map = {rid: int(amt or 0) for rid, amt in prior_rows}
            for item in body.items:
                cumulative = prior_refund_map.get(item.registration_id, 0) + int(
                    item.amount
                )
                require_approve_for_large_refund(
                    cumulative,
                    current_user,
                    label=f"報名 {item.registration_id} 累積退費總額",
                )

        total_map = _batch_calc_total_amounts(session, reg_ids)
        course_map = _fetch_reg_course_details(session, reg_ids)
        supply_map = _fetch_reg_supplies(session, reg_ids)

        # 產生不碰撞的 receipt_no（極低機率下重試）
        # 碰撞檢測改用 receipt_no 欄位（有 index、不受 notes 污染）
        receipt_no = _make_receipt_no()
        for _ in range(_RECEIPT_NO_RETRIES):
            exists = (
                session.query(ActivityPaymentRecord.id)
                .filter(ActivityPaymentRecord.receipt_no == receipt_no)
                .first()
            )
            if exists is None:
                break
            receipt_no = _make_receipt_no()
        stored_notes = _build_notes(receipt_no, body.notes)

        total_charged = 0
        response_items = []
        type_label = "繳費" if body.type == "payment" else "退費"

        for idx, item in enumerate(body.items):
            reg = reg_by_id[item.registration_id]
            total_amount_pre = total_map.get(reg.id, 0) or 0
            if body.type == "refund" and item.amount > (reg.paid_amount or 0):
                raise HTTPException(
                    status_code=400,
                    detail=f"報名 {reg.id}（{reg.student_name}）的退費金額超過已繳金額",
                )
            if body.type == "payment":
                # 空報名（無 enrolled 課程/用品）不得收款，避免產生孤兒收款
                if total_amount_pre <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"報名 {reg.id}（{reg.student_name}）無應繳金額，無法收款",
                    )
                # 超收守衛：已繳 + 本次金額不得超過應繳
                if (reg.paid_amount or 0) + item.amount > total_amount_pre:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"報名 {reg.id}（{reg.student_name}）本次收款 NT${item.amount} "
                            f"將導致超收（應繳 NT${total_amount_pre}，已繳 NT${reg.paid_amount or 0}）"
                        ),
                    )

            # idempotency_key 只落在整張收據的「第一筆」記錄上，其餘為 NULL。
            # Why: ActivityPaymentRecord.idempotency_key 有全域 UNIQUE 約束；
            # 若一張收據 N 筆 item 都寫同一個 key，第二筆 flush 就會 IntegrityError
            # 造成整張交易 rollback。Replay 時先用 key 找到第一筆，再透過
            # receipt_no 拉整張收據（_parse_receipt_response_from_record）。
            # NULL 在標準 SQL 允許重複，不受 UNIQUE 約束影響。
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type=body.type,
                amount=item.amount,
                payment_date=body.payment_date,
                payment_method=body.payment_method,
                notes=stored_notes,
                operator=operator,
                idempotency_key=body.idempotency_key if idx == 0 else None,
                receipt_no=receipt_no,
            )
            session.add(rec)

            if body.type == "payment":
                reg.paid_amount = (reg.paid_amount or 0) + item.amount
            else:
                reg.paid_amount = max(0, (reg.paid_amount or 0) - item.amount)

            total_amount = total_map.get(reg.id, 0) or 0
            # 應繳為 0 的報名（全免課程）視為未結清，維持 is_paid=False；
            # 避免被後台「已繳」查詢誤算入營收或人數。
            reg.is_paid = _compute_is_paid(reg.paid_amount or 0, total_amount)
            total_charged += item.amount

            activity_service.log_change(
                session,
                reg.id,
                reg.student_name,
                f"POS{type_label}",
                f"{receipt_no} NT${item.amount}，方式：{body.payment_method}",
                operator,
            )

            response_items.append(
                {
                    "registration_id": reg.id,
                    "student_name": reg.student_name,
                    "class_name": reg.class_name or "",
                    "amount_applied": item.amount,
                    "new_paid_amount": reg.paid_amount,
                    "total_amount": total_amount,
                    "new_payment_status": _derive_payment_status(
                        reg.paid_amount, total_amount
                    ),
                    "courses": course_map.get(reg.id, []),
                    "supplies": supply_map.get(reg.id, []),
                }
            )

        # 總額上限保護（後端獨立於前端大額警告）
        if total_charged > _MAX_CHECKOUT_TOTAL:
            raise HTTPException(
                status_code=400,
                detail=f"單次交易總額超過上限 NT${_MAX_CHECKOUT_TOTAL:,}",
            )

        # 現金且提供 tendered：驗證實收 >= 應收（前端已不再送 tendered，保留供相容）
        change = None
        if (
            body.type == "payment"
            and body.payment_method == "現金"
            and body.tendered is not None
        ):
            if body.tendered < total_charged:
                raise HTTPException(status_code=400, detail="實收金額少於應收金額")
            change = body.tendered - total_charged

        try:
            session.commit()
        except IntegrityError as e:
            # DB 層 UNIQUE 攔下並發同 idempotency_key 的第二筆：把它轉成 idempotent replay
            session.rollback()
            if body.idempotency_key and "idempotency_key" in str(e.orig).lower():
                existing = _find_idempotent_hit(session, body.idempotency_key)
                if existing is not None:
                    replay = _parse_receipt_response_from_record(session, existing)
                    if replay is not None:
                        logger.info(
                            "POS checkout idempotent replay via UNIQUE: key=%s operator=%s",
                            body.idempotency_key,
                            operator,
                        )
                        return replay
            raise
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        # 金流變動影響 /finance-summary 報表快取（TTL 30 分），同步失效
        try:
            report_cache_service.invalidate_category(None, "reports_finance_summary")
        except Exception:
            logger.warning("invalidate finance_summary cache failed", exc_info=True)

        logger.warning(
            "POS checkout: receipt=%s operator=%s total=%d items=%d method=%s type=%s idk=%s",
            receipt_no,
            operator,
            total_charged,
            len(body.items),
            body.payment_method,
            body.type,
            body.idempotency_key or "-",
        )

        request.state.audit_entity_id = receipt_no
        request.state.audit_summary = (
            f"POS {type_label}：{receipt_no} NT${total_charged}"
            f"（{body.payment_method}，{len(body.items)} 筆）"
        )
        request.state.audit_changes = {
            "receipt_no": receipt_no,
            "type": body.type,
            "total": total_charged,
            "item_count": len(body.items),
            "payment_method": body.payment_method,
            "payment_date": body.payment_date.isoformat(),
            "registration_ids": reg_ids,
        }

        return {
            "receipt_no": receipt_no,
            "type": body.type,
            "total": total_charged,
            "tendered": body.tendered if body.payment_method == "現金" else None,
            "change": change,
            "payment_method": body.payment_method,
            "payment_date": body.payment_date.isoformat(),
            "operator": operator,
            "notes": (body.notes or "").strip(),
            "created_at": datetime.now(TAIPEI_TZ)
            .replace(tzinfo=None)
            .isoformat(timespec="seconds"),
            "items": response_items,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="POS checkout")
    finally:
        session.close()


# ── 端點 3：今日日結摘要 ─────────────────────────────────────────────────


@router.get("/pos/daily-summary")
async def pos_daily_summary(
    date_: Optional[str] = Query(
        None, alias="date", description="YYYY-MM-DD，預設今日"
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """回傳指定日期的繳費/退費摘要，以 payment_date 聚合。"""
    if date_:
        try:
            target_date = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=400, detail="date 格式必須為 YYYY-MM-DD")
    else:
        target_date = datetime.now(TAIPEI_TZ).date()

    session = get_session()
    try:
        snap = compute_daily_snapshot(session, target_date)
        # 保持既有 response 結構（不含 transaction_count / by_method_net）
        return {
            "date": snap["date"],
            "payment_total": snap["payment_total"],
            "refund_total": snap["refund_total"],
            "net": snap["net"],
            "payment_count": snap["payment_count"],
            "refund_count": snap["refund_count"],
            "by_method": snap["by_method"],
        }
    finally:
        session.close()


# ── 端點 4：今日交易明細（可重印） ──────────────────────────────────────

_RECEIPT_NO_RE = re.compile(r"\[(POS-\d{8}-[A-F0-9]+)\]")


@router.get("/pos/recent-transactions")
async def pos_recent_transactions(
    date_: Optional[str] = Query(
        None, alias="date", description="YYYY-MM-DD，預設今日"
    ),
    limit: int = Query(20, ge=1, le=100),
    include_system: bool = Query(
        False,
        description="是否一併列出系統沖帳（notes 無 [POS-...] 標記的 payment_records，例如學生離園自動沖帳）",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """回傳指定日期內的交易清單（供前端列表 + 重印）。

    預設只列 POS 交易（依 receipt_no 聚合）。簽核頁傳 include_system=true
    時，額外列出系統沖帳（每筆獨立，receipt_no 以 `SYS-<record_id>` 合成）。
    """
    if date_:
        try:
            target_date = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=400, detail="date 格式必須為 YYYY-MM-DD")
    else:
        target_date = datetime.now(TAIPEI_TZ).date()

    session = get_session()
    try:
        # 軟刪（voided）紀錄不出現在交易列表：避免誤以為某張收據還在、且 tx_total 會錯算
        query = session.query(ActivityPaymentRecord).filter(
            ActivityPaymentRecord.payment_date == target_date,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        if not include_system:
            # 優先用 receipt_no 欄位過濾 POS 交易；舊資料尚未 backfill 時回退到 notes 標記
            query = query.filter(
                (ActivityPaymentRecord.receipt_no.isnot(None))
                | (ActivityPaymentRecord.notes.like("%[POS-%"))
            )
        records = query.order_by(ActivityPaymentRecord.id.desc()).all()

        # 依 receipt_no 聚合（POS 交易）；無 POS 標記者每筆獨立成 SYS-<id>
        by_receipt: dict = {}
        receipt_source: dict = {}  # receipt_key -> 'pos' | 'system'
        order: list = []
        for r in records:
            rno = r.receipt_no
            if not rno:
                m = _RECEIPT_NO_RE.search(r.notes or "")
                rno = m.group(1) if m else None
            if rno:
                source = "pos"
            else:
                rno = f"SYS-{r.id}"
                source = "system"
            if rno not in by_receipt:
                by_receipt[rno] = []
                receipt_source[rno] = source
                order.append(rno)
            by_receipt[rno].append(r)

        # 限制最多 limit 張
        order = order[:limit]
        if not order:
            return {"date": target_date.isoformat(), "transactions": []}

        reg_ids = list({r.registration_id for rno in order for r in by_receipt[rno]})
        reg_by_id = {
            reg.id: reg
            for reg in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(reg_ids))
            .all()
        }
        total_map = _batch_calc_total_amounts(session, reg_ids)
        course_map = _fetch_reg_course_details(session, reg_ids)
        supply_map = _fetch_reg_supplies(session, reg_ids)

        transactions = []
        for rno in order:
            recs = by_receipt[rno]
            first = recs[0]
            tx_type = first.type
            tx_total = sum(rec.amount for rec in recs)

            # 解析使用者備註（notes 剝除 [POS-...] 與舊版 [IDK:...] 標記）
            user_note = _strip_system_tags(first.notes or "")

            items_payload = []
            student_names = []
            for rec in recs:
                reg = reg_by_id.get(rec.registration_id)
                if reg is None:
                    continue
                student_names.append(reg.student_name)
                items_payload.append(
                    {
                        "registration_id": reg.id,
                        "student_name": reg.student_name,
                        "class_name": reg.class_name or "",
                        "amount_applied": rec.amount,
                        "new_paid_amount": reg.paid_amount or 0,
                        "total_amount": total_map.get(reg.id, 0) or 0,
                        "new_payment_status": _derive_payment_status(
                            reg.paid_amount or 0, total_map.get(reg.id, 0) or 0
                        ),
                        "courses": course_map.get(reg.id, []),
                        "supplies": supply_map.get(reg.id, []),
                    }
                )

            transactions.append(
                {
                    "receipt_no": rno,
                    "source": receipt_source[rno],
                    "type": tx_type,
                    "total": tx_total,
                    "tendered": None,  # 歷史無紀錄
                    "change": None,
                    "payment_method": first.payment_method or "",
                    "payment_date": (
                        first.payment_date.isoformat() if first.payment_date else None
                    ),
                    "operator": first.operator or "",
                    "notes": user_note,
                    "created_at": (
                        first.created_at.isoformat(timespec="seconds")
                        if first.created_at
                        else None
                    ),
                    "student_names": student_names,
                    "items": items_payload,
                }
            )

        return {
            "date": target_date.isoformat(),
            "transactions": transactions,
        }
    finally:
        session.close()


# ── 端點 5：學期對帳總表 ──────────────────────────────────────────────────

_APPROVAL_STATUS_VALUES = {
    "fully_approved",
    "partially_approved",
    "pending_approval",
    "no_payment",
}


@router.get("/pos/semester-reconciliation")
async def pos_semester_reconciliation(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    classroom_name: Optional[str] = Query(None, max_length=50),
    payment_status: Optional[Literal["paid", "partial", "unpaid", "overpaid"]] = Query(
        None
    ),
    approval_status: Optional[str] = Query(
        None,
        description="fully_approved / partially_approved / pending_approval / no_payment",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """以學期為單位列出所有報名 + 繳費狀態 + 簽核狀態，供老闆定期對帳稽核。

    簽核狀態四態：
    - fully_approved: 全部繳費記錄都落在已簽核日
    - partially_approved: 部分繳費記錄落在已簽核日
    - pending_approval: 有繳費但都在未簽核日
    - no_payment: 完全尚未繳費（paid_amount == 0）
    """
    from utils.academic import resolve_academic_term_filters

    if approval_status is not None and approval_status not in _APPROVAL_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"approval_status 必須為 {sorted(_APPROVAL_STATUS_VALUES)} 之一",
        )

    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(school_year, semester)
        q = _build_registration_filter_query(
            session,
            school_year=sy,
            semester=sem,
            classroom_name=classroom_name,
            payment_status=payment_status,
        )
        active_regs = (
            q.order_by(ActivityRegistration.created_at.desc()).limit(2000).all()
        )

        # 額外納入 inactive 但本學期仍有 payment_records 的 reg，
        # 否則這些流水（多為軟刪除後的系統沖帳）會從學期對帳總表消失，
        # 跟日結 snapshot 對不起來。
        active_ids = {r.id for r in active_regs}
        inactive_with_records = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
                ActivityRegistration.is_active.is_(False),
                ActivityRegistration.id.in_(
                    session.query(ActivityPaymentRecord.registration_id).distinct()
                ),
                ~ActivityRegistration.id.in_(active_ids) if active_ids else True,
            )
            .all()
        )

        regs = list(active_regs) + list(inactive_with_records)
        reg_ids = [r.id for r in regs]
        total_map = _batch_calc_total_amounts(session, reg_ids)
        course_name_map = _fetch_reg_course_names(session, reg_ids)

        # 一次取回所有相關繳費紀錄，避免 N+1
        payment_records_by_reg: dict = defaultdict(list)
        if reg_ids:
            for rec in (
                session.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id.in_(reg_ids))
                .all()
            ):
                payment_records_by_reg[rec.registration_id].append(rec)

        # 已簽核日集合（單次 query）
        closed_dates = {
            row[0] for row in session.query(ActivityPosDailyClose.close_date).all()
        }

        # 彙總器
        agg_total_amount = 0
        agg_paid_amount = 0
        agg_approved_paid = 0
        agg_pending_paid = 0
        agg_offline_paid = 0
        by_payment_status: dict = defaultdict(int)
        by_approval_status: dict = defaultdict(int)

        items = []
        for reg in regs:
            total = total_map.get(reg.id, 0) or 0
            paid = reg.paid_amount or 0
            owed = max(0, total - paid)
            ps = _derive_payment_status(paid, total)

            recs = payment_records_by_reg.get(reg.id, [])
            approved_paid = 0
            pending_paid = 0
            approved_refund = 0
            pending_refund = 0
            latest_date: Optional[date] = None
            any_approved = False
            any_pending = False
            for rec in recs:
                pd = rec.payment_date
                if pd is None:
                    continue
                is_closed = pd in closed_dates
                if rec.type == "refund":
                    if is_closed:
                        approved_refund += rec.amount
                    else:
                        pending_refund += rec.amount
                else:
                    if is_closed:
                        approved_paid += rec.amount
                    else:
                        pending_paid += rec.amount
                if is_closed:
                    any_approved = True
                else:
                    any_pending = True
                if latest_date is None or pd > latest_date:
                    latest_date = pd

            approved_net = max(0, approved_paid - approved_refund)
            pending_net = max(0, pending_paid - pending_refund)
            # 非 POS 已繳：reg.paid_amount 未對應到任何 payment_record 的差額
            # 常見於歷史匯入或直接寫入 paid_amount 的資料，系統無從判斷簽核狀態
            offline_paid = max(0, paid - approved_net - pending_net)

            if paid <= 0:
                approval = "no_payment"
            elif not recs:
                # 有 paid_amount 但沒任何 payment_record → 全數為非 POS 已繳
                approval = "pending_approval"
            elif any_approved and any_pending:
                approval = "partially_approved"
            elif any_approved:
                approval = "fully_approved"
            else:
                approval = "pending_approval"

            if approval_status and approval != approval_status:
                continue

            agg_total_amount += total
            agg_paid_amount += paid
            agg_approved_paid += approved_net
            agg_pending_paid += pending_net
            agg_offline_paid += offline_paid
            by_payment_status[ps] += 1
            by_approval_status[approval] += 1

            items.append(
                {
                    "id": reg.id,
                    "student_name": reg.student_name,
                    "class_name": reg.class_name or "",
                    "is_active": bool(reg.is_active),
                    "course_names": course_name_map.get(reg.id, []),
                    "total_amount": total,
                    "paid_amount": paid,
                    "owed": owed,
                    "payment_status": ps,
                    "approval_status": approval,
                    "approved_paid_amount": approved_net,
                    "pending_paid_amount": pending_net,
                    "offline_paid_amount": offline_paid,
                    "latest_payment_date": (
                        latest_date.isoformat() if latest_date else None
                    ),
                    "created_at": (
                        reg.created_at.isoformat() if reg.created_at else None
                    ),
                }
            )

        return {
            "school_year": sy,
            "semester": sem,
            "items": items,
            "totals": {
                "registration_count": len(items),
                "total_amount": agg_total_amount,
                "paid_amount": agg_paid_amount,
                "outstanding_amount": max(0, agg_total_amount - agg_paid_amount),
                "approved_paid_amount": agg_approved_paid,
                "pending_paid_amount": agg_pending_paid,
                "offline_paid_amount": agg_offline_paid,
                "by_payment_status": dict(by_payment_status),
                "by_approval_status": dict(by_approval_status),
            },
        }
    finally:
        session.close()
