"""
api/fees.py — 學費/費用管理 API endpoints
"""

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import outerjoin, func, case
from sqlalchemy.exc import IntegrityError

from api.activity._shared import validate_payment_date
from models.base import session_scope
from models.classroom import (
    Classroom,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    Student,
)
from models.fees import (
    FeeItem,
    FeeTemplate,
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from models.student_leave import StudentLeaveRequest
from services.fee_refund_calculator import (
    calc_enrollment_refund,
    calc_monthly_refund,
    longest_consecutive_workdays,
)
from services.report_cache_service import report_cache_service
from services.workday_rules import classify_day, load_day_rule_maps
from utils.audit import write_audit_in_session
from utils.auth import require_staff_permission
from utils.finance_guards import require_adjustment_reason, require_finance_approve
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access, is_unrestricted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fees", tags=["fees"])

# 報表快取 category：任何學費寫入後呼叫 invalidate，避免 /finance-summary 30 分內
# 給舊數字。與 api/activity + api/salary 共用同一 category key。
_FINANCE_SUMMARY_CACHE_CATEGORY = "reports_finance_summary"


def _invalidate_finance_summary_cache() -> None:
    """money write path 結束後呼叫，讓 finance-summary 下次請求重算。

    invalidate_categories 內部自開 session，不依賴當前 session，也不會因
    cache 寫入失敗而影響主交易（例外被 service 自行 log+swallow）。
    """
    try:
        report_cache_service.invalidate_category(None, _FINANCE_SUMMARY_CACHE_CATEGORY)
    except Exception:
        # 守衛：快取失效失敗不應影響金流交易
        logger.warning("invalidate finance_summary cache failed", exc_info=True)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

# 單筆費用金額上限（避免誤輸入或惡意輸入）
MAX_FEE_AMOUNT = 999_999

# 學費單筆繳款大額簽核閾值(NT$):本次入帳 delta 超過即需 ACTIVITY_PAYMENT_APPROVE。
# 50000 涵蓋一般月費正常區間(月費 NT$10K~30K),學期/年費等大筆才需簽核;
# 與 finance_guards.FINANCE_APPROVAL_THRESHOLD(NT$1000,薪資/退款用)區隔,
# 避免日常收款 100% 觸發簽核堵死流程。閾值可依園所實際收費結構調整。
FEE_PAYMENT_APPROVAL_THRESHOLD = 50_000


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


class FeeTemplateCreate(BaseModel):
    grade_id: int = Field(..., gt=0)
    school_year: int = Field(..., ge=100, le=200)
    semester: int = Field(..., ge=1, le=2)
    fee_type: str = Field(..., pattern="^(registration|miscellaneous|monthly)$")
    name: str = Field(..., min_length=1, max_length=100)
    amount: int = Field(..., ge=0, le=MAX_FEE_AMOUNT)
    breakdown: Optional[dict] = None
    due_date_offset_days: int = Field(14, ge=0, le=365)
    is_active: bool = True

    @field_validator("breakdown")
    @classmethod
    def _validate_breakdown(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict) or not v:
            raise ValueError("breakdown 必須為非空 dict")
        for k, amt in v.items():
            if not isinstance(amt, int) or amt < 0:
                raise ValueError(f"breakdown.{k} 必須為非負整數")
        return v


class FeeTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    amount: Optional[int] = Field(None, ge=0, le=MAX_FEE_AMOUNT)
    breakdown: Optional[dict] = None
    due_date_offset_days: Optional[int] = Field(None, ge=0, le=365)
    is_active: Optional[bool] = None


class GenerateFromTemplatesRequest(BaseModel):
    school_year: int = Field(..., ge=100, le=200)
    semester: int = Field(..., ge=1, le=2)
    fee_types: list[str] = Field(..., min_length=1)
    dry_run: bool = False

    @field_validator("fee_types")
    @classmethod
    def _validate_types(cls, v):
        allowed = {"registration", "miscellaneous", "monthly"}
        bad = [t for t in v if t not in allowed]
        if bad:
            raise ValueError(f"非法 fee_type: {bad}")
        return v


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

    # 與活動繳費同源守衛（禁未來日）；學費跨月分期合法，回補上限放寬至 90 天。
    # Why: 缺此守衛會計可填未來日或回填遠古日期搬動財報歸月；放 90 天涵蓋學期跨季合法分期。
    @field_validator("payment_date")
    @classmethod
    def _validate_payment_date(cls, v: date) -> date:
        return validate_payment_date(v, back_limit_days=90)


class RefundSuggestRequest(BaseModel):
    """退費建議請求：依學生離園日與費用類型自動計算退費金額。

    `T_total_override` / `T_served_override` 提供給罕見特例（例如手動調整教保日數），
    一般情況下會由 workday_rules + 學期區間計算得出。
    """

    withdrawal_date: date
    T_total_override: Optional[int] = Field(None, gt=0, le=400)
    T_served_override: Optional[int] = Field(None, ge=0, le=400)


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
    calc_method: Optional[str] = Field(
        None, pattern="^(enrollment_ratio|monthly_partial|no_refund|manual)$"
    )
    calc_payload: Optional[dict] = None


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
    request: Request,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """更新費用項目"""
    with session_scope() as session:
        item = session.query(FeeItem).filter(FeeItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="費用項目不存在")

        # 預先 snapshot 舊值，方便組 audit_changes diff
        before = {
            "name": item.name,
            "amount": item.amount,
            "classroom_id": item.classroom_id,
            "period": item.period,
            "is_active": item.is_active,
        }
        diff = {}

        if payload.name is not None and payload.name != item.name:
            diff["name"] = {"before": item.name, "after": payload.name}
            item.name = payload.name
        if payload.amount is not None and payload.amount != item.amount:
            diff["amount"] = {"before": item.amount, "after": payload.amount}
            item.amount = payload.amount
        if (
            payload.classroom_id is not None
            and payload.classroom_id != item.classroom_id
        ):
            cls = (
                session.query(Classroom)
                .filter(Classroom.id == payload.classroom_id)
                .first()
            )
            if not cls:
                raise HTTPException(status_code=404, detail="班級不存在")
            diff["classroom_id"] = {
                "before": item.classroom_id,
                "after": payload.classroom_id,
            }
            item.classroom_id = payload.classroom_id
        if payload.period is not None and payload.period != item.period:
            diff["period"] = {"before": item.period, "after": payload.period}
            item.period = payload.period
        if payload.is_active is not None and payload.is_active != item.is_active:
            diff["is_active"] = {"before": item.is_active, "after": payload.is_active}
            item.is_active = payload.is_active

        item.updated_at = datetime.now()

        # 統計受影響的學生費用紀錄數，amount 異動時揭露衝擊面積
        affected_records = 0
        if "amount" in diff:
            affected_records = (
                session.query(StudentFeeRecord)
                .filter(StudentFeeRecord.fee_item_id == item_id)
                .count()
            )

        request.state.audit_entity_id = str(item_id)
        request.state.audit_summary = f"更新費用項目 #{item_id}（{item.name}）" + (
            f"：金額 {diff['amount']['before']} → {diff['amount']['after']}"
            if "amount" in diff
            else ""
        )
        request.state.audit_changes = {
            "action": "fee_item_update",
            "item_id": item_id,
            "before": before,
            "diff": diff,
            "affected_fee_records": affected_records,
        }

    logger.info("更新費用項目 id=%s diff=%s", item_id, list(diff.keys()))
    return {"ok": True}


@router.delete("/items/{item_id}")
def delete_fee_item(
    item_id: int,
    request: Request,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """刪除費用項目（若有關聯記錄則拒絕）"""
    with session_scope() as session:
        # with_for_update：與 count linked + delete 同 transaction 持鎖，
        # 避免「檢查無關聯 → 他人 INSERT StudentFeeRecord → 刪除成功」造成 FK 違反或孤兒。
        item = (
            session.query(FeeItem)
            .filter(FeeItem.id == item_id)
            .with_for_update()
            .first()
        )
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

        snapshot = {
            "id": item.id,
            "name": item.name,
            "amount": item.amount,
            "classroom_id": item.classroom_id,
            "period": item.period,
            "is_active": item.is_active,
        }
        name = item.name
        session.delete(item)

        # 金流項目刪除必須留 audit；同 session 寫入確保與 delete 共生死
        write_audit_in_session(
            session,
            request,
            action="DELETE",
            entity_type="fee",
            entity_id=item_id,
            summary=f"刪除費用項目 #{item_id}（{name}，金額 {snapshot['amount']}）",
            changes={"action": "fee_item_delete", "snapshot": snapshot},
        )

    logger.warning("刪除費用項目 id=%s name=%s", item_id, name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 費用範本 CRUD
# ---------------------------------------------------------------------------


def _validate_template_breakdown(amount: int, breakdown: Optional[dict]) -> None:
    """月費 breakdown 各鍵總和需 == amount,否則拒絕。"""
    if not breakdown:
        return
    total = sum(int(v) for v in breakdown.values())
    if total != amount:
        raise HTTPException(
            status_code=400,
            detail=f"breakdown 總和 {total} 與 amount {amount} 不符",
        )


def _template_to_dict(t: FeeTemplate) -> dict:
    return {
        "id": t.id,
        "grade_id": t.grade_id,
        "school_year": t.school_year,
        "semester": t.semester,
        "fee_type": t.fee_type,
        "name": t.name,
        "amount": t.amount,
        "breakdown": t.breakdown,
        "due_date_offset_days": t.due_date_offset_days,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/templates")
def list_fee_templates(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=2),
    fee_type: Optional[str] = Query(
        None, pattern="^(registration|miscellaneous|monthly)$"
    ),
    is_active: Optional[bool] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    with session_scope() as session:
        q = session.query(FeeTemplate)
        if school_year is not None:
            q = q.filter(FeeTemplate.school_year == school_year)
        if semester is not None:
            q = q.filter(FeeTemplate.semester == semester)
        if fee_type is not None:
            q = q.filter(FeeTemplate.fee_type == fee_type)
        if is_active is not None:
            q = q.filter(FeeTemplate.is_active == is_active)
        items = q.order_by(
            FeeTemplate.school_year.desc(),
            FeeTemplate.semester,
            FeeTemplate.grade_id,
            FeeTemplate.fee_type,
        ).all()
        return [_template_to_dict(t) for t in items]


@router.post("/templates")
def create_fee_template(
    payload: FeeTemplateCreate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    _validate_template_breakdown(payload.amount, payload.breakdown)
    with session_scope() as session:
        existing = (
            session.query(FeeTemplate)
            .filter(
                FeeTemplate.grade_id == payload.grade_id,
                FeeTemplate.school_year == payload.school_year,
                FeeTemplate.semester == payload.semester,
                FeeTemplate.fee_type == payload.fee_type,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"已存在範本(grade={payload.grade_id} "
                    f"{payload.school_year}-{payload.semester} {payload.fee_type})"
                ),
            )
        t = FeeTemplate(
            grade_id=payload.grade_id,
            school_year=payload.school_year,
            semester=payload.semester,
            fee_type=payload.fee_type,
            name=payload.name,
            amount=payload.amount,
            breakdown=payload.breakdown,
            due_date_offset_days=payload.due_date_offset_days,
            is_active=payload.is_active,
            created_by=current_user.get("username"),
            updated_by=current_user.get("username"),
        )
        session.add(t)
        session.flush()
        result = _template_to_dict(t)

        request.state.audit_entity_id = str(t.id)
        request.state.audit_summary = f"建立費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_create",
            "template": result,
        }
        return result


@router.put("/templates/{template_id}")
def update_fee_template(
    template_id: int,
    payload: FeeTemplateUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        t = session.query(FeeTemplate).filter(FeeTemplate.id == template_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="範本不存在")
        before = _template_to_dict(t)
        new_amount = payload.amount if payload.amount is not None else t.amount
        new_breakdown = (
            payload.breakdown if payload.breakdown is not None else t.breakdown
        )
        _validate_template_breakdown(new_amount, new_breakdown)
        if payload.name is not None:
            t.name = payload.name
        if payload.amount is not None:
            t.amount = payload.amount
        if payload.breakdown is not None:
            t.breakdown = payload.breakdown
        if payload.due_date_offset_days is not None:
            t.due_date_offset_days = payload.due_date_offset_days
        if payload.is_active is not None:
            t.is_active = payload.is_active
        t.updated_by = current_user.get("username")
        session.flush()
        after = _template_to_dict(t)

        request.state.audit_entity_id = str(template_id)
        request.state.audit_summary = f"編輯費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_update",
            "before": before,
            "after": after,
        }
        return after


@router.delete("/templates/{template_id}")
def delete_fee_template(
    template_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """軟刪除(is_active=False),保留歷史記錄。"""
    with session_scope() as session:
        t = session.query(FeeTemplate).filter(FeeTemplate.id == template_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="範本不存在")
        t.is_active = False
        t.updated_by = current_user.get("username")
        session.flush()

        request.state.audit_entity_id = str(template_id)
        request.state.audit_summary = f"停用費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_delete",
            "template_id": template_id,
        }
        return {"ok": True, "template_id": template_id}


# ---------------------------------------------------------------------------
# 批次產生費用記錄
# ---------------------------------------------------------------------------


def _semester_months(school_year: int, semester: int) -> list[str]:
    """民國年+學期 → YYYY-MM list。

    上學期 (semester=1): 8-12 月本年 + 1 月隔年
    下學期 (semester=2): 2-7 月本年
    """
    western = school_year + 1911
    if semester == 1:
        return [f"{western}-{m:02d}" for m in range(8, 13)] + [f"{western + 1}-01"]
    return [f"{western + 1}-{m:02d}" for m in range(2, 8)]


def _ensure_fee_items_for_templates(
    session, keys: list, template_by_id: dict, period: str
) -> dict:
    """為每個 (template, target_month) 確保有對應的 placeholder FeeItem。

    Why: 既有 StudentFeeRecord.fee_item_id 是 NOT NULL,且 (student_id, fee_item_id)
    有 unique 約束。新流程以 source_template_id+target_month 驅動,但需相容舊欄位。
    每個 (template, month) 配一個 FeeItem 作為錨點,避免月費 6 筆共用同 fee_item
    撞 uq_student_fee_item。
    """
    out: dict = {}
    for tpl_id, tm in keys:
        tpl = template_by_id[tpl_id]
        fi_name = f"{tpl.name} ({tm})" if tm else tpl.name
        existing = (
            session.query(FeeItem)
            .filter(FeeItem.name == fi_name, FeeItem.period == period)
            .first()
        )
        if existing:
            out[(tpl_id, tm)] = existing.id
            continue
        fi = FeeItem(
            name=fi_name,
            amount=tpl.amount,
            classroom_id=None,
            period=period,
            is_active=True,
        )
        session.add(fi)
        session.flush()
        out[(tpl_id, tm)] = fi.id
    return out


@router.post("/generate-from-templates")
def generate_from_templates(
    payload: GenerateFromTemplatesRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """依該學年/學期所有啟用範本,為符合條件的在學學生產生 FeeRecord。

    - 範圍:fee_templates 表 is_active=True 且 (school_year, semester, fee_type) 命中。
    - 學生過濾:Classroom.school_year/semester 命中 + Student.lifecycle 為
      active/enrolled + Student.is_active。on_leave/withdrawn/transferred/graduated 跳過。
    - 月費展開:上學期 8-1 月、下學期 2-7 月 共 6 張單據。
    - 冪等:已存在 (student_id, source_template_id, target_month) 跳過。
    - dry_run:回傳 created/skipped 估算但不寫入 DB。
    """
    with session_scope() as session:
        # 1) 載入符合條件的範本
        templates = (
            session.query(FeeTemplate)
            .filter(
                FeeTemplate.school_year == payload.school_year,
                FeeTemplate.semester == payload.semester,
                FeeTemplate.fee_type.in_(payload.fee_types),
                FeeTemplate.is_active == True,
            )
            .all()
        )
        template_by_grade_type = {(t.grade_id, t.fee_type): t for t in templates}
        template_by_id = {t.id: t for t in templates}

        # 2) 載入該學期班級 + 在學學生
        rows = (
            session.query(Student, Classroom)
            .join(Classroom, Student.classroom_id == Classroom.id)
            .filter(
                Classroom.school_year == payload.school_year,
                Classroom.semester == payload.semester,
                Classroom.is_active == True,
                Student.is_active == True,
                Student.lifecycle_status.in_([LIFECYCLE_ACTIVE, LIFECYCLE_ENROLLED]),
            )
            .all()
        )

        # 3) 預載既存 (student_id, source_template_id, target_month) 冪等鍵
        existing_keys: set = set()
        if templates:
            existing = (
                session.query(
                    StudentFeeRecord.student_id,
                    StudentFeeRecord.source_template_id,
                    StudentFeeRecord.target_month,
                )
                .filter(
                    StudentFeeRecord.source_template_id.in_([t.id for t in templates])
                )
                .all()
            )
            existing_keys = {(s, tid, m) for s, tid, m in existing}

        period_str = f"{payload.school_year}-{payload.semester}"
        now = datetime.now()
        new_records: list = []
        created = 0
        skipped = 0
        preview: list = []

        for student, classroom in rows:
            if not classroom.grade_id:
                continue
            for ft in payload.fee_types:
                tpl = template_by_grade_type.get((classroom.grade_id, ft))
                if not tpl:
                    continue

                months = (
                    _semester_months(payload.school_year, payload.semester)
                    if ft == "monthly"
                    else [None]
                )
                for tm in months:
                    key = (student.id, tpl.id, tm)
                    if key in existing_keys:
                        skipped += 1
                        continue
                    record_name = f"{tpl.name}{f' ({tm})' if tm else ''}"
                    due_date_val = date.today() + timedelta(
                        days=tpl.due_date_offset_days
                    )
                    new_records.append(
                        {
                            "student_id": student.id,
                            "student_name": student.name,
                            "classroom_name": classroom.name,
                            # fee_item_id 留 None,稍後 (僅非 dry_run) 依
                            # (source_template_id, target_month) 填入
                            "fee_item_id": None,
                            "fee_item_name": record_name,
                            "amount_due": tpl.amount,
                            "amount_paid": 0,
                            "status": "unpaid",
                            "period": period_str,
                            "due_date": due_date_val,
                            "fee_type": tpl.fee_type,
                            "source_template_id": tpl.id,
                            "target_month": tm,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    created += 1
                    if len(preview) < 50:
                        preview.append(
                            {
                                "student_id": student.id,
                                "student_name": student.name,
                                "classroom_name": classroom.name,
                                "fee_item_name": record_name,
                                "amount_due": tpl.amount,
                                "target_month": tm,
                            }
                        )
                    existing_keys.add(key)

        if not payload.dry_run and new_records:
            # 依 (template, target_month) 唯一鍵預先 ensure FeeItem
            unique_keys = sorted(
                {(r["source_template_id"], r["target_month"]) for r in new_records},
                key=lambda x: (x[0], x[1] or ""),
            )
            fee_item_map = _ensure_fee_items_for_templates(
                session, unique_keys, template_by_id, period_str
            )
            for r in new_records:
                r["fee_item_id"] = fee_item_map[
                    (r["source_template_id"], r["target_month"])
                ]
            session.bulk_insert_mappings(StudentFeeRecord, new_records)

        request.state.audit_entity_id = f"{payload.school_year}-{payload.semester}"
        request.state.audit_summary = (
            f"批次產生費用({','.join(payload.fee_types)}): "
            f"created={created} skipped={skipped} dry_run={payload.dry_run}"
        )
        request.state.audit_changes = {
            "action": "fee_generate_from_templates",
            "school_year": payload.school_year,
            "semester": payload.semester,
            "fee_types": payload.fee_types,
            "dry_run": payload.dry_run,
            "created": created,
            "skipped": skipped,
        }

        if not payload.dry_run:
            _invalidate_finance_summary_cache()

        return {
            "created": created,
            "skipped": skipped,
            "dry_run": payload.dry_run,
            "preview": preview,
        }


@router.post("/generate")
def generate_fee_records(
    payload: GenerateRequest,
    request: Request,
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

        # 結構化 diff：批次操作通常一次掃整班，必須留下「誰、何時、產了多少筆」軌跡
        # 學生 id 列表只取前 50 個避免 changes 撐爆 64KB 上限（middleware 會 truncate）
        sampled_student_ids = [
            s.id for s, _c in students if s.id not in existing_student_ids
        ][:50]
        request.state.audit_entity_id = str(payload.fee_item_id)
        request.state.audit_summary = (
            f"批次產生費用記錄：{fee_item.name}（{fee_item.period}）"
            f" 新建 {created} 筆、跳過 {skipped} 筆"
        )
        request.state.audit_changes = {
            "action": "fee_generate_records",
            "fee_item_id": payload.fee_item_id,
            "fee_item_name": fee_item.name,
            "amount_due": fee_item.amount,
            "period": fee_item.period,
            "scope_classroom_id": payload.classroom_id or fee_item.classroom_id,
            "candidate_count": len(students),
            "created": created,
            "skipped": skipped,
            "sampled_student_ids": sampled_student_ids,
            "sampled_student_ids_truncated": created > len(sampled_student_ids),
        }

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
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """查詢費用記錄（支援分頁）。

    student_id：指定學生 ID 時，僅回傳該學生的費用紀錄（跨學期）。
    """
    with session_scope() as session:
        # F-034：班級 scope 守衛 — 非 admin/hr/supervisor caller 必須帶
        # student_id 並通過 assert_student_access；不帶 student_id 全校列出
        # 一律拒絕，避免「自訂財務角色」拿全校學生繳費明細。
        if not is_unrestricted(current_user):
            if student_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="非管理角色不得列出全校繳費紀錄，請指定 student_id",
                )
            assert_student_access(session, current_user, student_id)
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

    def _assert_pay_payload_matches(session, hit: StudentFeePayment, record_id: int):
        """同 key 必須對應完整相同的 payload 上下文（record_id + payment_date +
        payment_method + 目標 amount_paid）；任一欄位不符視為 key 誤用 → 409。

        Why: 若只驗 record_id，同 record 誤帶舊 key + 新 amount 會誤 replay，
        呼叫端以為已登記但實際沒新增流水，導致資料掉筆。
        """
        mismatch = []
        if hit.record_id != record_id:
            mismatch.append(f"record_id（已用於 {hit.record_id}）")
        if hit.payment_date != payload.payment_date:
            mismatch.append(f"payment_date（原 {hit.payment_date}）")
        if hit.payment_method != payload.payment_method:
            mismatch.append(f"payment_method（原 {hit.payment_method}）")
        # 推算 hit 建立當下 record 的累計已繳 = SUM(payments WHERE id <= hit.id)
        hit_cumulative = (
            session.query(func.coalesce(func.sum(StudentFeePayment.amount), 0))
            .filter(
                StudentFeePayment.record_id == hit.record_id,
                StudentFeePayment.id <= hit.id,
            )
            .scalar()
        ) or 0
        if payload.amount_paid is not None and int(payload.amount_paid) != int(
            hit_cumulative
        ):
            mismatch.append(
                f"amount_paid（原累計 NT${hit_cumulative}，本次 NT${payload.amount_paid}）"
            )
        if mismatch:
            raise HTTPException(
                status_code=409,
                detail="idempotency_key 與先前請求的 payload 不符："
                + "、".join(mismatch),
            )

    with session_scope() as session:
        # ── 冪等性重送檢查：先於任何寫入 ─────────────────────────────
        if payload.idempotency_key:
            hit = (
                session.query(StudentFeePayment)
                .filter(StudentFeePayment.idempotency_key == payload.idempotency_key)
                .first()
            )
            if hit is not None:
                _assert_pay_payload_matches(session, hit, record_id)
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
        # F-034：班級 scope 守衛 — 非 admin/hr/supervisor 不得對他班學生登記繳費
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, record.student_id)
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

        # ── A 錢守衛:本次入帳 delta 超 FEE_PAYMENT_APPROVAL_THRESHOLD 需金流簽核 ──
        # Why: 舊版 FEES_WRITE 即可登記 NT$999,999 為現金收入,財報直接受影響。
        # 用本次 delta(非累計)判斷:讓常規月費可走、學期/年費等大筆需 approver。
        if delta > 0:
            require_finance_approve(
                delta,
                current_user,
                threshold=FEE_PAYMENT_APPROVAL_THRESHOLD,
                action_label="學費單筆繳款",
            )

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

        # DB 層 UNIQUE 攔下並發同 key 的第二筆：轉為 replay
        # 和前置檢查共用 _assert_pay_payload_matches，不可放寬檢查力道
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
                    if hit is not None:
                        _assert_pay_payload_matches(replay_session, hit, record_id)
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
            raise

        # 同交易 outbox：AuditLog 必須與金流變動共生死。
        # Why: 過去走 middleware fire-and-forget；threadpool/DB 短路時 audit 會丟，
        # 但學費紀錄已 commit。改寫在此 session 後，audit 失敗整個 rollback。
        write_audit_in_session(
            session,
            request,
            action="UPDATE",
            entity_type="fee",
            entity_id=record_id,
            summary=(
                f"繳費登記 {record.period or ''} {student_name}: "
                f"NT${previous_paid} → NT${amount_paid}（本次 +NT${delta}）"
                f"（{payload.payment_method}，by {operator}）"
            ),
            changes={
                "action": "fee_pay",
                "record_id": record_id,
                "student_id": record.student_id,
                "student_name": student_name,
                "period": record.period,
                "fee_item_id": record.fee_item_id,
                "previous_paid": previous_paid,
                "new_paid": amount_paid,
                "delta": delta,
                "amount_due": record.amount_due,
                "status_after": record.status,
                "payment_method": payload.payment_method,
                "payment_date": payload.payment_date.isoformat(),
                "payment_id": payment.id if delta > 0 else None,
                "idempotency_key": payload.idempotency_key,
                "operator": operator,
            },
        )

    # session_scope commit 後失效報表快取
    _invalidate_finance_summary_cache()

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
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """統計摘要：總應繳金額、已繳、未繳人數/金額"""
    # F-034：班級 scope 守衛 — 全校聚合僅限 admin/hr/supervisor
    if not is_unrestricted(current_user):
        raise HTTPException(
            status_code=403,
            detail="非管理角色不得讀取全校費用統計",
        )
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


def _semester_date_range(school_year: int, semester: int) -> tuple[date, date]:
    """民國年+學期 → (start, end) 西元日期。

    上學期: 8/1 ~ 隔年 1/31（學年起始那年 8 月～次年 1 月）
    下學期: 2/1 ~ 7/31（學年起始那年的次年 2 月～7 月）
    """
    western = school_year + 1911
    if semester == 1:
        return date(western, 8, 1), date(western + 1, 1, 31)
    return date(western + 1, 2, 1), date(western + 1, 7, 31)


def _count_workdays(start: date, end: date, holiday_map: dict, makeup_map: dict) -> int:
    """區間內工作日數(排除週末+國定假日,加補班日)。"""
    if end < start:
        return 0
    total = 0
    d = start
    while d <= end:
        info = classify_day(d, holiday_map, makeup_map)
        if info["kind"] == "workday":
            total += 1
        d = d + timedelta(days=1)
    return total


@router.post("/records/{record_id}/refund-suggest")
def suggest_refund(
    record_id: int,
    payload: RefundSuggestRequest,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """根據學生離園日與費用類型,自動計算建議退費金額。

    - registration / miscellaneous → 走 enrollment_ratio
      (T_served/T_total 三段比例 <1/3 退 2/3、1/3..2/3 退 1/3、≥2/3 不退)
    - monthly → 走 monthly_partial
      (事先請假連續 ≥5 上課日, 按 meal+transport 比例退;無 breakdown fallback 全額)
    - material / insurance → no_refund
    - custom / 其他 → manual（不提供自動建議）
    """
    with session_scope() as session:
        rec = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .first()
        )
        if not rec:
            raise HTTPException(status_code=404, detail="費用記錄不存在")

        # F-034 同 list_fee_records:1003 — 非 admin/hr/supervisor caller
        # 必須通過 assert_student_access 才能拿到該學生月費 breakdown 與請假
        # 計算結果（refund-suggest 會 SELECT StudentLeaveRequest）。
        # bug sweep round 4 (2026-05-14) B7。
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, rec.student_id)

        fee_type = rec.fee_type or "custom"

        # 代購品 / 保險費 → 不退
        if fee_type in ("material", "insurance"):
            label = "代購品" if fee_type == "material" else "保險費"
            return {
                "suggested_amount": 0,
                "calc_method": "no_refund",
                "calc_payload": {
                    "fee_type": fee_type,
                    "reason": f"{label}依規定不予退費",
                },
                "warnings": [f"{label}依規定不予退費"],
            }

        # 學期區間 (依 period 解析,格式 民國年-學期 e.g. 114-1)
        if not rec.period or "-" not in rec.period:
            raise HTTPException(
                status_code=400,
                detail=f"record.period 格式錯誤: {rec.period}",
            )
        try:
            sy_str, sem_str = rec.period.split("-", 1)
            school_year, semester = int(sy_str), int(sem_str)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400,
                detail=f"record.period 格式錯誤: {rec.period}",
            )
        sem_start, sem_end = _semester_date_range(school_year, semester)

        # 註冊費 / 雜費:走 enrollment_ratio
        if fee_type in ("registration", "miscellaneous"):
            holiday_map, makeup_map = load_day_rule_maps(session, sem_start, sem_end)
            T_total = payload.T_total_override or _count_workdays(
                sem_start, sem_end, holiday_map, makeup_map
            )
            served_end = min(payload.withdrawal_date, sem_end)
            if payload.T_served_override is not None:
                T_served = payload.T_served_override
            else:
                T_served = (
                    _count_workdays(sem_start, served_end, holiday_map, makeup_map)
                    if served_end >= sem_start
                    else 0
                )
            return calc_enrollment_refund(
                amount_due=rec.amount_due,
                T_total=T_total,
                T_served=T_served,
            )

        # 月費:走 monthly_partial
        if fee_type == "monthly":
            target_month = rec.target_month
            if not target_month:
                raise HTTPException(status_code=400, detail="月費記錄缺 target_month")
            try:
                year_str, month_str = target_month.split("-", 1)
                year, month = int(year_str), int(month_str)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"target_month 格式錯誤: {target_month}",
                )
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
            holiday_map, makeup_map = load_day_rule_maps(
                session, month_start, month_end
            )
            work_days = _count_workdays(month_start, month_end, holiday_map, makeup_map)

            # 該學生該月所有 approved leave;判斷 advance_filed 與蒐集請假日
            leaves = (
                session.query(StudentLeaveRequest)
                .filter(
                    StudentLeaveRequest.student_id == rec.student_id,
                    StudentLeaveRequest.status == "approved",
                    StudentLeaveRequest.end_date >= month_start,
                    StudentLeaveRequest.start_date <= month_end,
                )
                .all()
            )
            advance_filed = False
            leave_dates: list[date] = []
            for lv in leaves:
                # 「事先」定義: created_at.date() < start_date
                if lv.created_at and lv.created_at.date() < lv.start_date:
                    advance_filed = True
                d = max(lv.start_date, month_start)
                end = min(lv.end_date, month_end)
                while d <= end:
                    leave_dates.append(d)
                    d = d + timedelta(days=1)

            L_consecutive = longest_consecutive_workdays(
                leave_dates, holiday_map, makeup_map
            )

            # 取 breakdown:rec.source_template_id 對應的 FeeTemplate
            breakdown = None
            if rec.source_template_id:
                tpl = (
                    session.query(FeeTemplate)
                    .filter(FeeTemplate.id == rec.source_template_id)
                    .first()
                )
                if tpl:
                    breakdown = tpl.breakdown

            return calc_monthly_refund(
                amount_due=rec.amount_due,
                breakdown=breakdown,
                L_consecutive=L_consecutive,
                work_days_in_month=work_days,
                advance_filed=advance_filed,
            )

        # custom / 其他:不提供自動建議
        return {
            "suggested_amount": 0,
            "calc_method": "manual",
            "calc_payload": {
                "fee_type": fee_type,
                "reason": "此類型無自動計算",
            },
            "warnings": ["此費用類型無自動退費規則,請手動填寫"],
        }


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
        # F-034：班級 scope 守衛 — 非 admin/hr/supervisor 不得對他班學生建立退款
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, record.student_id)

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
        # 累積退款簽核（最嚴格）：以同 record 過去已退 + 本次金額判斷，
        # 任一筆讓累積跨閾值即整筆需 ACTIVITY_PAYMENT_APPROVE。
        # Why: 舊版只看本次 amount，會計可拆成多筆 NT$1000 連退繞過簽核。
        prior_refunded = (
            session.query(func.coalesce(func.sum(StudentFeeRefund.amount), 0))
            .filter(StudentFeeRefund.record_id == record_id)
            .scalar()
        ) or 0
        cumulative_refund = int(prior_refunded) + int(payload.amount)
        require_finance_approve(
            cumulative_refund, current_user, action_label="學費累積退款"
        )

        operator = current_user.get("username") or current_user.get("name") or "unknown"

        refund = StudentFeeRefund(
            record_id=record.id,
            amount=payload.amount,
            reason=payload.reason,
            notes=payload.notes or "",
            refunded_by=operator,
            idempotency_key=payload.idempotency_key,
            calc_method=payload.calc_method,
            calc_payload=payload.calc_payload,
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

        # 同交易 outbox：退款的 AuditLog 必須與 StudentFeeRefund 共生死
        write_audit_in_session(
            session,
            request,
            action="UPDATE",
            entity_type="fee",
            entity_id=record_id,
            summary=(
                f"學費退款 {record.period or ''} {student_name_snapshot}: "
                f"NT${payload.amount}（{payload.reason}，by {operator}）"
            ),
            changes={
                "action": "fee_refund",
                "record_id": record_id,
                "student_id": record.student_id,
                "student_name": student_name_snapshot,
                "period": record.period,
                "fee_item_id": record.fee_item_id,
                "paid_before": paid,
                "refund_amount": payload.amount,
                "paid_after": new_paid,
                "amount_due": record.amount_due,
                "status_after": new_status,
                "reason": payload.reason,
                "refund_id": refund.id,
                "cumulative_refund_after": cumulative_refund,
                "idempotency_key": payload.idempotency_key,
                "calc_method": payload.calc_method,
                "calc_payload": payload.calc_payload,
                "operator": operator,
            },
        )

    # session_scope commit 後失效報表快取
    _invalidate_finance_summary_cache()

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
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
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
        # F-034：班級 scope 守衛 — 非 admin/hr/supervisor 不得看他班退款歷史
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, rec.student_id)
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
