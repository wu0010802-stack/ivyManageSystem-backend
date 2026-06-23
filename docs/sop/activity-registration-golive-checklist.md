# 課後才藝家長報名 — 上線前設定 Checklist

> 緣由：上線穩定度稽核（2026-06-23，workflow `activity-golive-stability-audit`）。
> 本檔只收「上線前必做、純設定/操作」項目。架構級改善（合併 bootstrap 端點、redis 多 worker、lock_timeout 等）另案。
> 後端目前 Zeabur **SUSPENDED**；以下標 **[T-0]** 者須在 **resume 後端後、開放報名前** 執行（多數需 live 後端才能取值/驗證）。

---

## 摘要

報名開放尖峰會「讓家長註冊不了」的三大根源，依優先序：

| 優先 | 問題 | 對策 | 性質 |
|------|------|------|------|
| 🔴 1 | rate limiter 取錯 client IP（Zeabur 代理後全體塌成同一把 key）→ 全站共用 `register` 額度，幾秒就全體 429 | 設 `TRUSTED_PROXY_IPS` + 依 runbook §8 驗證 | **[T-0]** Zeabur env |
| 🔴 2 | 單 worker + 20 連線池，並發 >20 即排隊逾時 500 並拖垮全 pod | 確認 PG `max_connections` 後調 pool；報名分批開放 | **[T-0]** + 營運 |
| 🟠 3 | 同一 NAT/校園 WiFi 多家長共用 `register 5/min` 互相擠掉 | 限流已改 **env 可調 + 放寬預設**（見 §3） | ✅ 已落地（可再調） |

---

## 1. [T-0] 修正 client IP 解析（最高優先，否則全體家長被當同一人擋掉）

**為什麼**：後端跑在 Zeabur edge 後面。`TRUSTED_PROXY_IPS` 預設 `"*"` 被 `utils/request_ip.py:_has_explicit_trusted_proxies()` 視為「未明設」→ 忽略 `X-Forwarded-For` → `get_client_ip()` 回傳 Zeabur 內網代理 IP，**對所有家長相同**。三個公開限流器（register/query/inquiry）因此全站共用一個桶 → 開放後 register 額度幾秒用完，**所有家長一律 429**。

**程式不需改**：`get_client_ip` 邏輯本身正確（fail-closed 防 XFF 偽造），且限流器吃的就是它。**設好 env 即生效，不需動 `zbpack.json` 的 `--proxy-headers`**（加 `--forwarded-allow-ips='*'` 反而會重新引入 XFF 偽造風險，**不要加**）。

**步驟**（完整版見 `docs/sop/zeabur-deployment-runbook.md` §8）：

- [ ] **抓 Zeabur edge 出口 CIDR**：resume 後端後，在 Zeabur log 找「不帶 XFF 的請求」看 `request.client.host`，或查 Zeabur 文件確認 edge IP range。
- [ ] **設 Zeabur 後端 Service Variable**（**不要填 `*`**）：
      `TRUSTED_PROXY_IPS=<zeabur-edge-cidr-1>,<zeabur-edge-cidr-2>`
      （runbook §8.3 範例 `103.20.128.0/18,103.30.0.0/16`，**依實際為準**）
- [ ] **重新部署 → 驗證**（runbook §8.2）：兩支不同來源 IP（如辦公室 + 手機熱點）各打 6 次 `register`，確認限流計數**各自獨立**。
- [ ] **確認啟動 log 不再出現** `TRUSTED_PROXY_IPS=* 解析後無有效 CIDR...`（此訊息＝設定未生效）。

```bash
# 偽造 XFF，確認後端記到的是「真實 peer IP」而非 1.2.3.4
curl -s -H 'X-Forwarded-For: 1.2.3.4' https://<prod-host>/api/activity/public/courses | head -c 200
# 觀察後端 log 的 rate-limit bucket key：應為真實來源 IP，非 1.2.3.4
```

---

## 2. [T-0] 確認 PostgreSQL `max_connections` 並決定 pool 容量

**為什麼**：單 pod 連線需求 = `db_pool_size(10) + max_overflow(10) = 20`（`config/core.py:34-37`）。`config/core.py:30-33` 自警：部分託管 PG / pgbouncer 預設僅 25-50 連線。若 prod 上限不足，尖峰搶連線會在 `pool_timeout=15s` 後 500。

- [ ] resume 後端後（或用 Zeabur PG 連線字串）執行：

```sql
SELECT current_setting('max_connections')::int AS max_conn,
       (SELECT count(*) FROM pg_stat_activity) AS current_conns,
       current_setting('max_connections')::int - (SELECT count(*) FROM pg_stat_activity) AS headroom;
```

- [ ] 判讀並調整：
  - `max_conn >= ~60` 且餘量充足 → 維持 20，**或**尖峰前用 env 上調（須同步 headroom）：
    `DB_POOL_SIZE`、`DB_POOL_MAX_OVERFLOW`、`THREAD_POOL_TOKEN` 對齊靠 `THREAD_POOL_HEADROOM`（threadpool token 會自動 = pool + headroom）。
  - `max_conn < ~40` 或與其他服務共用 → **下調** `DB_POOL_SIZE`/`DB_POOL_MAX_OVERFLOW`，確保 `20 + 其他用途 < 上限` 留安全餘量。
- [ ] 若 Zeabur PG 前置 **pgbouncer transaction mode**：驗證 `with_for_update` 長交易 + prepared statement 相容（建議報名熱路徑走 session mode）。
- [ ] **削峰（營運，最治本）**：報名**分批/分流開放**（不同年級、班級錯開時段），直接消掉 thundering herd——比調 pool 更有效。

> 本機 dev 參考值：`max_connections=100`，pool 20 綽綽有餘。

---

## 3. ✅ 公開端限流已改為 env 可調 + NAT 友善預設（本次落地）

**已改碼**（`config/network.py` + `api/activity/public.py`，2026-06-23）：三個公開限流器額度改自 `settings.network` 讀取，預設放寬以容忍校園/社區/CGNAT 共用出口 IP：

| 端點 | env（max / window） | 新預設 | 原值 |
|------|---------------------|--------|------|
| register（含 public_update，共用） | `ACTIVITY_REGISTER_RATE_MAX` / `ACTIVITY_REGISTER_RATE_WINDOW` | 20 / 60s | 5 / 60s |
| query | `ACTIVITY_QUERY_RATE_MAX` / `ACTIVITY_QUERY_RATE_WINDOW` | 30 / 60s | 10 / 60s |
| inquiry | `ACTIVITY_INQUIRY_RATE_MAX` / `ACTIVITY_INQUIRY_RATE_WINDOW` | 10 / 60s | 3 / 60s |

> 防超賣靠 register 的 `with_for_update` 行鎖 + `IntegrityError`，**放寬限流不影響正確性**。

- [ ] **[T-0] 逃生口**：上線尖峰若仍誤擋，**改 Zeabur 環境變數 + 重啟即生效，不需 push 後端**（重要：這不違反 §4 的部署凍結，因為不是 code push）。例：`ACTIVITY_REGISTER_RATE_MAX=40`。
- [ ] 若日後改多 worker / 多 pod：務必同時 `RATE_LIMIT_BACKEND=postgres`（否則各 worker 各自計數失準）。

---

## 4. 報名活動視窗內「凍結後端部署」

**為什麼**：`push origin/main` 觸發 Zeabur 自動部署 → cold start 重跑 `alembic upgrade`（綁在 `main.py:200` lifespan 啟動；migration 失敗整個 app 起不來）。單 pod 無冗餘，部署那數十秒＝全站中斷。

- [ ] 報名開放視窗內**不 push 後端**（前端同理，push 即自動部署上線）。
- [ ] 報名期間的調參一律走 **Zeabur 環境變數 + 重啟**（見 §2、§3），不走 code push。
- [ ] 確認近期 migration 無長鎖表 DDL（大表 `add_column` 帶 default rewrite、非 `CONCURRENTLY` 建索引、大筆 backfill）。

---

## 上線當天順序建議

1. resume 後端 → 跑 §2 SQL 確認 `max_connections`，必要時用 env 調 pool。
2. 設 §1 `TRUSTED_PROXY_IPS` → 重部署 → 跑 runbook §8.2 驗證（**這步沒過不要開放報名**）。
3. 確認 §3 限流預設/env 符合預期。
4. 進入 §4 部署凍結。
5.（建議）開放前用 `loadtest/` 對 `/public/register` 壓一次，量測崩潰點落在幾個並發。
