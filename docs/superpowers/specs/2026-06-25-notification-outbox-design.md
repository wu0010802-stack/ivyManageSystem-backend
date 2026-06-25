# 通知 Transactional Outbox + WS Bounded Queue 設計（LONG，成長觸發）

**狀態**：設計（成長觸發；當前負載用不到，不實作）
**背景**：設計審查 2026-06-25 主題（韌性/並發）。**觸發條件**：當通知量 / 收件者數
顯著成長，inline fan-out 的 tail latency 耦合進請求路徑變痛時才做。當前已有共享 circuit
breaker + retry scheduler 緩解，尚不痛。`services/notification/dispatch.py` `enqueue`
docstring 已預留「未來 outbox idempotency key」，本文件即該方向（延續
`2026-05-25-notification-dispatch-design.md`）。

## 問題

通知 fan-out 目前**內聯在業務交易的 after_commit hook**（`_drain_after_commit` →
`_fan_out`：寫 NotificationLog + 同步 LINE HTTP + WS 廣播）：
- 同步 LINE HTTP 在 after_commit 內執行 → tail latency 耦合進請求路徑（收件者多時尤甚）。
- after_commit 已在交易外：若 fan-out 中途崩潰 / process 重啟，**那批通知遺失**（NotificationLog
  雖在，但實際 send 是 fire-and-forget；retry_scheduler 只補「已寫 log 但 LINE 失敗」的）。
- 雪崩風險：大量收件者 × 同步 send 在單一 after_commit 內串行。

## 既有基礎（outbox 的前驅）

- `models/notification_log.py` `NotificationLog`：已記每筆通知（含狀態）。
- `services/notification/retry_scheduler.py`：DB-backed 輪詢重發 pending retry row
  ——已是「DB 當佇列 + 排程器消費」的雛形，但目前是 **retry 補償**而非 **primary 路徑**。
- 共享 circuit breaker（5 次失敗預算全系統共享、~25s trip）。

## 方案：transactional outbox

把「決定要發什麼」與「實際發送」用 DB 解耦，並讓「要發什麼」與業務寫入**同交易原子化**：

1. `enqueue(...)` 不再於 after_commit 內聯 `_fan_out`，改為在 **同一個業務交易內**寫一筆
   `notification_outbox` row（payload + idempotency key `source_entity_type:source_entity_id:event`
   + status=pending）。業務 commit ⇒ outbox row 一起 commit（原子；崩潰不遺失「要發」的意圖）。
2. 新增 dispatcher 排程器（`notification_outbox_dispatcher`）：輪詢 pending outbox row →
   `_fan_out`（LINE/WS/in_app）→ 成功標 done / 失敗增 attempts + 指數退避。`FOR UPDATE SKIP
   LOCKED` 讓多 dispatcher / 多 process 安全並發消費（與 batch/web 解耦連動）。
3. idempotency key 防重複發送（at-least-once + 去重 ⇒ effectively-once 觀感）。
4. 保留 NotificationLog 作為「已發歷史」；outbox 是「待發佇列」（done 後可定期清）。

## WS bounded outbound queue（並行子項）

WS 廣播目前在單一 event loop 對每個訂閱者**序列 await send**，慢消費者會 head-of-line
阻塞整個 event loop。改為 per-connection bounded outbound queue：
- 每連線一個 `asyncio.Queue(maxsize=N)`；廣播只 `put_nowait`（滿則丟最舊 / 標記 lagging）。
- 每連線一個 writer task `await ws.send(...)`，套 `asyncio.wait_for(send, timeout)`；逾時即
  斷線該慢消費者（不阻塞其他訂閱者 / event loop）。

## 實作要點

- 新表 `notification_outbox`（id, idempotency_key UNIQUE, payload JSONB, status, attempts,
  next_attempt_at, created_at）+ partial index `WHERE status='pending'`（並發消費）。
- `enqueue` 改寫 outbox（同 session，不另開交易）；移除 after_commit 內的同步 `_fan_out`。
- dispatcher 沿用 `scheduler_observability` + advisory lock（多 process 安全）。
- 漸進遷移：先讓 outbox 與既有 after_commit 路徑並存（feature flag），驗證 dispatcher 穩定
  後再切換 enqueue 不走 after_commit。

## 驗證

- 業務 commit 後崩潰 → 重啟後 dispatcher 仍把 pending outbox 發完（不遺失）。
- 同 idempotency key 重入 → 只發一次（UNIQUE 約束 + ON CONFLICT）。
- 多 dispatcher 並發 → SKIP LOCKED 不重複發。
- WS 慢消費者 → 逾時斷線，不阻塞其他訂閱者。

## 為何不現在做

過早優化 + 改通知核心路徑風險。當前 circuit breaker + retry scheduler 已緩解雪崩；通知量
未到痛點。等 tail latency / 通知遺失 / 雪崩有實測訊號再觸發。
