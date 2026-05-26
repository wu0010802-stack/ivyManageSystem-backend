# DR 異地備份 / Restore 演練 / Runbook 設計

> 日期：2026-05-26
> 範疇：workspace 跨 repo（ivy-backend ops + workspace 文件）
> 觸發原因：審計 finding「無自動異地備份、無 restore 演練、RTO 未知」
> 對應 finding 建議 (a)(b)(c)(d) 全部覆蓋

---

## 1. 目標與不變量

### 1.1 業務目標
解決三個既存風險：
1. **Supabase Pro PITR 7 天視窗外的 DB 災難** — 帳號鎖、字段被誤改太久才發現、整個 project 損壞
2. **Supabase Storage bucket 災損** — 假單附件、成長報告（合規/個資/教學紀錄）單一 region 無 fallback
3. **RTO 未知** — 「上次備份 2 天前」沒人能保證能還原；缺乏演練數據

### 1.2 服務水準目標
- **RPO 24h** — 最壞情況遺失最近 24 小時的資料變更
- **RTO 4–8h** — 從決定 restore 到服務恢復可登入操作，1 個工作日內完成
- **首選恢復路徑** 仍為 Supabase Pro PITR（RPO ~分鐘級 / RTO ~1 小時）；R2 dump 為 PITR 失效時的長期保險

### 1.3 架構不變量
- 主資料源永遠是 Supabase；R2 為 **單向** 鏡像，restore 流程顯式拉到臨時 PG，不會直接寫回 prod
- 所有寫 R2 的 workflow 必須 `concurrency: dr-backup`，避免同日重複跑覆蓋未完成檔
- GH Actions 用 Supabase **read-only role** 認證（不用 prod app DSN）
- Restore drill 不碰 prod；用 GH Actions services 跑臨時 PG，驗完即拋

---

## 2. 架構總覽

```
                  ┌─────────────────────────────────────┐
                  │  Supabase Pro (主資料)              │
                  │  • Postgres (PITR 7d)              │
                  │  • Storage buckets:                 │
                  │    - activity-posters  (public)     │
                  │    - leave-attachments (private)    │
                  │    - attendance-imports (private)   │
                  │    - growth-reports (private,新建)  │
                  └────────────┬────────────────────────┘
                               │ (1) pg_dump  (2) storage list+download
                               │     via backup_readonly role + service_role
                  ┌────────────▼────────────────────────┐
                  │  GitHub Actions (ivy-backend repo)  │
                  │  • dr-backup.yml      (cron daily)  │
                  │  • dr-restore-drill.yml (manual)    │
                  └────────────┬────────────────────────┘
                               │ (3) aws s3 cp (S3 API)
                  ┌────────────▼────────────────────────┐
                  │  Cloudflare R2 bucket: ivy-dr       │
                  │  • db/daily/ivy-YYYY-MM-DD.dump     │
                  │  • db/daily/ivy-YYYY-MM-DD.sha256   │
                  │  • db/monthly/ivy-YYYY-MM-01.dump   │
                  │  • storage/leave-attachments/...    │
                  │  • storage/growth-reports/...       │
                  │  Lifecycle:                         │
                  │    - db/daily/: 30d                 │
                  │    - db/monthly/: 365d              │
                  │    - storage/leave-attachments: 365d│
                  │    - storage/growth-reports: 永久   │
                  └─────────────────────────────────────┘
```

---

## 3. 範疇外（明確排除）

- **多 region 自動 failover** — 仍需人工切換，runbook §6 描述步驟
- **`activity-posters` bucket 同步** — 公開海報可重建，不納入鏡像
- **`attendance-imports` bucket 同步** — transient 解析完即刪，無備份價值
- **DR 告警的完整 monitoring stack** — Sentry / Prometheus 為獨立題；本 spec 用 LINE Notify 起步並列銜接接點
- **既有 Supabase PITR 流程文件化** — Supabase 官方文件已足夠，本 spec 只在 runbook §6 引用步驟摘要

---

## 4. Phase 0：growth-reports 上 Supabase Storage（前置）

### 4.1 動機
`api/portfolio/reports.py:69` 顯示 `REPORT_ROOT = settings.storage.growth_report_root` 為本機路徑（預設 `./growth_reports`）。Zeabur 單 container 沒掛持久 volume → container restart / redeploy 即丟所有歷史 PDF。這是既存資料 durability bug，需先修才能讓 Phase 2 同步有意義。

### 4.2 變更

| 動作 | 位置 |
|---|---|
| 建 Supabase bucket `growth-reports`（private, RLS=service_role only） | Supabase Dashboard |
| `_MODULE_TO_BUCKET` 加 `"growth_reports": "growth-reports"` | `utils/supabase_storage.py` |
| 改寫存取 PDF：local path → `storage.put(module="growth_reports", ...)` | `api/portfolio/reports.py` |
| `StudentGrowthReport.pdf_path` 改存 storage key（如 `students/{sid}/{report_id}.pdf`） | DB 不需 migration（既有欄位 type 兼容） |
| 下載端點：FileResponse → 302 redirect 至 signed URL | `api/portfolio/reports.py` |
| Migration script：掃 DB 把 local 檔上傳 + verify hash + 更新 path + 刪 local（含 `--dry-run`） | `scripts/migrate_growth_reports_to_supabase.py` |
| `docs/sop/storage-deployment.md` §1 表格加 `growth-reports` | ivy-backend docs |

### 4.3 行為相容
- 本地 dev (`STORAGE_BACKEND=local`)：仍走 local path，行為不變
- prod (`STORAGE_BACKEND=supabase`)：走新 backend
- 保留 reports.py local fallback 兩個 release cycle 後再移除（給 rollback 餘地）

### 4.4 測試
- `tests/test_portfolio_reports.py` 加 2 case：
  1. `STORAGE_BACKEND=supabase` 時生成 PDF 走 supabase backend，DB path 為 storage key
  2. download endpoint 回 302 + Location 含 signed URL pattern
- `tests/test_migrate_growth_reports.py` 新檔：
  1. dry-run 模式不寫不刪
  2. idempotent — 第二次跑跳過已遷移
  3. hash mismatch 時 raise 不刪 local

### 4.5 驗收
- prod 部署後新 PDF 在 Supabase Dashboard → Storage → `growth-reports/` 看得到
- DB `StudentGrowthReport.pdf_path` 全部為 storage key（無 `./growth_reports/...` 殘留）— migration script run report
- container restart 後既有 PDF 仍可下載

---

## 5. Phase 1：pg_dump → R2 daily

### 5.1 前置：Supabase backup_readonly role
在 Supabase SQL editor 執行：
```sql
CREATE ROLE backup_readonly WITH LOGIN PASSWORD '<32 字元亂數>' NOINHERIT;
GRANT CONNECT ON DATABASE postgres TO backup_readonly;
GRANT USAGE ON SCHEMA public TO backup_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_readonly;
```
新表加入後需重跑 `GRANT SELECT`（migration 流程 follow-up，列在 runbook §3 提醒）。

### 5.2 GH Actions workflow
檔案：`ivy-backend/.github/workflows/dr-backup.yml`

```yaml
name: dr-backup
on:
  schedule:
    - cron: '17 18 * * *'      # 02:17 UTC+8（台灣凌晨低峰）
  workflow_dispatch:
concurrency:
  group: dr-backup
  cancel-in-progress: false
jobs:
  dump-and-upload:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - name: Install postgresql-client-15
        run: |
          sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
          sudo apt-get update && sudo apt-get install -y postgresql-client-15
      - name: pg_dump
        env:
          PGPASSWORD: ${{ secrets.SUPABASE_BACKUP_DB_PASSWORD }}
        run: |
          DATE=$(date -u +%Y-%m-%d)
          pg_dump \
            --host=${{ secrets.SUPABASE_DB_HOST }} \
            --port=5432 \
            --username=backup_readonly \
            --dbname=postgres \
            --format=custom \
            --no-owner --no-privileges \
            --file=ivy-${DATE}.dump
          sha256sum ivy-${DATE}.dump > ivy-${DATE}.sha256
          echo "DUMP_DATE=$DATE" >> $GITHUB_ENV
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.R2_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          aws-region: auto
      - name: Upload to R2
        run: |
          # 所有 daily dump 一律走 db/daily/
          aws s3 cp ivy-${DUMP_DATE}.dump   s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          aws s3 cp ivy-${DUMP_DATE}.sha256 s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          # 每月 1 號額外複製到 db/monthly/（長期保留）
          DAY=$(date -u +%d)
          if [ "$DAY" = "01" ]; then
            aws s3 cp ivy-${DUMP_DATE}.dump   s3://ivy-dr/db/monthly/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
            aws s3 cp ivy-${DUMP_DATE}.sha256 s3://ivy-dr/db/monthly/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          fi
      - name: Mirror Supabase Storage → R2
        # 見 §6 Phase 2
        ...
      - name: Notify on failure
        if: failure()
        run: |
          curl -X POST ${{ secrets.LINE_NOTIFY_WEBHOOK }} \
            -d "message=[DR-Backup] ${DUMP_DATE} 失敗，查 GH Actions"
```

### 5.3 R2 bucket 設定
- Bucket 名：`ivy-dr`
- Prefix 結構（清晰前綴避免 lifecycle 互相誤匹配）：
  - `db/daily/ivy-YYYY-MM-DD.dump` — 每日 dump
  - `db/monthly/ivy-YYYY-MM-01.dump` — 每月 1 號的 dump 額外複製一份
  - `storage/leave-attachments/...` — Storage 鏡像
  - `storage/growth-reports/...` — Storage 鏡像
- Lifecycle rules（透過 wrangler 或 R2 dashboard）：
  - `db/daily/` 前綴 → 30 天後刪除
  - `db/monthly/` 前綴 → 365 天後刪除
  - `storage/leave-attachments/` 前綴 → 365 天後刪除
  - `storage/growth-reports/` 前綴 → 無 lifecycle（永久保留）

### 5.4 Secrets（ivy-backend repo settings）
| Secret name | 來源 |
|---|---|
| `SUPABASE_DB_HOST` | Supabase Dashboard → Settings → Database → Direct connection host |
| `SUPABASE_BACKUP_DB_PASSWORD` | §5.1 建 role 時設定的密碼 |
| `R2_ACCESS_KEY_ID` | Cloudflare → R2 → Manage API Tokens（限 `ivy-dr` bucket） |
| `R2_SECRET_ACCESS_KEY` | 同上 |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `LINE_NOTIFY_WEBHOOK` | 沿用既有 ops 群 |

### 5.5 測試與驗收
- 第一次手動 `workflow_dispatch` 跑通 → R2 console 確認 `db/ivy-YYYY-MM-DD.dump` 與 `.sha256` 存在
- `aws s3 cp` 下載 dump + `sha256sum -c` 對得上
- 連跑 2 天 cron 確認排程觸發
- pg_restore 到本地 PG 能成功（不需 sanity SQL 驗，那是 Phase 3）

### 5.6 成本估算
- dev DB 28 員工 ~1.5MB 壓縮 dump
- prod 預估 ~50–200MB（隨幾年資料成長）
- 30 daily + 12 monthly = 42 檔 × 200MB ≈ 8GB → R2 free tier 10GB 內 ✅
- GH Actions 每日 1 run，預估 5–10 min → 月 ~5h，遠低於 free tier 2000 min/月

---

## 6. Phase 2：Supabase Storage → R2 同步

### 6.1 範疇
| Bucket | 同步? | 理由 |
|---|---|---|
| `leave-attachments` | ✅ | 法定保存 5 年，cross-region 保險必要 |
| `growth-reports` | ✅ | 學生 portfolio，永久保留 |
| `activity-posters` | ❌ | 公開海報可重建 |
| `attendance-imports` | ❌ | transient 解析完即刪 |

### 6.2 同步 script
檔案：`ivy-backend/scripts/dr_storage_sync.py`（~120 行）

行為：
1. 用 `supabase-py` 列指定 bucket 所有物件 metadata（`name`, `updated_at`, `size`）
2. 用 `boto3` 列 R2 對應 prefix 物件 metadata（含 user metadata `x-source-updated-at`）
3. Diff 規則：
   - 來源有、目標沒有 → 上傳
   - 來源 `updated_at` > 目標 `x-source-updated-at` → 重傳
   - 目標有、來源沒有 → **不刪**（依賴 R2 lifecycle 自動老化）
4. 上傳時寫 user metadata `x-source-updated-at` 作為下次 diff 基準
5. CLI args：`--buckets`（多個）、`--target`（s3 URI）、`--mode {incremental,full}`、`--dry-run`
6. 純 idempotent，重複跑安全
7. 中斷後下次補齊（無 transaction 概念，逐物件處理）

### 6.3 整合進 Phase 1 workflow
在 §5.2 workflow 後追加 step：
```yaml
      - name: Mirror Supabase Storage → R2
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          R2_ENDPOINT: ${{ secrets.R2_ENDPOINT }}
          AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          AWS_DEFAULT_REGION: auto
        run: |
          pip install supabase boto3
          python scripts/dr_storage_sync.py \
            --buckets leave-attachments growth-reports \
            --target s3://ivy-dr/storage/ \
            --mode incremental
```

### 6.4 R2 storage prefix lifecycle
- `storage/leave-attachments/`：365 天刪
- `storage/growth-reports/`：永久保留（無 lifecycle rule）

### 6.5 額外 secrets
| Secret name | 來源 |
|---|---|
| `SUPABASE_URL` | Supabase Dashboard → Settings → API |
| `SUPABASE_SERVICE_ROLE_KEY` | 同上（已存在於後端 env，這裡複用） |

### 6.6 測試
- `tests/test_dr_storage_sync.py` 新檔：
  1. Mock supabase client list + boto3 stub
  2. 新檔上傳一次後 metadata 寫入正確
  3. 目標已有最新版（user metadata 比對）→ 跳過
  4. 目標版本舊 → 重傳
  5. 目標多餘檔（來源已刪）→ 不刪
  6. `--dry-run` 不發生任何寫入

### 6.7 驗收
- dev 環境跑一次 → R2 console `storage/leave-attachments/` 與 `storage/growth-reports/` 結構鏡像 Supabase
- 故意改一個物件再跑 → R2 對應檔被覆蓋，metadata 更新
- 故意 Supabase 刪一個物件再跑 → R2 對應檔還在

---

## 7. Phase 3：Restore drill workflow

### 7.1 目的
每月手動觸發一次：拉最新 R2 dump → 還原到臨時 PG → 跑 sanity SQL → 出報告 → 記 RTO。把 RTO 從「未知」變「實測 N 分鐘」。

### 7.2 GH Actions workflow
檔案：`ivy-backend/.github/workflows/dr-restore-drill.yml`

```yaml
name: dr-restore-drill
on:
  workflow_dispatch:
    inputs:
      dump_date:
        description: "Dump 日期（YYYY-MM-DD），留空抓最新"
        required: false
jobs:
  drill:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    services:
      pg:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: drilltest
        ports: [5432:5432]
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s --health-timeout 5s --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - name: Install pg client
        run: sudo apt-get install -y postgresql-client-15
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.R2_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          aws-region: auto
      - name: Download dump from R2
        run: |
          START_TS=$(date +%s)
          DATE="${{ inputs.dump_date }}"
          if [ -z "$DATE" ]; then
            # 抓 db/daily/ 最新一筆
            DATE=$(aws s3 ls s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }} \
                   | grep '\.dump$' | sort | tail -1 | awk '{print $4}' \
                   | sed 's/ivy-//' | sed 's/\.dump$//')
          fi
          aws s3 cp s3://ivy-dr/db/daily/ivy-${DATE}.dump   ./drill.dump   --endpoint-url=${{ secrets.R2_ENDPOINT }}
          aws s3 cp s3://ivy-dr/db/daily/ivy-${DATE}.sha256 ./drill.sha256 --endpoint-url=${{ secrets.R2_ENDPOINT }}
          # sha256 manifest 內含原檔名，先 normalize 對應 drill.dump
          sed -i "s/ivy-${DATE}\.dump/drill.dump/" drill.sha256
          sha256sum -c drill.sha256
          echo "DRILL_DATE=$DATE"          >> $GITHUB_ENV
          echo "START_TS=$START_TS"        >> $GITHUB_ENV
          echo "DOWNLOAD_END_TS=$(date +%s)" >> $GITHUB_ENV
      - name: pg_restore
        env:
          PGPASSWORD: drilltest
        run: |
          pg_restore --no-owner --no-privileges \
            -h localhost -U postgres -d postgres \
            --jobs=4 ./drill.dump
          echo "RESTORE_END_TS=$(date +%s)" >> $GITHUB_ENV
      - name: Sanity SQL
        env:
          PGPASSWORD: drilltest
        run: |
          psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 \
            -f .github/workflows/dr_restore_sanity.sql > sanity_output.txt
          cat sanity_output.txt
      - name: Generate drill report
        run: |
          python .github/workflows/dr_drill_report.py \
            --dump-date $DRILL_DATE \
            --start-ts $START_TS \
            --download-end-ts $DOWNLOAD_END_TS \
            --restore-end-ts $RESTORE_END_TS \
            --sanity-output sanity_output.txt \
            > drill-report.md
      - uses: actions/upload-artifact@v4
        with:
          name: drill-report-${{ env.DRILL_DATE }}
          path: drill-report.md
          retention-days: 90
      - name: LINE notify result
        if: always()
        run: |
          STATUS="${{ job.status }}"
          curl -X POST ${{ secrets.LINE_NOTIFY_WEBHOOK }} \
            -d "message=[DR-Drill] ${DRILL_DATE} 結果：${STATUS}"
```

### 7.3 Sanity SQL
檔案：`ivy-backend/.github/workflows/dr_restore_sanity.sql`

```sql
-- 1. 核心表 row count > 0
SELECT 'users' AS tbl, count(*) n FROM users
UNION ALL SELECT 'employees', count(*) FROM employees
UNION ALL SELECT 'students', count(*) FROM students
UNION ALL SELECT 'salary_records', count(*) FROM salary_records
UNION ALL SELECT 'attendance_records', count(*) FROM attendance_records
UNION ALL SELECT 'guardians', count(*) FROM guardians
UNION ALL SELECT 'leaves', count(*) FROM leaves;

-- 2. Latest event 時間（驗備份新鮮度）
SELECT 'latest_attendance' AS check, MAX(created_at)::text AS value FROM attendance_records
UNION ALL SELECT 'latest_audit', MAX(created_at)::text FROM audit_logs;

-- 3. Alembic head（runbook 對齊 repo HEAD 的 alembic heads）
SELECT 'alembic_version' AS check, string_agg(version_num, ',') AS value FROM alembic_version;

-- 4. 跨表 join 抽 3 員工不爛
SELECT u.id, u.username, e.name, e.position
FROM users u JOIN employees e ON e.user_id = u.id
LIMIT 3;
```

### 7.4 Drill report 產生器
檔案：`ivy-backend/.github/workflows/dr_drill_report.py`（~40 行）

輸出 Markdown 包含：
- 演練日期、dump 日期、與當日落差天數
- RTO 拆解：download N 秒 / restore M 秒 / sanity O 秒 / 總計
- Sanity SQL 結果摘要：row counts、latest_attendance 與 dump 日期落差、alembic version
- 判定：PASS（所有 row count > 0 且 latest_attendance 距 dump 日 ≤ 2 天） / WARN（有 0 row 或時間落差 > 2 天） / FAIL（pg_restore 出錯）

### 7.5 驗收
- 第一次手動觸發 → 取得 GH Actions artifact `drill-report-YYYY-MM-DD`
- 故意指定 7 天前的 `dump_date` 跑一次 → report 標記 staleness warning
- LINE notify 收到 `[DR-Drill] ... 結果：success`
- 月度演練成果填入 runbook §5 的 RTO 紀錄表

---

## 8. Phase 4：dr-runbook.md + 既存文件更新

### 8.1 新檔：`docs/sop/dr-runbook.md`（workspace 層級）

骨架（最終 ~250–300 行）：

```markdown
# Disaster Recovery Runbook

文件最後更新：YYYY-MM-DD
適用版本：ivy-backend / ivy-frontend / Supabase Pro / R2 ivy-dr

## 1. 目的與保證
- RPO 24h / RTO 4–8h（實測 RTO 見 §5）
- 涵蓋場景：見本 spec §1.1
- 不涵蓋：跨 region 自動 failover

## 2. 備份組成
- DB：每日 02:17 +08 pg_dump → R2 `ivy-dr/db/`
- Storage：同 workflow 鏡像 leave-attachments + growth-reports
- 保留：daily 30 + monthly 12（DB）/ leave-attachments 365d / growth-reports 永久

## 3. 認證與角色清單
- Supabase `backup_readonly` role（建立步驟、權限範圍、新表 GRANT 流程、輪替週期 90 天）
- GitHub secrets 對照表（誰能改 / 輪替紀錄）
- R2 access key IAM scope（只給 ivy-dr）

## 4. 例行檢核（每週）
- GH Actions `dr-backup` workflow 連續 7 天綠燈
- R2 console `ivy-dr/db/` 最新檔 size 與 sha256 manifest 存在

## 5. 月度演練 SOP
- 觸發步驟（GH UI → Actions → dr-restore-drill → Run workflow）
- 結果判讀（report artifact 各欄位意義）
- 不過關時的升級路徑
- 月份 → 實測秒數 紀錄表

## 6. 災難演習實戰 SOP（真的要 restore 到 prod）
- 決策樹：PITR 還是 R2 dump?
  - 災難發生時間在 7 天內 + 資料未實體損壞 → Supabase PITR
  - 超過 7 天 / 資料實體損壞 / Supabase project 鎖 → R2 dump
- PITR 路徑：Dashboard 操作步驟 + 預估 RTO ~1h
- R2 dump 路徑：
  a. Supabase 建新 PG（或新 project）
  b. 從 R2 拉最新 .dump + 驗 sha256
  c. pg_restore 指令（含 --jobs=4 加速）
  d. 切 DATABASE_URL → 重新部署 Zeabur backend
  e. Storage：手動 supabase storage upload 從 R2 鏡像回填（含命令範例）
  f. Smoke test：admin 登入 / 員工列表 / 最新一筆 salary_records
- 預估各步驟耗時（首次實戰演練後填）

## 7. Storage 災損 SOP（DB 沒事、bucket 沒了）
- 從 R2 拉 leave-attachments / growth-reports → supabase storage upload
- DB 內 path 不變，服務自動接上

## 8. 告警銜接
- 目前：dr-backup workflow 失敗 → LINE Notify
- Sentry 啟用後：workflow failure 額外 capture 一筆 event
- 連續 2 天 backup 失敗 = P0

## 9. 已知限制與 backlog
- 跨 region failover 仍需人工
- PITR 視窗 7 天，超出後資料落後最多 24h（接受的 RPO）
- growth-reports migration 期既存 local PDF 須由 Phase 0 script 補搬
```

### 8.2 更新 `docs/sop/zeabur-deployment-runbook.md` §4.2

改寫為：
```markdown
### 4.2 Backup
- Supabase Pro 內建 PITR（最近 7 天）— 首選恢復路徑
- 異地備份：GH Actions `dr-backup.yml` 每日 02:17 +08 推送 pg_dump 至 Cloudflare R2
- 完整 DR 流程、演練 SOP、retention：見 `docs/sop/dr-runbook.md`
- 月度演練：手動觸發 `dr-restore-drill.yml`，report artifact 存 GH Actions 90 天
```

### 8.3 更新 `docs/sop/zeabur-deployment-runbook.md` §5

在「P1 待辦」清單後追加：
```markdown
DR backup 失敗會 LINE Notify；Sentry 啟用後納入監控（見 dr-runbook.md §8）
```

### 8.4 更新 `ivy-backend/docs/sop/storage-deployment.md`

- §1 表格加入 `growth-reports`（Private，Purpose：學生成長報告 PDF，後端發 signed URL）
- §5「切回 local」章節補一句：「另有 R2 異地鏡像 `ivy-dr/storage/`，可用 `aws s3 cp ... --endpoint-url=$R2_ENDPOINT` 拉回後再 supabase storage upload」

---

## 9. 開放細節（spec review 時定案）

| 項目 | 預設建議 | 替代選項 |
|---|---|---|
| Backup retention | daily 30 + monthly 12 | daily 14 + monthly 24 |
| Storage retention | leave-attachments 365d / growth-reports 永久 | 全 365d、或全永久 |
| R2 encryption | server-side（R2 預設加密）足夠 | + 客戶端 GnuPG（multi-team 機敏才需要） |
| Drill 觸發人 | admin 每月 1 號手動 | GH issue scheduled + bot 自動 ping |
| LINE Notify channel | 沿用既有 ops 群 | 開新「DR 告警」群 |
| 跨 region 規劃 | 不在本 spec | 開新 spec 評估 Supabase region migration |

---

## 10. 實作順序與依賴

```
Phase 0 (growth-reports → Supabase) 
   └── 必須先 ship + migrate 完成
        ↓
Phase 1 (pg_dump → R2 daily)        ← 可獨立 ship，不依賴 Phase 0
        ↓
Phase 2 (Storage sync, leave + growth)  ← 依賴 Phase 0 完成（growth bucket 才有東西可同步）
        ↓
Phase 3 (Restore drill workflow)    ← 依賴 Phase 1（要有 dump 可拉）
        ↓
Phase 4 (Runbook + 文件更新)        ← 依賴 1–3 都實測過，再固化文字
```

**注意：**
- Phase 1 與 Phase 0 可平行作業（不同子目錄、不同 PR）
- Phase 2 在 Phase 0 prod migration 完成且 driver 驗證後再開
- Phase 3 需要 Phase 1 已連續跑 ≥ 3 天累積素材
- Phase 4 在 Phase 3 第一次成功演練後落地（runbook 才能寫實測 RTO）

---

## 11. 失敗模式 / 風險

| 失敗模式 | 偵測 | 緩解 |
|---|---|---|
| pg_dump 連 Supabase 超時 | workflow timeout 30min | 改 `--jobs=4` 加速 dump；連續 2 天失敗升 P0 |
| R2 上傳被中斷 | sha256 verify 階段失敗 | workflow rerun；R2 上傳 idempotent |
| backup_readonly role 對新表沒 SELECT | sanity SQL row count = 0 | runbook §3 列每次 alembic migration 後跑 `GRANT SELECT ON ALL TABLES` |
| Storage sync 超出 GH Actions 30min | step timeout | 改分批；或拆出獨立 workflow |
| GH Actions free quota 用盡 | workflow run 排隊 | 預估 <5h/月，遠低於 2000min 上限；用盡時切付費 |
| R2 free tier 超出 | 10GB 上限 | 監控 + lifecycle 觸發；超過再評估 paid tier $0.015/GB-月 |
| Drill workflow 用 Service Role Key | 洩漏即影響 prod Storage | drill 不需 service role；只用 R2 creds |
| growth-reports migration 中斷 | dry-run + idempotent script | 重跑安全；hash mismatch raise 不刪 local |

---

## 12. 測試矩陣

| Phase | 單元測試 | 整合測試 | 手動驗證 |
|---|---|---|---|
| 0 | pytest growth_reports backend / migration script idempotent / dry-run | dev STORAGE_BACKEND=supabase 上下載往返 | prod migration dry-run → 真跑 → container restart 驗 PDF 仍在 |
| 1 | — | — | manual `workflow_dispatch` + R2 console 驗檔 + sha256 + pg_restore 本地能跑 |
| 2 | dr_storage_sync.py diff 邏輯 6 case | mock supabase + boto3 | dev bucket 跑一次 + 改檔重跑驗 metadata 更新 |
| 3 | — | sanity SQL 在臨時 PG 跑得過 | drill workflow_dispatch → artifact 取得 → LINE 收到 |
| 4 | — | — | runbook 由非作者 (user) 走過一遍 §6 步驟，找漏洞 |

---

## 13. 不做的事

- 不換掉 Supabase Pro PITR 為首選恢復路徑（R2 dump 為長期保險，非取代）
- 不引入新監控 SaaS（沿用 LINE Notify + 等 Sentry 銜接）
- 不做客戶端加密（R2 server-side 已加密；除非後續法遵要求）
- 不做 backup_readonly 自動輪替（90 天人工，列入 runbook §3）
- 不在本 spec 處理整套 §5 監控告警（只解決 DR backup 相關那塊）
