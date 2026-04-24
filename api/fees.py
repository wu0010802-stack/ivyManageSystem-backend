"""
api/fees.py — 學費/費用管理 API endpoints
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import outerjoin, func, case
from sqlalchemy.exc import IntegrityError

from models.base import session_scope
from models.classroom import Classroom, Student
from models.fees import (
    FeeItem,
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from utils.auth import require_staff_permission
from utils.finance_guards import require_adjustment_reason, require_finance_approve
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fees", tags=["fees"])


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

# 單筆費用金額上限（避免誤輸入或惡意輸入）
MAX_FEE_AMOUNT = 999_999


class FeeItemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    amount: int = Field(..., ge=0, le=MAX_FEE_AMOUNT)
    classroom_id: Optional[int] = None
    period: str = Field(..., min_length=1, max_length=20)
    is_active: bool = True


class FeeItemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    amount: Optional[int] = Field(None, ge=0, le=MAX_FEE_AMOUNT)
    classroom_id: Optional[int] = None
    period: Optional[str] = Field(None, min_length=1, max_length=20)
    is_active: Optional[bool] = None


class GenerateRequest(BaseModel):
    fee_item_id: int
    classroom_id: Optional[int] = None  # None = 全校


class PayRequest(BaseModel):
    payment_date: date
    amount_paid: Optional[int] = Field(
        None,
        ge=1,
        le=MAX_FEE_AMOUNT,
        description=f"累計已繳金額（None=全額；上限 NT${MAX_FEE_AMOUNT:,}）",
    )
    payment_method: str = Field(..., pattern="^(現金|轉帳|其他)$")
    notes: Optional[str] = Field("", max_length=200)
    idempotency_key: Optional[str] = Field(
        None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="繳費冪等鍵（全域唯一；同 key 重送視為重試並回放先前結果）",
    )


class RefundRequest(BaseModel):
    """退款請求。退款走獨立流程，於 StudentFeeRefund 表留下歷史。

    reason 最短 5 字（避免「.」或「誤」等敷衍）；金額 > FINANCE_APPROVAL_THRESHOLD
    需 ACTIVITY_PAYMENT_APPROVE 權限（handler 層檢查）。
    """

    amount: int = Field(
        ...,
        ge=1,
        le=MAX_FEE_AMOUNT,
        description=f"退款金額（正整數，上限 NT${MAX_FEE_AMOUNT:,}）",
    )
    reason: str = Field(..., min_length=5, max_length=100)
    notes: Optional[str] = Field("", max_length=200)
    idempotency_key: Optional[str] = Field(
        None,
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="冪等鍵（10 分鐘視窗內同 key 視為重試，避免重複退款）",
    )


def _apply_fee_record_filters(
    query,
    *,
    period: Optional[str] = None,
    classroom_name: Optional[str] = None,
    status: Optional[str] = None,
    fee_item_id: Optional[int] = None,
    student_name: Optional[str] = None,
    student_id: Optional[int] = None,
):
    if period:
        query = query.filter(StudentFeeRecord.period == period)
    if classroom_name:
        query = query.filter(StudentFeeRecord.classroom_name == classroom_name)
    if status:
        query = query.filter(StudentFeeRecord.status == status)
    if fee_item_id:
        query = query.filter(StudentFeeRecord.fee_item_id == fee_item_id)
    if student_id:
        query = query.filter(StudentFeeRecord.student_id == student_id)
    keyword = (student_name or "").strip()
    if keyword:
        from utils.search import LIKE_ESCAPE_CHAR, escape_like_pattern

        safe_kw = escape_like_pattern(keyword)
        query = query.filter(
            StudentFeeRecord.student_name.ilike(f"%{safe_kw}%", escape=LIKE_ESCAPE_CHAR)
        )
    return query


# ---------------------------------------------------------------------------
# 費用項目
# ---------------------------------------------------------------------------


@router.get("/items")
def list_fee_items(
    period: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """取得費用項目清單（JOIN classroom，一次查詢）"""
    with session_scope() as session:
        q = session.query(FeeItem, Classroom).outerjoin(
            Classroom, FeeItem.classroom_id == Classroom.id
        )
        if period:
            q = q.filter(FeeItem.period == period)
        if is_active is not None:
            q = q.filter(FeeItem.is_active == is_active)

        rows = q.order_by(FeeItem.period.desc(), FeeItem.id).all()
        return [
            {
                "id": item.id,
                "name": item.name,
                "amount": item.amount,
                "classroom_id": item.classroom_id,
                "classroom_name": cls.name if cls else None,
                "period": item.period,
                "is_active": item.is_active,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item, cls in rows
        ]


@router.get("/periods")
def list_fee_periods(
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """取得所有已建立的學期列表（供前端下拉選單使用）"""
    with session_scope() as session:
        rows = (
            session.query(FeeItem.period)
            .distinct()
            .order_by(FeeItem.period.desc())
            .all()
        )
        return [r.period for r in rows]


@router.post("/items", status_code=201)
def create_fee_item(
    payload: FeeItemCreate,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """新增費用項目"""
    with session_scope() as session:
        if payload.classroom_id:
            cls = (
                session.query(Classroom)
                .filter(Classroom.id == payload.classroom_id)
                .first()
            )
            if not cls:
                raise HTTPException(status_code=404, detail="班級不存在")

        item = FeeItem(
            name=payload.name,
            amount=payload.amount,
            classroom_id=payload.classroom_id,
            period=payload.period,
            is_active=payload.is_active,
        )
        session.add(item)
        session.flush()
        result = {
            "id": item.id,
            "name": item.name,
            "amount": item.amount,
            "period": item.period,
        }

    logger.info(
        "新增費用項目 id=%s name=%s period=%s",
        result["id"],
        result["name"],
        result["period"],
    )
    return result


@router.put("/items/{item_id}")
def update_fee_item(
    item_id: int,
    payload: FeeItemUpdate,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """更新費用項目"""
    with session_scope() as session:
        item = session.query(FeeItem).filter(FeeItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="費用項目不存在")

        if payload.name is not None:
            item.name = payload.name
        if payload.amount is not None:
            item.amount = payload.amount
        if payload.classroom_id is not None:
            cls = (
                session.query(Classroom)
                .filter(Classroom.id == payload.classroom_id)
                .first()
            )
            if not cls:
                raise HTTPException(status_code=404, detail="班級不存在")
            item.classroom_id = payload.classroom_id
        if payload.period is not None:
            item.period = payload.period
        if payload.is_active is not None:
            item.is_active = payload.is_active

        item.updated_at = datetime.now()

    logger.info("更新費用項目 id=%s", item_id)
    return {"ok": True}


@router.delete("/items/{item_id}")
def delete_fee_item(
    item_id: int,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """刪除費用項目（若有關聯記錄則拒絕）"""
    with session_scope() as session:
        item = session.query(FeeItem).filter(FeeItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="費用項目不存在")

        linked = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.fee_item_id == item_id)
            .count()
        )
        if linked > 0:
            raise HTTPException(
                status_code=400,
                detail=f"此費用項目已有 {linked} 筆學生記錄，無法刪除。請先刪除相關記錄或改為停用。",
            )

        name = item.name
        session.delete(item)

    logger.warning("刪除費用項目 id=%s name=%s", item_id, name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 批次產生費用記錄
# ---------------------------------------------------------------------------


@router.post("/generate")
def generate_fee_records(
    payload: GenerateRequest,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """批次為指定班級或全校的在校學生產生費用記錄"""
    with session_scope() as session:
        fee_item = (
            session.query(FeeItem).filter(FeeItem.id == payload.fee_item_id).first()
        )
        if not fee_item:
            raise HTTPException(status_code=404, detail="費用項目不存在")
        if not fee_item.is_active:
            raise HTTPException(status_code=400, detail="費用項目已停用，無法產生記錄")

        # 查詢在校學生（LEFT JOIN classroom 取班級名稱）
        q = (
            session.query(Student, Classroom)
            .outerjoin(Classroom, Student.classroom_id == Classroom.id)
            .filter(Student.is_active == True)
        )
        if payload.classroom_id:
            q = q.filter(Student.classroom_id == payload.classroom_id)
        elif fee_item.classroom_id:
            q = q.filter(Student.classroom_id == fee_item.classroom_id)

        students = q.all()

        # 一次查完已存在的 student_id，避免 N 次單筆查詢
        existing_student_ids = {
            r.student_id
            for r in session.query(StudentFeeRecord.student_id)
            .filter(StudentFeeRecord.fee_item_id == payload.fee_item_id)
            .all()
        }

        now = datetime.now()
        created = 0
        skipped = 0
        new_records = []
        for student, classroom in students:
            if student.id in existing_student_ids:
                skipped += 1
                continue

            new_records.append(
                {
                    "student_id": student.id,
                    "student_name": student.name,
                    "classroom_name": classroom.name if classroom else "",
                    "fee_item_id": fee_item.id,
                    "fee_item_name": fee_item.name,
                    "amount_due": fee_item.amount,
                    "amount_paid": 0,
                    "status": "unpaid",
                    "period": fee_item.period,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            created += 1

        if new_records:
            session.bulk_insert_mappings(StudentFeeRecord, new_records)

    logger.info(
        "批次產生費用記錄 fee_item_id=%s 新建=%s 跳過=%s",
        payload.fee_item_id,
        created,
        skipped,
    )
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# 費用記錄查詢（含分頁）
# ---------------------------------------------------------------------------


@router.get("/records")
def list_fee_records(
    period: Optional[str] = Query(None),
    classroom_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(unpaid|partial|paid)$"),
    fee_item_id: Optional[int] = Query(None),
    student_name: Optional[str] = Query(None),
    student_id: Optional[int] = Query(None, gt=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """查詢費用記錄（支援分頁）。

    student_id：指定學生 ID 時，僅回傳該學生的費用紀錄（跨學期）。
    """
    with session_scope() as session:
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            fee_item_id=fee_item_id,
            student_name=student_name,
            student_id=student_id,
        )

        total = q.count()
        records = (
            q.order_by(
                StudentFeeRecord.period.desc(),
                StudentFeeRecord.classroom_name,
                StudentFeeRecord.student_name,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": r.id,
                    "student_id": r.student_id,
                    "student_name": r.student_name,
                    "classroom_name": r.classroom_name,
                    "fee_item_id": r.fee_item_id,
                    "fee_item_name": r.fee_item_name,
                    "amount_due": r.amount_due,
                    "amount_paid": r.amount_paid,
                    "status": r.status,
                    "payment_date": (
                        r.payment_date.isoformat() if r.payment_date else None
                    ),
                    "payment_method": r.payment_method,
                    "notes": r.notes,
                    "period": r.period,
                }
                for r in records
            ],
        }


# ---------------------------------------------------------------------------
# 登記繳費
# ---------------------------------------------------------------------------


@router.put("/records/{record_id}/pay")
def pay_fee_record(
    record_id: int,
    payload: PayRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """登記繳費 — API 契約保留「累計已繳」語意，底層改為 append-only 流水。

    Why: 財務月報過去用 StudentFeeRecord.payment_date / status 聚合，分期收款
    會把前期收入搬到最後一次付款月份，退款後月份可能整筆消失。現在每次 pay
    都會 INSERT 一筆 StudentFeePayment（delta 金額 + 本次付款日），財報改
    SUM 流水表即可正確歸月。

    - payload.amount_paid 仍代表「累計到此值」，後端自動算 delta 插入
    - delta < 0 拒絕（走退款流程）；delta = 0 視為只更新 method/notes 快照
    - record 上的 amount_paid / payment_date / payment_method 保持「最後一次」
      快照供清單顯示；真正的月度聚合看 StudentFeePayment
    - idempotency_key：全域唯一，同 key 重送回放（DB UNIQUE 兜底）
    """
    with session_scope() as session:
        # ── 冪等性重送檢查：先於任何寫入 ─────────────────────────────
        if payload.idempotency_key:
            hit = (
                session.query(StudentFeePayment)
                .filter(StudentFeePayment.idempotency_key == payload.idempotency_key)
                .first()
            )
            if hit is not None:
                # 同 key 必須對應同一 record 與同 payload；上下文不同視為 key 誤用
                if hit.record_id != record_id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"idempotency_key 已用於 record {hit.record_id}，"
                            f"不可重複用於 record {record_id}"
                        ),
                    )
                rec = (
                    session.query(StudentFeeRecord)
                    .filter(StudentFeeRecord.id == record_id)
                    .first()
                )
                return {
                    "ok": True,
                    "amount_paid": rec.amount_paid if rec else None,
                    "previous_amount_paid": (rec.amount_paid if rec else 0)
                    - hit.amount,
                    "idempotent_replay": True,
                }

        record = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .with_for_update()
            .first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="費用記錄不存在")
        if record.status == "paid":
            raise HTTPException(status_code=400, detail="此記錄已完成繳費")

        amount_paid = (
            payload.amount_paid
            if payload.amount_paid is not None
            else record.amount_due
        )
        if amount_paid > record.amount_due:
            raise HTTPException(
                status_code=400,
                detail=f"繳費金額（{amount_paid}）不得超過應繳金額（{record.amount_due}）",
            )

        previous_paid = record.amount_paid or 0
        if amount_paid < previous_paid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"新金額 NT${amount_paid} 低於已登記金額 NT${previous_paid}，"
                    "請改用退款流程（POST /records/{id}/refund）"
                ),
            )

        delta = amount_paid - previous_paid
        operator = current_user.get("username", "") or "unknown"

        # Append-only 流水：delta > 0 時才寫一筆（delta=0 只更新快照）
        if delta > 0:
            payment = StudentFeePayment(
                record_id=record.id,
                amount=delta,
                payment_date=payload.payment_date,
                payment_method=payload.payment_method,
                notes=payload.notes or "",
                operator=operator,
                idempotency_key=payload.idempotency_key,
            )
            session.add(payment)

        record.amount_paid = amount_paid
        record.payment_date = payload.payment_date
        record.payment_method = payload.payment_method
        record.notes = payload.notes or ""
        record.status = "paid" if amount_paid >= record.amount_due else "partial"
        record.updated_at = datetime.now()

        student_name = record.student_name

        # 把金額變動塞進 AuditMiddleware 的 summary（含前後值，不會被 body mask 掉）
        request.state.audit_summary = (
            f"繳費登記 {record.period or ''} {student_name}: "
            f"NT${previous_paid} → NT${amount_paid}（本次 +NT${delta}）"
            f"（{payload.payment_method}，by {operator}）"
        )
        request.state.audit_entity_id = record_id

        # DB 層 UNIQUE 攔下並發同 key 的第二筆：轉為 replay
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            if (
                payload.idempotency_key
                and "idempotency_key" in str(getattr(e, "orig", e)).lower()
            ):
                with session_scope() as replay_session:
                    hit = (
                        replay_session.query(StudentFeePayment)
                        .filter(
                            StudentFeePayment.idempotency_key == payload.idempotency_key
                        )
                        .first()
                    )
                    if hit and hit.record_id == record_id:
                        rec = (
                            replay_session.query(StudentFeeRecord)
                            .filter(StudentFeeRecord.id == record_id)
                            .first()
                        )
                        return {
                            "ok": True,
                            "amount_paid": rec.amount_paid if rec else None,
                            "previous_amount_paid": (
                                (rec.amount_paid if rec else 0) - hit.amount
                            ),
                            "idempotent_replay": True,
                        }
                    if hit:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"idempotency_key 已用於 record {hit.record_id}，"
                                f"不可重複用於 record {record_id}"
                            ),
                        )
            raise

    # 金額變動 warning 保留一份（AuditLog 寫失敗時仍有日誌可查）
    if delta != 0:
        logger.warning(
            "FEE_PAY_CHANGE record_id=%s student=%s operator=%s prev=%s new=%s delta=%s method=%s",
            record_id,
            student_name,
            operator,
            previous_paid,
            amount_paid,
            delta,
            payload.payment_method,
        )
    return {
        "ok": True,
        "amount_paid": amount_paid,
        "previous_amount_paid": previous_paid,
        "delta": delta,
    }


# ---------------------------------------------------------------------------
# 統計摘要
# ---------------------------------------------------------------------------


@router.get("/summary")
def fee_summary(
    period: Optional[str] = Query(None),
    classroom_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(unpaid|partial|paid)$"),
    fee_item_id: Optional[int] = Query(None),
    student_name: Optional[str] = Query(None),
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """統計摘要：總應繳金額、已繳、未繳人數/金額"""
    with session_scope() as session:
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            fee_item_id=fee_item_id,
            student_name=student_name,
        )

        agg_q = q.with_entities(
            func.count(StudentFeeRecord.id).label("total_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "paid", 1), else_=0)), 0
            ).label("paid_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "partial", 1), else_=0)), 0
            ).label("partial_count"),
            func.coalesce(func.sum(StudentFeeRecord.amount_due), 0).label("total_due"),
            func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0).label(
                "total_paid"
            ),
        )
        row = agg_q.one()
        total_count = row.total_count or 0
        paid_count = int(row.paid_count or 0)
        partial_count = int(row.partial_count or 0)
        total_due = int(row.total_due or 0)
        total_paid = int(row.total_paid or 0)

        return {
            "total_count": total_count,
            "paid_count": paid_count,
            "partial_count": partial_count,
            "unpaid_count": total_count - paid_count - partial_count,
            "total_due": total_due,
            "total_paid": total_paid,
            "total_unpaid": total_due - total_paid,
        }


# ---------------------------------------------------------------------------
# 退款流程
# ---------------------------------------------------------------------------

# 冪等視窗：同 idempotency_key 於視窗內視為重試（避免網路重送導致重複退款）
_REFUND_IDEMPOTENCY_WINDOW_SECONDS = 10 * 60


def _find_refund_idempotent_hit(
    session, idempotency_key: str
) -> Optional[StudentFeeRefund]:
    """查詢相同 idempotency_key 的退款紀錄（全域，不限時間視窗）。

    Why: DB 層 UniqueConstraint 已保證 idempotency_key 永久唯一。
    過去用 10 分鐘 window 過濾會造成：key 在 window 外重送 → 查不到 →
    繼續 INSERT → UNIQUE 拒絕 → 客戶端收 500（原本第一次可能已成功）。
    改為全域查詢，上下文驗證由呼叫端負責（record_id / amount 必須一致）。
    """
    return (
        session.query(StudentFeeRefund)
        .filter(StudentFeeRefund.idempotency_key == idempotency_key)
        .order_by(StudentFeeRefund.id.asc())
        .first()
    )


@router.post("/records/{record_id}/refund", status_code=201)
def refund_fee_record(
    record_id: int,
    payload: RefundRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """建立退款紀錄並扣除已繳金額。

    - 退款金額必須 ≤ 當下已繳
    - 一次退款一筆，需填退款原因（稽核要求）
    - 鎖住該筆 fee record，避免與 pay_fee_record 併發衝突
    - 若帶 idempotency_key，10 分鐘視窗內同 key 視為重試，回傳原退款結果
      （避免網路重送造成重複扣款；DB UniqueConstraint 於並發時攔下第二筆）
    """
    idempotent_replay = False
    with session_scope() as session:
        # 先檢冪等：若已有紀錄，直接回放原結果，不鎖 record 也不動 amount_paid
        # 上下文必須一致（record_id / amount 相符），否則視為 key 誤用 → 409
        if payload.idempotency_key:
            existing = _find_refund_idempotent_hit(session, payload.idempotency_key)
            if existing is not None:
                if existing.record_id != record_id or existing.amount != payload.amount:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"idempotency_key 已用於 record {existing.record_id} "
                            f"（NT${existing.amount}），不可重複用於本請求"
                        ),
                    )
                rec = (
                    session.query(StudentFeeRecord)
                    .filter(StudentFeeRecord.id == record_id)
                    .first()
                )
                return {
                    "ok": True,
                    "refund_amount": existing.amount,
                    "new_amount_paid": rec.amount_paid if rec else None,
                    "status": rec.status if rec else None,
                    "idempotent_replay": True,
                }

        record = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .with_for_update()
            .first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="費用記錄不存在")

        paid = record.amount_paid or 0
        if paid <= 0:
            raise HTTPException(status_code=400, detail="此記錄尚未有任何繳費可退")
        if payload.amount > paid:
            raise HTTPException(
                status_code=400,
                detail=f"退款金額 NT${payload.amount} 超過已繳金額 NT${paid}",
            )

        # ── A 錢守衛 ─────────────────────────────────────────────────
        # Pydantic 已強制 reason ≥ 5 字；此處再過一層 strip 並寫回 payload
        payload.reason = require_adjustment_reason(payload.reason)
        # 大額退款需金流簽核權限
        require_finance_approve(payload.amount, current_user, action_label="學費退款")

        operator = current_user.get("username") or current_user.get("name") or "unknown"

        refund = StudentFeeRefund(
            record_id=record.id,
            amount=payload.amount,
            reason=payload.reason,
            notes=payload.notes or "",
            refunded_by=operator,
            idempotency_key=payload.idempotency_key,
        )
        session.add(refund)

        record.amount_paid = paid - payload.amount
        # 若還有剩餘，視為 partial；若清 0 則回 unpaid
        if record.amount_paid <= 0:
            record.status = "unpaid"
        elif record.amount_paid < (record.amount_due or 0):
            record.status = "partial"
        else:
            record.status = "paid"
        record.updated_at = datetime.now()

        request.state.audit_summary = (
            f"學費退款 {record.period or ''} {record.student_name}: "
            f"NT${payload.amount}（{payload.reason}，by {operator}）"
        )
        request.state.audit_entity_id = record_id
        new_paid = record.amount_paid
        new_status = record.status
        student_name_snapshot = record.student_name

        # DB 層 UNIQUE 攔下並發同 idempotency_key 的第二筆：把它轉成 replay
        # 上下文必須一致，否則回 409 而非誤 replay
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            if (
                payload.idempotency_key
                and "idempotency_key" in str(getattr(e, "orig", e)).lower()
            ):
                # 另一個並發請求剛建完；重新查出來以 replay 方式回
                with session_scope() as replay_session:
                    existing = _find_refund_idempotent_hit(
                        replay_session, payload.idempotency_key
                    )
                    if existing is not None and (
                        existing.record_id != record_id
                        or existing.amount != payload.amount
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"idempotency_key 已用於 record {existing.record_id} "
                                f"（NT${existing.amount}），不可重複用於本請求"
                            ),
                        )
                    rec = (
                        replay_session.query(StudentFeeRecord)
                        .filter(StudentFeeRecord.id == record_id)
                        .first()
                    )
                    if existing is not None:
                        return {
                            "ok": True,
                            "refund_amount": existing.amount,
                            "new_amount_paid": rec.amount_paid if rec else None,
                            "status": rec.status if rec else None,
                            "idempotent_replay": True,
                        }
            raise

    logger.warning(
        "FEE_REFUND record_id=%s student=%s operator=%s amount=%s reason=%s new_paid=%s",
        record_id,
        student_name_snapshot,
        operator,
        payload.amount,
        payload.reason,
        new_paid,
    )
    return {
        "ok": True,
        "refund_amount": payload.amount,
        "new_amount_paid": new_paid,
        "status": new_status,
        "idempotent_replay": idempotent_replay,
    }


@router.get("/records/{record_id}/refunds")
def list_fee_refunds(
    record_id: int,
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """列出某筆學費記錄的退款歷史（按時間新→舊）"""
    with session_scope() as session:
        rec = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .first()
        )
        if not rec:
            raise HTTPException(status_code=404, detail="費用記錄不存在")
        refunds = (
            session.query(StudentFeeRefund)
            .filter(StudentFeeRefund.record_id == record_id)
            .order_by(StudentFeeRefund.refunded_at.desc())
            .all()
        )
        return {
            "record_id": record_id,
            "student_name": rec.student_name,
            "total_refunded": sum(r.amount for r in refunds),
            "refunds": [
                {
                    "id": r.id,
                    "amount": r.amount,
                    "reason": r.reason,
                    "notes": r.notes or "",
                    "refunded_by": r.refunded_by,
                    "refunded_at": (
                        r.refunded_at.isoformat() if r.refunded_at else None
                    ),
                }
                for r in refunds
            ],
        }
