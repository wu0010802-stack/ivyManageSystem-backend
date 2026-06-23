"""activity_stats_attendance 快取 wiring 守衛。

2026-06-23 優化：get_attendance_stats 改 report_cache 包覆（原本每次 dashboard
載入都重算最重的出席率聚合）。survey 警告的關鍵 wrinkle：新快取若沒納入
dashboard 失效清單、且點名儲存沒觸發失效，出席率會 stale 到 TTL。本測試鎖住
這兩條 wiring，避免日後新增快取卻漏接失效。
"""

from services.activity_service import (
    ACTIVITY_DASHBOARD_CACHE_CATEGORIES,
    ACTIVITY_STATS_ATTENDANCE_CACHE_TTL_SECONDS,
)


def test_attendance_stats_category_in_dashboard_invalidation_set():
    # invalidate_dashboard_caches 會清這個 tuple 內的所有 category；出席率快取
    # 必須在內，否則點名儲存後 dashboard 出席率不會更新。
    assert "activity_stats_attendance" in ACTIVITY_DASHBOARD_CACHE_CATEGORIES


def test_attendance_stats_ttl_is_positive():
    assert ACTIVITY_STATS_ATTENDANCE_CACHE_TTL_SECONDS > 0


def test_batch_update_attendance_invalidates_dashboard_caches():
    # 點名儲存端點必須在 commit 後呼叫 dashboard 快取失效；否則新快取會 stale。
    import inspect
    import api.activity.attendance as attendance_mod

    src = inspect.getsource(attendance_mod.batch_update_attendance)
    assert "_invalidate_activity_dashboard_caches" in src
