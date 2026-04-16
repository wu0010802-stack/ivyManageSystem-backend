"""
api/fees.py — 學費/費用管理 API endpoints
"""

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import outerjoin, func, case

from models.base import session_scope
from models.classroom import Classroom, Student
from models.fees import FeeItem, StudentFeeRecord
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fees", tags=["fees"])


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class FeeItemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    amount: int = Field(..., ge=0)
    classroom_id: Optional[int] = None
    period: str = Field(..., min_length=1, max_length=20)
    is_active: bool = True


class FeeItemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    amount: Optional[int] = Field(None, ge=0)
    classroom_id: Optional[int] = None
    period: Optional[str] = Field(None, min_length=1, max_length=20)
    is_active: Optional[bool] = None


class GenerateRequest(BaseModel):
    fee_item_id: int
    classroom_id: Optional[int] = None  # None = 全校


class PayRequest(BaseModel):
    payment_date: date
    amount_paid: Optional[int] = Field(None, ge=1, description="繳費金額（None=全額）")
    payment_method: str = Field(..., pattern="^(現金|轉帳|其他)$")
    notes: Optional[str] = ""


def _apply_fee_record_filters(
    query,
    *,
    period: Optional[str] = None,
    classroom_name: Optional[str] = None,
    status: Optional[str] = None,
    fee_item_id: Optional[int] = None,
    student_name: Optional[str] = None,
):
    if period:
        query = query.filter(StudentFeeRecord.period == period)
    if classroom_name:
        query = query.filter(StudentFeeRecord.classroom_name == classroom_name)
    if status:
        query = query.filter(StudentFeeRecord.status == status)
    if fee_item_id:
        query = query.filter(StudentFeeRecord.fee_item_id == fee_item_id)
    keyword = (student_name or "").strip()
    if keyword:
        query = query.filter(StudentFeeRecord.student_name.ilike(f"%{keyword}%"))
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
        q = (
            session.query(FeeItem, Classroom)
            .outerjoin(Classroom, FeeItem.classroom_id == Classroom.id)
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
            cls = session.query(Classroom).filter(Classroom.id == payload.classroom_id).first()
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
        result = {"id": item.id, "name": item.name, "amount": item.amount, "period": item.period}

    logger.info("新增費用項目 id=%s name=%s period=%s", result["id"], result["name"], result["period"])
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
            cls = session.query(Classroom).filter(Classroom.id == payload.classroom_id).first()
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

        linked = session.query(StudentFeeRecord).filter(
            StudentFeeRecord.fee_item_id == item_id
        ).count()
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
        fee_item = session.query(FeeItem).filter(FeeItem.id == payload.fee_item_id).first()
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
            for r in session.query(StudentFeeRecord.student_id).filter(
                StudentFeeRecord.fee_item_id == payload.fee_item_id
            ).all()
        }

        now = datetime.now()
        created = 0
        skipped = 0
        new_records = []
        for student, classroom in students:
            if student.id in existing_student_ids:
                skipped += 1
                continue

            new_records.append({
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
            })
            created += 1

        if new_records:
            session.bulk_insert_mappings(StudentFeeRecord, new_records)

    logger.info(
        "批次產生費用記錄 fee_item_id=%s 新建=%s 跳過=%s",
        payload.fee_item_id, created, skipped,
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
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """查詢費用記錄（支援分頁）"""
    with session_scope() as session:
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            fee_item_id=fee_item_id,
            student_name=student_name,
        )

        total = q.count()
        records = (
            q.order_by(StudentFeeRecord.period.desc(), StudentFeeRecord.classroom_name, StudentFeeRecord.student_name)
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
                    "payment_date": r.payment_date.isoformat() if r.payment_date else None,
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
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """登記繳費"""
    with session_scope() as session:
        record = session.query(StudentFeeRecord).filter(StudentFeeRecord.id == record_id).with_for_update().first()
        if not record:
            raise HTTPException(status_code=404, detail="費用記錄不存在")
        if record.status == "paid":
            raise HTTPException(status_code=400, detail="此記錄已完成繳費")

        amount_paid = payload.amount_paid if payload.amount_paid is not None else record.amount_due
        if amount_paid > record.amount_due:
            raise HTTPException(status_code=400, detail=f"繳費金額（{amount_paid}）不得超過應繳金額（{record.amount_due}）")
        record.amount_paid = amount_paid
        record.payment_date = payload.payment_date
        record.payment_method = payload.payment_method
        record.notes = payload.notes or ""
        record.status = "paid" if amount_paid >= record.amount_due else "partial"
        record.updated_at = datetime.now()

        student_name = record.student_name

    logger.info("登記繳費 record_id=%s student=%s amount=%s", record_id, student_name, amount_paid)
    return {"ok": True, "amount_paid": amount_paid}


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
            func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0).label("total_paid"),
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
