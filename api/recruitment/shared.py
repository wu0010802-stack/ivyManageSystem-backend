"""招生模組共用常數、helpers、Pydantic schemas。

供 api/recruitment/ 套件下所有 sub-router 共用。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, case, cast, func, or_, String

from models.recruitment import (
    RecruitmentGeocodeCache,
    RecruitmentMonth,
    RecruitmentPeriod,
    RecruitmentVisit,
)
from services import recruitment_market_intelligence as market_service
from utils.roc_month_utils import (
    PERIOD_RANGE_RE as _PERIOD_RANGE_RE,
    VISIT_DATE_MONTH_RE as _VISIT_DATE_MONTH_RE,
    extract_roc_month_from_visit_date as _extract_roc_month_from_visit_date,
    normalize_roc_month as _normalize_roc_month,
    parse_roc_month_parts as _parse_roc_month_parts,
    roc_month_sort_key as _roc_month_sort_key,
    roc_month_start as _roc_month_start,
    safe_normalize_roc_month as _safe_normalize_roc_month,
    shift_roc_month as _shift_roc_month,
)

logger = logging.getLogger(__name__)

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

TOP_SOURCES_COUNT = 10
ADDRESS_SYNC_MODES = {"incremental", "resync_google"}
HOTSPOT_CACHE_LOOKUP_CHUNK_SIZE = 200
DATASET_SCOPE_ALL = "all"

HIGH_PRIORITY_NO_DEPOSIT_REASONS = {
    "時程未到／仍在觀望",
    "課程／環境仍在評估",
}
MEDIUM_PRIORITY_NO_DEPOSIT_REASONS = {
    "距離／地點因素",
    "費用考量",
    "家庭照顧安排考量",
}
LOW_PRIORITY_NO_DEPOSIT_REASONS = {
    "已有其他就學選項／比較他校",
    "特殊需求／名額限制",
}
NO_DEPOSIT_PRIORITY_REASON_MAP = {
    "high": HIGH_PRIORITY_NO_DEPOSIT_REASONS,
    "medium": MEDIUM_PRIORITY_NO_DEPOSIT_REASONS,
    "low": LOW_PRIORITY_NO_DEPOSIT_REASONS,
}
FUNNEL_DROP_THRESHOLD = 10.0
HIGH_POTENTIAL_BACKLOG_THRESHOLD = 5
DEFAULT_OVERDUE_DAYS = 14
COLD_LEAD_DAYS = 90
SOURCE_IMBALANCE_SHARE_THRESHOLD = 40.0
ACTION_QUEUE_LIMIT = 3

_CHUANNIAN_KW = "童年綠地"
_YAOTING_KW = "班導-雅婷"
_BRANCH_INTRO_KW = "分校介紹"
_SOURCE_GROUP_ALIASES = {
    _CHUANNIAN_KW: {"二人同行", "七人同行", "9人同行", "九人同行", "Ruby老師"},
    _BRANCH_INTRO_KW: {
        "國際校介紹",
        "仁武校介紹",
        "明華校介紹",
        "崇德校介紹",
        _BRANCH_INTRO_KW,
    },
}

_EXPECTED_MONTH_RE = re.compile(r"(1\d\d)\.(\d{1,2})")
_GRADE_RE = re.compile(r"(幼幼班|小班|中班|大班)")


# ---------------------------------------------------------------------------
# 純函式 helpers
# ---------------------------------------------------------------------------


def _extract_expected_label_from_text(
    notes: Optional[str],
    parent_response: Optional[str],
    grade: Optional[str],
) -> str:
    """從 notes / parent_response 解析「預計就讀月份＋班別」。"""
    text = (notes or "") + " " + (parent_response or "")
    matches = list(_EXPECTED_MONTH_RE.finditer(text))
    if not matches:
        return "未知"
    m = matches[-1]
    month_num = int(m.group(2))
    if not (1 <= month_num <= 12):
        return "未知"
    label = f"{m.group(1)}.{month_num:02d}"
    after = text[m.end() : m.end() + 30]
    gm = _GRADE_RE.search(after)
    if gm:
        return f"{label} 讀{gm.group(1)}"
    return f"{label} 讀{grade}" if grade else label


def _extract_expected_label(r: RecruitmentVisit) -> str:
    return _extract_expected_label_from_text(r.notes, r.parent_response, r.grade)


def _clean_source_label(source: Optional[str]) -> str:
    text = re.sub(r"\s+", " ", (source or "").strip())
    return text or "未填寫"


def _normalize_source_label(source: Optional[str]) -> str:
    label = _clean_source_label(source)
    if label.startswith(_CHUANNIAN_KW):
        return _CHUANNIAN_KW
    if label == _BRANCH_INTRO_KW or label in _SOURCE_GROUP_ALIASES[_BRANCH_INTRO_KW]:
        return _BRANCH_INTRO_KW
    return label


def _chuannian_sql_cond():
    """童年綠地 SQL 判定條件（與 Python _is_chuannian 邏輯一致）"""
    return or_(
        RecruitmentVisit.source.contains(_CHUANNIAN_KW),
        RecruitmentVisit.notes.contains(_CHUANNIAN_KW),
        RecruitmentVisit.parent_response.contains(_CHUANNIAN_KW),
        RecruitmentVisit.notes.contains(_YAOTING_KW),
        RecruitmentVisit.parent_response.contains(_YAOTING_KW),
    )


def _branch_intro_sql_cond():
    return RecruitmentVisit.source.in_(
        tuple(sorted(_SOURCE_GROUP_ALIASES[_BRANCH_INTRO_KW]))
    )


def _source_group_sql_expr():
    return case(
        (_chuannian_sql_cond(), _CHUANNIAN_KW),
        (_branch_intro_sql_cond(), _BRANCH_INTRO_KW),
        else_=func.coalesce(RecruitmentVisit.source, "未填寫"),
    )


def _normalize_dataset_scope(dataset_scope: Optional[str]) -> str:
    return DATASET_SCOPE_ALL


def _dataset_scope_filters(dataset_scope: Optional[str]) -> list:
    return []


def _build_source_filter_condition(source: Optional[str]):
    label = _clean_source_label(source)
    if label == _CHUANNIAN_KW:
        return _chuannian_sql_cond()
    if label == _BRANCH_INTRO_KW:
        return _branch_intro_sql_cond()
    return RecruitmentVisit.source == label


def _parse_period_range(period_name: str) -> Optional[tuple]:
    """解析期間名稱，回傳 (start_ym, end_ym)，如 ('114.09', '115.03')。"""
    m = _PERIOD_RANGE_RE.search(period_name.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _metric_snapshot(
    visit: int = 0,
    deposit: int = 0,
    enrolled: int = 0,
    transfer_term: int = 0,
    pending_deposit: int = 0,
    effective_deposit: int = 0,
) -> dict:
    def _pct_value(num: int, den: int) -> float:
        return round(num / den * 100, 1) if den else 0

    return {
        "visit": visit,
        "deposit": deposit,
        "enrolled": enrolled,
        "transfer_term": transfer_term,
        "pending_deposit": pending_deposit,
        "effective_deposit": effective_deposit,
        "visit_to_deposit_rate": _pct_value(deposit, visit),
        "visit_to_enrolled_rate": _pct_value(enrolled, visit),
        "deposit_to_enrolled_rate": _pct_value(enrolled, deposit),
        "effective_to_enrolled_rate": _pct_value(enrolled, effective_deposit),
    }


def _empty_snapshot() -> dict:
    return _metric_snapshot()


def _aggregate_snapshot(session, *filters) -> dict:
    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    enrolled_case = case((RecruitmentVisit.enrolled == True, 1), else_=0)
    transfer_case = case((RecruitmentVisit.transfer_term == True, 1), else_=0)
    pending_dep_case = case(
        (
            and_(
                RecruitmentVisit.has_deposit == True,
                RecruitmentVisit.enrolled == False,
                RecruitmentVisit.transfer_term == False,
            ),
            1,
        ),
        else_=0,
    )
    effective_dep_case = case(
        (
            and_(
                RecruitmentVisit.has_deposit == True,
                RecruitmentVisit.transfer_term == False,
            ),
            1,
        ),
        else_=0,
    )

    query = session.query(
        func.count(RecruitmentVisit.id),
        func.sum(dep_case),
        func.sum(enrolled_case),
        func.sum(transfer_case),
        func.sum(pending_dep_case),
        func.sum(effective_dep_case),
    )
    applied_filters = [item for item in filters if item is not None]
    if applied_filters:
        query = query.filter(*applied_filters)

    row = query.one()
    return _metric_snapshot(
        visit=row[0] or 0,
        deposit=row[1] or 0,
        enrolled=row[2] or 0,
        transfer_term=row[3] or 0,
        pending_deposit=row[4] or 0,
        effective_deposit=row[5] or 0,
    )


def _select_reference_month(
    monthly: list[dict], requested: Optional[str]
) -> Optional[str]:
    if requested:
        return _normalize_roc_month(requested)
    if not monthly:
        return None
    return _safe_normalize_roc_month(monthly[-1]["month"])


def _build_month_over_month(
    current_month: Optional[str],
    previous_month: Optional[str],
    monthly_map: dict[str, dict],
) -> dict:
    current_row = (
        monthly_map.get(current_month, _empty_snapshot())
        if current_month
        else _empty_snapshot()
    )
    previous_row = (
        monthly_map.get(previous_month, _empty_snapshot())
        if previous_month
        else _empty_snapshot()
    )
    tracked_fields = [
        "visit",
        "deposit",
        "enrolled",
        "effective_deposit",
        "pending_deposit",
        "visit_to_deposit_rate",
        "visit_to_enrolled_rate",
        "deposit_to_enrolled_rate",
        "effective_to_enrolled_rate",
    ]
    diff = {
        field: {
            "current": current_row.get(field, 0),
            "previous": previous_row.get(field, 0),
            "delta": round(
                (current_row.get(field, 0) or 0) - (previous_row.get(field, 0) or 0), 1
            ),
        }
        for field in tracked_fields
    }
    return {
        "current_month": current_month,
        "previous_month": previous_month,
        **diff,
    }


def _build_ytd_snapshot(
    reference_month: Optional[str], monthly_map: dict[str, dict]
) -> dict:
    if not reference_month:
        return _empty_snapshot()

    ref_year, ref_month = _parse_roc_month_parts(reference_month)
    totals = _empty_snapshot()
    for label, snapshot in monthly_map.items():
        try:
            year_num, month_num = _parse_roc_month_parts(label)
        except ValueError:
            continue
        if year_num != ref_year or month_num > ref_month:
            continue
        for field in (
            "visit",
            "deposit",
            "enrolled",
            "transfer_term",
            "pending_deposit",
            "effective_deposit",
        ):
            totals[field] += snapshot.get(field, 0) or 0

    return _metric_snapshot(
        visit=totals["visit"],
        deposit=totals["deposit"],
        enrolled=totals["enrolled"],
        transfer_term=totals["transfer_term"],
        pending_deposit=totals["pending_deposit"],
        effective_deposit=totals["effective_deposit"],
    )


def _build_alerts(
    month_over_month: dict,
    high_potential_backlog_count: int,
    source_imbalance: Optional[dict],
    reference_month: Optional[str],
) -> list[dict]:
    alerts: list[dict] = []

    visit_to_deposit_delta = month_over_month.get("visit_to_deposit_rate", {}).get(
        "delta", 0
    )
    visit_to_enrolled_delta = month_over_month.get("visit_to_enrolled_rate", {}).get(
        "delta", 0
    )
    if (
        visit_to_deposit_delta <= -FUNNEL_DROP_THRESHOLD
        or visit_to_enrolled_delta <= -FUNNEL_DROP_THRESHOLD
    ):
        alerts.append(
            {
                "code": "FUNNEL_DROP",
                "level": "warning",
                "title": "本月漏斗轉換下滑",
                "message": (
                    f"{reference_month or '當期'} 參觀轉預繳 {visit_to_deposit_delta:.1f} 個百分點，"
                    f"參觀轉註冊 {visit_to_enrolled_delta:.1f} 個百分點。"
                ),
                "target_tab": "detail",
                "target_filter": {"month": reference_month},
            }
        )

    if high_potential_backlog_count >= HIGH_POTENTIAL_BACKLOG_THRESHOLD:
        alerts.append(
            {
                "code": "HIGH_POTENTIAL_BACKLOG",
                "level": "danger",
                "title": "高潛力未預繳名單堆積",
                "message": f"超過 {DEFAULT_OVERDUE_DAYS} 天仍未預繳的高潛力名單有 {high_potential_backlog_count} 筆。",
                "target_tab": "nodeposit",
                "target_filter": {
                    "priority": "high",
                    "overdue_days": DEFAULT_OVERDUE_DAYS,
                },
            }
        )

    if source_imbalance:
        alerts.append(
            {
                "code": "SOURCE_IMBALANCE",
                "level": "info",
                "title": "來源結構失衡",
                "message": (
                    f"{source_imbalance['source']} 近 90 天占比 {source_imbalance['share']:.1f}% ，"
                    f"預繳率 {source_imbalance['deposit_rate']:.1f}% 低於整體 {source_imbalance['overall_rate']:.1f}%。"
                ),
                "target_tab": "area",
                "target_filter": {"source": source_imbalance["source"]},
            }
        )

    return alerts


def _build_action_queue(
    current_month: Optional[str],
    high_potential_backlog_count: int,
    dominant_district: Optional[str],
    source_imbalance: Optional[dict],
) -> list[dict]:
    actions: list[dict] = []

    if high_potential_backlog_count:
        actions.append(
            {
                "code": "FOLLOW_HIGH_POTENTIAL",
                "title": "查看高風險未預繳",
                "description": f"目前有 {high_potential_backlog_count} 筆高潛力名單逾期未追。",
                "target_tab": "nodeposit",
                "target_filter": {
                    "priority": "high",
                    "overdue_days": DEFAULT_OVERDUE_DAYS,
                },
            }
        )

    if current_month:
        actions.append(
            {
                "code": "REVIEW_CURRENT_MONTH",
                "title": "查看本月明細",
                "description": f"切換到 {current_month} 明細，檢查本月漏斗掉點。",
                "target_tab": "detail",
                "target_filter": {"month": current_month},
            }
        )

    if dominant_district or source_imbalance:
        target_filter = {}
        if dominant_district:
            target_filter["district"] = dominant_district
        if source_imbalance:
            target_filter["source"] = source_imbalance["source"]
        actions.append(
            {
                "code": "AREA_OPPORTUNITY",
                "title": "查看區域機會",
                "description": f"優先檢查 {dominant_district or '重點行政區'} 的來源分布與通勤熱區。",
                "target_tab": "area",
                "target_filter": target_filter,
            }
        )

    return actions[:ACTION_QUEUE_LIMIT]


def _find_source_imbalance(session, window_start: datetime, *filters) -> Optional[dict]:
    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    source_group_expr = _source_group_sql_expr().label("source")
    rows = (
        session.query(
            source_group_expr,
            func.count(RecruitmentVisit.id).label("visit"),
            func.sum(dep_case).label("deposit"),
        )
        .filter(*filters, RecruitmentVisit.created_at >= window_start)
        .group_by(source_group_expr)
        .all()
    )
    normalized_rows = [
        {"source": row.source, "visit": row.visit or 0, "deposit": row.deposit or 0}
        for row in rows
    ]

    total_visit = sum((row["visit"] or 0) for row in normalized_rows)
    total_deposit = sum((row["deposit"] or 0) for row in normalized_rows)
    overall_rate = round(total_deposit / total_visit * 100, 1) if total_visit else 0
    candidate: Optional[dict] = None
    for row in normalized_rows:
        visit = row["visit"] or 0
        if not visit or not total_visit:
            continue
        share = round(visit / total_visit * 100, 1)
        deposit = row["deposit"] or 0
        deposit_rate = round(deposit / visit * 100, 1) if visit else 0
        if share >= SOURCE_IMBALANCE_SHARE_THRESHOLD and deposit_rate < overall_rate:
            current = {
                "source": row["source"],
                "visit": visit,
                "deposit": deposit,
                "share": share,
                "deposit_rate": deposit_rate,
                "overall_rate": overall_rate,
            }
            if candidate is None or current["share"] > candidate["share"]:
                candidate = current
    return candidate


def _extract_district_from_address(address: Optional[str]) -> Optional[str]:
    """從完整地址盡量提取行政區，例如「高雄市三民區民族一路...」→「三民區」"""
    if not address:
        return None

    text = address.strip()
    if not text:
        return None

    match = re.search(r"[縣市]([一-龥]{1,4}區)", text)
    if match:
        return match.group(1)

    fallback = re.search(r"([一-龥]{1,4}區)", text)
    return fallback.group(1) if fallback else None


def _expand_roc_month_range(start_ym: str, end_ym: str) -> set[str]:
    start_normalized = _normalize_roc_month(start_ym)
    end_normalized = _normalize_roc_month(end_ym)
    start_year, start_month = map(int, start_normalized.split("."))
    end_year, end_month = map(int, end_normalized.split("."))

    cursor_year, cursor_month = start_year, start_month
    labels: set[str] = set()
    while (cursor_year, cursor_month) <= (end_year, end_month):
        labels.add(f"{cursor_year}.{cursor_month:02d}")
        labels.add(f"{cursor_year}.{cursor_month}")
        if cursor_month == 12:
            cursor_year += 1
            cursor_month = 1
        else:
            cursor_month += 1
    return labels


def _build_period_month_labels(periods: list[RecruitmentPeriod]) -> list[str]:
    labels: set[str] = set()
    for period in periods:
        period_range = _parse_period_range(period.period_name)
        if not period_range:
            logger.warning("略過無法解析的招生期間：%s", period.period_name)
            continue
        labels |= _expand_roc_month_range(*period_range)
    return sorted(labels, key=_roc_month_sort_key)


def _normalize_hotspot_sync_mode(sync_mode: str) -> str:
    normalized = (sync_mode or "").strip().lower() or "incremental"
    if normalized not in ADDRESS_SYNC_MODES:
        raise HTTPException(
            status_code=400, detail="sync_mode 僅支援 incremental 或 resync_google"
        )
    return normalized


def _is_google_stale_cache(cached: Optional[RecruitmentGeocodeCache]) -> bool:
    return market_service.is_google_stale_cache_row(cached)


def _needs_incremental_sync(cached: Optional[RecruitmentGeocodeCache]) -> bool:
    if not cached:
        return True
    if cached.status in {"pending", "failed"}:
        return True
    return cached.status == "resolved" and (cached.lat is None or cached.lng is None)


def _load_hotspot_cache_rows(
    session, addresses: list[str]
) -> dict[str, RecruitmentGeocodeCache]:
    cache_rows: dict[str, RecruitmentGeocodeCache] = {}
    deduped_addresses = list(dict.fromkeys(addresses))
    if not deduped_addresses:
        return cache_rows

    for index in range(0, len(deduped_addresses), HOTSPOT_CACHE_LOOKUP_CHUNK_SIZE):
        chunk = deduped_addresses[index : index + HOTSPOT_CACHE_LOOKUP_CHUNK_SIZE]
        for row in (
            session.query(RecruitmentGeocodeCache)
            .filter(RecruitmentGeocodeCache.address.in_(chunk))
            .all()
        ):
            cache_rows[row.address] = row
    return cache_rows


def _build_scoped_query(session, dataset_scope: Optional[str] = None):
    q = session.query(RecruitmentVisit)
    scope_filters = _dataset_scope_filters(dataset_scope)
    if scope_filters:
        q = q.filter(*scope_filters)
    return q


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

    @field_validator("month")
    @classmethod
    def validate_month_format(cls, v: str) -> str:
        return _normalize_roc_month(v)


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

    @field_validator("month")
    @classmethod
    def validate_month_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _normalize_roc_month(v)


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


class CampusSettingPayload(BaseModel):
    campus_name: str = Field(..., min_length=1, max_length=100)
    campus_address: str = Field("", max_length=255)
    campus_lat: Optional[float] = Field(None, ge=-90, le=90)
    campus_lng: Optional[float] = Field(None, ge=-180, le=180)
    travel_mode: str = Field("driving", pattern="^(driving|walking|cycling)$")


class MonthCreate(BaseModel):
    month: str = Field(..., min_length=1, max_length=10)

    @field_validator("month")
    @classmethod
    def validate_month_format(cls, v: str) -> str:
        return _normalize_roc_month(v)


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
# 序列化 helpers
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
    visit = p.visit_count or 0
    deposit = p.deposit_count or 0
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
        "visit_to_deposit_rate": _pct(deposit, visit),
        "visit_to_enrolled_rate": _pct(enrolled, visit),
        "deposit_to_enrolled_rate": _pct(enrolled, deposit),
        "effective_to_enrolled_rate": _pct(enrolled, effective),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
