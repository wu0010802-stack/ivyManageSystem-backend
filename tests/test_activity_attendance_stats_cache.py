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
    #
    # 讀「原始檔的函式區塊」而非 inspect.getsource(live function 物件)：後者在
    # 同 pytest-xdist worker 內若有其他測試對 api.activity.attendance 做
    # reload / monkeypatch（module 全局態污染），getsource 會循 live 物件錯亂的
    # co_firstlineno 取到錯誤行（serial 順序碰不到、xdist 順序才暴露）。改從檔案
    # 切出 def batch_update_attendance ~ 下一個頂層 def 的區塊，免疫於此污染。
    import inspect
    import re

    import api.activity.attendance as attendance_mod

    source_file = inspect.getsourcefile(attendance_mod)
    assert source_file is not None
    with open(source_file, encoding="utf-8") as fh:
        lines = fh.readlines()

    start = next(
        i
        for i, ln in enumerate(lines)
        if re.match(r"^(async\s+)?def batch_update_attendance\b", ln)
    )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(async\s+)?def \w", lines[j]):
            end = j
            break
    block = "".join(lines[start:end])
    assert "_invalidate_activity_dashboard_caches" in block
