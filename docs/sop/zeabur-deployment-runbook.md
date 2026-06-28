# Zeabur 部署 Runbook

> 適用於 ivy-backend（FastAPI + PostgreSQL）與 ivy-frontend（Vue 3 + Vite + Nginx）。
> 部署平台為 Zeabur；**DB 自 2026-06-23 起為 Zeabur PostgreSQL service**（早期為 Supabase
> Postgres，已遷移）；檔案儲存（leave-attachments / growth-reports）仍用 Supabase Storage。
> 文件最後更新：2026-06-28

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
              └── PostgreSQL (Zeabur PostgreSQL service，同專案內網)
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
| `DATABASE_URL` | ✅ | `postgresql://user:pass@host:5432/dbname`（Zeabur PostgreSQL service 提供；同專案可用內網 host） |
| `JWT_SECRET_KEY` | ✅ | 32+ 字元隨機字串；用 `openssl rand -hex 32` 生成 |
| `CORS_ORIGINS` | ✅ | `https://ivykids.example.com,https://api.ivykids.example.com` |
| `ALLOWED_HOSTS` | ✅ | `ivykids.example.com,api.ivykids.example.com,*.zeabur.app` |
| `GOOGLE_MAPS_API_KEY` | ⭕ | 後端專用 key（招生 geocoding 用） |
| `GEOCODING_PROVIDER` | ⭕ | `google` 或 `nominatim` |
| `LINE_LOGIN_CHANNEL_ID` | ⭕ | LINE Login Channel（家長 LIFF） |
| `LINE_LOGIN_CHANNEL_SECRET` | ⭕ | 同上 |
| `LIFF_ID` | ⭕ | 後端只用於驗證 token；前端另外 bake in build |
| `RATE_LIMIT_BACKEND` | ❌ | **單 worker 部署免設**（保持預設 `memory`）。詳見 §6 部署模式 |
| `CACHE_BACKEND` | ❌ | 預設 `memory`；多 worker / 多 instance 時設 `redis`，一般 cache 走 Redis |
| `BROADCAST_BACKEND` | ❌ | 預設沿用 `CACHE_BACKEND`；只想讓 WebSocket 廣播走 Redis 時可設 `redis` |
| `CACHE_REDIS_URL` | 條件必設 | `CACHE_BACKEND=redis` 或 `BROADCAST_BACKEND=redis` 時必設 |
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

### 2.1 準備 PostgreSQL（Zeabur PostgreSQL service）
1. 在同一 Zeabur 專案內建 PostgreSQL service，記下其連線字串（內網 + 對外各一個）
2. `DATABASE_URL` 用內網連線（給後端 app + Alembic）；異地備份（GH Actions，見 §4.2）需用
   **對外可達**的連線字串
3. ⚠ **備份能力需自行確認**：Zeabur PostgreSQL 的內建 snapshot / PITR 能力與保留窗口須查證，
   **不可假設等同 Supabase Pro PITR**。異地備份以 §4.2 的 `dr-backup.yml`（每日 pg_dump → R2）
   為主要保障，務必確認該 workflow 已指向此 Zeabur PG（見 §4.2 ⚠）

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

#### ⚠ 上線前 migration 預演（防 cold-start boot loop，崩潰防護 P0）
**風險**：push 後端 = Zeabur cold-start 跑 `alembic upgrade heads`，且 `on_startup()` 對此
**無 try/except**（fail-fast 是刻意的——schema 沒上對就不該收流量）。任一支 migration 在
prod 失敗 → 啟動失敗 → process 退出 → **反覆 boot loop，全服務 down**。

**push 含 migration 的後端前，依序做**：
1. 快速本地靜態 sanity（無需 DB，秒級）：
   ```bash
   cd ivy-backend && python scripts/validate_migrations.py
   ```
   檢查單一 head / 全部 version 檔可載入 / 每支有 upgrade()+downgrade()。
   （CI 另有 `alembic-roundtrip`（per-migration up/down pytest）、`alembic-symmetry-lint`、
   single-head gate；上述本地 script 是 push 前的即時版。）
2. **權威預演（必做，會抓 SQLite/靜態測試照不到的 prod-only 失敗）**：對 **DR 還原的 prod
   副本**（見 `docs/sop/dr-runbook.md` 還原流程）跑 cold-start 路徑
   `alembic upgrade heads`，確認套用成功、無 `WARNING`/exception。

**⚠ 不要用「空 DB / `create_all` 基底」當預演**（2026-06-24 實測確認會誤失敗）：
prod 以 `create_all + stamp head` 建立，**跳過 migration 的 `op.execute` 基礎建設**
（SECURITY DEFINER functions / roles / RLS，見記憶 `reference_prod_create_all_stamp_skips_infra`）。
從 `create_all` 基底跑 downgrade/upgrade 會因缺 `public_count_enrolled` / `ivy_parent_role`
等而炸 → 給出假失敗。**唯有對真實 prod schema（DR 還原）預演才有意義。**

#### 一次性前置（2026-06-11 考核規章對齊批次，含 aprreg01）
- ~~部署前確認 prod 的 114上 appraisal cycle 已 finalized~~ **已由程式護欄取代（2026-06-12）**：
  `sync_score_items` 對「基準日早於規章生效日 2026-02-01 且已 sync 過」的 cycle 一律 400
  （考勤 leave/absent 分流 54259658 對歷史 cycle 重 sync 是回溯生效：有曠職者會少扣
  〔114上無 ABSENTEEISM 規則〕、全天請假者會多扣，偏離 06-11 對帳基線）。dry_run 預覽
  與首次 sync 不受影響。部署順序不再受 prod cycle 狀態牽制；114上 cycle 仍建議照常
  finalize 收尾，但非部署 blocker。
- aprreg01 為純 DML data migration，**無法 `--sql` 離線產出**，只能 online 跑；
  upgrade 後抽驗 `appraisal_bonus_rates` 5 組值（10000/8000/8000/6000/3500）與
  `appraisal_scoring_rules` count(effective_from='2026-02-01')=24，console 不得出現 `WARNING aprreg01`。

### 4.2 Backup
- ⚠ **首選恢復路徑待確認**：DB 已遷至 Zeabur PostgreSQL，原「Supabase Pro PITR（最近 7 天）」
  **不再適用**。需查證 Zeabur PG 是否提供 snapshot / PITR 及其保留窗口；在確認前，**唯一可信
  的恢復來源是下方異地 pg_dump（R2）**。
- 異地備份：GH Actions `dr-backup.yml` 每日 02:17 +08 推送 pg_dump 至 Cloudflare R2 `ivy-dr/db/daily/`，每月 1 號額外複製至 `db/monthly/`
- ⚠ **`dr-backup.yml` 的 pg_dump 目標須指向 Zeabur PG**：自 6/23 DB 遷移後，原 workflow 仍
  打 `SUPABASE_DB_HOST`（舊 Supabase）。已改為單一 `DR_DATABASE_URL` secret（Zeabur PG 對外
  連線字串），未設時 workflow fail-loud。**部署前必設此 secret 並手動觸發一次驗證**（確認 R2
  收到的 dump 是 Zeabur PG 真資料、抽查一張表 row count），否則異地備份等同無效。
- Storage 鏡像：同 workflow 把 leave-attachments + growth-reports 鏡像至 R2 `ivy-dr/storage/`
- 完整 DR 流程、演練 SOP、retention、回填步驟：見 `ivy-backend/docs/sop/dr-runbook.md`
- 月度演練：手動觸發 `dr-restore-drill.yml`，report artifact 存 GH Actions 90 天

---

## 5. 監控 / 告警

日常監控設定以 `docs/sop/observability.md` 為準，目前包含：
- L1：UptimeRobot 打 `/api/health/ready`
- L1b：UptimeRobot 可另打 `/api/health/schedulers` 監控排程 heartbeat lag
- L2：慢請求累計後透過 LINE ops 群告警
- L3：Sentry Performance / exception 追蹤

DR backup 失敗會 LINE Notify ops 群；DR 細節見 `ivy-backend/docs/sop/dr-runbook.md` §8。

健康檢查端點：
- `GET /health/live` — 進程活著就 200
- `GET /health/ready` — DB 可連即 200；`?deep=1` 需權限並檢查 LINE / Supabase / DB pool
- `GET /health/schedulers` — 排程 heartbeat 無 lag 即 200；至少一個 lagging 回 503

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
2. 設 `CACHE_BACKEND=redis`；若只要 WebSocket 跨 worker，至少設 `BROADCAST_BACKEND=redis` + `CACHE_REDIS_URL`
3. 評估 PostgreSQL connection pool 是否夠（每 worker 一個 pool）
4. 確認所有 scheduler 都有 advisory lock / row claim / persistent watermark，避免多 worker 重複執行

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
- 訊息含 DB 連線失敗 → 檢查 `DATABASE_URL`、Zeabur PostgreSQL service 狀態（是否 RUNNING）

### 7.2 `/api/*` 502 / 504
- 前端 nginx 反代不到後端：檢查 `BACKEND_URL` Runtime Variable
- 後端 healthcheck 過但 API 慢：查 Zeabur PostgreSQL connection pool 是否耗盡（單 worker pool 20 / PG max_connections）

### 7.3 LIFF 開啟後 401 重定向迴圈
- 前後端 cookie domain 不一致：確認前端反代 `/api/*` 走的是同網域
- `LIFF_ID` 前後端不一致：對齊 build args 與後端 env

### 7.4 Migration 跑不過
- Zeabur log 看 `startup/migrations.py` 錯誤
- 本機 `alembic heads` 確認只有一個 head
- Multi-head：本機產 merge migration 後 push

---

## 8. 上線前 XFF / rate-limit 驗證

### 8.1 風險說明

本系統用 `X-Forwarded-For`（XFF）解析真實 client IP，並以此作為 per-IP 滑動視窗限流的 bucket key（`utils/request_ip.py` + `utils/rate_limit.py`）。

**攻擊面**：若 Zeabur edge **不把真實 client IP append 到 XFF**，則攻擊者可自帶 `X-Forwarded-For: 1.2.3.4` header，讓後端誤判 client IP，繞過 per-IP 限流。

**預設設定（`TRUSTED_PROXY_IPS="*"`）的問題**：
- `"*"` 不是合法 CIDR，`_parse_trusted_proxies()` 會 fallback 成只信任 RFC1918（10/8, 172.16/12, 192.168/16, 127/8）。
- 若 Zeabur edge IP 不在 RFC1918，後端不會剝除它的 XFF append → 攻擊者仍可偽造。
- 正確做法：把 `TRUSTED_PROXY_IPS` 設為 Zeabur edge 出口 CIDR，讓後端只信任邊緣 proxy 的 XFF append。

### 8.2 驗證步驟

**前置**：系統已部署 prod，後端 log 可存取。

**Step 1：確認 Zeabur 是否正確 append client IP 到 XFF**

從兩個不同來源 IP（例如：辦公室網路、手機熱點）各執行：

```bash
# 偽造 XFF，觀察後端看到的是哪個 IP
curl -s -H 'X-Forwarded-For: 1.2.3.4' \
  https://<prod-host>/api/activity/public/courses | head -c 200

# 同時觀察後端 log 中 audit / rate-limit bucket 記錄的 IP
```

**預期行為**（Zeabur 設定正確時）：
- 後端 log 顯示 rate-limit bucket IP = 真實 peer IP（非 `1.2.3.4`）
- Zeabur edge 會在 XFF chain 最右追加真實 client IP，後端剝除 edge IP 後拿到真實來源

**異常行為**（需修正時）：
- 後端 log 顯示 rate-limit bucket IP = `1.2.3.4`（偽造值）→ 攻擊者可繞過限流

**Step 2：實測觸發 429 並確認 bucket 是否正確**

```bash
# 對公開報名端點連送請求，確認 429 回應的限流 bucket 是真實 IP
for i in $(seq 1 25); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H 'X-Forwarded-For: 1.2.3.4' \
    -X POST https://<prod-host>/api/activity/public/register \
    -H 'Content-Type: application/json' \
    -d '{"course_id": 1, "student_name": "test"}'
done
```

- 若第 21+ 次（或設定的限流閾值後）回 429 → 限流生效
- 確認 log 中 bucket key 是真實 IP，而非 `1.2.3.4`

### 8.3 修正動作

若 Step 1 確認 bucket IP = 偽造值（Zeabur edge 未正確 append XFF）：

1. **抓 Zeabur edge 出口 CIDR**：
   ```bash
   # 在後端 log 中找不帶 XFF 的請求，看 request.client.host
   # 或在 Zeabur 文件確認 edge IP range
   ```

2. **設定 `TRUSTED_PROXY_IPS`**（Zeabur Service Variables）：
   ```
   TRUSTED_PROXY_IPS=<zeabur-edge-cidr-1>,<zeabur-edge-cidr-2>
   ```
   例如：`TRUSTED_PROXY_IPS=103.20.128.0/18,103.30.0.0/16`（依 Zeabur 實際 edge 為準）

3. **重新部署**後重跑 Step 1 + Step 2 驗證。

4. **確認啟動 log 不再出現** `TRUSTED_PROXY_IPS 未明設可信代理`（啟動告警，由 `warn_if_trusted_proxies_unset` 在 `on_startup` 主動發出）。未設或填 `*` 時會出現；設好合法 CIDR 重啟即消失＝生效；**仍出現＝設定未生效**。
   > ⚠ 舊版「`TRUSTED_PROXY_IPS=* 解析後無有效 CIDR`」訊息來自 `_parse_trusted_proxies`，因 `get_client_ip` 在未明設時短路 return 而**永不觸發（死碼）**，不可作為驗證依據（P2-7，2026-06-23 資安掃描已改）。**Step 1/Step 2 的 curl 偽造 XFF 驗 bucket key 才是有效驗證。**

### 8.4 本機 dev 說明

本機 dev（`TRUSTED_PROXY_IPS` 預設 `"*"`）fallback RFC1918 是 **by design**：
- dev 環境無 LB，`request.client.host` 即是真實 IP，無需剝 XFF
- RFC1918 預設信任讓 nginx / docker-compose 內網反代正常運作

> 僅 prod 需設定 `TRUSTED_PROXY_IPS`。

---

## 9. 版本 / 維護

- Python: 3.11+
- Node: 22+
- Postgres: 14+
- Alembic head 數：1（保持唯一）
- 依賴上限規則：見 `requirements.txt` 註解
