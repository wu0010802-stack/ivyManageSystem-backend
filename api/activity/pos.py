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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import CompileError, OperationalError

from models.database import (
    ActivityPaymentRecord,
    ActivityRegistration,
    get_session,
)
from services.activity_service import activity_service
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter

from ._shared import (
    TAIPEI_TZ,
    _batch_calc_total_amounts,
    _derive_payment_status,
    _fetch_reg_course_details,
    _fetch_reg_supplies,
    _invalidate_activity_dashboard_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# 單個品項與實收金額上限（NT$999,999 / NT$9,999,999）— 避免誤輸入或整型誇張值
_MAX_ITEM_AMOUNT = 999_999
_MAX_TENDERED = 9_999_999

# payment_date 合理範圍：最多回補 30 天、不得指定未來
_PAYMENT_DATE_BACK_LIMIT_DAYS = 30

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
    items: List[POSCheckoutItem] = Field(..., min_length=1, max_length=50)
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
        today = datetime.now(TAIPEI_TZ).date()
        if v > today:
            raise ValueError("繳費日期不可指定未來日期")
        earliest = today - timedelta(days=_PAYMENT_DATE_BACK_LIMIT_DAYS)
        if v < earliest:
            raise ValueError(
                f"繳費日期超出範圍，最多回補 {_PAYMENT_DATE_BACK_LIMIT_DAYS} 天"
            )
        return v

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _IDK_PATTERN.match(v):
            raise ValueError("idempotency_key 格式不合（需 8-64 英數/底線/連字號）")
        return v


# ── 常數 ─────────────────────────────────────────────────────────────────

_VALID_METHODS = {"現金", "轉帳", "其他"}


def _make_receipt_no() -> str:
    """產生收據編號 POS-YYYYMMDD-XXXXXXXXXXXX（12 字元 hex，碰撞機率 ~10^-14）"""
    today_str = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
    return f"POS-{today_str}-{uuid.uuid4().hex[:12].upper()}"


def _build_notes(
    receipt_no: str, user_note: str, idempotency_key: Optional[str]
) -> str:
    """組合 notes：含 receipt_no、idempotency_key（若有）、使用者備註。"""
    parts = [f"[{receipt_no}]"]
    if idempotency_key:
        parts.append(f"[IDK:{idempotency_key}]")
    note = (user_note or "").strip()
    if note:
        parts.append(note)
    return " ".join(parts)


def _parse_receipt_response_from_record(
    session, record: ActivityPaymentRecord, req: POSCheckoutRequest
) -> Optional[dict]:
    """用已存在的 ActivityPaymentRecord 重建 checkout response（冪等重試用）。

    從 notes 解析 receipt_no，查出同收據的所有記錄聚合金額。
    """
    notes = record.notes or ""
    m = re.search(r"\[(POS-\d{8}-[A-F0-9]+)\]", notes)
    if not m:
        return None
    receipt_no = m.group(1)

    # 該收據對應的所有付款記錄（同 receipt_no 代表一張收據）
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
    user_note_match = re.search(r"\]\s*(?!\[)(.+)$", notes)
    user_note = user_note_match.group(1).strip() if user_note_match else ""

    return {
        "receipt_no": receipt_no,
        "type": first.type,
        "total": total_charged,
        "tendered": req.tendered if first.payment_method == "現金" else None,
        "change": (
            max(0, (req.tendered or 0) - total_charged)
            if req.tendered is not None and first.payment_method == "現金"
            else None
        ),
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
    """查詢視窗內是否已有相同 idempotency_key 的記錄。"""
    threshold = datetime.now(TAIPEI_TZ).replace(tzinfo=None) - timedelta(
        seconds=_IDEMPOTENCY_WINDOW_SECONDS
    )
    return (
        session.query(ActivityPaymentRecord)
        .filter(
            ActivityPaymentRecord.notes.like(f"%[IDK:{idempotency_key}]%"),
            ActivityPaymentRecord.created_at >= threshold,
        )
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

    operator = current_user.get("username", "") or ""

    session = get_session()
    try:
        # ── 冪等性檢查 ──────────────────────────────────────────
        if body.idempotency_key:
            existing = _find_idempotent_hit(session, body.idempotency_key)
            if existing is not None:
                replay = _parse_receipt_response_from_record(session, existing, body)
                if replay is not None:
                    logger.info(
                        "POS checkout idempotent replay: key=%s operator=%s",
                        body.idempotency_key,
                        operator,
                    )
                    return replay

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

        total_map = _batch_calc_total_amounts(session, reg_ids)
        course_map = _fetch_reg_course_details(session, reg_ids)
        supply_map = _fetch_reg_supplies(session, reg_ids)

        # 產生不碰撞的 receipt_no（極低機率下重試）
        receipt_no = _make_receipt_no()
        for _ in range(_RECEIPT_NO_RETRIES):
            exists = (
                session.query(ActivityPaymentRecord.id)
                .filter(ActivityPaymentRecord.notes.like(f"%[{receipt_no}]%"))
                .first()
            )
            if exists is None:
                break
            receipt_no = _make_receipt_no()
        stored_notes = _build_notes(receipt_no, body.notes, body.idempotency_key)

        total_charged = 0
        response_items = []
        type_label = "繳費" if body.type == "payment" else "退費"

        for item in body.items:
            reg = reg_by_id[item.registration_id]
            if body.type == "refund" and item.amount > (reg.paid_amount or 0):
                raise HTTPException(
                    status_code=400,
                    detail=f"報名 {reg.id}（{reg.student_name}）的退費金額超過已繳金額",
                )

            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type=body.type,
                amount=item.amount,
                payment_date=body.payment_date,
                payment_method=body.payment_method,
                notes=stored_notes,
                operator=operator,
            )
            session.add(rec)

            if body.type == "payment":
                reg.paid_amount = (reg.paid_amount or 0) + item.amount
            else:
                reg.paid_amount = max(0, (reg.paid_amount or 0) - item.amount)

            total_amount = total_map.get(reg.id, 0) or 0
            reg.is_paid = reg.paid_amount >= total_amount > 0
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

        # 現金且提供 tendered：驗證實收 >= 應收
        change = None
        if (
            body.type == "payment"
            and body.payment_method == "現金"
            and body.tendered is not None
        ):
            if body.tendered < total_charged:
                raise HTTPException(status_code=400, detail="實收金額少於應收金額")
            change = body.tendered - total_charged

        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)

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
        logger.exception("POS checkout 發生非預期錯誤")
        raise HTTPException(status_code=500, detail=f"結帳失敗：{e}")
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
        rows = (
            session.query(
                ActivityPaymentRecord.type,
                ActivityPaymentRecord.payment_method,
                func.count(ActivityPaymentRecord.id),
                func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
            )
            .filter(ActivityPaymentRecord.payment_date == target_date)
            .group_by(
                ActivityPaymentRecord.type,
                ActivityPaymentRecord.payment_method,
            )
            .all()
        )

        payment_total = 0
        refund_total = 0
        payment_count = 0
        refund_count = 0
        by_method_map: dict = defaultdict(
            lambda: {"payment": 0, "refund": 0, "count": 0}
        )

        for rec_type, method, cnt, amt in rows:
            amt_int = int(amt or 0)
            cnt_int = int(cnt or 0)
            method_key = method or "未指定"
            if rec_type == "payment":
                payment_total += amt_int
                payment_count += cnt_int
                by_method_map[method_key]["payment"] += amt_int
            else:
                refund_total += amt_int
                refund_count += cnt_int
                by_method_map[method_key]["refund"] += amt_int
            by_method_map[method_key]["count"] += cnt_int

        by_method = [
            {
                "method": method_key,
                "payment": data["payment"],
                "refund": data["refund"],
                "count": data["count"],
            }
            for method_key, data in sorted(by_method_map.items())
        ]

        return {
            "date": target_date.isoformat(),
            "payment_total": payment_total,
            "refund_total": refund_total,
            "net": payment_total - refund_total,
            "payment_count": payment_count,
            "refund_count": refund_count,
            "by_method": by_method,
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
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """回傳指定日期內以 receipt_no 聚合的交易清單（供前端列表+重印）。"""
    if date_:
        try:
            target_date = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=400, detail="date 格式必須為 YYYY-MM-DD")
    else:
        target_date = datetime.now(TAIPEI_TZ).date()

    session = get_session()
    try:
        records = (
            session.query(ActivityPaymentRecord)
            .filter(
                ActivityPaymentRecord.payment_date == target_date,
                ActivityPaymentRecord.notes.like("%[POS-%"),
            )
            .order_by(ActivityPaymentRecord.id.desc())
            .all()
        )

        # 依 receipt_no 聚合
        by_receipt: dict = {}
        order: list = []
        for r in records:
            m = _RECEIPT_NO_RE.search(r.notes or "")
            if not m:
                continue
            rno = m.group(1)
            if rno not in by_receipt:
                by_receipt[rno] = []
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

            # 解析使用者備註（notes 剝除 [POS-...] 與 [IDK:...]）
            raw_notes = first.notes or ""
            user_note = re.sub(r"\[POS-\d{8}-[A-F0-9]+\]", "", raw_notes)
            user_note = re.sub(r"\[IDK:[A-Za-z0-9_-]+\]", "", user_note).strip()

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
