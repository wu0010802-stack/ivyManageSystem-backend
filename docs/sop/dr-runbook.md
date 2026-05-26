# Disaster Recovery Runbook

文件最後更新：2026-05-26
適用版本：ivy-backend / ivy-frontend / Supabase Pro / R2 ivy-dr

## 1. 目的與保證

- **RPO 24h** — 最壞情況遺失最近 24 小時資料變更
- **RTO 4–8h** — 從決定 restore 到服務恢復可登入操作，1 個工作日內完成
- **實測 RTO**（依月度演練更新）：YYYY-MM-DD = N 分鐘（首次演練後填入；見 §5 紀錄表）

**涵蓋場景：**
- Supabase Pro PITR 7 天視窗外的 DB 災難（帳號鎖、長期誤改未發現、project 損壞）
- Supabase Storage bucket 災損（cross-region 鏡像保險）
- Supabase 帳號完全鎖死

**不涵蓋：**
- 跨 region 自動 failover（需人工切換，見 §6）

## 2. 備份組成

| 內容 | 來源 | 目標 | 頻率 | 保留 |
|---|---|---|---|---|
| pg_dump（custom format） | Supabase Postgres（backup_readonly role） | R2 `ivy-dr/db/daily/` | 每日 02:17 +08 | 30 天 |
| pg_dump 月度長期 | 同上 | R2 `ivy-dr/db/monthly/` | 每月 1 號 | 365 天 |
| leave-attachments | Supabase Storage | R2 `ivy-dr/storage/leave-attachments/` | 同 workflow daily | 365 天 |
| growth-reports | Supabase Storage | R2 `ivy-dr/storage/growth-reports/` | 同 workflow daily | 永久 |

**首選恢復路徑：** Supabase Pro PITR（RPO ~分鐘級 / RTO ~1h）。R2 dump 為 PITR 失效時的長期保險。

## 3. 認證與角色清單

### Supabase `backup_readonly` role
- 權限：`CONNECT` / `USAGE ON SCHEMA public` / `SELECT ON ALL TABLES`
- 密碼：1Password「DR / Supabase backup_readonly」條目
- **新表加入後須跑：** `GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_readonly;`（已用 `ALTER DEFAULT PRIVILEGES` 涵蓋未來表，但既有表新加 column 不需重做）
- 輪替：每 90 天

### GitHub secrets（ivy-backend repo）

| Name | 用途 | 輪替 |
|---|---|---|
| `SUPABASE_DB_HOST` | pg_dump 目標 | 不需 |
| `SUPABASE_BACKUP_DB_PASSWORD` | 上面 role 密碼 | 90 天 |
| `SUPABASE_URL` | Storage sync API | 不需 |
| `SUPABASE_SERVICE_ROLE_KEY` | Storage sync 認證 | 90 天 |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 寫入 | 90 天 |
| `R2_ENDPOINT` | R2 URL | 不需 |
| `LINE_NOTIFY_WEBHOOK` | 失敗告警 | 依 LINE 群輪換 |

### R2 access token IAM scope
- 限定 bucket `ivy-dr`
- 權限：Object Read & Write

## 4. 例行檢核（每週）

- [ ] 看 GH Actions `dr-backup` 連續 7 天綠燈
- [ ] R2 `db/daily/` 最新檔 size 與前一天落差 ±30% 內
- [ ] R2 `db/daily/` 對應 sha256 manifest 存在

## 5. 月度演練 SOP

1. 每月 1 號（或最近工作日）admin 觸發 GH UI → Actions → `dr-restore-drill` → Run workflow
2. 留空 `dump_date` 抓最新；或指定特定日期（測 staleness）
3. 等 < 30 分鐘
4. 下載 artifact `drill-report-YYYY-MM-DD`
5. 讀 Judgment：
   - `PASS` — 所有 row count > 0 且 latest_attendance 距 dump 日 ≤ 2 天
   - `WARN` — 有警告但 restore 成功
   - `FAIL` — pg_restore 出錯（升級 P0）

### 實測 RTO 紀錄表

| 月份 | 觸發人 | Download 秒 | Restore 秒 | Total 秒 | Judgment | 備註 |
|---|---|---|---|---|---|---|
| 2026-05 | | | | | | 首次演練 |
| 2026-06 | | | | | | |

## 6. 災難演習實戰 SOP（真的要 restore 到 prod）

### 決策樹

```
災難發生時間 < 7 天前？─yes─→ Supabase PITR
        │
        no
        ↓
Supabase project 仍可登入？─yes─→ Supabase PITR（若資料未實體損壞）
        │
        no
        ↓
    R2 dump restore
```

### Path A：Supabase PITR

1. Supabase Dashboard → Database → Backups → Point-in-time recovery
2. 選 target timestamp（事故前 5 分鐘）
3. 等 Supabase 還原（~1h）
4. Smoke test：見 §6 Path B Step 5

### Path B：R2 dump restore

1. **建新 Postgres 目的地**
   - 選 a：Supabase 建新 project（適合整個 project 損壞）
   - 選 b：Supabase Dashboard 還原到既有 project（適合 schema/data 部分壞）

2. **從 R2 拉最新 dump 並驗 sha256**

   ```bash
   aws s3 ls s3://ivy-dr/db/daily/ --endpoint-url=$R2_ENDPOINT
   aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.dump   ./restore.dump   --endpoint-url=$R2_ENDPOINT
   aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.sha256 ./restore.sha256 --endpoint-url=$R2_ENDPOINT
   sed -i "s/ivy-YYYY-MM-DD\.dump/restore.dump/" restore.sha256
   sha256sum -c restore.sha256
   ```

3. **pg_restore（加 --jobs=4 加速）**

   ```bash
   PGPASSWORD='<new-postgres-pwd>' pg_restore \
     --no-owner --no-privileges --jobs=4 \
     -h <new-host> -U postgres -d postgres \
     ./restore.dump
   ```

4. **切 DATABASE_URL → 重新部署 Zeabur backend**
   - Zeabur Console → ivy-backend → Settings → Environment Variables
   - 改 `DATABASE_URL` 為新目的地 connection string
   - Restart service（等 healthcheck 過）

5. **Storage：從 R2 鏡像回填**

   ```bash
   # 確認 supabase CLI 已安裝
   supabase login

   for bucket in leave-attachments growth-reports; do
     aws s3 sync \
       s3://ivy-dr/storage/$bucket/ \
       ./restore_storage/$bucket/ \
       --endpoint-url=$R2_ENDPOINT

     # 對每個檔上傳到對應 Supabase bucket（可寫迴圈或用 supabase-py 腳本）
     # 注意：bucket 命名 hyphen vs underscore；新 project 需先建同名 bucket
   done
   ```

6. **Smoke test**
   - [ ] admin 帳號可登入
   - [ ] `/api/employees` 回 200 且 list > 0
   - [ ] 最新一筆 `salary_records` 可在 admin UI 看到
   - [ ] 任一 leave-attachment 可下載
   - [ ] 任一 growth-report 可下載

7. **預估各步驟耗時（首次實戰演練後填）**

| 步驟 | 估計 | 實測（首次） |
|---|---|---|
| 1. 建新 PG | 30min（新 project）/ 10min（既有 project） | |
| 2. R2 download + sha256 | 5min（500MB dump） | |
| 3. pg_restore | 10min | |
| 4. 切 DATABASE_URL + Zeabur 重啟 | 5min | |
| 5. Storage 回填 | 30min（依檔數量） | |
| 6. Smoke test | 10min | |
| **總計** | ~1h30min – 2h | |

## 7. Storage 災損 SOP（DB 沒事、bucket 沒了）

若僅 Supabase Storage bucket 損壞（DB 仍正常）：

1. 確認 Supabase Storage 仍可寫（建測試 bucket 上傳一個檔驗證）
2. 從 R2 拉對應前綴

   ```bash
   aws s3 sync \
     s3://ivy-dr/storage/leave-attachments/ \
     ./recovery/leave-attachments/ \
     --endpoint-url=$R2_ENDPOINT
   ```

3. 用 supabase CLI 或 supabase-py 上傳回原 bucket（路徑保持一致）
4. DB 內 `attachment_paths` / `file_path` 不變，服務自動接上
5. 確認 admin UI 下載一筆原本壞掉的檔 = pass

## 8. 告警銜接

- **目前：** `dr-backup` workflow 失敗 → `LINE_NOTIFY_WEBHOOK` 通知 ops 群
- **Sentry 啟用後：** workflow failure 額外 `capture_exception`
- **連續 2 天 backup 失敗 = P0**：admin 立即介入，先看 GH Actions log 是 pg_dump 連線問題還是 R2 寫入問題

## 9. 已知限制與 backlog

- 跨 region failover 仍需人工切換 Supabase region（規劃中）
- PITR 視窗 7 天；超出後 R2 dump 最多落後 24h（接受的 RPO）
- backup_readonly role 對新 schema 新表需手動 `GRANT SELECT`（已用 default privileges 涵蓋未來表）
- growth-reports migration 期既存 local PDF 須由 `scripts/migrate_growth_reports_to_supabase.py` 補搬
- Storage sync 不刪 R2 上多餘檔（依 lifecycle 自然老化）
