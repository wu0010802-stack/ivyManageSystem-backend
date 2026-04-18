"""招生模組 API sub-router 套件。

此套件取代原本的 `api/recruitment.py` 單一檔案實作，按功能領域拆分為：
  - shared   —— 共用常數、helpers、Pydantic schemas、序列化函式
  - records  —— 訪視記錄 CRUD + 匯入 + normalize_existing_months
  - stats    —— 統計查詢 + Excel 匯出 + 未預繳名單分析
  - hotspots —— 地址熱點聚合 + geocode 同步
  - market   —— 園所設定 + 鄰近幼兒園 + 市場情報
  - competitors —— 教育部 competitor_school 相關 API
  - periods  —— 近五年期間 + 選項列表 + 手動月份登記

對外介面：
  `router` — 聚合後的 APIRouter，供 main.py 的 include_router 使用
  此外所有原 `api.recruitment.*` 的公開符號（Pydantic schemas、endpoint 函式、
  normalize_existing_months、_extract_expected_label_from_text）都從這裡 re-export，
  以維持既有 test 與 startup 匯入路徑不變。
"""

from fastapi import APIRouter

from services import recruitment_market_intelligence as market_service

from api.recruitment import (
    competitors as _competitors,
    hotspots as _hotspots,
    market as _market,
    periods as _periods,
    records as _records,
    stats as _stats,
)

# 對外共用 schema / helper / endpoint（保留原 import 路徑）
from api.recruitment.shared import (
    CampusSettingPayload,
    ImportRecord,
    MonthCreate,
    PeriodCreate,
    PeriodUpdate,
    RecruitmentVisitCreate,
    RecruitmentVisitUpdate,
    _extract_expected_label,
    _extract_expected_label_from_text,
)

from api.recruitment.records import (
    convert_recruitment_record_to_student,
    create_recruitment_record,
    delete_recruitment_record,
    import_recruitment_records,
    list_recruitment_records,
    normalize_existing_months,
    update_recruitment_record,
)

from api.recruitment.stats import (
    export_recruitment_stats,
    get_no_deposit_analysis,
    get_recruitment_stats,
)

from api.recruitment.hotspots import (
    get_recruitment_address_hotspots,
    sync_recruitment_address_hotspots,
)

from api.recruitment.market import (
    get_nearby_kindergartens,
    get_recruitment_campus_setting,
    get_recruitment_market_intelligence,
    sync_recruitment_market_intelligence,
    update_recruitment_campus_setting,
)

from api.recruitment.competitors import (
    geocode_competitor_schools,
    get_campus_competition,
    get_geocode_pending_count,
    sync_kiang_data,
)

from api.recruitment.periods import (
    create_month,
    create_period,
    delete_month,
    delete_period,
    get_periods_summary,
    get_recruitment_options,
    list_months,
    list_periods,
    sync_period_from_visits,
    update_period,
)

# 聚合 router：每個 sub-module 各自宣告自己的 APIRouter（prefix 相同），
# 這裡用 include_router 合併成對外單一 router 交給 main.py。
router = APIRouter()
router.include_router(_records.router)
router.include_router(_stats.router)
router.include_router(_hotspots.router)
router.include_router(_market.router)
router.include_router(_competitors.router)
router.include_router(_periods.router)


__all__ = [
    "router",
    # 測試 monkeypatch 需要的 module-level re-export
    "market_service",
    # Pydantic schemas
    "CampusSettingPayload",
    "ImportRecord",
    "MonthCreate",
    "PeriodCreate",
    "PeriodUpdate",
    "RecruitmentVisitCreate",
    "RecruitmentVisitUpdate",
    # helpers
    "_extract_expected_label",
    "_extract_expected_label_from_text",
    "normalize_existing_months",
    # records endpoints
    "convert_recruitment_record_to_student",
    "create_recruitment_record",
    "delete_recruitment_record",
    "import_recruitment_records",
    "list_recruitment_records",
    "update_recruitment_record",
    # stats endpoints
    "export_recruitment_stats",
    "get_no_deposit_analysis",
    "get_recruitment_stats",
    # hotspots endpoints
    "get_recruitment_address_hotspots",
    "sync_recruitment_address_hotspots",
    # market endpoints
    "get_nearby_kindergartens",
    "get_recruitment_campus_setting",
    "get_recruitment_market_intelligence",
    "sync_recruitment_market_intelligence",
    "update_recruitment_campus_setting",
    # competitors endpoints
    "geocode_competitor_schools",
    "get_campus_competition",
    "get_geocode_pending_count",
    "sync_kiang_data",
    # periods endpoints
    "create_month",
    "create_period",
    "delete_month",
    "delete_period",
    "get_periods_summary",
    "get_recruitment_options",
    "list_months",
    "list_periods",
    "sync_period_from_visits",
    "update_period",
]
