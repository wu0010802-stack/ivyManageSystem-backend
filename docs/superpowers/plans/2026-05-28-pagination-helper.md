# Pagination Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立共用分頁 helper（`utils/pagination.py`），統一 60+ list endpoint 為 `page+page_size` 命名與 `{items, total, page, page_size}` response shape，並同步前端 ~10+ view payload key。

**Architecture:** FastAPI Depends 注入 `PaginationParams`（Pydantic）+ 純函式 `paginate(query, params)`。Factory `paginated_params(default, max_size)` 提供 per-endpoint default/max override。7 phase PR 漸進式遷移，每 phase BE+FE 同 PR 上線避免中間態。

**Tech Stack:** FastAPI ≥ 0.110、Pydantic v2、SQLAlchemy ORM Query、pytest、Vitest、TypeScript。

**Spec reference:** `docs/superpowers/specs/2026-05-28-pagination-helper-design.md`

---

## Phase 0：Helper 落地 + tests

**目標**：建立 `utils/pagination.py` + `tests/test_pagination.py`，**不動任何 endpoint**。落地後即可獨立 ship。

### Task P0-1：建 utils/pagination.py 骨架（PaginationParams + paginate signature）

**Files:**
- Create: `ivy-backend/utils/pagination.py`
- Test: `ivy-backend/tests/test_pagination.py`

- [ ] **Step 1：寫 failing test — PaginationParams 基本構造**

`ivy-backend/tests/test_pagination.py`：
```python
"""utils/pagination.py 行為測試。

涵蓋：
- PaginationParams 基本構造與型別約束
- paginated_params(default, max_size) factory 邊界
- paginate(query, params) 對 SAQuery 的 offset/limit/count 行為
"""

import pytest
from pydantic import ValidationError
from utils.pagination import PaginationParams


def test_pagination_params_basic():
    """PaginationParams 基本可建構。"""
    p = PaginationParams(page=2, page_size=20)
    assert p.page == 2
    assert p.page_size == 20


def test_pagination_params_rejects_zero_page():
    """page 必須 >= 1。"""
    with pytest.raises(ValidationError):
        PaginationParams(page=0, page_size=20)


def test_pagination_params_rejects_zero_page_size():
    """page_size 必須 >= 1。"""
    with pytest.raises(ValidationError):
        PaginationParams(page=1, page_size=0)
```

- [ ] **Step 2：跑 test 確認失敗（module 不存在）**

Run: `cd ivy-backend && python3 -m pytest tests/test_pagination.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'utils.pagination'`

- [ ] **Step 3：寫最小實作（PaginationParams + 型別約束）**

`ivy-backend/utils/pagination.py`：
```python
"""utils/pagination.py — 共用分頁工具。

慣例：所有 list endpoint 一律：
1. Query 參數 page + page_size（透過 paginated_params() 注入）
2. Response shape {items, total, page, page_size}
3. 呼叫 paginate(query, pagination) 取得 (items, total)

範例：
    from utils.pagination import PaginationParams, paginated_params, paginate

    @router.get("/items")
    def list_items(
        pagination: PaginationParams = Depends(paginated_params(default=50, max_size=200)),
    ):
        q = session.query(Item).order_by(Item.created_at.desc())
        items, total = paginate(q, pagination)
        return {
            "items": [...],
            "total": total,
            "page": pagination.page,
            "page_size": pagination.page_size,
        }
"""

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Query as SAQuery


class PaginationParams(BaseModel):
    """FastAPI Depends() 注入的分頁參數。

    透過 paginated_params() factory 產生，default/max_size 由呼叫端決定。
    """

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


def paginated_params(default: int = 20, max_size: int = 200):
    """產生 PaginationParams Depends factory，支援 per-endpoint default/max。

    Why factory 而非單一 dependency class：不同 endpoint 的合理 page_size default 不同
    （audit_log 50、recruitment_gov_kindergartens 100/最大 500、students 50）。
    Closure 內 default / max_size 進入 FastAPI Query 才能對應 OpenAPI 正確 default，
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
    """對 SAQuery 執行 count + offset/limit，回 (items, total)。

    呼叫端責任：
    - 先 apply 所有 filter + order_by 再傳 query 進來。
    - 自行決定如何 serialize items（不強制 Pydantic model_validate，避免 60+
      endpoint 大量 schema 改寫）。

    Why tuple 而非 dict：呼叫端要回傳的 dict 還有自家 serialize 後的 items
    與其他額外欄位（如 audit_log 加 meta、students 加 has_archived flag），
    讓呼叫端拼最後 dict 較直覺。
    """
    total = query.count()
    items = (
        query.offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
        .all()
    )
    return items, total
```

- [ ] **Step 4：跑 test 確認通過**

Run: `cd ivy-backend && python3 -m pytest tests/test_pagination.py -v`
Expected: 3 passed

- [ ] **Step 5：Commit Step 1-4**

```bash
cd ivy-backend
git add utils/pagination.py tests/test_pagination.py
git commit -m "feat(pagination): 建 PaginationParams + paginate helper 骨架

P0-1：utils/pagination.py 落地 PaginationParams Pydantic model、
paginated_params factory、paginate(query, params) 純函式。
驗證基本構造與型別約束。

Spec: docs/superpowers/specs/2026-05-28-pagination-helper-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P0-2：補 paginated_params factory 邊界 test

**Files:**
- Modify: `ivy-backend/tests/test_pagination.py`

- [ ] **Step 1：加 factory 邊界 test**

Append to `tests/test_pagination.py`：
```python
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from utils.pagination import paginated_params


def _make_test_app(default: int = 20, max_size: int = 200):
    """Helper：建一個小 FastAPI app 注入 paginated_params 拿來測 factory。"""
    app = FastAPI()

    @app.get("/test")
    def _endpoint(p: PaginationParams = Depends(paginated_params(default=default, max_size=max_size))):
        return {"page": p.page, "page_size": p.page_size}

    return TestClient(app)


def test_paginated_params_default_values():
    """不傳參數時，使用 factory default。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test")
    assert r.status_code == 200
    assert r.json() == {"page": 1, "page_size": 50}


def test_paginated_params_custom_values():
    """傳 query string 覆蓋 default。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test?page=3&page_size=10")
    assert r.status_code == 200
    assert r.json() == {"page": 3, "page_size": 10}


def test_paginated_params_rejects_page_zero():
    """page < 1 → 422（FastAPI Query ge 驗證）。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test?page=0")
    assert r.status_code == 422


def test_paginated_params_rejects_page_size_over_max():
    """page_size > max_size → 422。"""
    client = _make_test_app(default=20, max_size=100)
    r = client.get("/test?page_size=500")
    assert r.status_code == 422


def test_paginated_params_max_size_override():
    """factory max_size 高於 default 時，page_size 上限放寬。"""
    client = _make_test_app(default=100, max_size=500)
    r = client.get("/test?page_size=300")
    assert r.status_code == 200
    assert r.json()["page_size"] == 300
```

- [ ] **Step 2：跑 test 確認 5 個新 test 全過**

Run: `cd ivy-backend && python3 -m pytest tests/test_pagination.py -v`
Expected: 8 passed（3 + 5 新增）

- [ ] **Step 3：Commit**

```bash
cd ivy-backend
git add tests/test_pagination.py
git commit -m "test(pagination): 補 paginated_params factory 邊界 5 test

P0-2：驗 default 值注入、custom 值覆蓋、page<1 拒絕、page_size>max
拒絕、max_size override 機制。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P0-3：補 paginate(query, params) 對 SAQuery 行為 test

**Files:**
- Modify: `ivy-backend/tests/test_pagination.py`

- [ ] **Step 1：加 paginate 純函式 test（用 conftest test_db_session）**

Append to `tests/test_pagination.py`：
```python
from utils.pagination import paginate
from models.database import Employee


def _seed_employees(session, n: int):
    """Helper：建 n 個 employee 給 paginate 測試（順序由 id 決定）。"""
    for i in range(n):
        session.add(Employee(
            employee_id=f"P{i:03d}",
            name=f"員工{i}",
            position="教師",
        ))
    session.commit()


def test_paginate_empty_query(test_db_session):
    """空 query → ([], 0)。"""
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert items == []
    assert total == 0


def test_paginate_first_page(test_db_session):
    """25 列、page=1 size=10 → 回 10 列 + total=25。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert len(items) == 10
    assert total == 25
    assert items[0].employee_id == "P000"
    assert items[-1].employee_id == "P009"


def test_paginate_last_partial_page(test_db_session):
    """25 列、page=3 size=10 → 回剩餘 5 列 + total=25。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=3, page_size=10))
    assert len(items) == 5
    assert total == 25
    assert items[0].employee_id == "P020"
    assert items[-1].employee_id == "P024"


def test_paginate_overshoot_returns_empty(test_db_session):
    """25 列、page=99 → 回空 list 不 raise，total 仍正確。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=99, page_size=10))
    assert items == []
    assert total == 25


def test_paginate_preserves_order_by(test_db_session):
    """order_by 由呼叫端設定，paginate 不改動。"""
    _seed_employees(test_db_session, 5)
    q = test_db_session.query(Employee).order_by(Employee.id.desc())
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert [e.employee_id for e in items] == ["P004", "P003", "P002", "P001", "P000"]
```

- [ ] **Step 2：跑 test 確認 13 個全過**

Run: `cd ivy-backend && python3 -m pytest tests/test_pagination.py -v`
Expected: 13 passed

- [ ] **Step 3：Commit**

```bash
cd ivy-backend
git add tests/test_pagination.py
git commit -m "test(pagination): paginate 純函式 5 test 覆蓋邊界

P0-3：empty/first/partial-last/overshoot/order_by 五個場景驗證 paginate
對 SAQuery 的 offset/limit/count 行為。共 13 個 pagination test 通過。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P0-4：跑全套 backend pytest 驗證零 regression

- [ ] **Step 1：baseline 紀錄**

Run: `cd ivy-backend && python3 -m pytest --tb=no -q 2>&1 | tail -5`
Expected: 記下 passed/failed 數（main pre-existing fail 不應變動）

- [ ] **Step 2：跑全套 pytest（不引入新 fail）**

Run: `cd ivy-backend && python3 -m pytest --tb=short -q 2>&1 | tail -10`
Expected: 與 baseline 一致；新加 13 個 test pass

- [ ] **Step 3：若全綠或新 fail = 0，open PR**

```bash
cd ivy-backend
git push origin HEAD
gh pr create --title "feat(pagination): P0 共用分頁 helper 落地 + 13 test" --body "$(cat <<'EOF'
## Summary
- 新建 \`utils/pagination.py\`：\`PaginationParams\` Pydantic model、\`paginated_params(default, max_size)\` factory、\`paginate(query, params) -> (items, total)\` 純函式
- 新建 \`tests/test_pagination.py\`：13 test 覆蓋 model 構造、factory 邊界、paginate 對 SAQuery 五個場景

## Test plan
- [x] \`pytest tests/test_pagination.py\` 13 passed
- [x] 全套 pytest 相對 baseline 無新增 fail
- [ ] PR review

## Scope
P0 only — 不動任何 endpoint。後續 P1-P6 phase 各自獨立 PR。

## Spec
docs/superpowers/specs/2026-05-28-pagination-helper-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 1：audit + students 遷移

**目標**：把 `api/audit.py` 與 `api/students.py` 改用 helper，前端 `AuditLogView.vue` 與 `StudentListPanel.vue` 同步 payload key。**BE+FE 同 PR**。

### Task P1-1：BE — api/audit.py 改用 helper

**Files:**
- Modify: `ivy-backend/api/audit.py:80-141`
- Modify: `ivy-backend/api/audit.py:170-180`（檢查是否有第二個 paginated endpoint，依 grep）
- Test: `ivy-backend/tests/test_audit_router.py`

- [ ] **Step 1：grep 確認 audit.py 內所有 paginated endpoint**

Run: `grep -n "\.count()\|skip.*Query\|page.*Query\|page_size" ivy-backend/api/audit.py`
Expected: 列出所有受影響行（spec 已示範 line 80-141 + 170-180 區）

- [ ] **Step 2：在 audit.py 頂部 import helper**

```python
# 在既有 imports 中加：
from utils.pagination import PaginationParams, paginated_params, paginate
```

- [ ] **Step 3：修改 get_audit_logs 簽章與內部（spec §3.2 pattern）**

`api/audit.py:80-141` 改為（保留 _apply_filters 不變）：
```python
from fastapi import Depends

@router.get("/audit-logs")
def get_audit_logs(
    pagination: PaginationParams = Depends(paginated_params(default=50, max_size=200)),
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    username: Optional[str] = None,
    entity_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """查詢操作審計紀錄"""
    if start_at is None and end_at is None:
        start_at = now_taipei_naive() - timedelta(days=LIST_DEFAULT_DAYS)

    session = get_session()
    try:
        q = _apply_filters(
            session.query(AuditLog),
            entity_type,
            action,
            username,
            entity_id,
            ip_address,
            start_at,
            end_at,
        ).order_by(desc(AuditLog.created_at))

        items, total = paginate(q, pagination)

        return {
            "items": [
                {
                    "id": log.id,
                    "user_id": log.user_id,
                    "username": log.username,
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "summary": log.summary,
                    "changes": _parse_changes(log.changes),
                    "ip_address": log.ip_address,
                    "created_at": (
                        log.created_at.isoformat() if log.created_at else None
                    ),
                }
                for log in items
            ],
            "total": total,
            "page": pagination.page,
            "page_size": pagination.page_size,
        }
    finally:
        session.close()
```

- [ ] **Step 4：對 api/audit.py:170-180 第二個 paginated endpoint 同樣處理**

依 Step 1 grep 結果，套用同樣 pattern。

- [ ] **Step 5：補 regression test — response shape**

`tests/test_audit_router.py` 加一條：
```python
def test_audit_logs_pagination_shape(client, admin_token):
    """P1: 確認 response shape 含 items/total/page/page_size。"""
    r = client.get(
        "/audit-logs?page=1&page_size=5",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) >= {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 5
```

（依 test_audit_router.py 既有 fixture 命名調整 `client`/`admin_token`）

- [ ] **Step 6：跑 audit test**

Run: `cd ivy-backend && python3 -m pytest tests/test_audit_router.py -v 2>&1 | tail -20`
Expected: 既有 test 全過 + 新加 pagination shape test pass

- [ ] **Step 7：Commit BE part**

```bash
cd ivy-backend
git add api/audit.py tests/test_audit_router.py
git commit -m "refactor(audit): 遷移 paginate helper + 統一 response shape

P1-1：api/audit.py 改用 utils.pagination 的 paginated_params + paginate。
default=50 max_size=200 對齊原 Query 約束。新增 pagination shape regression test。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P1-2：BE — api/students.py 改用 helper

**Files:**
- Modify: `ivy-backend/api/students.py:417-517`（skip+limit → page+page_size，spec §3.2）
- Test: `ivy-backend/tests/test_students.py`（如存在，否則 `test_students_api.py`）

- [ ] **Step 1：grep 確認 students.py 內所有 paginated endpoint**

Run: `grep -n "\.count()\|skip.*Query\|page.*Query\|page_size\|limit.*Query" ivy-backend/api/students.py`
Expected: 列出 line 417、485、486、517 等

- [ ] **Step 2：import helper**

```python
from utils.pagination import PaginationParams, paginated_params, paginate
```

- [ ] **Step 3：修改 list_students endpoint（skip+limit → page+page_size）**

`api/students.py:417-517` 套 spec §3.2 第二範例（skip+limit → page+page_size 換算）。把
```python
skip: int = Query(0, ge=0),
limit: int = Query(50, ge=1, le=500),
```
改為
```python
pagination: PaginationParams = Depends(paginated_params(default=50, max_size=500)),
```
內部 `offset(skip).limit(limit)` 與 `q.count()` 換成 `paginate(q.order_by(Student.id), pagination)`。
return dict 改 `"page": pagination.page, "page_size": pagination.page_size`，移除 `skip` / `limit` key。

- [ ] **Step 4：補 regression test**

加 `test_students_pagination_shape`：與 P1-1 step 5 同 pattern，調整 endpoint path 為 `/students`。

- [ ] **Step 5：跑 students test**

Run: `cd ivy-backend && python3 -m pytest tests/test_students.py tests/test_students_api.py -v 2>&1 | tail -20`
Expected: 既有 test 全過 + 新 regression test pass

- [ ] **Step 6：Commit BE part**

```bash
cd ivy-backend
git add api/students.py tests/test_students*.py
git commit -m "refactor(students): skip+limit → page+page_size + paginate helper

P1-2：api/students.py 換掉 skip/limit Query 參數為 paginated_params(default=50, max_size=500)，
內部用 paginate(q, params)。Response shape 從 {items, total, skip, limit} 改為
{items, total, page, page_size}。Breaking change 由 P1 FE 同 PR 同步處理。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P1-3：FE — schema.d.ts regen + AuditLogView/StudentListPanel 同步

**Files:**
- Modify: `ivy-frontend/src/api/_generated/schema.d.ts`（regen）
- Modify: `ivy-frontend/src/views/AuditLogView.vue`
- Modify: `ivy-frontend/src/components/student/workbench/StudentListPanel.vue`
- Modify: `ivy-frontend/src/api/audit.ts`（如存在）
- Modify: `ivy-frontend/src/api/students.ts`（如存在）

- [ ] **Step 1：BE 先跑 dump_openapi，再 FE regen schema**

```bash
cd ivy-backend && python3 scripts/dump_openapi.py
cd ../ivy-frontend && npm run gen:api
```
Expected: `src/api/_generated/schema.d.ts` 更新；audit/students endpoint query 由 `skip` / `limit` 改為 `page` / `page_size`

- [ ] **Step 2：執行 typecheck 找出所有 breakage**

Run: `cd ivy-frontend && npm run typecheck 2>&1 | tail -30`
Expected: 列出 AuditLogView.vue、StudentListPanel.vue、api/audit.ts、api/students.ts 等使用 `skip` / `limit` 的 type error

- [ ] **Step 3：改 src/api/audit.ts payload key**

打開 `src/api/audit.ts`，把 list query payload 從 `{skip, limit, ...}` 改為 `{page, page_size, ...}`；回傳型別由 schema.d.ts auto 推導，無需手改 interface。

- [ ] **Step 4：改 src/api/students.ts payload key**

同樣處理 students.ts。

- [ ] **Step 5：改 AuditLogView.vue 對齊新 payload + response key**

打開 `src/views/AuditLogView.vue`，把 ElPagination 的 v-model 從 `skip`/`limit` 改為 `page`/`page_size`，呼叫 API 處 payload key 同步，response 解構 key 從 `body.skip`/`body.limit` 改為 `body.page`/`body.page_size`。

- [ ] **Step 6：改 StudentListPanel.vue 同上**

對 `src/components/student/workbench/StudentListPanel.vue` 套同樣 pattern。

- [ ] **Step 7：跑 typecheck + vitest**

```bash
cd ivy-frontend
npm run typecheck
npm run test -- src/views/AuditLogView.test.ts src/components/student/workbench/StudentListPanel.test.ts
```
Expected: typecheck 0 error；對應 test 全綠（若 test 命名不同，依實際命名跑）

- [ ] **Step 8：跑全套 vitest 確認零 regression**

Run: `cd ivy-frontend && npm test 2>&1 | tail -10`
Expected: 相對 main baseline 無新增 fail

- [ ] **Step 9：Commit FE part**

```bash
cd ivy-frontend
git add src/api/_generated/schema.d.ts src/api/audit.ts src/api/students.ts src/views/AuditLogView.vue src/components/student/workbench/StudentListPanel.vue
git commit -m "refactor(audit,students): payload key skip+limit → page+page_size

P1-3：對齊後端 BE P1-1/P1-2 改動。schema.d.ts regen + AuditLogView /
StudentListPanel 元件 payload key + response 解構 key 同步。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task P1-4：開 PR（BE + FE 跨 repo coordination）

- [ ] **Step 1：BE 推 PR**

```bash
cd ivy-backend && git push origin HEAD
gh pr create --title "refactor(pagination): P1 audit + students 遷移 helper" --body "$(cat <<'EOF'
## Summary
- api/audit.py、api/students.py 改用 utils.pagination helper
- students 從 skip+limit 改為 page+page_size（breaking — 對應 FE PR 同步）
- 補 pagination shape regression test

## Test plan
- [x] pytest tests/test_audit_router.py + tests/test_students*.py 全綠
- [x] 全套 pytest 相對 baseline 無新增 fail

## Pairs with
ivy-frontend PR <FE-PR-URL>（必須同時 merge）

## Spec
docs/superpowers/specs/2026-05-28-pagination-helper-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2：FE 推 PR**

```bash
cd ivy-frontend && git push origin HEAD
gh pr create --title "refactor(pagination): P1 AuditLogView + StudentListPanel payload key" --body "$(cat <<'EOF'
## Summary
- src/api/audit.ts + src/api/students.ts payload key skip+limit → page+page_size
- AuditLogView + StudentListPanel ElPagination v-model 同步
- schema.d.ts regen

## Test plan
- [x] npm run typecheck 0 error
- [x] vitest 對應 view test 全綠
- [x] 全套 vitest 相對 baseline 無新增 fail

## Pairs with
ivy-backend PR <BE-PR-URL>（必須同時 merge）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3：兩 PR 同時 merge（避免中間態）**

User 操作，BE 先 merge → 觸發前端 openapi-drift CI 失敗 → 立即 merge FE PR 解。或反向同樣 OK。

---

## Phase 2-5：模組式遷移（templated）

> **Pattern：** 每個 Phase 套 P1 的 6 步驟模板（BE refactor → BE test → FE schema regen → FE element 同步 → typecheck + vitest → BE PR + FE PR 同 merge）。每 phase 一個 BE PR + 一個 FE PR。

### Phase 2：fees

**BE 檔（grep `q.count()\|skip.*Query\|skip: int` `api/fees/`）：**
- `api/fees/records.py`
- （再次 grep 確認其他 fees 子模組是否命中：`adjustments.py / refunds.py / generation.py / templates.py`）

**FE 檔：**
- `src/components/fees/FeeRecordsTab.vue`
- `src/components/fees/FeeRefundsTab.vue`
- `src/api/fees.ts`（payload key）

- [ ] **Task P2-1**：BE refactor + test（套 P1-1 pattern，per file）
- [ ] **Task P2-2**：FE schema regen + 元件同步 + typecheck（套 P1-3 pattern）
- [ ] **Task P2-3**：BE PR + FE PR 同 merge（套 P1-4 pattern）

### Phase 3：recruitment

**BE 檔：**
- `api/recruitment/records.py`
- `api/recruitment/stats.py`
- `api/recruitment_gov_kindergartens.py`（max_size=500）
- `api/recruitment_ivykids.py`

**FE 檔：**
- `src/components/recruitment/RecruitmentIvykidsTab.vue`
- `src/components/recruitment/RecruitmentDetailTab.vue`
- `src/views/RecruitmentView.vue`
- `src/api/recruitmentIvykids.ts`、`src/api/recruitment.ts`

- [ ] **Task P3-1**：BE refactor + test
- [ ] **Task P3-2**：FE schema regen + 元件同步 + typecheck
- [ ] **Task P3-3**：BE PR + FE PR 同 merge

### Phase 4：parent_messages + student_change_logs + vendor_payments

**BE 檔：**
- `api/portal/parent_messages.py`（注意：`unread_count` per-thread aggregate 不動，只動主分頁）
- `api/student_change_logs.py`
- `api/vendor_payments.py`

**FE 檔：**
- `src/components/student/tabs/RecordsTab.vue`
- 對應 api wrapper

- [ ] **Task P4-1**：BE refactor + test
- [ ] **Task P4-2**：FE schema regen + 元件同步 + typecheck
- [ ] **Task P4-3**：BE PR + FE PR 同 merge

### Phase 5：剩餘小 endpoint

**BE 檔（grep 命中但 P1-P4 未處理的）：**
- `api/activity/*.py`（attendance / inquiries / courses / registrations / registrations_pending / registrations_static / settings / supplies）
- `api/portal/announcements.py`、`api/portal/leaves.py`、`api/portal/incidents.py`、`api/portal/overtimes.py`、`api/portal/assessments.py`
- `api/portfolio/*.py`（milestones / reports / observations / measurements / student_attachments）
- `api/parent_portal/photos.py`、`api/parent_portal/announcements.py`、`api/parent_portal/messages.py`
- `api/salary/records.py`
- `api/student_assessments.py`、`api/student_communications.py`、`api/student_incidents.py`、`api/employees.py`、`api/recruitment_gov_kindergartens.py`（若 P3 未涵蓋）

**FE 檔：**
- `src/composables/useActivityRegistration.ts`
- `src/composables/useTableFilters.ts`
- 對應 view/component

- [ ] **Task P5-1**：BE refactor + test（分子模組，逐檔 commit）
- [ ] **Task P5-2**：FE schema regen + 元件同步 + typecheck
- [ ] **Task P5-3**：BE PR + FE PR 同 merge

---

## Phase 6：驗證 + Cleanup

**目標**：grep 確認 0 leftover、acceptance criteria 全達成。

### Task P6-1：grep leftover 驗證

- [ ] **Step 1：BE 應為 0 leftover**

```bash
cd ivy-backend
grep -rn "q\.count()" api/ --include="*.py" | grep -v __pycache__
grep -rn "skip.*Query\|skip: int" api/ --include="*.py" | grep -v __pycache__
grep -rn "\.offset(.*)\.limit(" api/ --include="*.py" | grep -v __pycache__ | grep -v "LIMIT 1"
```
Expected: 全部結果為空（若有 inline 非分頁用途如 `.limit(1)` 抓單筆，需逐條判斷是否例外）

- [ ] **Step 2：FE 應無 skip/limit payload 殘留**

```bash
cd ivy-frontend
grep -rn "skip:\|limit:" src/ --include="*.ts" --include="*.vue" | grep -E "skip:|limit:" | grep -v "skip_count\|rate_limit\|timeout_limit"
```
Expected: 0 處 paginate 用途的 skip/limit（filter 後）

- [ ] **Step 3：BE pytest 相對 main baseline 無新增 fail**

Run: `cd ivy-backend && python3 -m pytest --tb=no -q 2>&1 | tail -5`
Expected: passed/failed 數與 P0 baseline 一致或更好

- [ ] **Step 4：FE typecheck + vitest 全綠**

```bash
cd ivy-frontend
npm run typecheck
npm test
```
Expected: typecheck 0；vitest 無新增 fail

- [ ] **Step 5：OpenAPI codegen 不漂移**

Run: `cd ivy-frontend && npm run gen:api:check`
Expected: pass（schema.d.ts 與後端最新 OpenAPI 對齊）

### Task P6-2：CLAUDE.md 補新 helper 慣例

**Files:**
- Modify: `ivy-backend/CLAUDE.md`（在 router/api 慣例段落加 paginate helper 用法）

- [ ] **Step 1：找到 CLAUDE.md 對 api router 的慣例段落**

Run: `grep -n "router\|api/" ivy-backend/CLAUDE.md | head -10`

- [ ] **Step 2：加 paginate helper 慣例**

在合適段落加：
```markdown
- **新 list endpoint 一律走 `utils/pagination`**：`Depends(paginated_params(default=N, max_size=M))` 注入 `PaginationParams`，
  內部 `items, total = paginate(q, pagination)`，回傳 `{items, total, page, page_size}`。
  禁用 `skip+limit` Query 參數命名（已於 2026-05-28 統一）。
```

- [ ] **Step 3：Commit + 開 P6 PR**

```bash
cd ivy-backend
git add CLAUDE.md
git commit -m "docs(claude.md): 加 paginate helper 慣例條目

P6-2：新 list endpoint 一律走 utils.pagination；禁用 skip+limit Query。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push origin HEAD
gh pr create --title "chore(pagination): P6 leftover 驗證 + CLAUDE.md 慣例" --body "$(cat <<'EOF'
## Summary
- grep 確認 0 leftover q.count() / skip Query / inline offset+limit
- CLAUDE.md 加 paginate helper 慣例
- pagination spec 收尾，全 60+ endpoint 統一命名與 response shape

## Test plan
- [x] BE pytest 相對 main baseline 無新增 fail
- [x] FE typecheck 0 + vitest 無新增 fail
- [x] npm run gen:api:check pass

## Spec
docs/superpowers/specs/2026-05-28-pagination-helper-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review 紀錄（plan 寫完後 inline 修正項）

- ✅ 每 task 有明確 Files / Steps / 預期輸出
- ✅ 無 TBD / TODO / "implement later"
- ✅ helper API 在 P0-1 完整定義；P1+ 使用同名簽章 `paginated_params(default, max_size)` 與 `paginate(query, params)` 對齊
- ✅ P0 為純 additive，不 break 既有 endpoint；可獨立 ship
- ✅ P1-P5 每 phase BE+FE 同 PR cadence 明確
- ✅ P6 acceptance criteria 與 spec §4 對齊（grep 0 leftover + baseline 無新增 fail）
- ⚠️ P2-P5 為 templated task（非全 unrolled steps）— 實作時 subagent 套 P1 pattern；若 phase 內檔案數 > 5 可再拆 sub-task
