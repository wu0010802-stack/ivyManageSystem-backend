# Zeabur 部署 Runbook

> 適用於 ivy-backend（FastAPI + PostgreSQL）與 ivy-frontend（Vue 3 + Vite + Nginx）。
> 部署平台為 Zeabur，DB 採 Supabase Postgres。
> 文件最後更新：2026-05-11

---

## 0. 部署架構

```
[使用者瀏覽器 / LINE LIFF]
        │ HTTPS
        ▼
[Zeabur 邊緣 / TLS 終結]
        │
        ├── ivy-frontend  (Nginx Alpine, port 8080)
        │     └── 反代 /api/* → ivy-backend 內網
        │
        └── ivy-backend   (Python 3.11+, uvicorn, port $PORT)
              └── PostgreSQL (Supabase / 自建)
```

關鍵點：
- 前端 nginx 反代 `/api/*` 到後端內網，**避免 LIFF webview 第三方 cookie 被擋**。
- 後端 `/api/*` 路由由各 router prefix 構成，前端打 `/api/...` 即可。
- TLS 由 Zeabur 邊緣終結；nginx.conf.template 監聽 8080 純 http。

---

## 1. 環境變數 Checklist

### 1.1 ivy-backend（Zeabur Service Variables）

| 變數 | 必設 | 範例 / 說明 |
|---|---|---|
| `ENV` | ✅ | `production` |
| `DATABASE_URL` | ✅ | `postgresql://user:pass@host:5432/dbname`（Supabase 提供） |
| `JWT_SECRET_KEY` | ✅ | 32+ 字元隨機字串；用 `openssl rand -hex 32` 生成 |
| `CORS_ORIGINS` | ✅ | `https://ivykids.example.com,https://api.ivykids.example.com` |
| `ALLOWED_HOSTS` | ✅ | `ivykids.example.com,api.ivykids.example.com,*.zeabur.app` |
| `GOOGLE_MAPS_API_KEY` | ⭕ | 後端專用 key（招生 geocoding 用） |
| `GEOCODING_PROVIDER` | ⭕ | `google` 或 `nominatim` |
| `LINE_LOGIN_CHANNEL_ID` | ⭕ | LINE Login Channel（家長 LIFF） |
| `LINE_LOGIN_CHANNEL_SECRET` | ⭕ | 同上 |
| `LIFF_ID` | ⭕ | 後端只用於驗證 token；前端另外 bake in build |
| `RATE_LIMIT_BACKEND` | ❌ | **單 worker 部署免設**（保持預設 `memory`）。詳見 §6 部署模式 |
| `IVYKIDS_USERNAME` | ⭕ | 義華官網招生同步（若啟用） |
| `IVYKIDS_PASSWORD` | ⭕ | 同上 |
| `IVYKIDS_SYNC_ENABLED` | ⭕ | `true` 開啟同步 |
| `OFFICIAL_CALENDAR_SYNC_ENABLED` | ✅ | `1` 啟用 DGPA 國定假日 / 補班日每日背景同步；未啟用時行事曆會顯示「官方日曆暫時無法同步」警告。單 worker 部署直接開即可 |

⚠️ Messaging Bot 的 `channel_access_token` / `channel_secret` 儲存在 DB `line_configs` 表，**不放 env**。

### 1.2 ivy-frontend（Zeabur Build Args）

前端是「build-time 環境變數」，必須在 Zeabur Service Settings 的 **Build Arguments** 區設定，**不是 Runtime Variables**。

| Build Arg | 必設 | 範例 |
|---|---|---|
| `VITE_API_BASE_URL` | ✅ | `/api` |
| `VITE_LIFF_ID` | ✅ | LIFF App ID（注意 Dockerfile 目前有預設值 `2009899896-2qCpwrdC`，須覆寫成正式） |
| `VITE_GOOGLE_MAPS_API_KEY` | ⭕ | 前端專用 key（含 referrer 白名單） |
| `VITE_LINE_BOT_FRIEND_URL` | ⭕ | LINE 加好友 URL |

| Runtime Variable | 必設 | 範例 |
|---|---|---|
| `BACKEND_URL` | ✅ | `http://ivy-backend.zeabur.internal:8000`（內網位址） |

---

## 2. 首次部署步驟

### 2.1 準備 Supabase Postgres
1. Supabase 建專案，記下 connection string（pooler 與 direct 各一個）
2. `DATABASE_URL` 用 **Direct Connection**（給 Alembic + app 用）
3. 啟用 PITR（Point-in-Time Recovery）— Pro 方案內建，免費方案僅有每日備份

### 2.2 後端 Service
1. 在 Zeabur 建 Service，從 GitHub `ivy-backend` repo
2. 設定 1.1 表格中所有 ✅ 必設變數
3. 等 Zeabur build（會跑 `pip install -r requirements.txt`）
4. **首次部署 alembic 會自動跑** `upgrade heads`（見 `startup/migrations.py`）— 若資料庫為空會建表
5. 部署完成後驗證：
   ```bash
   curl https://<backend-host>/health/live    # 應回 200 {"status":"ok"}
   curl https://<backend-host>/health/ready   # 應回 200，含 DB + migration 狀態
   curl https://<backend-host>/docs           # 應回 404（prod 已關閉）
   ```

### 2.3 首次建立 admin 帳號
- `startup/seed.py` 已實作但非自動執行
- 從 Zeabur Console 進入後端 service，跑：
  ```bash
  python -c "from startup.seed import seed_admin; seed_admin()"
  ```
- 或自行寫一次性 SQL（從 `models/database.py` User 表結構）

### 2.4 前端 Service
1. Zeabur 建 Service，從 `ivy-frontend` repo
2. 設定 1.2 表格的 Build Args 與 Runtime Variables
3. Build 完成後驗證：
   ```bash
   curl -I https://<frontend-host>/                 # 應回 200，content-type: text/html
   curl -I https://<frontend-host>/manifest.webmanifest  # PWA manifest
   curl https://<frontend-host>/api/health/live     # 反代到後端，應回 200
   ```

### 2.5 域名 + SSL
1. Zeabur Service Settings 新增 Custom Domain
2. DNS 設 CNAME 指向 Zeabur 提供的目標
3. TLS 憑證由 Zeabur 自動申請（Let's Encrypt）
4. 完成後把該網域加進後端 `CORS_ORIGINS` 與 `ALLOWED_HOSTS`，重新部署後端

---

## 3. 日常部署 / Hotfix

1. 後端 PR merge → main：Zeabur 自動 webhook → rebuild → migration auto-run
2. 前端 PR merge → main：同上
3. 觀察 deployment log 直到 service 變綠（healthcheck 過）
4. 出問題：Zeabur Console → Service → Deployments → 點上一筆綠的 → "Promote" 即 rollback

---

## 4. DB Migration 與 Backup

### 4.1 Migration
- 自動：app 啟動時 `startup/migrations.py` 跑 `alembic upgrade heads`
- 手動驗證：本機 `cd ivy-backend && alembic heads` 應只有 1 個 head
- Rollback：alembic downgrade **非自動**；prod 出事先 Promote 上一版部署，DB 改動再考慮 downgrade

### 4.2 Backup
- Supabase Pro 內建 PITR（最近 7 天）— 首選恢復路徑（RTO ~1h）
- 異地備份：GH Actions `dr-backup.yml` 每日 02:17 +08 推送 pg_dump 至 Cloudflare R2 `ivy-dr/db/daily/`，每月 1 號額外複製至 `db/monthly/`
- Storage 鏡像：同 workflow 把 leave-attachments + growth-reports 鏡像至 R2 `ivy-dr/storage/`
- 完整 DR 流程、演練 SOP、retention、回填步驟：見 `ivy-backend/docs/sop/dr-runbook.md`
- 月度演練：手動觸發 `dr-restore-drill.yml`，report artifact 存 GH Actions 90 天

---

## 5. 監控 / 告警（待補）

⚠️ 目前無監控告警系統。上線後 P1 待辦：
- [ ] Sentry 串接（後端 `sentry-sdk[fastapi]`、前端 `@sentry/vue`）
- [ ] Uptime monitor（UptimeRobot / Healthchecks.io 打 `/health/live`）
- [ ] LINE 告警 channel（取代 Slack）

DR backup 失敗會 LINE Notify ops 群；Sentry 啟用後納入監控（見 `ivy-backend/docs/sop/dr-runbook.md` §8）

健康檢查端點：
- `GET /health/live` — 進程活著就 200
- `GET /health/ready` — DB 可連、migration 在 head 才 200

---

## 6. 部署模式：單 worker（已決策）

本系統採 **單 worker** 部署：

```json
// zbpack.json — 不要加 --workers N
"start_command": "uvicorn main:app --host 0.0.0.0 --port $PORT"
```

理由：
- 園所規模單一、員工 < 50、家長 < 500，單 worker 吞吐量足夠
- 限流（`utils/rate_limit.py` SlidingWindowLimiter）使用進程內 memory dict，**多 worker 會失效**
- 登入相關限流（`utils/rate_limit_db.py`）已強制走 DB，不受此影響

⚠️ 若未來改多 worker（`uvicorn --workers N` / `gunicorn -w N`），**必須**：
1. 設 `RATE_LIMIT_BACKEND=postgres`（讓 SlidingWindowLimiter 切到 PG-backed 版本）
2. 評估 Supabase connection pool 是否夠（每 worker 一個 pool）
3. 確認 `services/security_gc_scheduler.py` 等定期任務不會多次啟動

### 已知限制 / 待補

| 項目 | 狀態 | 影響 |
|---|---|---|
| LIFF_ID 在 Dockerfile 寫死預設值 | ⚠️ | 必須在 Build Args 覆寫，否則所有環境共用 |
| Activity fee F-01 超收檢查未完成 | ⚠️ | 高權限 admin 仍可繞過單筆累計檢查 |
| Sentry / Prometheus 未串接 | ⚠️ | 出錯只靠 stdout log |
| Service Worker cache 策略 | ✅ | PWA manifest + SW 已啟用 |

---

## 7. Troubleshoot

### 7.1 啟動立刻爆 RuntimeError
- 訊息含「CORS_ORIGINS 環境變數未設定」→ 補 `CORS_ORIGINS`
- 訊息含「ALLOWED_HOSTS 環境變數未設定」→ 補 `ALLOWED_HOSTS`
- 訊息含 DB 連線失敗 → 檢查 `DATABASE_URL`、Supabase pause 狀態

### 7.2 `/api/*` 502 / 504
- 前端 nginx 反代不到後端：檢查 `BACKEND_URL` Runtime Variable
- 後端 healthcheck 過但 API 慢：查 Supabase connection pool 是否耗盡

### 7.3 LIFF 開啟後 401 重定向迴圈
- 前後端 cookie domain 不一致：確認前端反代 `/api/*` 走的是同網域
- `LIFF_ID` 前後端不一致：對齊 build args 與後端 env

### 7.4 Migration 跑不過
- Zeabur log 看 `startup/migrations.py` 錯誤
- 本機 `alembic heads` 確認只有一個 head
- Multi-head：本機產 merge migration 後 push

---

## 8. 版本 / 維護

- Python: 3.11+
- Node: 22+
- Postgres: 14+
- Alembic head 數：1（保持唯一）
- 依賴上限規則：見 `requirements.txt` 註解
