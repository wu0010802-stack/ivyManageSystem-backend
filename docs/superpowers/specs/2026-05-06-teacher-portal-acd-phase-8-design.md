# 教師端 Portal 大 polish — Phase 8 設計（後端 polish 收尾）

## 背景

教師端 ACD 改造 6 個 view phase 全完成；最後一個 phase 處理「跟著 view 改不會剛好碰到、但 spec 列為改善」的後端項目。

## 範圍

### 必做

**1. parent_messages.py attachment N+1 修補**

3 處迴圈呼叫 `_attachments_for_message(session, m.id)` 每 message 一次 query：
- line 325（thread list endpoint）
- line 409（mark-read endpoint）
- line 457（reply endpoint）

修補：用 `Attachment.owner_id.in_(message_ids)` + dict batch lookup。

**2. ETag for 3 個 endpoint**

| Endpoint | ETag 計算 | 預期收益 |
|---|---|---|
| `/my-schedule` | `Last-Modified: max(DailyShift.updated_at, ShiftAssignment.updated_at)` for the month + If-Modified-Since 處理 | mobile 重整 -50% 流量 |
| `/announcements` | `ETag: hash(sorted_id_list + max(created_at))` + If-None-Match 處理 | 列表查詢 304 回應 |
| `/my-class-attendance/monthly` | `Last-Modified: max(StudentAttendance.updated_at)` | 月統計輪詢 304 |

只實作 304 not modified 流程（不引入 Redis）。

**3. audit_skip 清理 + audit_summary 補強（4 處）**

- `api/portal/home.py:254`（GET /home/summary）
- `api/portal/medications.py:190`（GET 列表）
- `api/portal/parent_messages.py:254`（GET 列表）
- `api/portal/parent_messages.py:544`（attachment GET）

把 `request.state.audit_skip = True` 拿掉，改為 `request.state.audit_summary = "<簡短描述>"` 讓 audit middleware 紀錄但不寫業務 detail。

### 砍掉

- ❌ `_assert_classroom_owned` dedupe：既有 helper 在 `contact_book.py` 內部 DRY 用 11 次，沒散落多檔，不需移到 `_shared.py`

---

## Branch

`feat/teacher-acd-v1-8-backend-polish` from `origin/main`（純後端）。

## 驗收

- [ ] parent_messages 3 處 N+1 修補；補 query count regression test
- [ ] 3 個 endpoint 加 ETag/Last-Modified；If-None-Match 命中回 304
- [ ] 4 處 audit_skip 改為 audit_summary
- [ ] pytest 全綠（除 main 既有 4 個 fail）

## 預估工作量

~4-6 小時。
