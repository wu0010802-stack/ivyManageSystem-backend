# LINE service Phase 4 退役設計

**Status**: Spec (待 user 決定啟動時機，無急迫)
**Author**: Claude (Phase 2 PR-D follow-up)
**Created**: 2026-05-26
**Prerequisites**: Phase 2 全部 5 個 PR 已 merged（PR-A/B/C-1/C-2/C-3/D）

---

## Context

Phase 2 完成後，`services/line_service.py` 是 dispatch._channels.line 的內部 handler module（19 個 `_notify_*` private method + `_push` LINE group helper + `_push_to_user` 個人推送）。直接呼叫只剩 2 個 hybrid path：

- `api/dismissal_calls.py:_notify_dismissal_created` — LINE 群組推送無 cleanly defined perm pool
- `api/portfolio/reports.py:push_to_user` — `sent_count` + Phase 3 rollback 需從個別 push 拿真實 ACK

兩個 hybrid 在 PR-D 加的 CI grep gate 列為 exception。Phase 4 目標：兩個 hybrid 收尾後，`line_service.py` 整檔可從 grep gate exception 清空、進一步 inline 到 LINE_HANDLERS dict 或退役。

---

## 4 個 Sub-objective

### Section 1: `_notify_*` inline 到 `LINE_HANDLERS` dict

**目標**：把 `services/line_service.py` 19 個 `_notify_*` method 內容（含 message builder + push）抽到 `services/notification/_channels/line.py:LINE_HANDLERS` dict。

**現狀**：`LINE_HANDLERS = {}`（Phase 1 設為空，Phase 2 沒填）。dispatch._fan_out 對 LINE channel 走 fallback `push_text_to_user(rendered.title + rendered.body)` — 純文字，丟失 Flex / quick reply 功能。

**改動**：
- 每個 event_type 對映一個 `handler(line_service, evt, rendered) -> None`
- handler 內部用 `build_*_message(...)` 純函式建文字，或建 Flex/quick reply
- LINE_HANDLERS 覆蓋率達到後，`_notify_*` 可從 line_service.py 移除
- 保留 `services/line_service.py` 純為 `LineService` class（含 `_enabled` / `_token` config + `_push_to_user` / `push_text_to_user` 個人推送 helper），不再有 event-specific method

**驗收**：
- `grep -c "def _notify_" services/line_service.py` 回 0
- `LINE_HANDLERS` dict 覆蓋全部 23 個 event_type
- 既有 LINE 測試（test_line_service.py 31 / test_line_service_parent.py 8 / test_line_push_gate.py 9 / test_line_webhook_v2.py 15）全綠

**Risk**：
- 每個 event_type 的 Flex schema 不一致；message builder 與 dispatch context 對齊需細看
- LINE quick-reply postback（家長 thread reply、活動候補確認）需 handler 內保留

---

### Section 2: LINE 群組推送加 `group_id` mode（解 dismissal hybrid）

**目標**：dispatch._channels.line 支援「群組推送」channel mode，讓 `dismissal.created` 等 group-targeted event 不再需要 hybrid `_line_service._notify_dismissal_created` caller。

**現狀**：
- `LineService._push(text)` 推到單一 group_id（環境變數 `LINE_TARGET_GROUP_ID` 設）
- dispatch._fan_out LINE adapter 只支援個人推送（`recipient_user_id` → `_resolve_line_user_id`）
- dismissal_calls.py hybrid 保留 `_line_service._notify_dismissal_created` 推群組

**改動**：
- dispatch.enqueue 加 `recipient_group_id: Optional[str]` 參數（或用 `channels_override + context["line_group_id"]`）
- channel_matrix 加 `dismissal.created` 預設走「group LINE」
- LINE adapter `send` 分流：`recipient_user_id` 走 push_to_user，`line_group_id` in context 走 `_push` 群組
- `api/dismissal_calls.py` 移除 `_line_service._notify_dismissal_created` caller，純走 dispatch.enqueue
- CI grep gate exception 從 list 移除 `api/dismissal_calls.py`

**驗收**：
- dispatch.enqueue 對 dismissal.created 同時觸發 group LINE + ws broadcast
- `api/dismissal_calls.py` 內 `_line_service` global 與 `_line_service._notify_dismissal_created` 全部移除
- CI grep gate exception list 縮為 1 個（只剩 reports.py）
- dismissal 16 case + dismissal_http 18 case + dismissal_permissions 11 case 全綠

**Risk**：
- LINE_TARGET_GROUP_ID 是全域單一群組，未來若多群組（如「教師群」/「家長群」/「行政群」）要分流，需 `event_type → group_id` 對映表
- group_id 來源：環境變數 / 資料庫設定 / runtime context

---

### Section 3: dispatch._fan_out 加 send result callback（解 reports.py hybrid）

**目標**：dispatch 對個別 LINE 推送提供 ACK 回呼機制，讓 `api/portfolio/reports.py` 不再需要 Phase 2/3 的 sent_count + line_sent_at rollback 自管邏輯。

**現狀**：
- dispatch._fan_out 內 LINE adapter 對 channels_failed 寫入 NotificationLog 但 caller 拿不到結果
- reports.py send-line endpoint Phase 1 預 claim `line_sent_at` → Phase 2 用 `_line_service.push_to_user` loop 推 → Phase 3 全失敗時開新 session 回滾 claim + 回 502
- sent_count 與 line_sent_at rollback 是核心 admin UX（admin 知道推沒推、可重試 5 分鐘冪等內）

**改動方案 A（events / callback）**：
- dispatch.enqueue 加 `on_send_result: Optional[Callable[[evt, success_count], None]]`
- dispatch._fan_out 對 LINE channel 個別推送結果累積 → 全處理完呼叫 callback
- reports.py 用 callback 寫 line_sent_at（成功 ≥ 1 才 commit claim；全失敗 rollback）

**改動方案 B（同步 batch dispatch）**：
- dispatch 加 `dispatch.send_now(...)` synchronous API for caller 自管 session 場景
- 不走 after_commit hook，直接 fan-out + 回 `SendResult` (sent_count / channels_failed)
- reports.py 用 send_now 拿 sent_count + 直接 commit/rollback line_sent_at

**驗收**：
- reports.py send-line endpoint 移除 `_line_service.push_to_user` direct caller
- sent_count + line_sent_at rollback 邏輯改走 dispatch API（callback 或 send_now）
- CI grep gate exception list 清空（reports.py 也移除）
- `test_growth_report_api.py` 14 case 全綠（含 Phase 1-4 路徑）

**Risk**：
- 方案 A 把 dispatch 從 fire-and-forget 改為 partial sync（callback 在 fan-out 結束時 fire），增加 caller 複雜度
- 方案 B 引入第二個 dispatch API surface，concept 變大
- 選哪個方案需評估其他未來 caller 是否也有 ACK 需求

---

### Section 4: services/line_service.py 整檔退役評估

**目標**：完成 Section 1+2+3 後，評估 `services/line_service.py` 是否仍需保留。

**現狀**（PR-D 後）：
- 19 個 `_notify_*` (Section 1 抽到 LINE_HANDLERS)
- `_push(text)` LINE 群組推送 (Section 2 抽到 LINE adapter)
- `_push_to_user(line_user_id, text)` / `push_text_to_user` 個人推送（dispatch LINE adapter 已用）
- `should_push_to_parent(session, user_id, event_type)` 家長端 gate（dispatch._resolve_line_user_id + _pref_enabled 已內建等價邏輯）
- `handle_webhook_message(...)` LINE webhook 處理（home / 我的薪資 等指令）
- `_reply(reply_token, text)` LINE webhook 回覆
- `configure(...)` config 載入 + `_enabled` / `_token` 設定

**Section 4 改動**：
- 拆檔：`services/notification/_channels/line.py:LineAdapter` 內聯 push_to_user/_push 群組推送 + LINE_HANDLERS
- LINE webhook 處理保留為 `api/line_webhook.py` 內聯邏輯 + 抽至 `services/line_webhook_service.py`（與通知 dispatch 解耦）
- `should_push_to_parent` 整段移除（已被 dispatch 取代）
- `services/line_service.py` 整檔 delete 或縮為 `LineService` config-only class

**驗收**：
- `services/line_service.py` 若保留則 < 100 行（純 config + LINE API URL constants）
- `api/line_webhook.py` 與通知 dispatch 完全解耦
- CI grep gate 可考慮移除（line_service.notify_* 已不存在）
- 全套 LINE 相關 test 全綠

---

## 已知 follow-up（Phase 4 啟動時順便處理）

### test pollution from PR-B (未解 bug)

PR-B (PR #17) 描述提到「`test_manual_adjust_writes_audit_row` 在全套 pytest 跑時 deterministic fail，單跑 / subset 跑都 pass」，無法乾淨 bisect。可能與 dispatch hook 在 conftest 重綁到 test factory + cross-test 累積 sessionmaker reference 有關。

**建議**：Phase 4 啟動 Section 3 (dispatch fan-out callback) 時，順便檢視 `tests/conftest.py:test_db_session` 的 `_dispatch.install_session_hooks(test_session_factory)` + `_HOOKS_INSTALLED.discard(test_session_factory)` lifecycle，看是否 SQLAlchemy `event.listen` 對 dispose 過的 sessionmaker 仍持有 strong ref，影響全套跑時 audit middleware 在不對的 factory 上寫 log。

### PR-A `_line_service` global 還有沒清的 file

PR-D D4 清了 7 個 router 的 `_line_service`，但下列 file 仍保留（因有非 Phase-2 用途）：
- `api/line_webhook.py` — webhook handler 需要 line_service
- `api/config/__init__.py` — config sub-router 內部用
- `api/salary/__init__.py` — salary service injection 帶 line_service（未來 Section 4 重新評估）
- `api/portal/contact_book.py` — fixture mock 用（test_contact_book 仍呼叫 init_contact_book_line_service）

Section 4 整檔退役時這些一起重新評估。

---

## 預估時程

| Section | 工作日 | Acceptance |
|---------|-------|-----------|
| 1: LINE_HANDLERS inline | 2–3 | 19 method 全部抽出；既有 LINE test 零回歸 |
| 2: group_id mode | 1–2 | dismissal hybrid 收尾；CI exception -1 |
| 3: fan-out callback | 2–3 | reports.py hybrid 收尾；CI exception -1（全清） |
| 4: 整檔退役評估 | 1 | line_service.py < 100 行 or delete |
| **合計** | **6–9 工作日** | line_service.py 從 800 行縮到 < 100 行（或退役） |

---

## 不在 scope

- LINE Messaging API SDK 升級
- 家長端 LIFF login service (`services/line_login_service.py`) — 獨立服務，不在 line_service.py
- 新 event_type 加（保留給 future product 需求）
- LINE Flex / quick reply 重新設計（保留現有 builder）

---

## 啟動條件

- Phase 2 全部 PR merged（已完成 2026-05-26）
- 無 LINE 相關功能新需求（避免邊改邊加）
- 至少 1 個工作日連續時間（Section 1 動 LINE_HANDLERS 不適合分批）
