"""
api/recruitment.py — 招生統計 API endpoints
"""

import logging
import re
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, and_, case, cast, String

from models.base import session_scope
from models.recruitment import RecruitmentVisit, RecruitmentPeriod, RecruitmentMonth
from utils.auth import require_permission
from utils.excel_utils import xlsx_streaming_response
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment"])

# ---------------------------------------------------------------------------
# 常數
# ---------------------------------------------------------------------------

NO_DEPOSIT_REASONS = [
    "時程未到／仍在觀望",
    "已有其他就學選項／比較他校",
    "未註明／待追蹤",
    "距離／地點因素",
    "家庭照顧安排考量",
    "特殊需求／名額限制",
    "課程／環境仍在評估",
    "費用考量",
]

TOP_SOURCES_COUNT = 10   # 接待×來源交叉表顯示最大來源數

# 童年綠地判定關鍵字
_CHUANNIAN_KW = "童年綠地"
_YAOTING_KW   = "班導-雅婷"

# 就讀月份 / 班別 regex
_EXPECTED_MONTH_RE = re.compile(r'(1\d\d)\.(\d{1,2})')
_GRADE_RE          = re.compile(r'(幼幼班|小班|中班|大班)')

# 期間名稱解析 regex："114.09.16~115.03.15" 或 "114.09.16-115.03.15"
_PERIOD_RANGE_RE = re.compile(
    r'(\d{3}\.\d{2})\.\d{2}[~\-](\d{3}\.\d{2})\.\d{2}'
)

# ---------------------------------------------------------------------------
# 純函式 helpers
# ---------------------------------------------------------------------------

def _extract_expected_label_from_text(
    notes: Optional[str],
    parent_response: Optional[str],
    grade: Optional[str],
) -> str:
    """從 notes / parent_response 解析「預計就讀月份＋班別」。
    取最後一個民國年月匹配（通常為最終確認），後向 30 字找班別。
    """
    text = (notes or "") + " " + (parent_response or "")
    matches = list(_EXPECTED_MONTH_RE.finditer(text))
    if not matches:
        return "未知"
    m = matches[-1]
    month_num = int(m.group(2))
    if not (1 <= month_num <= 12):
        return "未知"
    label = f"{m.group(1)}.{month_num:02d}"
    after = text[m.end():m.end() + 30]
    gm = _GRADE_RE.search(after)
    if gm:
        return f"{label} 讀{gm.group(1)}"
    return f"{label} 讀{grade}" if grade else label


def _extract_expected_label(r: RecruitmentVisit) -> str:
    return _extract_expected_label_from_text(r.notes, r.parent_response, r.grade)


def _parse_period_range(period_name: str) -> Optional[tuple]:
    """解析期間名稱，回傳 (start_ym, end_ym)，如 ('114.09', '115.03')。"""
    m = _PERIOD_RANGE_RE.search(period_name.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class RecruitmentVisitCreate(BaseModel):
    month: str = Field(..., min_length=1, max_length=10)
    seq_no: Optional[str] = Field(None, max_length=10)
    visit_date: Optional[str] = Field(None, max_length=50)
    child_name: str = Field(..., min_length=1, max_length=50)
    birthday: Optional[date] = None
    grade: Optional[str] = Field(None, max_length=20)
    phone: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = Field(None, max_length=200)
    district: Optional[str] = Field(None, max_length=30)
    source: Optional[str] = Field(None, max_length=50)
    referrer: Optional[str] = Field(None, max_length=50)
    deposit_collector: Optional[str] = Field(None, max_length=50)
    has_deposit: bool = False
    notes: Optional[str] = None
    parent_response: Optional[str] = None
    no_deposit_reason: Optional[str] = Field(None, max_length=60)
    no_deposit_reason_detail: Optional[str] = None
    enrolled: bool = False
    transfer_term: bool = False

    @field_validator('month')
    @classmethod
    def validate_month_format(cls, v: str) -> str:
        parts = v.strip().split('.')
        if len(parts) != 2:
            raise ValueError('月份格式應為 民國年.月，如 115.03')
        try:
            month_num = int(parts[1])
        except ValueError:
            raise ValueError('月份格式錯誤')
        if not (1 <= month_num <= 12):
            raise ValueError(f'月份須在 1-12 之間，收到 {month_num}')
        return v


class RecruitmentVisitUpdate(BaseModel):
    month: Optional[str] = Field(None, min_length=1, max_length=10)
    seq_no: Optional[str] = Field(None, max_length=10)
    visit_date: Optional[str] = Field(None, max_length=50)
    child_name: Optional[str] = Field(None, min_length=1, max_length=50)
    birthday: Optional[date] = None
    grade: Optional[str] = Field(None, max_length=20)
    phone: Optional[str] = Field(None, max_length=100)
    address: Optional[str] = Field(None, max_length=200)
    district: Optional[str] = Field(None, max_length=30)
    source: Optional[str] = Field(None, max_length=50)
    referrer: Optional[str] = Field(None, max_length=50)
    deposit_collector: Optional[str] = Field(None, max_length=50)
    has_deposit: Optional[bool] = None
    notes: Optional[str] = None
    parent_response: Optional[str] = None
    no_deposit_reason: Optional[str] = Field(None, max_length=60)
    no_deposit_reason_detail: Optional[str] = None
    enrolled: Optional[bool] = None
    transfer_term: Optional[bool] = None

    @field_validator('month')
    @classmethod
    def validate_month_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        parts = v.strip().split('.')
        if len(parts) != 2:
            raise ValueError('月份格式應為 民國年.月，如 115.03')
        try:
            month_num = int(parts[1])
        except ValueError:
            raise ValueError('月份格式錯誤')
        if not (1 <= month_num <= 12):
            raise ValueError(f'月份須在 1-12 之間，收到 {month_num}')
        return v


class ImportRecord(BaseModel):
    月份: Optional[str] = None
    序號: Optional[str] = None
    日期: Optional[str] = None
    幼生姓名: Optional[str] = None
    生日: Optional[str] = None
    適讀班級: Optional[str] = None
    電話: Optional[str] = None
    地址: Optional[str] = None
    行政區: Optional[str] = None
    幼生來源: Optional[str] = None
    介紹者: Optional[str] = None
    收預繳人員: Optional[str] = None
    是否預繳: Optional[str] = None
    備註: Optional[str] = None
    電訪後家長回應: Optional[str] = None


class PeriodCreate(BaseModel):
    period_name: str = Field(..., min_length=1, max_length=50)
    visit_count: int = Field(0, ge=0)
    deposit_count: int = Field(0, ge=0)
    enrolled_count: int = Field(0, ge=0)
    transfer_term_count: int = Field(0, ge=0)
    effective_deposit_count: int = Field(0, ge=0)
    not_enrolled_deposit: int = Field(0, ge=0)
    enrolled_after_school: int = Field(0, ge=0)
    notes: Optional[str] = None
    sort_order: int = 0


class PeriodUpdate(BaseModel):
    period_name: Optional[str] = Field(None, min_length=1, max_length=50)
    visit_count: Optional[int] = Field(None, ge=0)
    deposit_count: Optional[int] = Field(None, ge=0)
    enrolled_count: Optional[int] = Field(None, ge=0)
    transfer_term_count: Optional[int] = Field(None, ge=0)
    effective_deposit_count: Optional[int] = Field(None, ge=0)
    not_enrolled_deposit: Optional[int] = Field(None, ge=0)
    enrolled_after_school: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = None
    sort_order: Optional[int] = None


def _parse_roc_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        parts = s.strip().split(".")
        if len(parts) == 3:
            year = int(parts[0]) + 1911
            return date(year, int(parts[1]), int(parts[2]))
    except (ValueError, AttributeError):
        pass
    return None


# ---------------------------------------------------------------------------
# 基本 CRUD
# ---------------------------------------------------------------------------

@router.get("/records")
def list_recruitment_records(
    month: Optional[str] = Query(None),
    grade: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    referrer: Optional[str] = Query(None),
    has_deposit: Optional[bool] = Query(None),
    no_deposit_reason: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        q = session.query(RecruitmentVisit)
        if month:
            q = q.filter(RecruitmentVisit.month == month)
        if grade:
            q = q.filter(RecruitmentVisit.grade == grade)
        if source:
            q = q.filter(RecruitmentVisit.source == source)
        if referrer:
            q = q.filter(RecruitmentVisit.referrer == referrer)
        if has_deposit is not None:
            q = q.filter(RecruitmentVisit.has_deposit == has_deposit)
        if no_deposit_reason:
            q = q.filter(RecruitmentVisit.no_deposit_reason == no_deposit_reason)
        if keyword:
            kw = f"%{keyword}%"
            q = q.filter(
                RecruitmentVisit.child_name.ilike(kw) |
                RecruitmentVisit.address.ilike(kw) |
                RecruitmentVisit.notes.ilike(kw) |
                RecruitmentVisit.parent_response.ilike(kw)
            )
        total = q.count()
        records = (
            q.order_by(RecruitmentVisit.month.desc(), RecruitmentVisit.seq_no)
             .offset((page - 1) * page_size)
             .limit(page_size)
             .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": [_to_dict(r) for r in records],
        }


@router.post("/records", status_code=201)
def create_recruitment_record(
    payload: RecruitmentVisitCreate,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = RecruitmentVisit(**payload.model_dump())
        record.expected_start_label = _extract_expected_label_from_text(
            record.notes, record.parent_response, record.grade
        )
        session.add(record)
        session.flush()
        return _to_dict(record)


@router.put("/records/{record_id}")
def update_recruitment_record(
    record_id: int,
    payload: RecruitmentVisitUpdate,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = session.query(RecruitmentVisit).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="紀錄不存在")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, field, value)
        record.expected_start_label = _extract_expected_label_from_text(
            record.notes, record.parent_response, record.grade
        )
        record.updated_at = datetime.now()
        session.flush()
        return _to_dict(record)


@router.delete("/records/{record_id}", status_code=204)
def delete_recruitment_record(
    record_id: int,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = session.query(RecruitmentVisit).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="紀錄不存在")
        session.delete(record)


# ---------------------------------------------------------------------------
# 統計（SQL GROUP BY，避免全表 in-memory 聚合）
# ---------------------------------------------------------------------------

def _chuannian_sql_cond():
    """童年綠地 SQL 判定條件（與 Python _is_chuannian 邏輯一致）"""
    return or_(
        RecruitmentVisit.source.contains(_CHUANNIAN_KW),
        RecruitmentVisit.notes.contains(_CHUANNIAN_KW),
        RecruitmentVisit.parent_response.contains(_CHUANNIAN_KW),
        RecruitmentVisit.notes.contains(_YAOTING_KW),
        RecruitmentVisit.parent_response.contains(_YAOTING_KW),
    )


def _query_stats(session) -> dict:
    """執行招生統計所有 SQL 查詢，回傳統計字典（供 /stats 與 /stats/export 共用）。"""
    ch_cond = _chuannian_sql_cond()
    dep_case   = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    ch_case    = case((ch_cond, 1), else_=0)
    ch_dep_case = case((and_(ch_cond, RecruitmentVisit.has_deposit == True), 1), else_=0)

    # ── 1. 整體 KPI（單次查詢）──────────────────────────────────
    kpi = session.query(
        func.count(RecruitmentVisit.id),
        func.sum(dep_case),
        func.sum(ch_case),
        func.sum(ch_dep_case),
    ).one()
    total_visit, total_deposit, chuannian_visit, chuannian_deposit = (
        kpi[0] or 0, kpi[1] or 0, kpi[2] or 0, kpi[3] or 0
    )

    # ── 2. 唯一幼生（child_name + birthday 組合去重，1 次查詢）──
    unique_key = func.coalesce(RecruitmentVisit.child_name, '') + '|' + \
                 func.coalesce(cast(RecruitmentVisit.birthday, String), '')
    dep_unique_key = case((RecruitmentVisit.has_deposit == True, unique_key), else_=None)
    uq_row = session.query(
        func.count(func.distinct(unique_key)),
        func.count(func.distinct(dep_unique_key)),
    ).one()
    unique_visit   = uq_row[0] or 0
    unique_deposit = uq_row[1] or 0

    # ── 3. 月度統計 ─────────────────────────────────────────────
    monthly_rows = session.query(
        func.coalesce(RecruitmentVisit.month, '未知').label('month'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
        func.sum(ch_case).label('chuannian_visit'),
        func.sum(ch_dep_case).label('chuannian_deposit'),
    ).group_by(RecruitmentVisit.month).order_by(RecruitmentVisit.month).all()

    monthly = [
        {
            'month': r.month,
            'visit': r.visit or 0,
            'deposit': r.deposit or 0,
            'chuannian_visit': r.chuannian_visit or 0,
            'chuannian_deposit': r.chuannian_deposit or 0,
        }
        for r in monthly_rows
    ]

    # ── 3b. 年度統計（從 month 欄位取年份前綴）─────────────────
    year_expr = func.split_part(RecruitmentVisit.month, '.', 1)
    year_rows = session.query(
        year_expr.label('year'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
        func.sum(ch_case).label('chuannian_visit'),
        func.sum(ch_dep_case).label('chuannian_deposit'),
    ).filter(
        RecruitmentVisit.month != None,
        RecruitmentVisit.month != '未知',
        RecruitmentVisit.month != '',
    ).group_by(year_expr).order_by(year_expr).all()

    by_year = [
        {
            'year': r.year,
            'visit': r.visit or 0,
            'deposit': r.deposit or 0,
            'chuannian_visit': r.chuannian_visit or 0,
            'chuannian_deposit': r.chuannian_deposit or 0,
        }
        for r in year_rows
    ]

    # ── 4. 班別統計 ─────────────────────────────────────────────
    grade_rows = session.query(
        func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).group_by(RecruitmentVisit.grade).order_by(func.count(RecruitmentVisit.id).desc()).all()

    by_grade = [
        {'grade': r.grade, 'visit': r.visit or 0, 'deposit': r.deposit or 0}
        for r in grade_rows
    ]

    # ── 5. 月份 × 班別 ───────────────────────────────────────────
    mg_rows = session.query(
        func.coalesce(RecruitmentVisit.month, '未知').label('month'),
        func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
        func.count(RecruitmentVisit.id).label('cnt'),
    ).group_by(RecruitmentVisit.month, RecruitmentVisit.grade).all()

    month_grade: dict = {}
    for r in mg_rows:
        m = r.month
        if m not in month_grade:
            month_grade[m] = {}
        month_grade[m][r.grade] = r.cnt
        month_grade[m]['合計'] = month_grade[m].get('合計', 0) + r.cnt

    # ── 6. 來源統計 ──────────────────────────────────────────────
    source_rows = session.query(
        func.coalesce(RecruitmentVisit.source, '未填寫').label('source'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).group_by(RecruitmentVisit.source).order_by(func.count(RecruitmentVisit.id).desc()).all()

    by_source = [
        {'source': r.source, 'visit': r.visit or 0, 'deposit': r.deposit or 0}
        for r in source_rows
    ]

    # ── 7. 接待人員 × 各年級（GROUP BY referrer + grade）─────────
    ref_grade_rows = session.query(
        func.coalesce(RecruitmentVisit.referrer, '未填寫').label('referrer'),
        func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).group_by(RecruitmentVisit.referrer, RecruitmentVisit.grade).all()

    by_referrer: dict = {}
    for r in ref_grade_rows:
        ref = r.referrer
        if ref not in by_referrer:
            by_referrer[ref] = {'referrer': ref, 'visit': 0, 'deposit': 0, 'by_grade': {}}
        by_referrer[ref]['visit'] += r.visit or 0
        by_referrer[ref]['deposit'] += r.deposit or 0
        by_referrer[ref]['by_grade'][r.grade] = {
            'visit': r.visit or 0, 'deposit': r.deposit or 0
        }

    by_referrer_list = sorted(by_referrer.values(), key=lambda x: -x['visit'])

    # ── 8. 接待者 × 來源 交叉表 ──────────────────────────────────
    cross_qrows = session.query(
        func.coalesce(RecruitmentVisit.referrer, '未填寫').label('referrer'),
        func.coalesce(RecruitmentVisit.source, '未填寫').label('source'),
        func.count(RecruitmentVisit.id).label('cnt'),
    ).group_by(RecruitmentVisit.referrer, RecruitmentVisit.source).all()

    source_totals: dict = {}
    _cross_raw: dict = {}
    for r in cross_qrows:
        source_totals[r.source] = source_totals.get(r.source, 0) + r.cnt
        if r.referrer not in _cross_raw:
            _cross_raw[r.referrer] = {}
        _cross_raw[r.referrer][r.source] = r.cnt

    top_source_names = [
        s for s, _ in sorted(source_totals.items(), key=lambda x: -x[1])[:TOP_SOURCES_COUNT]
    ]

    cross_rows_out = sorted(
        [
            {
                'referrer': ref,
                'sources': {s: _cross_raw[ref].get(s, 0) for s in top_source_names},
                'total': sum(_cross_raw[ref].values()),
            }
            for ref in _cross_raw
        ],
        key=lambda x: -x['total'],
    )
    referrer_source_cross = {'referrers': cross_rows_out, 'sources': top_source_names}

    # ── 9. 行政區統計 ────────────────────────────────────────────
    district_rows = session.query(
        func.coalesce(RecruitmentVisit.district, '未填寫').label('district'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).group_by(RecruitmentVisit.district).order_by(func.count(RecruitmentVisit.id).desc()).all()

    by_district = [
        {'district': r.district, 'visit': r.visit or 0, 'deposit': r.deposit or 0}
        for r in district_rows
    ]

    # ── 10. 未預繳原因（GROUP BY reason + grade）─────────────────
    reason_rows = session.query(
        func.coalesce(RecruitmentVisit.no_deposit_reason, '未分類').label('reason'),
        func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
        func.count(RecruitmentVisit.id).label('cnt'),
    ).filter(RecruitmentVisit.has_deposit == False) \
     .group_by(RecruitmentVisit.no_deposit_reason, RecruitmentVisit.grade).all()

    no_deposit_total = session.query(func.count(RecruitmentVisit.id)) \
                              .filter(RecruitmentVisit.has_deposit == False).scalar() or 0

    reason_stats: dict = {}
    for r in reason_rows:
        if r.reason not in reason_stats:
            reason_stats[r.reason] = {'reason': r.reason, 'count': 0, 'by_grade': {}}
        reason_stats[r.reason]['count'] += r.cnt
        reason_stats[r.reason]['by_grade'][r.grade] = r.cnt

    no_deposit_reasons = sorted(reason_stats.values(), key=lambda x: -x['count'])

    def _expected_sort_key(x: dict):
        label = x['expected_month']
        return (1, '') if label == '未知' else (0, label)

    # ── 11. 童年綠地 by expected label（SQL GROUP BY expected_start_label）────
    ch_expected_rows = session.query(
        func.coalesce(RecruitmentVisit.expected_start_label, '未知').label('expected_month'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).filter(ch_cond).group_by(RecruitmentVisit.expected_start_label).all()

    chuannian_by_expected_list = sorted(
        [
            {
                'expected_month': r.expected_month,
                'visit': r.visit or 0,
                'deposit': r.deposit or 0,
            }
            for r in ch_expected_rows
        ],
        key=_expected_sort_key,
    )

    # ── 12. 童年綠地各班別（SQL GROUP BY）───────────────────────
    ch_grade_rows = session.query(
        func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
        func.count(RecruitmentVisit.id).label('visit'),
        func.sum(dep_case).label('deposit'),
    ).filter(ch_cond).group_by(RecruitmentVisit.grade).all()

    chuannian_by_grade = sorted(
        [{'grade': r.grade, 'visit': r.visit or 0, 'deposit': r.deposit or 0}
         for r in ch_grade_rows],
        key=lambda x: -x['visit'],
    )

    return {
        'total_visit': total_visit,
        'total_deposit': total_deposit,
        'unique_visit': unique_visit,
        'unique_deposit': unique_deposit,
        'chuannian_visit': chuannian_visit,
        'chuannian_deposit': chuannian_deposit,
        'monthly': monthly,
        'by_grade': by_grade,
        'month_grade': month_grade,
        'by_source': by_source,
        'by_referrer': by_referrer_list,
        'referrer_source_cross': referrer_source_cross,
        'top_source_names': top_source_names,
        'by_district': by_district,
        'no_deposit_reasons': no_deposit_reasons,
        'no_deposit_total': no_deposit_total,
        'chuannian_by_expected': chuannian_by_expected_list,
        'chuannian_by_grade': chuannian_by_grade,
        'by_year': by_year,
    }


# ---------------------------------------------------------------------------
# 統計 API endpoints
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_recruitment_stats(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """完整統計匯總（全 SQL GROUP BY，效能最佳化版）"""
    with session_scope() as session:
        return _query_stats(session)


_HEADER_FONT  = Font(bold=True, color="FFFFFF")
_HEADER_FILL  = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_TITLE_FONT   = Font(bold=True, size=13)
_CENTER       = Alignment(horizontal="center")


def _hrow(ws, row: int, headers: list[str]) -> None:
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=col, value=h)
        c.font, c.fill, c.alignment = _HEADER_FONT, _HEADER_FILL, _CENTER


def _pct(num: int, den: int) -> str:
    return f"{num / den * 100:.1f}%" if den else "—"


@router.get("/stats/export")
def export_recruitment_stats(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """匯出招生統計 Excel（多頁簽）"""
    with session_scope() as session:
        s = _query_stats(session)

    wb = Workbook()

    # ── Sheet 1：總覽 KPI ─────────────────────────────────────────
    ws = wb.active
    ws.title = "總覽"
    ws.append(["招生統計總覽"])
    ws["A1"].font = _TITLE_FONT
    ws.append([])
    _hrow(ws, 3, ["指標", "數值"])
    kpi_rows = [
        ("總參觀紀錄",         s["total_visit"]),
        ("唯一幼生數",         s["unique_visit"]),
        ("總預繳人數",         s["total_deposit"]),
        ("唯一幼生預繳數",     s["unique_deposit"]),
        ("整體預繳率",         _pct(s["total_deposit"], s["total_visit"])),
        ("唯一幼生預繳率",     _pct(s["unique_deposit"], s["unique_visit"])),
        ("童年綠地參觀人數",   s["chuannian_visit"]),
        ("童年綠地預繳人數",   s["chuannian_deposit"]),
        ("童年綠地預繳率",     _pct(s["chuannian_deposit"], s["chuannian_visit"])),
    ]
    for row in kpi_rows:
        ws.append(list(row))
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 14

    # ── Sheet 2：月度明細 ─────────────────────────────────────────
    ws2 = wb.create_sheet("月度明細")
    _hrow(ws2, 1, ["月份", "參觀人數", "預繳人數", "預繳率", "童年綠地參觀", "童年綠地預繳", "童年綠地預繳率"])
    for r in s["monthly"]:
        ws2.append([
            r["month"],
            r["visit"],
            r["deposit"],
            _pct(r["deposit"], r["visit"]),
            r["chuannian_visit"],
            r["chuannian_deposit"],
            _pct(r["chuannian_deposit"], r["chuannian_visit"]),
        ])
    for col_letter, width in zip("ABCDEFG", [10, 10, 10, 10, 12, 12, 14]):
        ws2.column_dimensions[col_letter].width = width

    # ── Sheet 3：班別分析 ─────────────────────────────────────────
    ws3 = wb.create_sheet("班別分析")
    _hrow(ws3, 1, ["班別", "參觀人數", "預繳人數", "預繳率"])
    for r in s["by_grade"]:
        ws3.append([r["grade"], r["visit"], r["deposit"], _pct(r["deposit"], r["visit"])])
    for col_letter, width in zip("ABCD", [10, 10, 10, 10]):
        ws3.column_dimensions[col_letter].width = width

    # ── Sheet 4：來源分析 ─────────────────────────────────────────
    ws4 = wb.create_sheet("來源分析")
    _hrow(ws4, 1, ["來源", "參觀人數", "預繳人數", "預繳率"])
    for r in s["by_source"]:
        ws4.append([r["source"], r["visit"], r["deposit"], _pct(r["deposit"], r["visit"])])
    ws4.column_dimensions["A"].width = 20
    for col_letter, width in zip("BCD", [10, 10, 10]):
        ws4.column_dimensions[col_letter].width = width

    # ── Sheet 5：接待人員 ─────────────────────────────────────────
    ws5 = wb.create_sheet("接待人員")
    _hrow(ws5, 1, ["接待人員", "參觀人數", "預繳人數", "預繳率"])
    for r in s["by_referrer"]:
        ws5.append([r["referrer"], r["visit"], r["deposit"], _pct(r["deposit"], r["visit"])])
    ws5.column_dimensions["A"].width = 16
    for col_letter, width in zip("BCD", [10, 10, 10]):
        ws5.column_dimensions[col_letter].width = width

    # ── Sheet 6：行政區 ───────────────────────────────────────────
    ws6 = wb.create_sheet("行政區")
    _hrow(ws6, 1, ["行政區", "參觀人數", "預繳人數", "預繳率"])
    for r in s["by_district"]:
        ws6.append([r["district"], r["visit"], r["deposit"], _pct(r["deposit"], r["visit"])])
    ws6.column_dimensions["A"].width = 14
    for col_letter, width in zip("BCD", [10, 10, 10]):
        ws6.column_dimensions[col_letter].width = width

    # ── Sheet 7：未預繳原因 ───────────────────────────────────────
    ws7 = wb.create_sheet("未預繳原因")
    _hrow(ws7, 1, ["原因", "人數"])
    for r in s["no_deposit_reasons"]:
        ws7.append([r["reason"], r["count"]])
    ws7.append(["（合計）", s["no_deposit_total"]])
    ws7.column_dimensions["A"].width = 28
    ws7.column_dimensions["B"].width = 10

    # ── Sheet 8：年度統計 ─────────────────────────────────────────
    ws8 = wb.create_sheet("年度統計")
    _hrow(ws8, 1, ["年份", "參觀人數", "預繳人數", "預繳率", "童年綠地參觀", "童年綠地預繳"])
    for r in s["by_year"]:
        ws8.append([
            f"{r['year']}年",
            r["visit"],
            r["deposit"],
            _pct(r["deposit"], r["visit"]),
            r["chuannian_visit"],
            r["chuannian_deposit"],
        ])
    for col_letter, width in zip("ABCDEF", [10, 10, 10, 10, 12, 12]):
        ws8.column_dimensions[col_letter].width = width

    return xlsx_streaming_response(wb, "招生統計.xlsx")


@router.get("/no-deposit-analysis")
def get_no_deposit_analysis(
    reason: Optional[str] = Query(None),
    grade: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """未預繳名單明細（含原因分類篩選，支援分頁）"""
    with session_scope() as session:
        q = session.query(RecruitmentVisit).filter(
            RecruitmentVisit.has_deposit == False
        )
        if reason:
            q = q.filter(RecruitmentVisit.no_deposit_reason == reason)
        if grade:
            q = q.filter(RecruitmentVisit.grade == grade)
        total = q.count()
        records = (
            q.order_by(RecruitmentVisit.month, RecruitmentVisit.seq_no)
             .offset((page - 1) * page_size)
             .limit(page_size)
             .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": [_to_dict(r) for r in records],
        }


# ---------------------------------------------------------------------------
# 近五年期間轉換整合
# ---------------------------------------------------------------------------

@router.get("/periods/summary")
def get_periods_summary(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """近五年整體量體 KPI + 班別轉換分析"""
    with session_scope() as session:
        periods = session.query(RecruitmentPeriod).order_by(RecruitmentPeriod.sort_order).all()
        if not periods:
            return {
                "total_visit": 0, "total_deposit": 0, "total_enrolled": 0,
                "total_effective": 0, "total_transfer_term": 0,
                "total_not_enrolled_deposit": 0, "total_enrolled_after_school": 0,
                "visit_to_deposit_rate": 0, "visit_to_enrolled_rate": 0,
                "deposit_to_enrolled_rate": 0, "effective_to_enrolled_rate": 0,
                "period_count": 0,
                "best_visit_to_enrolled": None, "worst_visit_to_enrolled": None,
                "best_deposit_to_enrolled": None, "worst_deposit_to_enrolled": None,
                "trend": [], "by_grade": [],
            }

        def _pct(num, den):
            return round(num / den * 100, 1) if den else 0

        tv   = sum(p.visit_count or 0 for p in periods)
        td   = sum(p.deposit_count or 0 for p in periods)
        te   = sum(p.enrolled_count or 0 for p in periods)
        teff = sum(p.effective_deposit_count or 0 for p in periods)
        ttr  = sum(p.transfer_term_count or 0 for p in periods)

        trend = [
            {
                "period_name": p.period_name,
                "visit_count": p.visit_count or 0,
                "deposit_count": p.deposit_count or 0,
                "enrolled_count": p.enrolled_count or 0,
                "visit_to_deposit_rate": _pct(p.deposit_count or 0, p.visit_count or 0),
                "visit_to_enrolled_rate": _pct(p.enrolled_count or 0, p.visit_count or 0),
                "deposit_to_enrolled_rate": _pct(p.enrolled_count or 0, p.deposit_count or 0),
                "effective_to_enrolled_rate": _pct(p.enrolled_count or 0, p.effective_deposit_count or 0),
            }
            for p in periods
        ]

        active = [d for d in trend if d["visit_count"] > 0]
        best_v2e  = max(active, key=lambda x: x["visit_to_enrolled_rate"])  if active else None
        worst_v2e = min(active, key=lambda x: x["visit_to_enrolled_rate"])  if active else None
        best_d2e  = max(active, key=lambda x: x["deposit_to_enrolled_rate"]) if active else None
        worst_d2e = min(active, key=lambda x: x["deposit_to_enrolled_rate"]) if active else None

        # 班別轉換（從 RecruitmentVisit）
        dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
        grade_rows = session.query(
            func.coalesce(RecruitmentVisit.grade, '未填寫').label('grade'),
            func.count(RecruitmentVisit.id).label('visit'),
            func.sum(dep_case).label('deposit'),
            func.sum(case((RecruitmentVisit.enrolled == True, 1), else_=0)).label('enrolled'),
        ).group_by(RecruitmentVisit.grade).all()

        grade_order = ["幼幼班", "小班", "中班", "大班"]

        def _grade_rates(r) -> dict:
            v, dep, enr = r.visit or 0, r.deposit or 0, r.enrolled or 0
            return {
                "grade": r.grade,
                "visit": v, "deposit": dep, "enrolled": enr,
                "visit_to_deposit_rate": _pct(dep, v),
                "visit_to_enrolled_rate": _pct(enr, v),
                "deposit_to_enrolled_rate": _pct(enr, dep),
            }

        by_grade_list = sorted(
            [_grade_rates(r) for r in grade_rows],
            key=lambda x: grade_order.index(x["grade"]) if x["grade"] in grade_order else 99,
        )

        return {
            "total_visit": tv, "total_deposit": td, "total_enrolled": te,
            "total_effective": teff, "total_transfer_term": ttr,
            "total_not_enrolled_deposit": sum(p.not_enrolled_deposit or 0 for p in periods),
            "total_enrolled_after_school": sum(p.enrolled_after_school or 0 for p in periods),
            "visit_to_deposit_rate": _pct(td, tv),
            "visit_to_enrolled_rate": _pct(te, tv),
            "deposit_to_enrolled_rate": _pct(te, td),
            "effective_to_enrolled_rate": _pct(te, teff),
            "period_count": len(periods),
            "best_visit_to_enrolled":  {"period": best_v2e["period_name"],  "rate": best_v2e["visit_to_enrolled_rate"]}  if best_v2e  else None,
            "worst_visit_to_enrolled": {"period": worst_v2e["period_name"], "rate": worst_v2e["visit_to_enrolled_rate"]} if worst_v2e else None,
            "best_deposit_to_enrolled":  {"period": best_d2e["period_name"],  "rate": best_d2e["deposit_to_enrolled_rate"]}  if best_d2e  else None,
            "worst_deposit_to_enrolled": {"period": worst_d2e["period_name"], "rate": worst_d2e["deposit_to_enrolled_rate"]} if worst_d2e else None,
            "trend": trend,
            "by_grade": by_grade_list,
        }


@router.get("/periods")
def list_periods(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        periods = session.query(RecruitmentPeriod).order_by(RecruitmentPeriod.sort_order).all()
        return [_period_to_dict(p) for p in periods]


@router.post("/periods", status_code=201)
def create_period(
    payload: PeriodCreate,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        existing = session.query(RecruitmentPeriod).filter_by(period_name=payload.period_name).first()
        if existing:
            raise HTTPException(status_code=409, detail="期間名稱已存在")
        p = RecruitmentPeriod(**payload.model_dump(), created_at=datetime.now(), updated_at=datetime.now())
        session.add(p)
        session.flush()
        return _period_to_dict(p)


@router.put("/periods/{period_id}")
def update_period(
    period_id: int,
    payload: PeriodUpdate,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(p, field, value)
        p.updated_at = datetime.now()
        session.flush()
        return _period_to_dict(p)


@router.delete("/periods/{period_id}", status_code=204)
def delete_period(
    period_id: int,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")
        session.delete(p)


@router.post("/periods/{period_id}/sync")
def sync_period_from_visits(
    period_id: int,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    """從訪視明細自動計算並更新指定期間的統計數字（依期間名稱解析月份範圍）"""
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")

        period_range = _parse_period_range(p.period_name)
        if not period_range:
            raise HTTPException(
                status_code=400,
                detail=f"無法從期間名稱解析日期範圍：{p.period_name}，格式應為 111.03.16~111.09.15",
            )

        start_ym, end_ym = period_range
        dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)

        row = session.query(
            func.count(RecruitmentVisit.id).label('visit_count'),
            func.sum(dep_case).label('deposit_count'),
            func.sum(case((RecruitmentVisit.enrolled == True, 1), else_=0)).label('enrolled_count'),
            func.sum(case((RecruitmentVisit.transfer_term == True, 1), else_=0)).label('transfer_term_count'),
        ).filter(
            RecruitmentVisit.month >= start_ym,
            RecruitmentVisit.month <= end_ym,
        ).one()

        visit     = row.visit_count or 0
        deposit   = row.deposit_count or 0
        enrolled  = row.enrolled_count or 0
        transfer  = row.transfer_term_count or 0
        effective = max(deposit - transfer, 0)

        p.visit_count            = visit
        p.deposit_count          = deposit
        p.enrolled_count         = enrolled
        p.transfer_term_count    = transfer
        p.effective_deposit_count = effective
        p.updated_at             = datetime.now()

        logger.info(
            f"期間 [{p.period_name}] 已同步：參觀={visit} 預繳={deposit} "
            f"註冊={enrolled} 轉期={transfer} 有效預繳={effective}"
        )
        return _period_to_dict(p)


# ---------------------------------------------------------------------------
# 選項 & 批次匯入
# ---------------------------------------------------------------------------

@router.get("/options")
def get_recruitment_options(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """篩選用選項（1 次全表掃描，Python 去重排序）"""
    with session_scope() as session:
        rows = session.query(
            RecruitmentVisit.month,
            RecruitmentVisit.grade,
            RecruitmentVisit.source,
            RecruitmentVisit.referrer,
        ).all()

        months_set:    set = set()
        grades_set:    set = set()
        sources_set:   set = set()
        referrers_set: set = set()
        for r in rows:
            if r.month:    months_set.add(r.month)
            if r.grade:    grades_set.add(r.grade)
            if r.source:   sources_set.add(r.source)
            if r.referrer: referrers_set.add(r.referrer)

        # 合併手動登記月份
        registered = {r.month for r in session.query(RecruitmentMonth.month).all()}
        months_set |= registered

        return {
            "months":    sorted(months_set),
            "grades":    sorted(grades_set),
            "sources":   sorted(sources_set),
            "referrers": sorted(referrers_set),
            "no_deposit_reasons": NO_DEPOSIT_REASONS,
        }


# ---------------------------------------------------------------------------
# 月份管理
# ---------------------------------------------------------------------------

class MonthCreate(BaseModel):
    month: str = Field(..., min_length=1, max_length=10)

    @field_validator('month')
    @classmethod
    def validate_month_format(cls, v: str) -> str:
        parts = v.strip().split('.')
        if len(parts) != 2:
            raise ValueError('月份格式應為 民國年.月，如 115.04')
        try:
            month_num = int(parts[1])
        except ValueError:
            raise ValueError('月份格式錯誤')
        if not (1 <= month_num <= 12):
            raise ValueError(f'月份須在 1-12 之間，收到 {month_num}')
        return v.strip()


@router.get("/months")
def list_months(
    _=Depends(require_permission(Permission.RECRUITMENT_READ)),
):
    """列出所有已手動登記的月份"""
    with session_scope() as session:
        rows = session.query(RecruitmentMonth).order_by(RecruitmentMonth.month).all()
        return [{"id": r.id, "month": r.month} for r in rows]


@router.post("/months", status_code=201)
def create_month(
    payload: MonthCreate,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    """手動登記一個月份（若已有訪視記錄仍可登記，無重複效果）"""
    with session_scope() as session:
        existing = session.query(RecruitmentMonth).filter_by(month=payload.month).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"月份 {payload.month} 已存在")
        rec = RecruitmentMonth(month=payload.month)
        session.add(rec)
        session.flush()
        logger.info(f"手動登記月份：{payload.month}")
        return {"id": rec.id, "month": rec.month}


@router.delete("/months/{month}")
def delete_month(
    month: str,
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    """刪除手動登記月份（不影響該月份的訪視記錄）"""
    with session_scope() as session:
        rec = session.query(RecruitmentMonth).filter_by(month=month).first()
        if not rec:
            raise HTTPException(status_code=404, detail=f"登記月份 {month} 不存在")
        session.delete(rec)
        logger.info(f"刪除登記月份：{month}")
        return {"deleted": month}


@router.post("/import", status_code=201)
def import_recruitment_records(
    records: List[ImportRecord],
    _=Depends(require_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        existing = set(
            (r.child_name, r.month)
            for r in session.query(
                RecruitmentVisit.child_name, RecruitmentVisit.month
            ).all()
        )
        inserted = 0
        skipped = 0
        for rec in records:
            name  = (rec.幼生姓名 or "").strip()
            month = (rec.月份 or "").strip()
            if not name or not month:
                skipped += 1
                continue
            if (name, month) in existing:
                skipped += 1
                continue
            visit = RecruitmentVisit(
                month=month,
                seq_no=rec.序號,
                visit_date=rec.日期,
                child_name=name,
                birthday=_parse_roc_date(rec.生日),
                grade=rec.適讀班級,
                phone=rec.電話,
                address=rec.地址,
                district=rec.行政區,
                source=rec.幼生來源,
                referrer=rec.介紹者,
                deposit_collector=rec.收預繳人員,
                has_deposit=(rec.是否預繳 == "是"),
                notes=rec.備註,
                parent_response=rec.電訪後家長回應,
            )
            visit.expected_start_label = _extract_expected_label_from_text(
                visit.notes, visit.parent_response, visit.grade
            )
            session.add(visit)
            existing.add((name, month))
            inserted += 1
        logger.info(f"招生資料匯入：插入 {inserted} 筆，跳過 {skipped} 筆")
        return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_dict(r: RecruitmentVisit) -> dict:
    return {
        "id": r.id,
        "month": r.month,
        "seq_no": r.seq_no,
        "visit_date": r.visit_date,
        "child_name": r.child_name,
        "birthday": r.birthday.isoformat() if r.birthday else None,
        "grade": r.grade,
        "phone": r.phone,
        "address": r.address,
        "district": r.district,
        "source": r.source,
        "referrer": r.referrer,
        "deposit_collector": r.deposit_collector,
        "has_deposit": r.has_deposit,
        "notes": r.notes,
        "parent_response": r.parent_response,
        "no_deposit_reason": r.no_deposit_reason,
        "no_deposit_reason_detail": r.no_deposit_reason_detail,
        "enrolled": r.enrolled,
        "transfer_term": r.transfer_term,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _period_to_dict(p: RecruitmentPeriod) -> dict:
    visit    = p.visit_count or 0
    deposit  = p.deposit_count or 0
    enrolled = p.enrolled_count or 0
    effective = p.effective_deposit_count or 0

    def _pct(n, d):
        return round(n / d * 100, 1) if d else 0

    return {
        "id": p.id,
        "period_name": p.period_name,
        "visit_count": visit,
        "deposit_count": deposit,
        "enrolled_count": enrolled,
        "transfer_term_count": p.transfer_term_count or 0,
        "effective_deposit_count": effective,
        "not_enrolled_deposit": p.not_enrolled_deposit or 0,
        "enrolled_after_school": p.enrolled_after_school or 0,
        "notes": p.notes,
        "sort_order": p.sort_order or 0,
        # 回傳原始數值（百分比）供前端直接顯示加 %
        "visit_to_deposit_rate": _pct(deposit, visit),
        "visit_to_enrolled_rate": _pct(enrolled, visit),
        "deposit_to_enrolled_rate": _pct(enrolled, deposit),
        "effective_to_enrolled_rate": _pct(enrolled, effective),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
