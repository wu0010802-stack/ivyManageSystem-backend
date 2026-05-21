# PR1: Cache Layer Centralization (memory driver only)

## 動機
集中化 11 個散落 in-process TTLCache 為單一 facade（`utils/cache_layer.py`），
為未來 Redis driver（PR2）與前端 If-None-Match（PR3）鋪路。

## 範圍
- 新增 `utils/cache_layer.py`：Cache Protocol + MemoryCache + get_cache singleton
- 新增 `config/cache.py`：CacheSettings（backend / redis_url / key_prefix）
- 改造 12 個 callsite（含 plan 未列出但 spec 6 namespace 必要的 `api/config/bonus.py`）
- 刪除 `api/salary/__init__.py` unused `from cachetools import TTLCache`
- Migrate 5 個 test fixture 檔（test_salary_export / test_parent_home_summary / test_parent_display_name / test_parent_family_timeline / test_notifications / test_student_leave_notification_summary）

## Namespace 一覽（15 個）

| Namespace | 來源檔 | scope | TTL |
|---|---|---|---|
| `shift_type` | api/shifts.py | global | 300 |
| `attendance_shift_type` | api/attendance/upload.py | global | 300 |
| `portal_shift_type` | api/portal/_shared.py | global | 300 |
| `config_titles` | api/config/__init__.py | global | 300 |
| `config_attendance_policy` | api/config/__init__.py | global | 300 |
| `config_insurance_rates` | api/config/__init__.py | global | 300 |
| `config_deduction_types` | api/config/__init__.py | global | 300 |
| `config_bonus_types` | api/config/__init__.py | global | 300 |
| `config_bonus` | api/config/bonus.py | global | 300 |
| `salary_snapshot` | api/salary/detail.py | global | 60 |
| `parent_home_summary` | api/parent_portal/home.py | user | 60 |
| `parent_family_timeline` | api/parent_portal/family.py | user | 30 |
| `dashboard_events` | services/dashboard_query_service.py | global | 15 |
| `dashboard_approval` | services/dashboard_query_service.py | global | 15 |
| `dashboard_notification` | services/dashboard_query_service.py | global | 15 |

## 非範圍（spec 明列）
- ReportCacheService / utils/finance_cache（DB-backed 跨 worker，獨立活）
- utils/etag.py（HTTP 層）
- utils/rate_limit.py（counter 語意）
- Redis driver（PR2）
- 前端 If-None-Match（PR3）

## 重要設計取捨
- **MemoryCache 同步 API**：避免把 11 個 sync callsite 全部傳染成 async
- **Namespace 為一級概念**：`clear_namespace(ns)` 一行清乾淨，跨 callsite 共用 invalidate 介面
- **MemoryCache 內 `self._lock` 覆蓋所有 op**：cachetools.TTLCache 文件明示非 thread-safe，FastAPI sync route 經 run_in_threadpool 可能並發
- **api/salary/detail.py 移除 threading.Lock**：MemoryCache 內鎖已保證單 op atomicity；version mismatch delete 為 idempotent，雙 thread 同時 delete 無害

## 驗收
- 14 new `tests/test_cache_layer.py` 全綠
- 既有 pytest 零回歸（除 pre-existing `test_audit_router` / `test_supabase_storage` 與本 PR 無關）
- `grep -rn "from cachetools" api/ services/` 無輸出
- 12 commits 一一獨立，皆 Conventional Commits 格式
