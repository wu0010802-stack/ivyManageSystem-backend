# SPEC-012：家長入口與 PII Retention

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `api/parent_portal/` 全 24 檔（含 `_shared.py` / `_dependencies.py` / `auth.py` 等）、`api/guardians_admin.py`、`api/line_webhook.py`、`services/pii_retention_scheduler.py`、`services/parent_assistant_service.py`、`services/parent_message_service.py`、`services/line_login_service.py`、`utils/student_lifecycle.py`、`models/guardian.py` + `parent_binding.py` + `parent_refresh_token.py` + `parent_message.py` + `parent_notification.py` + `parent_db.py`、`schemas/parent_assistant.py` |
| Related | SPEC-007（權限系統 `PARENT_MESSAGES_WRITE` / `GUARDIANS_*` / `STUDENTS_LIFECYCLE_WRITE`）、SPEC-008（家長通知 dispatch / `notification_preferences` gate）、`docs/superpowers/specs/2026-05-22-parent-pii-retention-data-export-design.md`、`docs/superpowers/specs/2026-05-03-parent-line-refresh-token-design.md`、`alembic/versions/20260522_pretent001_pii_retention_columns.py`（`students.terminal_entered_at` + `guardians.pii_redacted_at`） |

---

## Overview

家長入口是一個與員工 `api/portal/` 並列、走 **LINE LIFF 認證** 的家長端 API 子系統，全部掛在 `/api/parent/*` 前綴下；個別行政發碼端點掛在 `/api/guardians/*`。其業務目標分為三層：

1. **認證**：家長前端以 LINE LIFF SDK 取得 `id_token`，後端委派 LINE 官方 `/verify` 驗簽 + `aud` 校驗（`LineLoginService`，channel_id 由 `LINE_LOGIN_CHANNEL_ID` env 設定）；首次登入經行政簽發的一次性 **綁定碼**（`GuardianBindingCode`，sha256 hash、24h、12 位英數）建立 `User(role='parent')` 與 `Guardian.user_id` 關聯；後續以 access token（cookie）+ refresh token（rotation, family revoke on reuse）維持登入。
2. **資源讀寫**：家長僅能對「自己監護的學生」(`Guardian.user_id = current_user.id AND Guardian.deleted_at IS NULL`) 進行考勤 / 公告 / 聯絡簿 / 用藥 / 請假 / 訊息 / 才藝報名 / 通知偏好 / 個資匯出 等操作；**雙層 IDOR 防線**：應用層 `_assert_student_owned()` 給 403、DB 層 PostgreSQL Row-Level Security（`parent_isolate_*` policy，由 `get_parent_db` dependency 透過 `SET LOCAL app.current_user_id` 驅動）。
3. **PII Retention 合規（個資法 §11）**：學生 `lifecycle_status` 進終態（`graduated` / `transferred` / `withdrawn`）寫 `terminal_entered_at` 戳記，**365 天後**（ENV `PII_RETENTION_TERMINAL_DAYS` 可調）由 `services/pii_retention_scheduler.py` 每日 GC：抹除 `Guardian.phone/email/relation/custody_note=NULL`、`name='[已離校家長]'`、`user_id=NULL`，不刪 Guardian row、不動 Student PII、不刪 User row；**復學自動取消**（`set_lifecycle_status` 從終態回非終態時 `terminal_entered_at=NULL`）；ENV `PII_RETENTION_GC_DISABLED=1` 關閉、`PII_RETENTION_GC_DRY_RUN=1` 只 log 不寫。資料查閱權（個資法 §10）走 `GET /api/parent/me/data-export`，rate-limit 1/hr/user、50MB 上限。

合規重點：

- `Student.lifecycle_status` **任何變更必經** `utils/student_lifecycle.set_lifecycle_status`（內部呼叫的 `services/student_lifecycle.transition()` 已自動走 helper），絕不可直接 `student.lifecycle_status = ...`，否則 `terminal_entered_at` 戳記漏寫，PII GC 無法定位到期 row。
- 抹除動作為 **不可逆**；上線啟用流程必須先設 `PII_RETENTION_GC_DRY_RUN=1`，由 scheduler log 印出待抹清單，人工 review 後再設 `PII_RETENTION_GC_DRY_RUN=0` 正式執行。
- `Guardian.pii_redacted_at` 為冪等戳記（NOT NULL 即已抹過），GC SQL 帶 `pii_redacted_at IS NULL` 過濾避免重複處理同一筆。
- LINE Login Channel 必須與既有 Messaging Bot Channel 掛在同一個 LINE Provider 下，否則 `id_token.sub` / `webhook.source.userId` / `push.to` 會是三個不同的值，家長綁定後仍無法被推播。

---

## Interface Definitions

### 家長端 HTTP API（`api/parent_portal/`）

所有端點前綴 `/api/parent/`，登入後皆掛 `require_parent_role()`，並以 `_assert_student_owned()` 做 IDOR 守衛；DB session 走 `get_parent_db`（RLS engine）或 legacy `get_session()`（尚未遷移的子模組）。

#### 認證群（auth / profile / family）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `POST /auth/liff-login` | `auth.liff_login` | 無（IP rate-limit） | `{id_token: str}` | `{status: "ok"\|"need_binding", user?, line_user_id?, name_hint?}` |
| `POST /auth/bind` | `auth.bind_first_child` | `parent_bind_token` cookie + DB-backed line_user_id 失敗鎖（5次/15分） | `{code: str(4-20)}` | `{status: "ok", user}` |
| `POST /auth/bind-additional` | `auth.bind_additional_child` | `require_parent_role` | `{code: str(4-20)}` | `{status: "ok", guardian_id, student_id}` |
| `POST /auth/logout` (204) | `auth.parent_logout` | `require_parent_role` | — | 清 cookie + bump `token_version` + revoke refresh family |
| `POST /auth/refresh` | `auth.parent_refresh` | refresh cookie（rotation） | — | `{status: "ok", user}` ／ 409 race ／ 401 reuse-revoked |
| `GET /me` | `profile.get_me` | `require_parent_role` | — | `{user_id, name, line_user_id, role, can_push, last_login}` |
| `GET /my-children` | `profile.get_my_children` | `require_parent_role` | — | `{items: [...], total}` |
| `GET /students/{student_id}/profile` | `profile.get_child_profile` | `require_parent_role` + IDOR | path `student_id` | `{student, classroom, teachers, guardians, allergies}` |
| `GET /family/timeline` | `family.family_timeline` | `require_parent_role` + IDOR | query `student_id`、`limit(1-50, 預設7)` | `[{kind, id, title, subtitle, occurred_at, is_pending, href}]`（30s in-process cache） |

#### 首頁聚合（home）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `GET /home/summary` | `home.home_summary` | `require_parent_role` | — | `{me, children, summary:{unread_announcements, fees, pending_event_acks, unread_messages, pending_activity_promotions, recent_leave_reviews}}`（60s cache） |
| `GET /home/today-status` | `home.today_status` | `require_parent_role` | — | `{date, children:[{student_id, name, attendance, leave, medication, dismissal}]}` |

#### 學生資訊群（attendance / calendar / growth_reports / milestones / measurements / fees / leaves / medications / photos）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `GET /attendance/daily` | `attendance.get_daily_attendance` | `require_parent_role` + IDOR | query `student_id`、`date(YYYY-MM-DD, 選填)` | `{student_id, date, status, remark}` |
| `GET /attendance/monthly` | `attendance.get_monthly_attendance` | `require_parent_role` + IDOR | query `student_id`、`year(2000-2100)`、`month(1-12)` | `{student_id, year, month, items, counts, recorded_days}` |
| `GET /calendar/week` | `calendar.get_week_agenda` | `require_parent_role`（內部 IDOR） | query `days(1-14, 預設7)`、`student_id(選填)` | `{from, to, items}` |
| `GET /calendar/month` | `calendar.get_month_agenda` | `require_parent_role`（內部 IDOR） | query `year`、`month`、`student_id(選填)` | `{year, month, from, to, items}` |
| `GET /growth-reports` | `growth_reports.<list>` | `require_parent_role` + IDOR | query `student_id` | `{items}`（家長端白名單，遮 admin 內部欄如 `error_message`/`file_path`/`generated_by`） |
| `GET /growth-reports/{report_id}/download` | `growth_reports.<download>` | `require_parent_role` + IDOR | path `report_id`、query `student_id` | `FileResponse` PDF |
| `GET /milestones` | `milestones.parent_list_milestones` | `require_parent_role` + IDOR | query `student_id`、`limit(1-200, 預設50)` | `{items}` |
| `POST /milestones/{milestone_id}/react` | `milestones.parent_react` | `require_parent_role` + IDOR + 10/60s rate-limit | path、query `student_id`、`{reaction: like\|love\|celebrate}` | milestone dict（`with_for_update` 防 attribution race） |
| `POST /milestones/{milestone_id}/acknowledge` | `milestones.parent_acknowledge` | `require_parent_role` + IDOR | path、query `student_id` | milestone dict |
| `GET /measurements` | `measurements.parent_list_measurements` | `require_parent_role` + IDOR | query `student_id`、`months(1-36, 預設24)` | `{items}`（HARD_ROW_LIMIT=500） |
| `GET /measurements/chart-data` | `measurements.parent_measurement_chart` | `require_parent_role` + IDOR | 同上 | `{height, weight, head_circumference, vision_left, vision_right}` series |
| `GET /fees/summary` | `fees.fees_summary` | `require_parent_role`（內部 IDOR） | — | `{by_student, totals}` |
| `GET /fees/records` | `fees.list_records` | `require_parent_role` + IDOR | query `student_id`、`period(選填)` | `{items, total}`（不揭露 operator/refunded_by） |
| `GET /fees/records/{record_id}/payments` | `fees.list_payments` | `require_parent_role` + 403 collapse | path `record_id` | `{fee_item_name, period, payments, refunds}`（IDOR 與不存在合 403 防枚舉） |
| `POST /student-leaves` (201) | `leaves.create_leave` | `require_parent_role` + IDOR(for_write) | `CreateLeaveRequest` | `_serialize(leave)`（自動 `status=approved`，提交即生效並 `apply_attendance_for_leave`，`client_request_id` 冪等） |
| `GET /student-leaves` | `leaves.list_leaves` | `require_parent_role`（內部 IDOR） | — | `{items, total}` |
| `GET /student-leaves/{leave_id}` | `leaves.get_leave` | `require_parent_role` + IDOR | path | `_serialize(leave)` |
| `POST /student-leaves/{leave_id}/attachments` (201) | `leaves.<attach>` | `require_parent_role` + IDOR | multipart `file` | attachment dict |
| `DELETE /student-leaves/{leave_id}/attachments/{attachment_id}` | `leaves.delete_leave_attachment` | `require_parent_role` + IDOR | path | 204 |
| `POST /student-leaves/{leave_id}/cancel` | `leaves.cancel_leave` | `require_parent_role` + IDOR | path | `_serialize(leave)`（僅 status=approved 且 start_date>today 可取消，反向清除 attendance） |
| `GET /medication-orders` | `medications.list_medication_orders` | `require_parent_role` + IDOR | query `student_id`、`from`、`to` | `{items}` |
| `GET /medication-orders/{order_id}` | `medications.get_medication_order` | `require_parent_role` + IDOR | path | order dict（含 logs / photos） |
| `POST /medication-orders` (201) | `medications.create_medication_order` | `require_parent_role` + IDOR(for_write) | `ParentMedicationOrderCreate(+ acknowledge_allergy_warning?: bool)` | order dict ／ 409 `ALLERGY_WARNING` |
| `POST /medication-orders/{order_id}/photos` (201) | `medications.upload_medication_photo` | `require_parent_role` + IDOR | multipart | attachment dict |
| `DELETE /medication-orders/{order_id}/photos/{attachment_id}` | `medications.delete_medication_photo` | `require_parent_role` + IDOR | path | 204 |
| `GET /photos` | `photos.parent_list_photos` | `require_parent_role` + IDOR | query `student_id`、`skip(>=0)`、`limit(1-200, 預設50)` | `{total, items}` 反查 4 種 owner_type 圖片並過濾「未發布 / 軟刪 / 未 READY」 |

#### 通訊群（contact_book / messages / notifications）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `GET /contact-book/today` | `contact_book.get_today` | `require_parent_role` + IDOR | query `student_id` | `{student_id, log_date, entry}` |
| `GET /contact-book` | `contact_book.list_history` | `require_parent_role` + IDOR | query `student_id`、`from`、`to`、`limit(1-100, 預設30)` | `{student_id, from, to, entries}` |
| `GET /contact-book/{entry_id}` | `contact_book.get_detail` | `require_parent_role` + IDOR | path | entry dict |
| `POST /contact-book/{entry_id}/ack` | `contact_book.mark_read` | `require_parent_role` + IDOR | path | `{entry_id, read_at, already_marked}`（idempotent；非同步 broadcast 給班級教師 WS） |
| `POST /contact-book/{entry_id}/reply` (201) | `contact_book.reply` | `require_parent_role` + IDOR | `ReplyCreate(body, client_request_id?)` | reply dict（client_request_id 冪等） |
| `DELETE /contact-book/{entry_id}/replies/{reply_id}` | `contact_book.delete_reply` | `require_parent_role` + IDOR | path | 204 |
| `GET /messages/threads` | `messages.list_threads` | `require_parent_role` | query `cursor`、`limit(1-100, 預設20)` | `{items, next_cursor}` |
| `GET /messages/threads/{thread_id}` | `messages.get_thread` | `require_parent_role` + thread IDOR | path | thread summary |
| `GET /messages/threads/{thread_id}/messages` | `messages.list_messages` | `require_parent_role` + thread IDOR | path、query `cursor`、`limit(1-100, 預設30)` | `{items, next_cursor}` |
| `POST /messages/threads/{thread_id}/messages` (201) | `messages.post_reply` | `require_parent_role` + thread IDOR | `ReplyMessage(body, client_request_id?)` | message dict + `idempotent_replay`（家長僅可回覆既有 thread，**不可主動建 thread**） |
| `POST /messages/threads/{thread_id}/messages/{message_id}/attach` (201) | `messages.attach_to_message` | `require_parent_role` + thread IDOR | multipart `file` | attachment dict |
| `POST /messages/threads/{thread_id}/read` (200) | `messages.mark_thread_read` | `require_parent_role` + thread IDOR | path | 更新 `parent_last_read_at` |
| `POST /messages/messages/{message_id}/recall` (200) | `messages.recall_message` | `require_parent_role` + sender check | path | 30 分內 sender 可撤回 |
| `GET /messages/unread-count` | `messages.unread_count` | `require_parent_role` | — | `{unread_count}` |
| `GET /notifications/preferences` | `notifications.get_preferences` | `require_parent_role` | — | `{channel: "line", prefs: {event_type: enabled}}`（稀疏 row：缺 row 視為 enabled） |
| `PUT /notifications/preferences` | `notifications.update_preferences` | `require_parent_role` | `PreferenceUpdate(prefs: dict[str, bool])` | 同上（舊 key 自動 mapping 新 key） |

#### 才藝/活動群（activity / announcements / events）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `GET /activity/courses` | `activity.list_courses` | `require_parent_role` | query `school_year`、`semester(1-2)` | course list |
| `GET /activity/my-registrations` | `activity.my_registrations` | `require_parent_role`（內部 IDOR） | — | `{items, total}` |
| `POST /activity/register` (201) | `activity.register_courses` | `require_parent_role` + IDOR(for_write) | `RegisterPayload(student_id, school_year, semester, course_ids[], supply_ids[])` | registration summary |
| `POST /activity/registrations/{registration_id}/confirm-promotion` | `activity.confirm_promotion` | `require_parent_role` + IDOR | path | 候補升正式 |
| `GET /activity/registrations/{registration_id}/payments` | `activity.registration_payments` | `require_parent_role` + IDOR | path | payments list（隱藏 operator 欄位） |
| `GET /announcements` | `announcements.list_announcements` | `require_parent_role`（scope 過濾） | query `skip`、`limit(1-100, 預設20)` | `{items, total}` 依 `all`/`classroom`/`student`/`guardian` 4 scope 可見 |
| `GET /announcements/unread-count` | `announcements.unread_count` | `require_parent_role` | — | `{unread_count}` |
| `POST /announcements/{announcement_id}/read` (200) | `announcements.mark_read` | `require_parent_role` + 可見性檢查 | path | `{status: "ok"}`（不可見回 403，防洩漏存在） |
| `GET /events` | `events.list_events` | `require_parent_role` | — | `{items, total}` 近 30 天 + 未來 180 天的學校行事曆，含「每位子女是否已簽」狀態 |
| `POST /events/{event_id}/ack` (200) | `events.acknowledge_event` | `require_parent_role` + IDOR(for_write) | `AckRequest(student_id, signature_name?)` | `{status, already_acknowledged, ack_id, acknowledged_at}`（過 `ack_deadline` 拒 400） |
| `POST /events/{event_id}/ack/signature` (201) | `events.upload_ack_signature` | `require_parent_role` + IDOR | multipart `file` (PNG, <=200KB) | signature dict |

#### 個資群（data_export / parent_downloads / timeline / assistant）

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `GET /me/data-export` | `data_export.get_data_export` | `require_parent_role` + **rate-limit 1/hr/user** | — | `application/json` attachment（包 students/guardians/attendance/leaves/fees/medications/photos/messages/growth_reports 全歷史；50 MB 上限超過 413；寫 `write_explicit_audit READ`） |
| `GET /uploads/portfolio/{key:path}` | `parent_downloads.download_parent_portfolio` | `require_parent_role` + owner → student IDOR | path `key` | `FileResponse`（含 storage/display/thumb 三 key；軟刪 410、實體遺失 404；每次下載寫 audit 不 dedup） |
| `GET /timeline` | `timeline.<list>` | `require_parent_role` + IDOR | query `student_id`、`since`、`until`、`types`、`cursor`、`limit` | timeline items（聚合多 SOURCE_TYPES，read-only） |
| `GET /assistant/faq` | `assistant.get_faq` | `require_parent_role` | — | `FaqResponse`（mtime cache、`Cache-Control: private, max-age=300`） |

### Admin 端 HTTP（`api/guardians_admin.py`）

掛 `parent_router` 之外的 `admin_router`，前綴 `/api/guardians`。

| Endpoint | Function | Permission | Request | Response |
|----------|----------|------------|---------|----------|
| `POST /guardians/{guardian_id}/binding-code` | `guardians_admin.create_binding_code` | `require_staff_permission(Permission.GUARDIANS_WRITE)` | path | `{code: str(12), expires_at}`（明碼僅此次回；DB 存 sha256；24h TTL；單一 guardian active cap=3，超過 409；寫 AuditLog + logger.warning） |

### LINE Webhook 入口（`api/line_webhook.py`）

家長綁定觸發點。

| Endpoint | Function | 簽名驗證 | 路由分發 |
|----------|----------|----------|---------|
| `POST /api/line/webhook` | `line_webhook.line_webhook` | `verify_line_signature`（HMAC-SHA256 + channel_secret） | `webhookEventId` 去重（`LineWebhookEvent` UNIQUE）+ `event.timestamp` skew ±5 分鐘擋 replay；`follow` 事件寫 `User.line_follow_confirmed_at`；`message`/`postback` 區分教師（既有 handler）vs 家長（`line_reply_router.handle_parent_*`） |

### 內部 Python 函式

#### `services/pii_retention_scheduler.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `scheduler_enabled()` | function | `not get_settings().scheduler.pii_retention_gc_disabled` |
| `dry_run_enabled()` | function | `bool(get_settings().scheduler.pii_retention_gc_dry_run)` |
| `retention_days()` | function | `int(get_settings().scheduler.pii_retention_terminal_days or 365)` |
| `run_pii_retention_scheduler(stop_event)` | async function | 主迴圈；啟動後 60s 首跑（避免冷啟動同時打 DB），之後每 24h 跑一次 |
| `_run_pii_retention_gc(session=None)` | function | 單次 GC：`SELECT g.id FROM guardians g JOIN students s ON s.id = g.student_id WHERE s.lifecycle_status IN ('graduated','transferred','withdrawn') AND s.terminal_entered_at < cutoff AND g.pii_redacted_at IS NULL AND g.deleted_at IS NULL LIMIT 500 FOR UPDATE SKIP LOCKED`；dry_run 直接 rollback；正式則一次 UPDATE 抹 PII（`name='[已離校家長]'`、`phone/email/relation/custody_note/user_id=NULL`、`pii_redacted_at=NOW`）+ 每筆寫 `AuditLog(username='pii_retention_gc')` |

模組層常數：`_GC_INTERVAL_SEC = 86400`、`_INITIAL_DELAY_SEC = 60`、`_BATCH_LIMIT = 500`。

#### `services/line_login_service.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `LineLoginService(channel_id)` | class | LIFF id_token 驗證 singleton；`main.py` 啟動時建立並透過 `init_parent_line_service` 注入 |
| `LineLoginService.is_configured()` | method | `bool(self.channel_id)`；空 channel_id → 503 |
| `LineLoginService.verify_id_token(id_token)` | method | 呼叫 `https://api.line.me/oauth2/v2.1/verify`（timeout 5s）；驗 200 + `aud == channel_id`（嚴格相等）+ `sub` 為非空字串；最末步 `_check_id_token_replay` 用 sha256 hash dedup（5 分鐘 TTL，多 worker 仍 N× 通過率但配合 IP rate-limit 已足） |
| `LineLoginService._check_id_token_replay(id_token)` | private | in-process dict `_ID_TOKEN_SEEN` (`{hash: expire_ts}`)；每次呼叫順手 GC 過期項 |

模組層常數：`LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"`、`_VERIFY_TIMEOUT_SECONDS = 5.0`、`_ID_TOKEN_TTL_SECONDS = 300`。

#### `services/parent_message_service.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `RECALL_WINDOW` | constant | `timedelta(minutes=30)`；sender 撤回視窗 |
| `assert_teacher_is_homeroom(session, *, employee_id, student_id)` | function | 守衛：current employee 必須是該 student 所屬 classroom 的 head_teacher；助教/才藝可回覆既有 thread，不可發起；admin role bypass 由 endpoint 層處理 |
| `get_or_create_thread(session, *, parent_user_id, teacher_user_id, student_id)` | function | 三元組 UNIQUE thread upsert（race tolerant：`IntegrityError` 重查） |
| `assert_thread_participant(thread, *, user_id, role)` | function | IDOR 守衛：caller 必須是 thread 的 parent 或 teacher |
| `append_message(session, *, thread, sender_user_id, sender_role, body, client_request_id, source='app')` | function | 寫訊息；回 `(message, is_replay)`；`(thread_id, client_request_id)` 冪等；race 容忍 |
| `can_recall(msg, *, user_id)` | function | sender 自己 + 30 分內 + `deleted_at` 為空 才可撤 |
| `mark_read(session, *, thread, role, when=None)` | function | 更新 `parent_last_read_at` / `teacher_last_read_at` |
| `count_unread_for_parent(session, *, parent_user_id)` | function | 單一 JOIN query 計家長未讀（取代 N+1） |
| `count_unread_for_teacher(session, *, teacher_user_id)` | function | 教師未讀 |

#### `services/parent_assistant_service.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `ParentAssistantService.get_faq()` | classmethod | 讀 `data/parent_faq.json`，以檔案 mtime 做 in-memory cache（園所偶爾不重啟也能編輯 FAQ） |

#### `utils/student_lifecycle.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `set_lifecycle_status(session, student, new_status, *, actor_user_id=None, audit=True, reason=None, request=None)` | function | **必經點**。原子化變更 lifecycle + 維護 `terminal_entered_at`：非終態→終態寫 `datetime.now(timezone.utc)`、終態→非終態寫 NULL（取消 retention）、其他不動；可寫 `AuditLog`；有 request context 時觸發 `mark_soft_delete` 讓 AuditMiddleware 標軟刪 summary |

模組層常數：`_TERMINAL_LIFECYCLE = frozenset({LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN})`。

#### `api/parent_portal/_shared.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `_get_parent_user(session, current_user)` | function | 從 JWT payload 取家長 User；非 parent role / user 不存在 / is_active=False 一律 401/403 |
| `_get_parent_student_ids(session, user_id)` | function | 回 `(guardian_ids, student_ids)`；過 `deleted_at IS NULL`；同 student_id 多 Guardian 去重；**JWT 不快取此資訊**（即時查 DB 避免新增/移除小孩 stale token） |
| `_assert_student_owned(session, user_id, student_id, *, for_write=False)` | function | 非自己小孩 → 403；`for_write=True`（audit 2026-05-07 P0）額外擋已退學/畢業/轉出子女的寫入（投藥單/簽收/新聯絡簿/新請假），讀歷史路徑保留 |
| `resolve_parent_display_name(session, user)` | function | hero / 問候語顯示名解析（`user.display_name` → 主要 Guardian.name → 最早 Guardian.name → `"家長"`）；**絕不**回傳 `user.username`（為 `parent_line_<line_user_id>` 內部識別碼） |

#### `api/parent_portal/_dependencies.py`

| Symbol | Type | 用途 |
|--------|------|------|
| `get_parent_db(current_user=Depends(require_parent_role()))` | dependency | yield `Session` 綁 RLS 強制的 parent engine（`models/parent_db.get_parent_session_dep`），每筆 request 用 `SET LOCAL app.current_user_id` 設定當前 user_id；handler **絕不可** `session.commit()`（會結束 dep 的 tx 導致 `SET LOCAL` 失效，後續查詢回 0 row）；env 缺 `PARENT_DB_USER` / `PARENT_DB_PASSWORD` 直接 RuntimeError fail-loud |

#### `api/parent_portal/auth.py` 內部 helper

| Symbol | Type | 用途 |
|--------|------|------|
| `_gen_refresh_raw()` | function | 384-bit base64url（約 64 字）原始 token |
| `_hash_refresh(raw)` | function | sha256 hex |
| `_set_refresh_cookie(response, raw)` / `_clear_refresh_cookie(response)` | function | refresh cookie，path `/api/parent/auth`、HttpOnly、SameSite/Secure 依 env |
| `_issue_refresh_token(session, response, *, user_id, family_id=None, parent_token_id=None, ...)` | function | 寫 `ParentRefreshToken` row + Set-Cookie；family_id=None 表新裝置產新 family |
| `gc_expired_refresh_tokens(session, *, retention_days=7)` | function | 刪除 `expires_at < cutoff` 或 `revoked_at < cutoff` 的 row（GC retention 7 天） |
| `_claim_binding_code_atomic(session, code_hash, claimer_user_id)` | function | atomic `UPDATE WHERE used_at IS NULL AND expires_at > now`；rowcount=1 才算成功（防 race） |
| `_diagnose_binding_failure(session, code_hash)` | function | atomic claim 失敗後診斷，回 `BIND_CODE_NOT_FOUND` / `BIND_CODE_EXPIRED` / `BIND_CODE_USED`（讓前端分流文案） |
| `_check_bind_lockout(line_user_id)` / `_record_bind_failure` / `_clear_bind_failures` | function | DB-backed `rate_limit_buckets` 計數，line_user_id 為單位失敗 5 次鎖 15 分鐘；fail-open on DB 故障 |

---

## DTO Definitions

### `models/guardian.py` — `Guardian`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `student_id` | Integer FK → students.id (CASCADE) | False | |
| `user_id` | Integer FK → users.id (SET NULL) | True | 家長 User；同一 User 可被多 Guardian 引用支援一家長綁多孩 |
| `name` | String(50) | False | PII GC 後改為 `'[已離校家長]'` |
| `phone` | String(20) | True | PII GC 抹 NULL |
| `email` | String(100) | True | PII GC 抹 NULL |
| `relation` | String(20) | True | PII GC 抹 NULL；可選值 `GUARDIAN_RELATIONS = ["父親","母親","祖父","祖母","外公","外婆","監護人","其他"]` |
| `is_primary` | Boolean | False | 主要聯絡人（同學生至多一位） |
| `is_emergency` | Boolean | False | 緊急聯絡人 |
| `can_pickup` | Boolean | False | 授權接送 |
| `custody_note` | Text | True | 監護權說明；PII GC 抹 NULL |
| `sort_order` | Integer | False | |
| `deleted_at` | DateTime | True | 軟刪 |
| `pii_redacted_at` | DateTime | True | **GC 戳記**（NOT NULL 即已抹過，避免重複） |
| `created_at` / `updated_at` | DateTime | False | 用 `now_taipei_naive`（Asia/Taipei naive 契約） |

Indexes：`ix_guardians_student(student_id)`、`ix_guardians_student_active(student_id, deleted_at)`、`ix_guardians_phone(phone)`、`ix_guardians_user(user_id)`。

### `models/parent_binding.py` — `GuardianBindingCode`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `guardian_id` | Integer FK → guardians.id (CASCADE) | False | indexed |
| `code_hash` | String(64) UNIQUE | False | sha256(明碼) hex；明碼僅回簽發者一次 |
| `expires_at` | DateTime | False | 預設 24h |
| `used_at` | DateTime | True | claim 成功時間；non-null 不可重用 |
| `used_by_user_id` | Integer FK → users.id (SET NULL) | True | claim 該碼的家長 |
| `created_by` | Integer FK → users.id | False | 簽發行政（稽核用） |
| `created_at` | DateTime | False | |

Index：`ix_guardian_binding_expires_unused(expires_at, used_at)`。

明碼規則：12 位英數，alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（去 I/O/0/1 防誤讀）；單一 guardian active 上限 3，超過 409。

### `models/parent_refresh_token.py` — `ParentRefreshToken`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `user_id` | BigInteger FK → users.id (CASCADE) | False | indexed |
| `family_id` | String(36) | False | UUID；rotation 鏈唯一識別；reuse 偵測時整 family revoke |
| `token_hash` | String(64) UNIQUE | False | sha256(raw)；**DB 不存明文** |
| `parent_token_id` | BigInteger FK → parent_refresh_tokens.id (SET NULL) | True | rotation 上一個 token |
| `used_at` | DateTime | True | rotation 後填；reuse 偵測欄位 |
| `revoked_at` | DateTime | True | family 全撤時填 |
| `expires_at` | DateTime | False | 預設 now + 30 天 |
| `created_at` | DateTime | False | |
| `user_agent` | String(255) | True | 觀測 |
| `ip` | String(45) | True | IPv6 預留；觀測 |

Indexes：`ix_parent_refresh_user_family(user_id, family_id)`、`ix_parent_refresh_expires_at(expires_at)`。

Race window：5 秒內同 token 雙請求 → 409 retry；超出視為 reuse → 整 family revoke + `token_version += 1`。

### `models/parent_message.py`

#### `ParentMessageThread`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `parent_user_id` | Integer FK → users.id (CASCADE) | False | |
| `teacher_user_id` | Integer FK → users.id (CASCADE) | False | |
| `student_id` | Integer FK → students.id (CASCADE) | False | |
| `last_message_at` | DateTime | True | |
| `parent_last_read_at` / `teacher_last_read_at` | DateTime | True | 未讀計算依據（不每訊息一筆 receipt） |
| `deleted_at` | DateTime | True | |
| `created_at` / `updated_at` | DateTime | False | |

UNIQUE `(parent_user_id, teacher_user_id, student_id)`；indexes by parent / teacher / student。

#### `ParentMessage`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `thread_id` | Integer FK | False | |
| `sender_user_id` | Integer FK → users.id (CASCADE) | False | |
| `sender_role` | String(10) | False | `'parent'` 或 `'teacher'` |
| `body` | Text | True | 純附件訊息允許空 |
| `client_request_id` | String(64) | True | 前端 UUID；partial UNIQUE 防重送 |
| `source` | String(10) default `'app'` | False | `'app'` (LIFF/portal) 或 `'line'` (LINE webhook, Phase 5) |
| `deleted_at` | DateTime | True | 30 分內 sender 可撤回（顯示「此訊息已撤回」） |
| `created_at` | DateTime | False | |

UNIQUE `(thread_id, client_request_id)`；indexes `ix_parent_msg_thread_created(thread_id, created_at)`、`ix_parent_msg_sender(sender_user_id)`。

#### `LineReplyContext` / `LineWebhookEvent`

`LineReplyContext`：LINE 多孩家長 reply 路由上下文（line_user_id UNIQUE，10 分鐘 expires_at 滑動）。
`LineWebhookEvent`：webhook event 去重表（`webhook_event_id` UNIQUE，30 天 GC）。

### `models/parent_notification.py` — `ParentNotificationPreference`

| 欄位 | 型別 | Nullable | 備註 |
|------|------|----------|------|
| `id` | Integer PK | False | |
| `user_id` | Integer FK → users.id (CASCADE) | False | |
| `event_type` | String(40) | False | |
| `channel` | String(10) default `'line'` | False | v1 只支援 LINE |
| `enabled` | Boolean default True | False | false 即關閉該 event 該 channel 推播 |
| `created_at` / `updated_at` | DateTime | False | |

UNIQUE `(user_id, event_type, channel)`。設計為 **稀疏 row**：缺 row = enabled（預設全開），新增 event_type 不需資料遷移。

`PARENT_NOTIFICATION_EVENT_TYPES` 共 7 個：
- `parent.message_received` — 老師訊息
- `parent.announcement` — 園所公告
- `parent.event_ack_required` — 事件待簽
- `parent.fee_due` — 學費到期
- `parent.leave_result` — 學生請假審核結果
- `parent.attendance_alert` — 出席異常
- `parent.contact_book_published` — 每日聯絡簿發布（v3.1）

`PARENT_NOTIFICATION_CHANNELS = ("line",)`；過渡相容 `NotificationPreference` alias = `ParentNotificationPreference`。

### `models/auth.py` 中 `User` 家長相關欄位

| 欄位 | 型別 | 備註 |
|------|------|------|
| `username` | String(50) UNIQUE | 家長 = `parent_line_<line_user_id>` |
| `password_hash` | String(255) | 家長寫 `'!LINE_ONLY'` sentinel，`verify_password` 永不通過 |
| `role` | String(20) | `teacher` / `admin` / `hr` / `supervisor` / **`parent`** |
| `permission_names` | JSON / ARRAY(Text) | 家長為 `[]`（無 staff 權限） |
| `token_version` | Integer default 0 | logout / reuse-revoke 時 +=1，使所有現有 access token 失效 |
| `line_user_id` | String(100) UNIQUE indexed | 綁定 LINE User ID |
| `line_follow_confirmed_at` | DateTime | LINE Bot 被加為好友時刻；推播可達性旗標（webhook follow event 寫入） |
| `display_name` | String(100) | hero / 問候語顯示名；LIFF 登入以 LINE displayName 寫入 |

### `schemas/parent_assistant.py`

- `FaqAction(BaseModel)`：`type: 'route'\|'contact_teacher'\|'external'`、`label`、`path?`、`url?`
- `FaqCategory(BaseModel)`：`id`、`label`、`icon`、`color`
- `FaqItem(BaseModel)`：`id`、`category`、`question`、`keywords: list[str]`、`answer`、`action?`
- `FaqResponse(BaseModel)`：`version`、`updated_at`、`categories`、`items`

### `models/parent_db.py`

提供 URL-explicit `get_parent_engine_for_url` / `get_admin_engine_for_url` / `build_parent_session_for_user`（測試用）與 env-driven `get_parent_engine` / `get_parent_session_dep`（讀 `PARENT_DB_USER` + `PARENT_DB_PASSWORD` overlay 在 `DATABASE_URL` 上）。`connect` event listener 把 `app.current_user_id` 預設 reset 為空字串作為 defensive baseline；正式隔離由 `SET LOCAL` 內 tx 完成。

---

## Business Rules

### LINE LIFF 認證流程

1. 家長端 LIFF SDK 取 `id_token` → `POST /api/parent/auth/liff-login`，後端 IP rate-limit 後 `LineLoginService.verify_id_token` 驗簽（LINE 官方 `/verify` + `aud` 嚴格相等 channel_id + sub 非空 + sha256 反 replay 5 分鐘 TTL）。
2. 若 `User(line_user_id, role='parent')` 已存在 → 發正式 access token + refresh token，更新 `display_name` 與 `last_login`，回 `status: "ok"`。
3. 若不存在 → 發 5 分鐘 `parent_bind_token` cookie（scope=`bind`，可帶 LINE displayName），回 `status: "need_binding"`，引導去 `POST /auth/bind`。
4. `/auth/bind` 拿 cookie 中 line_user_id（防偽冒）+ 行政發的綁定碼 → `_claim_binding_code_atomic`（`UPDATE WHERE used_at IS NULL AND expires_at > now`，rowcount=1 才算成功）→ 建 User + 設 `Guardian.user_id`；**已被別家綁的 Guardian 即使持外洩碼仍擋 400**（F-001 防綁定覆寫）。
5. `/auth/bind-additional` 給已登入家長新增第二個小孩（共用 User），同樣 atomic claim + 防覆寫。
6. `/auth/logout` 清 cookie + `token_version += 1` + revoke 當前 refresh family。
7. `/auth/refresh` rotation：5 秒內同 token 並發 409；超出且 used token 被重送 → reuse 偵測，整 family revoke + `token_version += 1`，回 401。

[needs review] LINE Login Channel 必須與 Messaging Bot Channel 掛同一 LINE Provider，否則 `id_token.sub` / webhook userId / push to 不一致；目前由部署設定保證，無程式檢查。

### Permission 矩陣

| Permission | 位元值 | 用途 | 主要 callsite |
|------------|--------|------|---------------|
| `STUDENTS_LIFECYCLE_WRITE` | `1 << 36` | 學生狀態轉移 | `api/students.py:1195`（lifecycle transition endpoint） |
| `GUARDIANS_READ` | `1 << 37` | 監護人資料檢視 | `api/students.py:1322`、`api/activity/registrations.py`（缺權限時遮罩 PII） |
| `GUARDIANS_WRITE` | `1 << 38` | 監護人資料編輯 + 簽發綁定碼 | `api/guardians_admin.py:50`、`api/students.py:1362/1405/1459` |
| `PARENT_MESSAGES_WRITE` | `1 << 49` | 教師端發送家長訊息 | `api/portal/parent_messages.py` 全部、`api/portal/home.py:276`（hero 顯示用） |

家長端 `User.role = 'parent'`、`permission_names = []`：**不持有任何 staff Permission bit**；家長端 endpoint 一律靠 `require_parent_role()`（檢 `role == 'parent'`），對學生資源走 `_assert_student_owned`。

預設 role 模板包含上述 Permission 的 role：admin / supervisor / hr（部分）；明細以 `utils/permissions.py` 中 `ROLE_DEFAULTS` 為準（見 SPEC-007）。

### lifecycle_status 終態觸發 `terminal_entered_at`

`utils/student_lifecycle.set_lifecycle_status` 必經點：

- **非終態 → 終態**（`graduated` / `transferred` / `withdrawn`）：`student.terminal_entered_at = datetime.now(timezone.utc)`；有 HTTP request context 時 `mark_soft_delete(request, "student", ...)` 讓 AuditMiddleware 標軟刪 summary。
- **終態 → 非終態**（罕見復學）：`student.terminal_entered_at = None`（**取消 retention timer**）。
- **終態 → 終態** / **非終態 → 非終態**：戳記不動。
- **同狀態**：no-op、不寫 audit。
- `audit=True`（預設）時寫 `AuditLog(action="UPDATE", entity_type="student", summary="lifecycle: <old> → <new>")`。

`services/student_lifecycle.transition()` 內部以 `audit=False` 呼叫 helper（caller 已自寫 `StudentChangeLog`）。

[unverified] CLAUDE.md 提到「lifecycle 變更**必經** `utils/student_lifecycle.set_lifecycle_status`」，但程式碼層級無自動 enforcement（如 SQLAlchemy event listener 或 reflection check）；目前靠 code review + 慣例。

### Retention 期 365 天 + ENV 可調

`retention_days() = int(get_settings().scheduler.pii_retention_terminal_days or 365)`，env `PII_RETENTION_TERMINAL_DAYS` 可覆寫。`cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days())`。

### 抹除範圍

UPDATE 一次性處理整批（`_BATCH_LIMIT = 500`）：

```sql
UPDATE guardians
SET name = '[已離校家長]',
    phone = NULL, email = NULL,
    relation = NULL, custody_note = NULL,
    user_id = NULL,
    pii_redacted_at = :now,
    updated_at = :now
WHERE id IN :ids
```

### 抹除排除

- **不刪 Guardian row**（保留 student↔guardian 關係供稽核 / 復學）
- **不動 Student PII**（小孩個資受兒少法另議；本 GC 範圍僅監護人）
- **不刪 User row**（保留稽核軌跡；`user_id` 解綁即可）
- 已被抹過（`pii_redacted_at IS NOT NULL`）的 Guardian SQL WHERE 過濾掉，**冪等**

### 復學自動取消 retention

`set_lifecycle_status` 從終態 → 非終態時 `student.terminal_entered_at = None`；下次 GC 因 `terminal_entered_at IS NOT NULL` 過濾條件不再選中該 student 的 guardians。

### GC scheduler env flags

| ENV | 預設 | 行為 |
|-----|------|------|
| `PII_RETENTION_GC_DISABLED` | `0` | `1` 時 `scheduler_enabled() → False`，scheduler 不啟動 |
| `PII_RETENTION_GC_DRY_RUN` | `0` | `1` 時 SELECT 找到到期清單後 log 印出並 `rollback`，不寫 UPDATE / AuditLog |
| `PII_RETENTION_TERMINAL_DAYS` | `365` | retention 天數 |

GC 主迴圈：啟動後 60s 首跑（`_INITIAL_DELAY_SEC`，避免冷啟動同時打 DB），之後每 24h 跑一次（`_GC_INTERVAL_SEC = 86400`）。

PG-only 路徑採 `FOR UPDATE SKIP LOCKED`，多 instance 部署不會搶同一批 row；SQLite（測試）無此 clause。

### 上線啟用流程

1. **dry-run 階段**：設 `PII_RETENTION_GC_DRY_RUN=1`，scheduler 每日跑印出 `pii_retention GC: N 筆 (dry-run)` + 逐筆 `guardian_id / student_id / lifecycle / terminal_at`，**不寫任何 DB**。
2. **人工 review**：SRE / 業主檢查 log 中清單是否符合預期（誰要被抹、為什麼 365 天前進終態）。
3. **正式抹**：改 `PII_RETENTION_GC_DRY_RUN=0`（或移除），下次 24h 週期生效。

[needs review] 程式碼無自動「dry-run 必先跑 N 天才允許切正式」的 enforcement；靠人工流程。

### data-export 個資法 §10

- `GET /api/parent/me/data-export`：聚合家長所有監護學生的全歷史（contact_book / attendance / leaves / fees / medications / photos / messages / growth_reports）；photos / messages 僅匯出 metadata 不含 URL（payload size 控制）；fees / messages 隱藏員工 operator 欄位。
- **Rate-limit 1 / hour / user**：`create_limiter(max_calls=1, window_seconds=3600, name="parent_data_export")`，超出 429 `每小時限下載 1 次，請稍後再試`。
- **50 MB 上限**：序列化後 `len(body.encode('utf-8')) > 50*1024*1024` → 413 `資料量超過 50MB，請聯絡園所協助匯出`。
- **稽核**：`write_explicit_audit(action="READ", entity_type="parent_data_export", summary="家長下載個人資料 (N 學生)")` 必寫（AuditMiddleware 只攔 POST/PATCH/PUT/DELETE，GET 端點存取 PII 必須顯式 audit）。
- 檔名 `ivy_data_export_{user_id}_{YYYYMMDD}.json`，response header `Content-Disposition: attachment`。

### lifecycle 變更必經 `set_lifecycle_status` 的不變式

- `Student.lifecycle_status` 任何賦值都應透過 `utils/student_lifecycle.set_lifecycle_status`；否則：
  1. `terminal_entered_at` 漏寫 → PII GC 無法定位到期 row，永遠不會抹該家長 PII（合規風險）。
  2. 復學時漏清 `terminal_entered_at` → retention 365 天後仍會被誤抹（資料破壞風險）。
  3. AuditLog `lifecycle: X → Y` 漏寫 → 稽核軌跡斷裂。
- `services/student_lifecycle.transition()` 內部已正確呼叫 helper（`audit=False` 因 caller 自寫 `StudentChangeLog`）。
- [needs review] 全 codebase grep `\.lifecycle_status\s*=` 應僅出現在 helper 內或 ORM default；目前無 CI gate / reflection test enforce。

### IDOR / RLS 雙層防線

- **應用層**：`_assert_student_owned(session, user_id, student_id, *, for_write=False)`；`for_write=True` 額外擋已退學 / 畢業 / 轉出子女的寫入（用藥單 / 簽收 / 新聯絡簿 / 新請假），讀歷史路徑保留。
- **DB 層**：`get_parent_db` dependency 開 tx + `SET LOCAL app.current_user_id = :uid`；PG RLS policy（`parent_isolate_attendance` / `parent_self_guardian` 等）以該 setting 為過濾條件，即便應用層被繞過也只回 0 row。
- handler **絕不可** 呼叫 `session.commit()`，否則 `SET LOCAL` 失效；提交交由 dep 結束時自動進行。
- Phase 1（2026-05-18, parlsr001 migration）僅 `attendance.py` 用 RLS engine；其他子模組仍走 legacy admin engine 直到 GRANT + ENABLE RLS + POLICY 逐表遷移。

### 綁定碼安全性

- 12 位英數（去 I/O/0/1 防誤讀），熵度約 60 bits（`32^12`），sha256 hash 後 + 一次性 + IP rate-limit + per-guardian active cap 3，遠超暴力可行範圍。
- 明碼僅 API 回行政一次（行政再線下交家長），DB 只存 sha256。
- 24h TTL + atomic UPDATE 防 race + 失敗診斷分流（`BIND_CODE_NOT_FOUND` / `EXPIRED` / `USED`）讓前端能給有意義文案。
- line_user_id 為單位失敗鎖：DB-backed `rate_limit_buckets` 計數，5 次失敗鎖 15 分鐘，multi-worker 一致；DB 失敗時 fail-open。
- 防綁定覆寫（F-001）：Guardian 已被別家綁定，即使持碼者擁有未過期碼仍擋 400，阻擋「碼外洩→搶綁→奪取 PII」攻擊鏈。

### Refresh Token Rotation 與 reuse 偵測

- raw token 384-bit base64url，**永不入庫**；只存 sha256(raw) hex。
- `family_id` UUID 串起同裝置的 rotation 鏈；reuse 偵測時整 family revoke + `token_version += 1`。
- `used_at != NULL` 後再被送來 → 5 秒 race window 內回 409 `rotation in progress, please retry`；超出 → reuse → 整 family revoke + 401。
- Logout 同時 revoke 當前 refresh family + bump `token_version`。
- GC `gc_expired_refresh_tokens(retention_days=7)`：刪 `expires_at < cutoff` 或 `revoked_at < cutoff` 的 row。

### LINE Webhook 家長綁定觸發點

- `follow` event：寫 `User.line_follow_confirmed_at = now_taipei_naive()`，作為推播可達性旗標（FE `can_push` 依此判斷）。
- `message` / `postback`：先查 `User.line_user_id`；`role == 'parent'` → `line_reply_router.handle_parent_text_message` / `handle_parent_postback`；否則走教師既有 handler。
- `event.timestamp` 與 server time 偏差 > ±5 分鐘 → 拒收（防 replay）。
- `webhookEventId` UNIQUE dedup（`LineWebhookEvent` 表，30 天 GC）。

### 通訊：家長僅能回覆既有 thread

- `ParentMessageThread` 由教師端發起；家長不可主動 create thread。
- `assert_thread_participant`：caller 必須是 thread 的 parent 或 teacher，否則 403。
- 30 分鐘內 sender（不論 parent / teacher）可撤回自己訊息（tombstone `deleted_at`），UI 顯示「此訊息已撤回」。
- `client_request_id` 冪等：`(thread_id, client_request_id)` UNIQUE，replay 直接回原 message。

### 通知偏好稀疏 row 模型

- `ParentNotificationPreference` 預設全開（row 缺 = enabled）。
- `is_pref_enabled(session, *, user_id, event_type, channel='line')`：row 缺 → True；存在 → 看 `enabled` 欄。新增 event_type 不需資料遷移。
- `notifications.update_preferences` 容過渡舊 key（無 `parent.` 前綴）自動 mapping。

---

## Changelog

| Version | Date | 變更摘要 |
|---------|------|----------|
| v0.1 | 2026-05-28 | Initial draft：covers 家長入口 23 個子路由 + 1 個 admin router + LINE webhook 觸發點 + PII Retention GC + lifecycle helper + 6 個 models + parent_assistant schema |
