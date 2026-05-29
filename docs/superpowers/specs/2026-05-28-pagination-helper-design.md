# 共用分頁 Helper 與命名統一 — Design

- **日期**：2026-05-28
- **作者**：claude（assisted）
- **狀態**：Draft，待 user review
- **對應 P2**：list endpoint 共用分頁 helper + 大表優化
- **相關 commit / spec**：無前置；本 spec 為獨立 infrastructure 改造

---

## 1. 動機

`ivy-backend/api/` 下 60+ router 沒有共用分頁 helper。實況統計（2026-05-28 grep）：

- **`.count()` 出現 68 處**（純 api/）
- **`total+items` response shape 已 52 處**（事實標準）
- **參數命名兩種並存**：`skip+limit` 24 處（含 `students.py`）/ `page+page_size` 11 處
- **無既有 helper**：`utils/` 59 個檔，無 `pagination` 相關

導致：
- 每個新 list endpoint 複製貼上 ~5 行 offset/limit/count 程式碼
- 前端對應的 v-model + payload key 兩種混用
- 未來真要加 perf 優化（cache count / keyset）需逐 endpoint 改

本 spec 收斂為：**封裝 `paginate()` helper + 全 codebase 統一 `page+page_size` 命名 + 統一 `{items, total, page, page_size}` response shape**。

## 2. Non-goals（明確不做）

下列項目原 P2 描述包含但在 brainstorming 階段決定 **不在本 spec scope**：

- **Keyset cursor 改造**：原 P2 建議 audit_log / parent_messages 改 cursor。經分析：
  - `audit_log` 已用 30 天 window（H.P0.1 fix）防全表掃，count() 走 `created_at` index range scan 在 100K-300K 列規模 < 100ms。
  - `parent_messages` 的 `.count()` 是 per-thread `unread_count` aggregate，每 teacher 的 thread 數量小（~50），非分頁熱點。
  - Keyset 會把 UI 從「跳第 N 頁」改為「上一頁/下一頁」，UX 退化；目前無真實 perf 訊號驅動。
- **count() optimization**（cached count / pg_class.reltuples estimate）：同樣無真實 perf 訊號。Helper 落地後 slow_query_logger（既有 utils/slow_query_logger.py）會持續監控；> 200ms 由 future spec 處理對應 endpoint。
- **`q.count()` 慢查詢監控加強**：既有 slow_query_logger 已足夠。
- **棄用 `parent_messages.unread_count` per-thread aggregate**：不是 paginate 場景。

## 3. 設計

### 3.1 Helper API（`utils/pagination.py`）

```python
"""utils/pagination.py — 共用分頁工具。

慣例：所有 list endpoint 一律：
1. Query 參數 page + page_size（透過 paginated_params() 注入）
2. Response shape `{items, total, page, page_size}`
3. 呼叫 paginate(query, pagination) 取得 (items, total)
"""

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Query as SAQuery


class PaginationParams(BaseModel):
    """FastAPI Depends() 注入的分頁參數。

    透過 paginated_params() factory 產生，default/max_size 由呼叫端決定。
    Field(ge=1) 為 defense in depth：HTTP 路徑經 paginated_params 的 Query(ge=1)
    早已擋下，但若呼叫端直接 PaginationParams(page=0) 構造（測試或非 HTTP caller），
    Pydantic ValidationError 確保不會傳入無效值給 paginate()。
    """

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


def paginated_params(default: int = 20, max_size: int = 200):
    """產生 PaginationParams Depends factory，支援 per-endpoint default/max。

    為何 factory 而非單一 class：不同 endpoint 對 page_size 的合理 default 不同
    （audit_log 50、recruitment_gov_kindergartens 100/最大 500、students 50）。
    Closure 內 default / max_size 進入 FastAPI Query 而非 dependency 函式 default，
    避免「全 codebase 同 default」失真。

    用法：
        pagination: PaginationParams = Depends(paginated_params(default=50, max_size=500))
    """
    def _params(
        page: int = Query(1, ge=1, description="第幾頁（從 1 開始）"),
        page_size: int = Query(default, ge=1, le=max_size, description="每頁筆數"),
    ) -> PaginationParams:
        return PaginationParams(page=page, page_size=page_size)

    return _params


def paginate(query: SAQuery, params: PaginationParams) -> tuple[list, int]:
    """執行 count + offset/limit，回 (items, total)。

    呼叫端責任：
    - 先 apply 所有 filter + order_by 再傳 query 進來。
    - 自行決定如何 serialize items（多數 endpoint 用 dict comprehension 手刻 row → dict，
      不強制走 Pydantic model_validate，避免 60+ endpoint 大量 schema 改寫）。

    Why tuple 而非 dict：呼叫端要回傳的 dict 還有自家 serialize 後的 items + 其它額外欄位
    （如 audit_log 加 meta、students 加 has_archived flag），讓呼叫端拼最後 dict 較直覺。
    """
    total = query.count()
    items = (
        query.offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
        .all()
    )
    return items, total
```

### 3.2 Endpoint refactor pattern

**Before**（`api/audit.py:80-141`）：

```python
@router.get("/audit-logs")
def get_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    entity_type: Optional[str] = None,
    ...
):
    q = _apply_filters(...)
    total = q.count()
    items = (
        q.order_by(desc(AuditLog.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [...],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
```

**After**：

```python
@router.get("/audit-logs")
def get_audit_logs(
    pagination: PaginationParams = Depends(paginated_params(default=50, max_size=200)),
    entity_type: Optional[str] = None,
    ...
):
    q = _apply_filters(...).order_by(desc(AuditLog.created_at))
    items, total = paginate(q, pagination)
    return {
        "items": [...],
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
    }
```

**`skip+limit` → `page+page_size` 換算（給 students.py 這類）：**

```python
# Before
@router.get("/students")
def list_students(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    ...
):
    total = q.count()
    students = q.order_by(Student.id).offset(skip).limit(limit).all()
    return {"items": items, "total": total, "skip": skip, "limit": limit}

# After
@router.get("/students")
def list_students(
    pagination: PaginationParams = Depends(paginated_params(default=50, max_size=500)),
    ...
):
    items, total = paginate(q.order_by(Student.id), pagination)
    return {
        "items": [...],
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
    }
```

呼叫端 `?skip=20&limit=10` 改為 `?page=3&page_size=10`，**Breaking change 由分 PR 內 BE+FE 同步處理**。

### 3.3 命名統一決策

- ✅ 全部 endpoint 統一 `page + page_size`（11 → 60+）
- ✅ Response shape 統一 `{items, total, page, page_size}`（52 → 60+）
- ❌ **不做** `skip+limit` backward-compat alias，避免 codebase 長期雙軌
- ❌ **不做** Pydantic `Page[T]` generic 強制 typed response — 多數 endpoint 已用 dict comprehension 手刻 row → dict，硬改 typed response 等於 schema 大改寫，與 helper 改造 decouple

### 3.4 Migration Cadence（7 PR）

每個 Phase PR 為一個獨立 worktree + branch，BE + FE 同 PR 上線（透過 schema.d.ts regen 強制 FE 編譯失敗才能 merge），保證階段內無「BE 改了 FE 沒跟」中間態。

| Phase | Scope | BE 檔 | FE 檔 |
|---|---|---|---|
| **P0** | helper 落地 + tests（**僅 helper 與其 tests，不動任何 endpoint**） | `utils/pagination.py`、`tests/test_pagination.py` | — |
| **P1** | audit + students | `api/audit.py`、`api/students.py` | `views/AuditLogView.vue`、`components/student/workbench/StudentListPanel.vue` |
| **P2** | fees（凡 `grep -l "q\.count()\|skip.*Query" api/fees/` 命中的檔案皆納入） | `api/fees/records.py`、`api/fees/refunds.py` 等 | `components/fees/FeeRecordsTab.vue`、`components/fees/FeeRefundsTab.vue` |
| **P3** | recruitment | `api/recruitment/*.py`、`api/recruitment_ivykids.py`、`api/recruitment_gov_kindergartens.py` | `components/recruitment/RecruitmentIvykidsTab.vue`、`RecruitmentDetailTab.vue`、`views/RecruitmentView.vue` |
| **P4** | parent_messages + student_change_logs + vendor_payments | `api/portal/parent_messages.py`、`api/student_change_logs.py`、`api/vendor_payments.py` | `components/student/tabs/RecordsTab.vue` 等對應 view |
| **P5** | 其餘小 endpoint（activity inquiries / registrations_pending / 其他剩下含 q.count 的 router） | 各對應 api/ | `composables/useActivityRegistration.ts`、`composables/useTableFilters.ts` 等 |
| **P6** | 驗證 leftover：grep `q.count()` + inline offset/limit 應為 0；移除任何遺留 alias | 全部 | — |

每 Phase PR commit 內容：
1. BE refactor：endpoint 改用 `Depends(paginated_params(...))` + `paginate(q, pagination)` + 統一 response shape
2. BE test：對應 `tests/test_<module>.py` 補一條「response keys 含 items/total/page/page_size + default page=1」regression
3. FE 同步：`src/api/<module>.ts` payload key 改 `page/page_size`；`schema.d.ts regen`（`npm run gen:api`）；view/composable 改用新 key
4. FE test：對應 `*.test.ts` smoke + vitest typecheck 0 error
5. CHANGELOG / spec back-link

### 3.5 Test 策略

**`tests/test_pagination.py`**（新檔，P0 PR 內）：
- `paginated_params` factory：
  - default / custom default / max_size 邊界
  - `page < 1` → 422
  - `page_size < 1 / > max_size` → 422
  - default 注入無 Query string
- `paginate(query, params)`：
  - 空 query → `([], 0)`
  - page=1, page_size=10, query 25 列 → 回 10 列 + total=25
  - page=3, page_size=10, query 25 列 → 回 5 列 + total=25
  - page=99, page_size=10, query 25 列 → 回 `[]` + total=25（過頭不 raise）
  - 已 apply order_by 的 query → 順序維持

**每 Phase PR**：對應 endpoint test 補 regression：
```python
def test_<endpoint>_pagination_shape():
    res = client.get("/<endpoint>?page=1&page_size=5", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) >= {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 5
```

### 3.6 Risk + Rollback

| Risk | 嚴重度 | Mitigation |
|---|---|---|
| 前端某 view 漏改 → 列表抓不到 items | 高 | Phase 內 BE+FE 同 PR；`schema.d.ts` regen 後 FE typecheck 失敗才能 merge |
| `_apply_filters` 與 `paginate()` 衝突 | 低 | helper 純接 query，呼叫端先 apply filter + order_by 再傳；無 helper 副作用 |
| Pydantic generic 在 FastAPI < 0.100 不 work | 低 | 本 repo FastAPI ≥ 0.110，Pydantic v2 已 generic OK |
| `q.count()` 大表慢沒解 | 中 | Out of scope；slow_query_logger 持續監控；future spec 處理 |
| `skip+limit` 廢除衝擊外部 caller | 低 | API 僅供內部 FE 使用；無 public API 文件 |
| Phase 間 main commit 撞到 paginated endpoint | 中 | 每 Phase worktree 從 origin/main；merge 前同步；衝突即時解 |
| P3 recruitment_gov_kindergartens max_size=500 |  低 | factory 已支援 `max_size` override，無特例 |

**Rollback**：
- 任何 Phase PR 上 prod 後爆 → `git revert <PR-merge-commit>`，該 Phase 內 BE+FE 同時還原
- helper 本身純 additive（不 break 既有未轉的 endpoint）
- P0 落地後即可 stop（helper 供未來新 endpoint 使用），後續 Phase 可彈性決定何時推進

## 4. Acceptance Criteria

P6 PR merge 後：
- `grep -rn "q\.count()" ivy-backend/api/` 結果為 0（除非有合理 exception 註解）
- `grep -rn "skip.*Query\|skip: int" ivy-backend/api/` 結果為 0
- `grep -rn "\.offset(.*)\.limit(" ivy-backend/api/` 結果為 0（除 inline 非分頁用途如 LIMIT 1）
- 全 60+ list endpoint response shape 含 `items / total / page / page_size`
- 前端 `views/` `components/` `composables/` 無 `?skip=` 或 `?limit=` 殘留（純 paginate 場景）
- BE pytest 相對 main baseline **無新增 fail**（既有 pre-existing fail 不計）
- FE vitest 相對 main baseline **無新增 fail** + typecheck 0
- OpenAPI codegen 不漂移（`npm run gen:api:check` pass）

## 5. Open Questions

無 — 設計階段皆已收斂。

## 6. References

- 既有相關 helper：`utils/cache_layer.py`、`utils/etag.py`、`utils/slow_query_logger.py`
- audit_log 30 天 window：`api/audit.py:94-97`（H.P0.1 fix）
- 前端 wrapper：`src/composables/useTableFilters.ts`
- CLAUDE.md：跨前後端變更流程 §4
