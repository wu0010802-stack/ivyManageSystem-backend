# 公告功能優化 — 排程發佈、Admin perf 修補、附件支援

**日期**：2026-05-29
**狀態**：Design
**範圍**：3 個獨立 PR（依序執行，後一 PR 依賴前一 PR 已合）

---

## 動機

公告功能（`Announcement` 表 + 員工 portal + 家長 portal）基建完整，但有三個明確痛點：

1. **無排程發佈/到期**：園長禮拜五寫好「下週一家長日通知」，目前只能放到當下立即發出；活動結束後沒有自動下架，舊公告永遠堆積。
2. **Admin list perf 隱性債**：`api/announcements.py:120-189` 對每筆公告 `selectinload(reads, recipients)` 全量載 ORM 後 `len()` + Python sort + slice，已讀累積後線性退化。
3. **無附件**：家長日通知 PDF 海報、菜單表、才藝介紹照片，目前只能塞在純文字內容，園所實務上會用 LINE 群組另外發。

本 spec 涵蓋三個改動。三個 PR 各自獨立可 ship，但 **#8 與 #2 的 list response 都被 #1 影響**（status / publish_at 欄位），所以序貫執行：#1 → #8 → #2。

---

## PR #1：排程發佈 + 到期下架

### Schema

```sql
ALTER TABLE announcements ADD COLUMN publish_at TIMESTAMP NULL;
ALTER TABLE announcements ADD COLUMN expires_at TIMESTAMP NULL;
CREATE INDEX ix_announcements_publish_at ON announcements (publish_at);
CREATE INDEX ix_announcements_expires_at ON announcements (expires_at);
```

NULL 語意：
- `publish_at IS NULL` ⇒ 立即發佈（既有資料維持此狀態，不 backfill）
- `expires_at IS NULL` ⇒ 永不過期

Alembic revision id：`annsched01`（single head）。

### 可見性 helper

新增 `api/announcements_visibility.py`（or `services/announcements.py`），匯出純函式：

```python
def visibility_time_predicate(now: datetime):
    """SQL filter：公告是否在當前時間可見（依 publish_at / expires_at）。"""
    return and_(
        or_(Announcement.publish_at.is_(None), Announcement.publish_at <= now),
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )


def derive_status(ann: Announcement, now: datetime) -> str:
    """admin 用 derived status：scheduled / active / expired。"""
    if ann.publish_at and ann.publish_at > now:
        return "scheduled"
    if ann.expires_at and ann.expires_at <= now:
        return "expired"
    return "active"
```

套用點：
- `api/portal/announcements.py` 的 `visible_filter` AND 串接
- `api/parent_portal/announcements.py` 的 `_build_visibility_subquery` 條件追加（`visible_subq.where(... AND time_predicate)`）
- `mark_announcement_read`（員工 + 家長）也套，排程前 / 過期後不允許標已讀
- `api/announcements.py` admin list **不套**（全顯示），但 response 加 `status` derived field

### Scheduler

新增 `services/announcement_publish_scheduler.py`，pattern 沿用 `leave_quota_expiry_scheduler.py`：

- asyncio polling，`stop_event` 控制
- `try_scheduler_lock` advisory（key = `"announcement_publish"`，run_key = ISO timestamp 分鐘級）
- `expected_interval_seconds=60`（足夠 8:00 排程最遲 8:01 推播）
- 走既有 `scheduler_heartbeat` 表（P0 phase observability 已落地）紀錄 last tick

Tick 邏輯：

```python
def tick(session, now: datetime, last_dispatched_at: datetime) -> int:
    """
    Find announcements with:
      publish_at > last_dispatched_at AND publish_at <= now
      AND EXISTS(parent_recipients)  -- 只對家長端有對象的才推
    For each, call _fire_announcement_push (既有 services/notification/dispatch helper)
    Persist new last_dispatched_at = now (in scheduler_heartbeat or dedicated key).
    Returns dispatched count.
    """
```

- `last_dispatched_at` 持久化：用 `scheduler_heartbeat.metadata` JSONB（既有 column）或新增專屬 key
- 員工端不發 LINE（員工只走 in-app portal，與既有設計一致；publish_at 到達時 portal list 自動顯示即可）
- Feature flag：`announcement_publish_scheduler_enabled`（預設 `True`）— 保留 kill switch
- 註冊位置：`main.py` 的 schedulers 啟動清單

### Admin endpoints 變更

`POST /api/announcements`、`PUT /api/announcements/{id}` request body 加：
- `publish_at: datetime | null`（ISO 8601）
- `expires_at: datetime | null`

驗證：
- 若兩者皆有，`expires_at > publish_at`（不可顛倒）
- `publish_at >= now() - 5min` 容錯（避免前端時間飄移阻擋立即發佈）
- 上述違規回 `400 BAD_REQUEST`，detail 中文訊息

Create-with-publish-at-future 行為：
- 既有 `create_announcement` 路徑不直接觸發推播（公告新建不推、推播由 `replace_parent_recipients` 觸發）
- `replace_parent_recipients` 觸發推播時，若 `ann.publish_at` 在未來，**跳過** `_fire_announcement_push` 呼叫（scheduler 接手）
- 過去 / NULL：維持現狀立即推

### Admin list response 加欄位

```jsonc
{
  // ... 既有欄位 ...
  "publish_at": "2026-05-30T08:00:00",
  "expires_at": null,
  "status": "scheduled"   // scheduled | active | expired
}
```

### 前端 admin（`AnnouncementView.vue`）

- form 加兩個 `el-date-picker type="datetime"`：「發佈時間（留空＝立即）」、「到期時間（留空＝永久）」
- table 新增「狀態」column：`scheduled` 灰 tag / `active` 綠 tag / `expired` 淺灰 tag
- 「對象」column 附 hint：當 publish_at 在未來且有家長 recipients 時，顯示「將於 5/30 08:00 自動推播」

### 測試

**pytest**：
- visible_filter 對 4 組合（NULL / 未來 publish / 過去 publish / 已過 expires）× 3 endpoint（admin / portal / parent）= 12 case
- scheduler tick：mock `now()`，給 5 筆混合狀態的公告 fixture，驗：
  - 進入推播的公告數 == 預期
  - `_fire_announcement_push` 對每筆呼叫一次
  - `last_dispatched_at` 推進
  - 重跑同 tick 不重複推播
- `create_announcement` with `publish_at` 在未來 + 有家長 recipients：驗 enqueue **未**被呼叫
- `replace_parent_recipients` 對 `publish_at` 未來公告：驗 enqueue 跳過

**vitest**：
- form 兩個 datetime picker 顯示與 binding
- table status column render 對 3 種狀態
- create dialog 含 publish_at 未來 + 家長 recipients 顯示推播 hint

### Out of scope（follow-up）

- 取消已排程公告（user 可手動把 publish_at 設 null 或 expires_at 設 now，不另開「取消」endpoint）
- 重複排程（每週/每月）
- 員工端 LINE 推播（員工不走 LINE）

---

## PR #8：Admin list perf 修補

### 問題

`api/announcements.py:120-189`：
- `selectinload(Announcement.reads)` 把每筆公告所有 `AnnouncementRead` 物件全載入
- `read_count = len(ann.reads)` Python in-memory count
- `recipient_count = len(recipient_ids)` 同
- `sorted_reads = sorted(ann.reads, key=...)` Python sort 全已讀後 slice top 3

100 公告 × 50 已讀 = 5000 row hydrate 只為了拿 count + 前 3 名。會隨已讀累積線性退化。

### 新 list response shape

```jsonc
{
  "id": 123,
  "title": "...",
  "content": "...",
  "priority": "...",
  "is_pinned": false,
  "created_by": 5,
  "created_by_name": "...",
  "created_at": "...",
  "updated_at": "...",
  "publish_at": null,        // PR #1 帶入
  "expires_at": null,        // PR #1 帶入
  "status": "active",         // PR #1 帶入
  "recipient_count": 12,      // SQL COUNT subquery
  "read_count": 8,            // SQL COUNT subquery
  "read_preview": [           // batch query 後 Python group + take 3
    { "employee_id": 5, "name": "陳小美", "read_at": "..." },
    { "employee_id": 7, "name": "...", "read_at": "..." }
  ],
  "has_more_readers": true    // read_count > len(read_preview)
}
```

**移除欄位**（breaking）：
- `recipient_ids`：改由 admin edit dialog 開啟時 lazy fetch
- `readers`（完整已讀名單）：改由 click popover 時 lazy fetch

### Backend 實作

`list_announcements` 改寫：

```python
read_count_subq = (
    select(func.count(AnnouncementRead.id))
    .where(AnnouncementRead.announcement_id == Announcement.id)
    .correlate(Announcement).scalar_subquery()
)
recipient_count_subq = (
    select(func.count(AnnouncementRecipient.id))
    .where(AnnouncementRecipient.announcement_id == Announcement.id)
    .correlate(Announcement).scalar_subquery()
)

query = (
    session.query(
        Announcement,
        read_count_subq.label("read_count"),
        recipient_count_subq.label("recipient_count"),
    )
    .options(joinedload(Announcement.author))
    .order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc())
)
total = session.query(func.count(Announcement.id)).scalar()
rows = query.offset((page - 1) * page_size).limit(page_size).all()

# Batch query top 3 readers per announcement
ann_ids = [r[0].id for r in rows]
preview_rows = (
    session.query(
        AnnouncementRead.announcement_id,
        Employee.id, Employee.name, AnnouncementRead.read_at,
    )
    .join(Employee, Employee.id == AnnouncementRead.employee_id)
    .filter(AnnouncementRead.announcement_id.in_(ann_ids))
    .order_by(
        AnnouncementRead.announcement_id,
        AnnouncementRead.read_at.desc(),
    )
    .all()
)
# Python group by announcement_id + take 3 per group
preview_map: dict[int, list[dict]] = {}
for ann_id, emp_id, emp_name, read_at in preview_rows:
    bucket = preview_map.setdefault(ann_id, [])
    if len(bucket) < 3:
        bucket.append({"employee_id": emp_id, "name": emp_name, "read_at": read_at.isoformat()})
```

Query 數固定：1 list + 1 count + 1 preview batch（無論公告/已讀規模）。

### 新 endpoints

#### `GET /api/announcements/{id}/recipients`

替代原 list 的 `recipient_ids`，admin 開編輯 dialog 時 lazy fetch。

- Permission：`ANNOUNCEMENTS_READ`
- Response：
  ```jsonc
  { "employee_ids": [1, 5, 7] }
  ```

#### `GET /api/announcements/{id}/readers?page=&page_size=`

替代原 list 的 `readers`，popover 點開時 lazy fetch。

- Permission：`ANNOUNCEMENTS_READ`
- Query：page (≥1, default 1), page_size (1-100, default 50) — 套既有 pagination helper（PR #43）
- Response：
  ```jsonc
  {
    "items": [
      { "employee_id": 5, "name": "陳小美", "read_at": "..." }
    ],
    "total": 8,
    "page": 1,
    "page_size": 50
  }
  ```

### 前端（`AnnouncementView.vue`）

- `openEdit()` 改 async：
  1. 立即打開 dialog，內容顯示 loading skeleton
  2. 平行 `Promise.all([fetch recipients, fetch parent-recipients])`
  3. 到齊後填表，loading 結束
- 「已讀 N 人」el-button 改 click trigger popover：
  - 首次點擊 fetch `/announcements/{id}/readers`
  - 結果 cache 在元件 reactive map，popover close 不釋放（重點：同筆公告同 session 不重 fetch）
  - 若 read_count > items.length，popover 內加「載入更多」(分頁)
- table 預設不顯示完整 readers，只渲染 read_preview tag×3 + 「已讀 N 人」按鈕

### 測試

**pytest**：
- list 對 0/1/50/100 已讀公告，read_count 正確、read_preview ≤3、has_more_readers 對齊
- `/recipients` 端權限 + 不存在公告 404
- `/readers` 端權限 + 分頁正確 + 按 read_at DESC 排序
- query 數 baseline 測試：100 公告 fixture + SQL profiler，total ≤ 3 query

**vitest**：
- `openEdit` 觸發 lazy fetch + dialog 內 loading state
- 「已讀 N 人」click 觸發 fetch，cache 命中第二次點擊不 fetch
- popover 分頁載入更多

### Out of scope

- 家長已讀進度（原優化清單 #7）— 保留為下個 PR
- list keyset pagination — 公告量級未到

---

## PR #2：附件（圖片 + PDF）

### Schema reuse

不新表。`models/portfolio.py` `Attachment` 既有多型表（`owner_type` + `owner_id`）完全適配。

新增常數：
```python
ATTACHMENT_OWNER_ANNOUNCEMENT = "announcement"
ATTACHMENT_OWNER_TYPES.add(ATTACHMENT_OWNER_ANNOUNCEMENT)
```

`Announcement` model 加 view-only relationship（不破壞既有 backref）：

```python
attachments = relationship(
    "Attachment",
    primaryjoin=(
        "and_(foreign(Attachment.owner_id) == Announcement.id, "
        "Attachment.owner_type == 'announcement', "
        "Attachment.deleted_at.is_(None))"
    ),
    viewonly=True,
    lazy="select",
)
```

無 alembic migration（只動 Python，DB 結構不變）。

### 檔案上傳擴展

`utils/file_upload.py`：
- 加 PDF magic bytes 驗證（前 5 bytes = `%PDF-` / `\x25\x50\x44\x46\x2D`）
- PDF 沿用既有 `MAX_UPLOAD_SIZE = 10MB`（不另開 PDF 上限）

`api/announcements.py` 內定義（不污染 portfolio 白名單）：
```python
_ANNOUNCEMENT_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".pdf"}
```

`storage.put_attachment`：
- 非影像副檔名（`.pdf`）跳過 PIL 縮圖路徑，原檔落 `storage_key`，`display_key` / `thumb_key` 留 NULL
- 影像維持既有 thumb / display 變體生成

### 新 endpoints

#### `POST /api/announcements/{id}/attachments` (multipart)

- Permission：`ANNOUNCEMENTS_WRITE`
- Form fields：`file: UploadFile`
- 流程：
  1. 驗 announcement 存在（404 otherwise）
  2. 驗 attachment 數量 < 5（既有 attachments 不含 deleted）；超過 reject 400「附件上限 5 個」
  3. 副檔名白名單檢查（_ANNOUNCEMENT_ALLOWED_EXT）
  4. `read_upload_with_size_check`（10MB）
  5. `validate_file_signature`（含新 PDF magic byte）
  6. `storage.put_attachment` → 取得 storage_key / display_key (影像) / thumb_key (影像)
  7. 建 `Attachment(owner_type="announcement", owner_id=announcement_id, ...)`
- Response：`_attachment_to_dict(att)` —`{ id, owner_type, owner_id, filename, mime_type, size_bytes, url, display_url, thumb_url, ... }`

#### `DELETE /api/announcements/{id}/attachments/{att_id}`

- Permission：`ANNOUNCEMENTS_WRITE`
- 驗 attachment 存在 + `owner_id == announcement_id` + `owner_type == "announcement"`（避免跨 owner 刪）
- `mark_soft_delete(att)`（既有 utility；90 天清理 job 接手實檔）
- Response：`{ "message": "附件已刪除" }`

### 下載路由 ACL 擴展

既有 `download_router` 的 `/api/uploads/portfolio/{key:path}` handler 在反查 attachment 時 dispatch：

```python
if att.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT:
    _assert_announcement_attachment_visible(session, att, current_user)
else:
    # 既有 portfolio 路徑（assert_student_access）
    ...
```

`_assert_announcement_attachment_visible`：

```python
def _assert_announcement_attachment_visible(session, att, current_user):
    """依 caller role 分流：
    - admin（is_unrestricted）：直接通過（與 admin 介面看公告管理一致）
    - employee：套 portal visible_filter（no_recipients OR targeted_to_me）+ time predicate
    - parent：套 parent visible_subquery + time predicate

    安全要點：不能用「持有 ANNOUNCEMENTS_READ」當 bypass — 該權限給 admin 才合
    理，員工 portal user 持有時仍須套 targeted_to_me 守衛（公告可能有限定對象）。
    """
    ann_id = att.owner_id
    role = current_user.get("role")

    if is_unrestricted(current_user):  # admin / hr / supervisor
        return

    if role == "parent":
        # 套既有 _build_visibility_subquery + PR #1 time predicate
        cond = _build_visibility_subquery(session, current_user["user_id"])
        time_pred = visibility_time_predicate(now_taipei_naive())
        visible = (
            session.query(Announcement)
            .filter(Announcement.id == ann_id, exists().where(...), time_pred)
            .first()
        )
        if visible is None:
            raise HTTPException(403, "無權存取此附件")
        return

    # employee role
    emp_id = current_user["employee_id"]
    no_recipients = ~exists().where(AnnouncementRecipient.announcement_id == ann_id)
    targeted_to_me = exists().where(
        and_(
            AnnouncementRecipient.announcement_id == ann_id,
            AnnouncementRecipient.employee_id == emp_id,
        )
    )
    time_pred = visibility_time_predicate(now_taipei_naive())
    visible = (
        session.query(Announcement)
        .filter(Announcement.id == ann_id, or_(no_recipients, targeted_to_me), time_pred)
        .first()
    )
    if visible is None:
        raise HTTPException(403, "無權存取此附件")
```

### List response 補欄位（3 端）

admin / portal / parent list 都加 `attachments` 欄位，由 `selectinload(Announcement.attachments)` + 序列化：

```jsonc
"attachments": [
  {
    "id": 9,
    "filename": "母親節活動.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 524288,
    "url": "/api/uploads/portfolio/...pdf",
    "thumb_url": null
  }
]
```

影像 `thumb_url` 非 NULL，PDF / 影像生成失敗時 NULL。

### LINE flex 推播

`services/notification/renderers.py` `parent.announcement` renderer：

```python
def render_parent_announcement(context, ...) -> FlexMessage:
    first_image = next(
        (a for a in context.get("attachments", []) if a["mime_type"].startswith("image/")),
        None,
    )
    if first_image and first_image.get("thumb_url"):
        # 含 hero block
        return _build_with_hero(
            title=context["title"],
            preview=context["preview"],
            hero_url=urljoin(settings.LINE_BASE_URL, first_image["thumb_url"]),
        )
    return _build_plain(title=context["title"], preview=context["preview"])
```

- 首個影像附件 → flex hero block 顯示 thumb（絕對 URL）
- PDF / 無附件 → 純文字 flex（與現狀一致）
- 不放 PDF 連結（家長點進 portal 看完整附件清單，避免 LINE 內連結失效或繞過 ACL）

需要在 `_fire_announcement_push` 的 `context` payload 補 `attachments` 陣列（從 announcement 反查）。

### 前端

#### Admin `AnnouncementView.vue`

- form 加 `<el-upload>`：
  - `multiple`, `:limit="5"`, `list-type="picture-card"`
  - `accept=".jpg,.jpeg,.png,.gif,.heic,.heif,.pdf"`
  - `:before-upload`：size > 10MB 警告 + reject
  - `:on-exceed`：「附件上限 5 個」
  - `action`：新公告流程不直接綁，**先 POST 公告拿 id**，再逐個 POST attachment
  - 編輯模式：dialog 開啟時 fetch 既有 attachments（合進 PR #8 的 lazy fetch 平行 promise），顯示縮圖（PDF 顯示 PDF icon），可逐個刪
- 新增公告 submit 流程：
  1. POST 公告 → 拿 `announcement_id`
  2. 對每個待上傳檔案 POST attachment（並行 `Promise.all`）
  3. POST replace_parent_recipients
  4. 全部成功才關 dialog；任一失敗保留 dialog + 顯示錯誤
- 編輯公告 submit 流程：
  1. PUT 公告（title / content / 限制對象等）
  2. 對新加入的檔案 POST attachment（announcement_id 已知，可直接綁）
  3. 對待刪除的既有附件 DELETE attachment
  4. PUT replace_parent_recipients
  5. 失敗策略同新增

#### Portal `PortalAnnouncementView.vue`

- 展開區塊下方 `<div class="ann-attachments">`：
  - 影像：縮圖 grid（thumb_url），點擊開新分頁看原檔
  - PDF：PDF icon + filename + 大小，點擊 `window.open(url, '_blank')`

#### Parent `AnnouncementDetailModal.vue`

- modal 內附件清單，行動裝置友善
- 縮圖網格（影像）+ PDF row（icon + filename + 下載按鈕）
- 點擊 → `window.open(url, '_blank')`

### 測試

**pytest**：
- upload：
  - PDF 成功 + Attachment row 建立 + owner_type='announcement'
  - 6 個附件第 6 個 reject 400
  - 非白名單副檔名（.exe）reject 400
  - PDF 假冒副檔名（內容非 PDF）reject 400（magic byte）
  - 超 10MB reject
  - 權限：無 ANNOUNCEMENTS_WRITE reject 403
- delete：soft delete + 不出現在 list + 不能跨 announcement 刪
- download ACL：
  - admin → 200
  - employee（targeted）→ 200
  - employee（non-targeted）→ 403
  - employee（公告 publish_at 在未來）→ 403
  - parent（可見 scope）→ 200
  - parent（不可見 scope）→ 403
  - 未登入 → 401
  - 6 case × announcement attachment
- list（admin/portal/parent）：attachments 欄位序列化正確
- soft-deleted attachment 不出現在 list

**vitest**：
- admin upload 元件：limit 5、size 10MB 警告 message、accept 副檔名
- admin 新增流程：公告 POST 成功後 attachment 上傳 + recipients 同步
- admin 編輯：dialog 開啟 lazy fetch 既有 attachments 顯示 + 可刪
- portal 詳情：影像縮圖 + PDF icon row + 點擊開新分頁
- parent modal：同 portal

**整合**：
- 手動跑：含影像附件的公告 → LINE flex hero 顯示縮圖
- 手動跑：含 PDF 附件的公告 → LINE flex 純文字、portal 內可下載 PDF

### Out of scope

- 影片附件
- 縮圖 inline 在 admin list（縮圖只在 dialog 開啟時顯示）
- 附件版本管理（取代 vs 新增）
- 公告附件獨立 audit 欄位（既有 audit middleware 已覆蓋 upload/delete 端點）

---

## 開發順序與 PR 拆法

1. **PR #1（後端 + 前端，各一 commit）** — 排程與到期
   - 後端：alembic + helper + 3 端 visible_filter + admin list status + scheduler + 創建/更新 endpoint 加欄位 + pytest
   - 前端：form datetime picker + table status column + create dialog hint + vitest
   - User 手測 + merge + push + 跑 prod migration
2. **PR #8（後端 + 前端）** — perf 修補（依賴 PR #1 已合，list response 已含 status）
   - 後端：list 改 SQL COUNT + batch preview + 兩個新 lazy endpoint + pytest（含 query count baseline）
   - 前端：openEdit 改 async lazy fetch + popover click trigger + cache + vitest
3. **PR #2（後端 + 前端）** — 附件（依賴 PR #1 time predicate helper 已存在，因為 ACL 要套）
   - 後端：Attachment owner_type 擴 + file_upload PDF magic + 2 新 endpoint + download ACL dispatch + 3 端 list 加 attachments + LINE flex hero + pytest
   - 前端：admin upload + portal/parent 詳情顯示 + vitest

每個 PR：後端 commit 一筆、前端 commit 一筆（CLAUDE.md SOP）。

---

## 既知風險

1. **`_PORTFOLIO_ALLOWED_EXT` 與 `_ANNOUNCEMENT_ALLOWED_EXT` 並存**：兩套白名單分流，避免污染 portfolio 路徑接受 PDF。upload endpoint 各自驗自己的白名單。
2. **下載路由 dispatch 風險**：`/api/uploads/portfolio/{key:path}` 加 announcement 分流，要小心測 5+ ACL case，**不能讓任一 owner_type 漏 ACL**。建議寫一個明確的 dispatch table 並 unit test 覆蓋。
3. **`scheduler_heartbeat` 表 metadata JSONB 用法**：`last_dispatched_at` 持久化要選對 key + 避免與其他 scheduler 衝突。若 metadata JSONB pattern 在現有 schedulers 中未明確，改用 `last_dispatched_at` 從 `scheduler_heartbeat` 表的 `last_tick_at` 推（每分鐘掃 publish_at <= now AND publish_at > last_tick_at）。
4. **`_fire_announcement_push` 對 publish_at 未來公告需跳過**：實作要明確在 `replace_parent_recipients` flow 內加 guard，否則 race 會雙推（人工 PUT + scheduler tick）。
5. **LINE flex hero URL**：thumb_url 是相對路徑，LINE 收到必須絕對 URL；用 `settings.LINE_BASE_URL` 組（既有設定）。
6. **`AnnouncementParentRecipient` 推播在 publish_at 未來的設定**：使用者可能在公告 publish_at 之前多次 PUT parent-recipients，每次都應跳過 enqueue；scheduler tick 才是唯一推播觸發。

---

## 不在本 spec 範圍（follow-up backlog）

來自原優化清單，留作後續 PR：
- #3 HTML/Markdown 內容（取代 `_strip_html`）
- #4 公告分類 tag（行政 / 活動 / 健康 / 政令）
- #5 強制簽收（合規公告 ack）
- #6 發送預覽 + LINE 可達性
- #7 admin 看家長已讀進度
- #10 搜尋/篩選
- #11 編輯標示（edited_at）
