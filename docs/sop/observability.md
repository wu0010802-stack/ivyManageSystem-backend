# 可觀測性 SOP — 三層監控告警

**最後更新**：2026-05-28
**適用範圍**：ivy-backend production；對應 spec
`docs/superpowers/specs/2026-05-28-slow-request-slo-alerting-design.md`

---

## 設計總覽

| 層級 | 工具 | 用途 | 設定者 |
|------|------|------|--------|
| L1 服務存活 | UptimeRobot → `/api/health/ready` | 5 min ping 確認 HTTP 200 + DB 連線 | user 帳號操作 |
| L2 慢請求即時告警 | RequestLoggingMiddleware → in-memory counter → LINE | 慢請求達 10 次/分鐘 → LINE ops 群組推播；per-path 5 分鐘 cooldown 防 spam | code 自動 |
| L3 效能 dashboard | Sentry Performance | p50/p75/p95/p99 / endpoint breakdown / span waterfall；事後查詢與趨勢分析 | env 設好即啟用 |

L1 是「服務有沒有活」；L2 是「服務活但慢爆了」即時感知；L3 是「平日基線追蹤 + 事後 root cause」。三層互補。

---

## L1：UptimeRobot 設定

### 為什麼

`/api/health/ready` 已 implemented（連 DB 跑 `SELECT 1` 失敗回 503），但 SaaS 部署沒人在外面 ping → 服務當機要等使用者反映才發現。

### 設定步驟（user 操作）

1. 註冊 [UptimeRobot](https://uptimerobot.com/) 免費帳號（50 monitor / 5 min interval）
2. 加 monitor：**Monitor Type**: HTTP(S)
3. **Friendly Name**: `ivy-backend prod readiness`
4. **URL**: `https://<prod-domain>/api/health/ready`
5. **Monitoring Interval**: 5 minutes
6. **Alert Contacts**:
   - 加 email contact（必填）
   - 可選：加 LINE Notify webhook（先到 LINE Notify 申請 token，或設 webhook 指到自家 LINE bot 推到 ops 群組）
7. Save

### 期望 SLO

- 99.9% uptime（月度允許 ~43 分鐘 down）
- 連續 2 次 ping fail 即觸發 email/LINE 告警

---

## L2：慢請求即時告警（in-process）

### 怎麼觸發

- `RequestLoggingMiddleware` 對每個 request 量 `elapsed_ms`
- 超過 `SLOW_REQUEST_THRESHOLD_MS = 2000`（`utils/request_logging.py:20`）即：
  1. 寫 `WARNING` log（既有）
  2. 呼叫 `utils.slow_request_alerter.record_slow(path, elapsed_ms, status)`
- alerter 維護 per-path sliding window deque[timestamp]
- 同 path 在 60 秒窗口內累計 ≥ 10 次（threshold）→ 推 LINE
- 推 LINE 後同 path 進入 300 秒 cooldown（防同一壞 endpoint 持續刷螢幕）

### 設定步驟（user 操作）

1. 建立或選一個 LINE 群組作為 ops 告警頻道（建議只放工程師 + bot）
2. 把 ivy bot 加入該群組
3. 取得該群組 ID：
   - 方法 A：bot 收到群組訊息時 webhook event 內有 `source.groupId`，查 bot log
   - 方法 B：用 LINE bot SDK 寫個臨時 endpoint 收 `source.groupId` 寫進 log
4. zeabur env 加：
   ```
   OPS_ALERT_LINE_GROUP_ID=C<group_id>
   ```
5. 可選 tune（皆有合理預設）：
   ```
   OPS_ALERT_SLOW_REQUEST_ALERT_WINDOW_SECONDS=60     # 預設 60s 窗口
   OPS_ALERT_SLOW_REQUEST_ALERT_THRESHOLD=10          # 預設達 10 次/分鐘觸發
   OPS_ALERT_SLOW_REQUEST_ALERT_COOLDOWN_SECONDS=300  # 預設 5 分鐘 cooldown
   ```
6. restart zeabur service

### 已知限制

- **多 worker 各自獨立計數**：gunicorn 跑 N workers 時，每個 worker 自己有一份 sliding window；prod 真實觸發量 = workers × threshold。設計時已含此 multiplier，threshold 10 是 per-worker 而非整服務（過於敏感再 tune up）。
- **process 重啟丟狀態**：每次 deploy 後 cooldown 與 counter 都歸零；對「慢請求突發」場景影響不大（突發本身是新事件）。
- **path 高基數風險**：path 經 starlette router 過濾，無 random unknown path 累積；但 `/api/students/{id}` 會以 raw `/api/students/42` 進 counter，不同 id 各自獨立計數（feature：同 id 多次 timeout 是 dependency 慢，不同 id 多次 timeout 是熱點）。

### 手測驗證

```bash
# 1. 臨時把 threshold 設低
export OPS_ALERT_SLOW_REQUEST_ALERT_THRESHOLD=3
export OPS_ALERT_SLOW_REQUEST_ALERT_WINDOW_SECONDS=10
export OPS_ALERT_LINE_GROUP_ID=C<test_group>

# 2. 啟動 backend
./start.sh

# 3. 對某個 endpoint 連續打（或用 ab/wrk）
for i in 1 2 3 4 5; do
  curl -s -o /dev/null http://localhost:8088/api/health/ready
done
# 注意：/health/ready 通常很快，要打慢端點才會觸發。可暫時 monkeypatch SLOW_REQUEST_THRESHOLD_MS=0
# 或對真實慢端點（如薪資批次預覽）打 N 次。

# 4. 預期：LINE 群組收到「⚠️ 慢請求突發」訊息
# 5. 立刻再連打 5 次同 endpoint → 不該再收第二則（cooldown 中）
```

### 異常處理

| 症狀 | 可能原因 | 排查 |
|------|---------|------|
| 預期該觸發但沒收 LINE | `OPS_ALERT_LINE_GROUP_ID` 未設 | grep 後端 log「OPS_ALERT_LINE_GROUP_ID 未設」 |
| 預期該觸發但沒收 LINE | `LineService` 未注入 | grep log「LineService 未注入」 |
| LINE 推送失敗 | network / token 過期 | grep log「Slow request alert push 失敗」 |
| LINE spam（同一 endpoint 反覆收） | cooldown 設太短 | tune `OPS_ALERT_SLOW_REQUEST_ALERT_COOLDOWN_SECONDS` |
| 沒有任何告警但 prod 真的慢 | threshold 設太高、或慢請求屬不同 path 分散計數 | 看 Sentry Performance 拉 transaction list；或降 threshold |

---

## L3：Sentry Performance Tracing

### 為什麼

- L1/L2 只覆蓋「即時感知」，不能回答「上週四下午 3 點到底發生什麼」「哪個 endpoint p95 在過去 7 天惡化」
- Sentry Performance 自帶 dashboard，免重建 metrics stack

### 設定步驟（user 操作）

1. 已有 prod Sentry project（CLAUDE.md `Sentry 錯誤監控` 章節）
2. zeabur env 確認：
   ```
   SENTRY_DSN=https://...@sentry.io/...
   SENTRY_ENVIRONMENT=production
   SENTRY_TRACES_SAMPLE_RATE=0.1   # 預設 0.1，可省略
   ```
3. restart zeabur service
4. 啟動 log 應出現：
   ```
   Sentry SDK initialised (env=production, traces_sample_rate=0.1)
   ```
5. 進 [sentry.io](https://sentry.io) → Performance tab → 等 5-10 分鐘後應有 transaction data

### Code 端已就緒

`utils/sentry_init.py` 已 register：
- `FastApiIntegration(transaction_style="endpoint")` → endpoint pattern 為 transaction name
- `SqlalchemyIntegration()` → 自動收 SQL span
- `traces_sample_rate=settings.sentry.traces_sample_rate` → env-driven

PII 過濾走 `_scrub_event` 已涵蓋 60+ key（薪資、保險、家長、學童、醫療）。新增業務模組若含 PII 欄位需同步加入 denylist（[[memory:project-frontend-ts-migration-complete]] 之前提過的 sync rule）。

### 期望 dashboard 用法

- **Transaction list**：按 p95 排序找慢端點
- **Span waterfall**：點開單個 transaction 看 DB query 細項
- **Trends 圖表**：對比過去 7/30 天 baseline 找退化點
- **Alert rules**：可在 Sentry UI 設「某 transaction p95 > 5s 持續 10 分鐘 → email」作為 L2 後備

### Quota 注意

- free tier 每月 5K events + 10K transactions
- traces_sample_rate=0.1 + prod 1000 req/min → 6K transactions/hour → 月度遠超 free tier
- 若 quota 不夠：降 `SENTRY_TRACES_SAMPLE_RATE=0.05` 或 0.01；或升級 paid plan

---

## 與既有 runbook 的關係

本 SOP 取代 `docs/sop/dr-runbook.md §5` 的「監控告警待補」placeholder；
dr-runbook 仍負責 DR 演練 / RTO 紀錄；本檔負責日常監控配置。

兩份文件之間：
- DR 演練若發現 restore 後 health check 不過 → 用本檔 L1 步驟驗證
- DR 演練後 prod 服務復原 → 本檔 L2/L3 自動恢復監控（無需手動 reset counter）
