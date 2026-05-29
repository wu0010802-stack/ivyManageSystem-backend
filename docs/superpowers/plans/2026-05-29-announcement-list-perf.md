# PR #8：公告 Admin list perf 修補 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admin list 改用 SQL COUNT subquery + batch preview，移除 `selectinload(reads, recipients)` 全載入；read preview tag 仍顯示前 3 名，完整名單與 recipient_ids 拆獨立 lazy endpoint。

**Architecture:** `list_announcements` 加兩個 correlated COUNT scalar subquery（read_count / recipient_count），用一次 batch query 撈所有 announcement 的前 N reads（Python group + slice top 3）。新增 `GET /announcements/{id}/recipients` 與 `GET /announcements/{id}/readers?page=&page_size=` 兩個 endpoint 提供 lazy fetch。前端 admin view 將 `openEdit` 改 async，popover 改 click 觸發。

**Tech Stack:** SQLAlchemy correlated subquery、Vue 3 Composition API、Element Plus `<el-popover trigger="click">`、pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-05-29-announcement-improvements-design.md` §「PR #8」

**前置依賴:** PR #1 已 merge（list response 已含 `publish_at` / `expires_at` / `status`，本 PR 不再動這幾個欄位）。

---

## 檔案結構

**Modify:**
- `api/announcements.py` — `list_announcements` 改寫；新增 `list_recipients` + `list_readers` endpoint；`AnnouncementListOut` schema 配合更新
- `schemas/announcements.py` — 拿掉 `readers` / `recipient_ids` 欄位；加 new endpoints response schema
- `tests/api/test_announcements.py` — 既有 readers/recipient_ids 斷言改為新 endpoint 呼叫；新增 query count baseline 測試

**Create:**
- `tests/api/test_announcements_perf.py` — 新 endpoint 行為 + perf baseline

**前端 Modify:**
- `ivy-frontend/src/api/announcements.ts` — 加兩個 wrapper
- `ivy-frontend/src/api/_generated/schema.d.ts` — `npm run gen:api` 自動 regen
- `ivy-frontend/src/views/AnnouncementView.vue` — `openEdit` async + popover click + cache map
- `ivy-frontend/tests/unit/views/AnnouncementView.test.js` — vitest 補 cache / lazy fetch 邏輯

---

## Task 1: List response shape — schemas 更新

**Files:**
- Modify: `schemas/announcements.py`

- [ ] **Step 1: Read 既有 schemas/announcements.py**

確認 `AnnouncementListOut` / item schema 欄位。

- [ ] **Step 2: 移除 item 的 `readers` 與 `recipient_ids`**

`AnnouncementListItemOut`（或對應名稱）：
- 移除 `readers: List[ReaderItem]`
- 移除 `recipient_ids: List[int]`
- 保留 `read_count`, `read_preview`, `recipient_count`, `has_more_readers`

- [ ] **Step 3: 新增兩個 response schema**

```python
class AnnouncementRecipientsOut(BaseModel):
    employee_ids: List[int]


class ReaderListItem(BaseModel):
    employee_id: int
    name: str
    read_at: Optional[str] = None


class AnnouncementReadersOut(BaseModel):
    items: List[ReaderListItem]
    total: int
    page: int
    page_size: int
```

- [ ] **Step 4: 跑 OpenAPI dump 確認無 schema 解析錯誤**

Run: `cd ivy-backend && python3 scripts/dump_openapi.py > /tmp/openapi.json`
Expected: 無 stack trace；`grep AnnouncementReaders /tmp/openapi.json` 命中。

- [ ] **Step 5: Commit**

```bash
git add schemas/announcements.py
git commit -m "feat(schema): announcement list trims readers/recipient_ids; new recipients/readers schemas"
```

---

## Task 2: list_announcements 改寫 — SQL COUNT subquery

**Files:**
- Modify: `api/announcements.py:110-193`
- Test: `tests/api/test_announcements_perf.py`

- [ ] **Step 1: 寫失敗的測試**

Create `tests/api/test_announcements_perf.py`：

```python
"""Admin announcements list perf + new lazy endpoints."""

from datetime import datetime

import pytest


def test_list_returns_read_count_recipient_count(admin_client, db_session, admin_emp):
    """list endpoint 回 read_count + recipient_count，不回完整 readers/recipient_ids。"""
    from models.database import (
        Announcement,
        AnnouncementRead,
        AnnouncementRecipient,
        Employee,
    )

    other = Employee(name="other", email="o@x", hire_date=None)
    db_session.add(other)
    db_session.flush()

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=other.id))
    db_session.add(AnnouncementRead(announcement_id=a.id, employee_id=other.id))
    db_session.commit()

    res = admin_client.get("/api/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert item["read_count"] == 1
    assert item["recipient_count"] == 1
    assert "readers" not in item
    assert "recipient_ids" not in item
    assert len(item["read_preview"]) == 1
    assert item["read_preview"][0]["employee_id"] == other.id


def test_list_read_preview_top3_by_read_at_desc(admin_client, db_session, admin_emp):
    from models.database import (
        Announcement,
        AnnouncementRead,
        Employee,
    )
    from datetime import datetime, timedelta

    emps = []
    for i in range(5):
        e = Employee(name=f"e{i}", email=f"e{i}@x", hire_date=None)
        db_session.add(e)
        db_session.flush()
        emps.append(e)

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()

    base = datetime(2026, 5, 29, 8, 0, 0)
    for i, e in enumerate(emps):
        # e0=最舊, e4=最新
        db_session.add(
            AnnouncementRead(
                announcement_id=a.id,
                employee_id=e.id,
                read_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    res = admin_client.get("/api/announcements")
    item = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert item["read_count"] == 5
    assert len(item["read_preview"]) == 3
    # 應為最新 3 名 (e4, e3, e2)
    preview_ids = [p["employee_id"] for p in item["read_preview"]]
    assert preview_ids == [emps[4].id, emps[3].id, emps[2].id]
    assert item["has_more_readers"] is True
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py -v`
Expected: FAIL（item 仍有 readers/recipient_ids，read_preview 順序不對）。

- [ ] **Step 3: 改寫 list_announcements**

替換 `api/announcements.py:110-193` `list_announcements`：

```python
@router.get("", response_model=AnnouncementListOut)
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """列出所有公告（admin）。read_count / recipient_count 走 SQL COUNT subquery；
    read_preview 走 batch query + Python group top 3。
    """
    from sqlalchemy import func, select
    from services.announcements.visibility import derive_status
    from utils.taipei_time import now_taipei_naive

    session = get_session()
    try:
        read_count_subq = (
            select(func.count(AnnouncementRead.id))
            .where(AnnouncementRead.announcement_id == Announcement.id)
            .correlate(Announcement)
            .scalar_subquery()
        )
        recipient_count_subq = (
            select(func.count(AnnouncementRecipient.id))
            .where(AnnouncementRecipient.announcement_id == Announcement.id)
            .correlate(Announcement)
            .scalar_subquery()
        )

        query = (
            session.query(
                Announcement,
                read_count_subq.label("read_count"),
                recipient_count_subq.label("recipient_count"),
            )
            .options(joinedload(Announcement.author))
            .order_by(
                Announcement.is_pinned.desc(),
                Announcement.created_at.desc(),
            )
        )
        total = session.query(func.count(Announcement.id)).scalar() or 0
        rows = query.offset((page - 1) * page_size).limit(page_size).all()

        ann_ids = [ann.id for ann, *_ in rows]
        preview_map: dict[int, list[dict]] = {}
        if ann_ids:
            preview_rows = (
                session.query(
                    AnnouncementRead.announcement_id,
                    Employee.id,
                    Employee.name,
                    AnnouncementRead.read_at,
                )
                .join(Employee, Employee.id == AnnouncementRead.employee_id)
                .filter(AnnouncementRead.announcement_id.in_(ann_ids))
                .order_by(
                    AnnouncementRead.announcement_id,
                    AnnouncementRead.read_at.desc(),
                )
                .all()
            )
            for ann_id, emp_id, emp_name, read_at in preview_rows:
                bucket = preview_map.setdefault(ann_id, [])
                if len(bucket) < 3:
                    bucket.append(
                        {
                            "employee_id": emp_id,
                            "name": emp_name,
                            "read_at": read_at.isoformat() if read_at else None,
                        }
                    )

        now = now_taipei_naive()
        results = []
        for ann, read_count, recipient_count in rows:
            preview = preview_map.get(ann.id, [])
            results.append(
                {
                    "id": ann.id,
                    "title": ann.title,
                    "content": ann.content,
                    "priority": ann.priority,
                    "is_pinned": ann.is_pinned,
                    "publish_at": ann.publish_at.isoformat() if ann.publish_at else None,
                    "expires_at": ann.expires_at.isoformat() if ann.expires_at else None,
                    "status": derive_status(ann, now),
                    "created_by": ann.created_by,
                    "created_by_name": ann.author.name if ann.author else "未知",
                    "created_at": ann.created_at.isoformat() if ann.created_at else None,
                    "updated_at": ann.updated_at.isoformat() if ann.updated_at else None,
                    "read_count": int(read_count or 0),
                    "read_preview": preview,
                    "has_more_readers": int(read_count or 0) > len(preview),
                    "recipient_count": int(recipient_count or 0),
                }
            )
        return {"total": total, "items": results}
    finally:
        session.close()
```

注意：移除原 `selectinload(reads, recipients)`、`read_employee_map`、`readers`/`recipient_ids` 序列化。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py::test_list_returns_read_count_recipient_count tests/api/test_announcements_perf.py::test_list_read_preview_top3_by_read_at_desc -v`
Expected: 2 PASS。

- [ ] **Step 5: 跑既有測試**

Run: `cd ivy-backend && pytest tests/api/test_announcements.py -v`
Expected: 若 assertion 依賴舊 `readers` / `recipient_ids` key，需在下一個 task 拆 endpoint 後一併修正；本 step 容許部分 FAIL，記錄 fail list。

- [ ] **Step 6: Commit**

```bash
git add api/announcements.py tests/api/test_announcements_perf.py
git commit -m "perf(announcements): list uses SQL COUNT subqueries + batch preview query"
```

---

## Task 3: `GET /announcements/{id}/recipients` endpoint

**Files:**
- Modify: `api/announcements.py`
- Test: `tests/api/test_announcements_perf.py`

- [ ] **Step 1: 寫失敗的測試**

於 `tests/api/test_announcements_perf.py` 追加：

```python
def test_recipients_endpoint_returns_employee_ids(admin_client, db_session, admin_emp):
    from models.database import Announcement, AnnouncementRecipient, Employee

    e1 = Employee(name="e1", email="e1@x", hire_date=None)
    e2 = Employee(name="e2", email="e2@x", hire_date=None)
    db_session.add_all([e1, e2])
    db_session.flush()

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=e1.id))
    db_session.add(AnnouncementRecipient(announcement_id=a.id, employee_id=e2.id))
    db_session.commit()

    res = admin_client.get(f"/api/announcements/{a.id}/recipients")
    assert res.status_code == 200
    assert set(res.json()["employee_ids"]) == {e1.id, e2.id}


def test_recipients_endpoint_returns_404_for_unknown(admin_client):
    res = admin_client.get("/api/announcements/999999/recipients")
    assert res.status_code == 404


def test_recipients_endpoint_requires_permission(no_perm_client):
    """無 ANNOUNCEMENTS_READ caller 應 403。"""
    res = no_perm_client.get("/api/announcements/1/recipients")
    assert res.status_code in (401, 403)
```

`no_perm_client` 若 repo 沒有現成 fixture，使用 `client.with_user(role='employee', perms=[])` 或近似 helper；找不到時可省略此 test 並在 Self-Review 補。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py -v -k "recipients"`
Expected: FAIL。

- [ ] **Step 3: 加 endpoint**

在 `api/announcements.py` 既有 `delete_announcement` 之後加：

```python
@router.get("/{announcement_id}/recipients", response_model=AnnouncementRecipientsOut)
def list_recipients(
    announcement_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """Lazy fetch admin edit dialog 用的 recipient 員工 id 清單。"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement.id)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        rows = (
            session.query(AnnouncementRecipient.employee_id)
            .filter(AnnouncementRecipient.announcement_id == announcement_id)
            .all()
        )
        return {"employee_ids": [r[0] for r in rows]}
    finally:
        session.close()
```

於檔首 import 補 `AnnouncementRecipientsOut`。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py -v -k "recipients"`
Expected: PASS（404 + 200 兩 case；perm case 視 fixture 可用性）。

- [ ] **Step 5: Commit**

```bash
git add api/announcements.py tests/api/test_announcements_perf.py
git commit -m "feat(api): GET /announcements/{id}/recipients lazy endpoint"
```

---

## Task 4: `GET /announcements/{id}/readers` endpoint

**Files:**
- Modify: `api/announcements.py`
- Test: `tests/api/test_announcements_perf.py`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_readers_endpoint_returns_paged_list_desc(admin_client, db_session, admin_emp):
    from datetime import datetime, timedelta
    from models.database import Announcement, AnnouncementRead, Employee

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.flush()
    base = datetime(2026, 5, 29, 8, 0, 0)
    emps = []
    for i in range(7):
        e = Employee(name=f"r{i}", email=f"r{i}@x", hire_date=None)
        db_session.add(e)
        db_session.flush()
        emps.append(e)
        db_session.add(
            AnnouncementRead(
                announcement_id=a.id,
                employee_id=e.id,
                read_at=base + timedelta(minutes=i),
            )
        )
    db_session.commit()

    res = admin_client.get(f"/api/announcements/{a.id}/readers?page=1&page_size=3")
    body = res.json()
    assert body["total"] == 7
    assert body["page"] == 1
    assert body["page_size"] == 3
    assert len(body["items"]) == 3
    # 最新 3 名
    assert [it["employee_id"] for it in body["items"]] == [
        emps[6].id, emps[5].id, emps[4].id,
    ]

    res2 = admin_client.get(f"/api/announcements/{a.id}/readers?page=3&page_size=3")
    body2 = res2.json()
    assert len(body2["items"]) == 1  # 最後一頁只剩 1


def test_readers_endpoint_404_for_unknown(admin_client):
    res = admin_client.get("/api/announcements/999999/readers")
    assert res.status_code == 404
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py -v -k "readers"`
Expected: FAIL。

- [ ] **Step 3: 加 endpoint**

```python
@router.get("/{announcement_id}/readers", response_model=AnnouncementReadersOut)
def list_readers(
    announcement_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """Lazy fetch admin popover 用的完整已讀名單（分頁、read_at DESC）。"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement.id)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        from sqlalchemy import func
        total = (
            session.query(func.count(AnnouncementRead.id))
            .filter(AnnouncementRead.announcement_id == announcement_id)
            .scalar() or 0
        )
        rows = (
            session.query(AnnouncementRead, Employee.name)
            .join(Employee, Employee.id == AnnouncementRead.employee_id)
            .filter(AnnouncementRead.announcement_id == announcement_id)
            .order_by(AnnouncementRead.read_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [
            {
                "employee_id": r.employee_id,
                "name": name,
                "read_at": r.read_at.isoformat() if r.read_at else None,
            }
            for r, name in rows
        ]
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()
```

於檔首 import 補 `AnnouncementReadersOut`。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py -v -k "readers"`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add api/announcements.py tests/api/test_announcements_perf.py
git commit -m "feat(api): GET /announcements/{id}/readers paginated lazy endpoint"
```

---

## Task 5: Query count baseline test（防 N+1 回歸）

**Files:**
- Test: `tests/api/test_announcements_perf.py`

- [ ] **Step 1: 寫測試**

```python
def test_list_query_count_baseline(admin_client, db_session, admin_emp):
    """100 公告 × 50 已讀 fixture 下，list endpoint 應只發 ≤4 query。"""
    from models.database import (
        Announcement,
        AnnouncementRead,
        Employee,
    )

    readers = []
    for i in range(50):
        e = Employee(name=f"r{i}", email=f"r{i}@x", hire_date=None)
        db_session.add(e)
        readers.append(e)
    db_session.flush()

    for i in range(100):
        a = Announcement(title=f"T{i}", content="C", created_by=admin_emp.id)
        db_session.add(a)
        db_session.flush()
        for r in readers:
            db_session.add(
                AnnouncementRead(announcement_id=a.id, employee_id=r.id)
            )
    db_session.commit()

    from sqlalchemy import event
    from models.database import get_session

    queries: list[str] = []

    sess = get_session()
    engine = sess.get_bind()
    sess.close()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        res = admin_client.get("/api/announcements?page=1&page_size=50")
        assert res.status_code == 200
        assert len(res.json()["items"]) == 50
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    # Expected query count（SELECT only，不含 BEGIN / ROLLBACK）：
    # 1) total COUNT
    # 2) items SELECT（含兩個 correlated COUNT subquery 仍算 1 statement）
    # 3) preview batch SELECT
    # ＋連線 setup 變動，允許 ≤4 SELECT
    selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 4, f"too many queries: {len(selects)}\n" + "\n---\n".join(
        selects
    )
```

- [ ] **Step 2: 跑測試確認 PASS（重點）**

Run: `cd ivy-backend && pytest tests/api/test_announcements_perf.py::test_list_query_count_baseline -v`
Expected: PASS。若 FAIL，檢查是否有意外 lazy load 觸發 ORM extra query（例：未移除的 `selectinload`）。

- [ ] **Step 3: Commit**

```bash
git add tests/api/test_announcements_perf.py
git commit -m "test(announcements): list query-count baseline (≤4 SELECT for 100 ann × 50 reads)"
```

---

## Task 6: 修既有 test 對舊欄位的依賴

**Files:**
- Modify: `tests/api/test_announcements.py`

- [ ] **Step 1: 跑既有測試**

Run: `cd ivy-backend && pytest tests/api/test_announcements.py -v 2>&1 | tail -40`

紀錄哪些 case 因 `readers` / `recipient_ids` 不在 list response 而 FAIL。

- [ ] **Step 2: 對每個 FAIL case 改用新 endpoint**

- 若 assertion 是 `assert item["recipient_ids"] == [...]` → 改 `res = admin_client.get(f"/api/announcements/{item['id']}/recipients"); assert set(res.json()["employee_ids"]) == {...}`
- 若 assertion 是 `assert item["readers"]` → 改打 `/readers` endpoint 比對

- [ ] **Step 3: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcements.py -v`
Expected: 全綠。

- [ ] **Step 4: Commit**

```bash
git add tests/api/test_announcements.py
git commit -m "test(announcements): migrate existing assertions to new lazy endpoints"
```

---

## Task 7: Frontend — API wrapper 加新函式

**Files:**
- Modify: `ivy-frontend/src/api/announcements.ts`

- [ ] **Step 1: Read 既有 src/api/announcements.ts**

確認 export pattern + types import 風格。

- [ ] **Step 2: 加兩個函式**

```typescript
import api from './index'
import type { AxiosResp, ApiResponse } from './_generated/typed'

// 既有 exports...

export const getAnnouncementRecipients = (
  id: number,
): Promise<AxiosResp<ApiResponse<'get', '/announcements/{announcement_id}/recipients'>>> =>
  api.get(`/announcements/${id}/recipients`)

export const getAnnouncementReaders = (
  id: number,
  params: { page?: number; page_size?: number } = {},
): Promise<AxiosResp<ApiResponse<'get', '/announcements/{announcement_id}/readers'>>> =>
  api.get(`/announcements/${id}/readers`, { params })
```

- [ ] **Step 3: 跑 OpenAPI regen**

```bash
cd ivy-backend && python3 scripts/dump_openapi.py > openapi.json
cd ../ivy-frontend && npm run gen:api
```

- [ ] **Step 4: typecheck**

Run: `cd ivy-frontend && npm run typecheck`
Expected: 0 error。

- [ ] **Step 5: Commit**

```bash
cd ivy-frontend && git add src/api/announcements.ts src/api/_generated/schema.d.ts
git commit -m "feat(api): client wrappers for /announcements/{id}/recipients|readers"
```

---

## Task 8: Frontend — AnnouncementView.vue lazy fetch + click popover

**Files:**
- Modify: `ivy-frontend/src/views/AnnouncementView.vue`

- [ ] **Step 1: import 兩個新 api**

```typescript
import {
  getAnnouncements,
  createAnnouncement,
  updateAnnouncement,
  deleteAnnouncement,
  getAnnouncementParentRecipients,
  replaceAnnouncementParentRecipients,
  getAnnouncementRecipients,
  getAnnouncementReaders,
} from '@/api/announcements'
```

- [ ] **Step 2: 加 readers cache map + loading state**

```typescript
const readersCache = ref<Record<number, { items: Array<{ employee_id: number; name: string; read_at: string | null }>; total: number; loaded: boolean }>>({})
const readersLoading = ref<Record<number, boolean>>({})
```

- [ ] **Step 3: 改寫 openEdit 為 async lazy fetch**

```typescript
const openEdit = async (row: AnnouncementItem) => {
  form.id = row.id
  form.title = row.title
  form.content = row.content
  form.priority = row.priority
  form.is_pinned = row.is_pinned
  form.publish_at = (row.publish_at as string | null) ?? null
  form.expires_at = (row.expires_at as string | null) ?? null
  form.target_employee_ids = []
  form.restrict_recipients = false
  form.parent_visibility = 'off'
  form.parent_target_classroom_ids = []
  isEdit.value = true
  dialogVisible.value = true

  try {
    const [recRes, parentRes] = await Promise.all([
      getAnnouncementRecipients(row.id),
      getAnnouncementParentRecipients(row.id),
    ])
    const empIds: number[] = (recRes.data as { employee_ids?: number[] })?.employee_ids || []
    form.target_employee_ids = empIds
    form.restrict_recipients = empIds.length > 0

    const items: { scope: string; classroom_id?: number }[] = (parentRes.data as { items?: { scope: string; classroom_id?: number }[] })?.items || []
    if (items.length === 0) {
      form.parent_visibility = 'off'
    } else if (items.some(it => it.scope === 'all')) {
      form.parent_visibility = 'all'
    } else if (items.every(it => it.scope === 'classroom' && it.classroom_id)) {
      form.parent_visibility = 'classroom'
      form.parent_target_classroom_ids = items.map(it => it.classroom_id as number)
    } else {
      form.parent_visibility = 'custom'
    }
  } catch (error) {
    ElMessage.warning('讀取設定失敗，部分欄位可能未填入')
  }
}
```

- [ ] **Step 4: 新增 ensureReadersLoaded helper**

```typescript
const ensureReadersLoaded = async (annId: number, force = false) => {
  if (!force && readersCache.value[annId]?.loaded) return
  readersLoading.value[annId] = true
  try {
    const res = await getAnnouncementReaders(annId, { page: 1, page_size: 50 })
    const data = res.data as { items?: Array<{ employee_id: number; name: string; read_at: string | null }>; total?: number }
    readersCache.value[annId] = {
      items: data.items || [],
      total: data.total ?? 0,
      loaded: true,
    }
  } catch (error) {
    ElMessage.error(apiError(error, '載入已讀名單失敗'))
  } finally {
    readersLoading.value[annId] = false
  }
}
```

- [ ] **Step 5: Popover trigger 改 click + visible-change lazy fetch**

Replace 既有 `<el-popover placement="top-start" trigger="hover" ...>` 段為：

```vue
<el-popover
  placement="top-start"
  trigger="click"
  width="280"
  @show="ensureReadersLoaded(row.id)"
>
  <template #reference>
    <el-button link type="success" class="read-preview-button">
      已讀 {{ row.read_count }} 人
    </el-button>
  </template>
  <div v-loading="readersLoading[row.id]" class="reader-popover">
    <div class="reader-popover-title">已讀名單</div>
    <div
      v-for="reader in readersCache[row.id]?.items || []"
      :key="`${row.id}-${reader.employee_id}`"
      class="reader-row"
    >
      <span>{{ reader.name }}</span>
      <span class="reader-read-at">{{ formatDate(reader.read_at) }}</span>
    </div>
    <div v-if="!readersLoading[row.id] && (readersCache[row.id]?.items || []).length === 0" class="text-muted">
      尚未有人已讀
    </div>
  </div>
</el-popover>
```

移除模板裡 `row.readers` / `getRemainingReaders` 的引用；只留 `row.read_preview` tag 列。

- [ ] **Step 6: 移除過時 helper**

刪掉 `getRemainingReaders` 與 `row.readers` 相關 type / template 段（若沒被別處用）。

- [ ] **Step 7: typecheck + build**

Run: `cd ivy-frontend && npm run typecheck && npm run build`
Expected: 0 error。

- [ ] **Step 8: Commit**

```bash
cd ivy-frontend && git add src/views/AnnouncementView.vue
git commit -m "feat(ui): announcement admin lazy fetch recipients + click popover for readers"
```

---

## Task 9: Vitest — cache + lazy fetch

**Files:**
- Modify: `ivy-frontend/tests/unit/views/AnnouncementView.test.js`

- [ ] **Step 1: 寫 test**

```javascript
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import ElementPlus from 'element-plus'
import AnnouncementView from '@/views/AnnouncementView.vue'

const getRecipients = vi.fn().mockResolvedValue({ data: { employee_ids: [1, 2] } })
const getReaders = vi.fn().mockResolvedValue({
  data: { items: [{ employee_id: 1, name: 'e1', read_at: '2026-05-29T08:00:00' }], total: 1 },
})

vi.mock('@/api/announcements', () => ({
  getAnnouncements: vi.fn().mockResolvedValue({
    data: { items: [{ id: 10, title: 'T', content: 'C', priority: 'normal', is_pinned: false, status: 'active', read_count: 1, read_preview: [], recipient_count: 2 }] },
  }),
  createAnnouncement: vi.fn(),
  updateAnnouncement: vi.fn(),
  deleteAnnouncement: vi.fn(),
  getAnnouncementParentRecipients: vi.fn().mockResolvedValue({ data: { items: [] } }),
  replaceAnnouncementParentRecipients: vi.fn(),
  getAnnouncementRecipients: getRecipients,
  getAnnouncementReaders: getReaders,
}))
vi.mock('@/stores/employee', () => ({ useEmployeeStore: () => ({ employees: [], fetchEmployees: vi.fn() }) }))
vi.mock('@/stores/classroom', () => ({ useClassroomStore: () => ({ classrooms: [], fetchClassrooms: vi.fn() }) }))

describe('AnnouncementView lazy fetch', () => {
  it('openEdit triggers recipients fetch', async () => {
    const wrapper = mount(AnnouncementView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    // 模擬點擊「編輯」
    const buttons = wrapper.findAll('button').filter(b => b.text() === '編輯')
    expect(buttons.length).toBeGreaterThan(0)
    await buttons[0].trigger('click')
    await flushPromises()
    expect(getRecipients).toHaveBeenCalledWith(10)
  })
})
```

- [ ] **Step 2: 跑 vitest**

Run: `cd ivy-frontend && npx vitest run tests/unit/views/AnnouncementView.test.js`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
cd ivy-frontend && git add tests/unit/views/AnnouncementView.test.js
git commit -m "test(ui): announcement edit dialog lazy fetch recipients"
```

---

## Self-Review checklist（implementer 完成後跑）

- [ ] Backend full pytest：`cd ivy-backend && pytest tests/ -q 2>&1 | tail -10` — 無新 regression
- [ ] Query count baseline：`pytest tests/api/test_announcements_perf.py::test_list_query_count_baseline -v` — PASS
- [ ] Frontend typecheck + build + vitest：`cd ivy-frontend && npm run typecheck && npm run build && npx vitest run` — 全綠
- [ ] OpenAPI drift：`cd ivy-frontend && npm run gen:api:check` — 無 drift
- [ ] 手測：admin 公告管理 list 顯示「已讀 N 人」按鈕，點開 popover 觸發 fetch；編輯既有公告 dialog 開啟後填入既有員工 / 家長 scope 對齊原狀

---

## Out-of-scope（不在本 plan）

- 家長已讀進度（原優化清單 #7）
- list endpoint keyset pagination
- popover 內分頁按鈕載入更多 readers（首頁 50 通常足夠；超出時關了重開重新 fetch）
