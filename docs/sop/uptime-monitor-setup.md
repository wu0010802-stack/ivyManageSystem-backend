# UptimeRobot 監控設定 SOP

**目的**：5 分鐘內偵測系統宕機 / scheduler 連環失敗，自動推播 LINE 告警群。

**前置條件**：
- prod 已部署 `/health/ready`、`/health/schedulers`、`/api/internal/uptime-webhook`
- prod env 已設：
  - `UPTIME_ROBOT_WEBHOOK_TOKEN`（建議用 `openssl rand -hex 16` 產隨機 32 字元）
  - `OPS_ALERT_LINE_GROUP_ID`（沿用慢請求告警群即可，集中收 ops 訊息）

---

## 步驟

### 1. 註冊 UptimeRobot 免費帳號

連結：<https://uptimerobot.com/signUp>

- 免費版可建 50 個 monitor / 最短 5 分鐘檢查間隔（付費版可降到 1 分鐘）
- 5 分鐘對本系統足夠（scheduler lag 容忍時間遠長於 5 分鐘）

### 2. 加 Monitor 1：`/health/ready`

進 Dashboard → Add New Monitor：

| 欄位 | 值 |
|------|----|
| Monitor Type | HTTP(s) |
| Friendly Name | `Ivy /health/ready` |
| URL (or IP) | `https://<prod-domain>/health/ready` |
| Monitoring Interval | 5 minutes |
| Expected Status | 200 |

按 Create Monitor。

### 3. 加 Monitor 2：`/health/schedulers`

同上，URL 改為 `https://<prod-domain>/health/schedulers`，Interval 5 分鐘即可。
此端點會掃 `scheduler_heartbeats` 表，任一 scheduler lag > 2 × expected_interval 時回 503，
UptimeRobot 即會觸發告警。

### 4. 設定 Alert Contact — LINE Webhook

進 My Settings → Alert Contacts → Add Alert Contact：

| 欄位 | 值 |
|------|----|
| Alert Contact Type | Webhook |
| Friendly Name | `Ivy LINE Group` |
| URL to Notify | `https://<prod-domain>/api/internal/uptime-webhook?token=<UPTIME_ROBOT_WEBHOOK_TOKEN 的值>` |
| POST Value (JSON) | 留空（使用 UptimeRobot 預設 payload） |
| Send POST data as JSON | 勾選（必須 JSON，本系統 endpoint 用 `request.json()` 解析） |
| Enable notifications for | 勾選 Down + Up，視需要勾 Paused |

按 Create Alert Contact。回到 Monitor 列表，把這個 contact 套用到上述 2 個 monitor
（Edit monitor → Select Alert Contacts To Notify）。

### 5. 加 Email Alert Contact（備援）

| 欄位 | 值 |
|------|----|
| Alert Contact Type | Email |
| Friendly Name | `Ivy Ops Email` |
| Email To Notify | dev/ops 群組信箱（建議共用收件匣，非個人信箱） |

同樣套用到 2 個 monitor。LINE 與 email 雙通道，避免任一通道失靈時錯過告警。

---

## 整合驗證

部署 + 設定完成後做一次端到端驗證：

1. 在 prod 暫停某個 scheduler（最快方法：直接 stop 一個 scheduler env flag 後重啟服務，
   或在 DB 直接 `UPDATE scheduler_heartbeats SET last_success_at = NOW() - INTERVAL '1 hour'
   WHERE scheduler_name = 'medication_reminder';`）
2. 等下次 UptimeRobot 觸發 `/health/schedulers`（最多 5 分鐘）
3. 預期：endpoint 回 503 + body 含 lagging list
4. UptimeRobot 觸發 alert contact webhook → LINE 群收到：
   ```
   ⚠️ 監控告警：Ivy /health/schedulers 宕機
   細節：HTTP 503
   ```
5. 恢復狀態（UPDATE last_success_at = NOW()），下次 check 後預期收到：
   ```
   ✅ 監控恢復：Ivy /health/schedulers 已上線
   ```

---

## 故障排除

| 症狀 | 可能原因 | 處置 |
|------|---------|------|
| LINE 告警未收到 | webhook URL token 不符 | 比對 prod env `UPTIME_ROBOT_WEBHOOK_TOKEN` 與 UptimeRobot Alert Contact URL 的 `?token=...` 值 |
| LINE 告警未收到 | `OPS_ALERT_LINE_GROUP_ID` 未設 | 檢查 prod env；endpoint log 會記 `"OPS_ALERT_LINE_GROUP_ID 未設定，跳過 LINE push"` |
| Webhook 回 401 但 token 對 | env 沒重啟生效 | zeabur 改 env 後須 restart service；確認 startup log 看到新 env |
| 同一告警一直重複 | UptimeRobot 預設每分鐘重試直到狀態改變 | 在 monitor 設定 alert when down for X minutes，或減少 retry |
| 啟動後 scheduler 短暫顯示 lag | 第一次 tick 尚未跑（`last_success_at IS NULL`） | 預期行為；spec 設計上 NULL 不告警，第一次成功 tick 後恢復 |
| Webhook 200 但 LINE 沒收到 | LINE channel access token 失效 | 看 `services/line_service.py` 的 LINE_BREAKER；P1 resilience 已有 token health scheduler 每日 08:00 ping |
| /api/internal/uptime-webhook 回 503 | 系統進入維護模式 | KillSwitch middleware bypass list 已含 `/api/internal/uptime-webhook`（Phase 2 BE-B 落地）；若 503 表示中間件設定漏配 |

---

## 後續擴充（follow-up）

- **告警去重**：目前 UptimeRobot 每次 503 都會推 LINE，連續 5 次 503 就會 5 則訊息。可在 webhook 端加 dedupe（同 monitor + alertType 在 N 分鐘內只推一次）。
- **/health/schedulers 細分**：未來可加 `?include=critical_only` 等 query，讓 UptimeRobot 只盯 critical scheduler 不被低優先 lag 干擾。
- **alert routing**：高峰時段 vs 半夜可分流到不同 LINE 群（值班輪值）。
