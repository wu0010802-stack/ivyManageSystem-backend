"""api/fees 子套件共用常數、Pydantic schemas、helper functions。

Why: 拆 sub-package 後，多個子檔（templates/generation/records/refunds）共用：
- 金額上限與簽核閾值常數
- Pydantic request schemas（schema 跨 endpoint 共用率低，但集中比四散好維護）
- finance_summary 報表快取失效 helper
- 學費紀錄 list/summary 共用的 filter builder
"""

import logging
from datetime import date
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, select, tuple_

from api.activity._shared import validate_payment_date
from models.fees import StudentFeeAdjustment, StudentFeeRecord
from services.report_cache_service import report_cache_service

logger = logging.getLogger(__name__)

# 報表快取 category：任何學費寫入後呼叫 invalidate，避免 /finance-summary 30 分內
# 給舊數字。與 api/activity + api/salary 共用同一 category key。
_FINANCE_SUMMARY_CACHE_CATEGORY = "reports_finance_summary"

# 單筆費用金額上限（避免誤輸入或惡意輸入）
MAX_FEE_AMOUNT = 999_999

# 學費單筆繳款大額簽核閾值(NT$):本次入帳 delta 超過即需 ACTIVITY_PAYMENT_APPROVE。
# 50000 涵蓋一般月費正常區間(月費 NT$10K~30K),學期/年費等大筆才需簽核;
# 與 finance_guards.FINANCE_APPROVAL_THRESHOLD(NT$1000,薪資/退款用)區隔,
# 避免日常收款 100% 觸發簽核堵死流程。閾值可依園所實際收費結構調整。
FEE_PAYMENT_APPROVAL_THRESHOLD = 50_000


from utils.finance_cache import (
    invalidate_finance_summary_cache as _invalidate_finance_summary_cache,
)

# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class FeeTemplateCreate(BaseModel):
    grade_id: int = Field(..., gt=0)
    school_year: int = Field(..., ge=100, le=200)
    semester: int = Field(..., ge=1, le=2)
    fee_type: str = Field(
        ...,
        # 既有：registration/miscellaneous/monthly/material/insurance
        # 新增（紙本對齊）：tuition/transport/summer_uniform/summer_sports
        # 折抵類（sibling_discount/prepayment/leave_deduction）需負金額支援，另開設計
        pattern="^(registration|miscellaneous|monthly|material|insurance|tuition|transport|summer_uniform|summer_sports)$",
    )
    name: str = Field(..., min_length=1, max_length=100)
    # 下限 ge=1：0 元範本無明確業務語意，「該年級該學期免收某類費用」應用
    # is_active=False 表達；保留 ge=0 會在 update 路徑出現「0 → 門檻」單步繞過
    # 守衛的灰色地帶（max rule 對 0 起點仍允許跳到剛好 threshold 一次）。
    amount: int = Field(..., ge=1, le=MAX_FEE_AMOUNT)
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
    # 下限 ge=1：同 FeeTemplateCreate 理由，禁止 amount=0；要免收請改 is_active=False。
    amount: Optional[int] = Field(None, ge=1, le=MAX_FEE_AMOUNT)
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
        allowed = {
            "registration",
            "miscellaneous",
            "monthly",
            "material",
            "insurance",
            "tuition",
            "transport",
            "summer_uniform",
            "summer_sports",
        }
        bad = [t for t in v if t not in allowed]
        if bad:
            raise ValueError(f"非法 fee_type: {bad}")
        return v


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
    student_name: Optional[str] = None,
    student_id: Optional[int] = None,
):
    if period:
        query = query.filter(StudentFeeRecord.period == period)
    if classroom_name:
        query = query.filter(StudentFeeRecord.classroom_name == classroom_name)
    if status:
        query = query.filter(StudentFeeRecord.status == status)
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


def compute_fee_summary(
    session,
    *,
    period: Optional[str] = None,
    classroom_name: Optional[str] = None,
    status: Optional[str] = None,
    student_name: Optional[str] = None,
) -> dict:
    """費用統計摘要的純查詢邏輯（route fee_summary 的可測 seam）。

    折抵（StudentFeeAdjustment）為 per-(student_id, period)，且無 classroom 欄位
    （見 model docstring：該生該學期 total_due = SUM(records) - SUM(adjustments)）。
    因此折抵聚合 scope 至 filtered records 的 (student_id, period) 組合，與
    total_due/total_paid 同口徑——避免「帶班級、不帶 period」時跨該生所有學期/
    他班的折抵累加，使 total_unpaid 被低估、遮蓋欠費。
    """
    q = _apply_fee_record_filters(
        session.query(StudentFeeRecord),
        period=period,
        classroom_name=classroom_name,
        status=status,
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

    # 折抵聚合：scope 至 filtered records 的 (student_id, period) 組合。
    # 用顯式 select(subquery) 而非 coerce ORM Query，避免 row-value IN 的方言/
    # coercion 歧義（PostgreSQL/SQLite 皆原生支援 (a,b) IN (SELECT a,b ...)）。
    record_keys = (
        q.with_entities(StudentFeeRecord.student_id, StudentFeeRecord.period)
        .distinct()
        .subquery()
    )
    # 折抵逐 (student_id, period) 聚合，scope 至 filtered records 的鍵集合。
    adj_rows = (
        session.query(
            StudentFeeAdjustment.student_id,
            StudentFeeAdjustment.period,
            func.coalesce(func.sum(StudentFeeAdjustment.amount), 0),
        )
        .filter(
            tuple_(StudentFeeAdjustment.student_id, StudentFeeAdjustment.period).in_(
                select(record_keys.c.student_id, record_keys.c.period)
            )
        )
        .group_by(StudentFeeAdjustment.student_id, StudentFeeAdjustment.period)
        .all()
    )
    adj_by_key: dict[tuple, int] = {
        (sid, per): int(amt or 0) for sid, per, amt in adj_rows
    }
    total_adjustment = sum(adj_by_key.values())

    # total_unpaid 逐 (student_id, period) 群組計算淨欠費並各自 clamp 至 0 再加總：
    # 全域 max(0, total_due−total_paid−total_adj) 會讓某生溢繳/折抵的負淨額（credit）
    # 抵銷他生真實欠費、遮蓋欠費（qa-loop P3#7）。單一正值群組仍與線性對帳相同。
    group_rows = (
        q.with_entities(
            StudentFeeRecord.student_id,
            StudentFeeRecord.period,
            func.coalesce(func.sum(StudentFeeRecord.amount_due), 0),
            func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0),
        )
        .group_by(StudentFeeRecord.student_id, StudentFeeRecord.period)
        .all()
    )
    total_unpaid = sum(
        max(0, int(g_due or 0) - int(g_paid or 0) - adj_by_key.get((sid, per), 0))
        for sid, per, g_due, g_paid in group_rows
    )

    return {
        "total_count": total_count,
        "paid_count": paid_count,
        "partial_count": partial_count,
        "unpaid_count": total_count - paid_count - partial_count,
        "total_due": total_due,
        "total_paid": total_paid,
        "total_unpaid": total_unpaid,
        "total_adjustment": total_adjustment,
    }
