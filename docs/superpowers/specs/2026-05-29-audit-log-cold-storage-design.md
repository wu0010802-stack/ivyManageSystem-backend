# audit_log GC + Cold Storage 合規設計

**Date**: 2026-05-29
**Status**: Phase 1 落地（trigger relax）+ Phase 2-3 待 user 決議
**Scope**: BE (ivy-backend) — alembic + scheduler + R2 + Postgres role
**Compliance**: 個資法 §11 特定目的消失應主動刪除

---

## 1. 問題陳述（第五輪 P0 audit #1）

- `audit_logs` 表被 `trg_audit_log_immutable_delete` trigger 鎖死，**連 superuser 都不能刪**
- 第四輪 P0 #4 已揭：`audit_logs.changes` JSON **明文存 PII**（身分證、銀行帳號 before/after）
- 量化：~50 admin ops × 30 人 × 22 天 = **33,000 row/月**，1 年 ~40 萬，5 年 ~200 萬 row 無法清
- 違規：(a) §11 retention 義務（特定目的消失應主動刪）；(b) trigger 設計使合規修補變極複雜

---

## 2. 三階段方案

### Phase 1：解阻塞（本 PR 已落地）

**alembic `auditrelax01`**：trigger 改為「擋一般 user / 放行 `audit_archiver` Postgres role」。

- UPDATE 仍 100% 擋（稽核內容不可改）
- DELETE 僅 `audit_archiver` 可
- 未來 cold storage script 走 `SET ROLE audit_archiver; ... RESET ROLE;` pattern

**user manual ops**（migration 落 prod 後）：
```sql
CREATE ROLE audit_archiver NOLOGIN;
GRANT DELETE ON audit_logs TO audit_archiver;
-- 不 grant SELECT 給 archiver；不 grant 任何 role 給 LOGIN user
```

### Phase 2：Cold Storage 匯出（follow-up PR）

每月對 1 個月前以上的 audit_logs：

1. SELECT 該月 row → 寫 R2 parquet：`s3://ivy-dr/audit/<YYYY-MM>.parquet`
2. parquet sha256 驗證 + 寫 manifest `s3://ivy-dr/audit/<YYYY-MM>.manifest.json`
3. **匯出成功** 才走 `SET ROLE audit_archiver; DELETE FROM audit_logs WHERE created_at < cutoff; RESET ROLE;`
4. 全程交易：任一步失敗則 ROLLBACK，row 不刪

**留庫期**：當前月 + 前一個整月（30-60 天熱資料）；其餘自動匯出冷儲

**檢索路徑**（rare）：DuckDB read R2 parquet：
```sql
SELECT * FROM read_parquet('s3://ivy-dr/audit/2025-11.parquet') WHERE entity_id = '...';
```

### Phase 3：scheduler 自動化（follow-up PR）

`services/security_gc_scheduler.py` 加 monthly step：
- 每月 1 號 03:00 觸發 `_run_audit_log_cold_storage()`
- workflow_dispatch 也可手動觸發 dry-run
- 失敗 Sentry alert，retry 24h

---

## 3. Phase 1 驗收

- [ ] alembic `auditrelax01` 上 prod 成功
- [ ] `CREATE ROLE audit_archiver NOLOGIN;` 手動建好
- [ ] 一般 LOGIN user 仍無法 `DELETE FROM audit_logs;`（驗 exception）
- [ ] `SET ROLE audit_archiver; DELETE FROM audit_logs WHERE id = ...; RESET ROLE;` 成功（驗 bypass）
- [ ] UPDATE 仍 100% 擋（任何 user 都不行）

---

## 4. Phase 2-3 業務決議點（user 待回）

| 決議 | 選項 |
|------|------|
| 留庫期 | 30 天 / 60 天 / 90 天（推薦 60 天 — 含當月 + 前一個整月） |
| 冷儲 retention | 5 年 / 7 年 / 永久（推薦 7 年 — 對齊 §15 個資法上限 + 商業會計法 5 年 buffer） |
| cold storage 介質 | Cloudflare R2（已有 DR runbook）/ AWS S3 Glacier / 本地 NAS |
| 自動化頻率 | 每月 1 號 / 每季 / 半年（推薦每月 — 攤平處理量） |

決議後再開 Phase 2 PR。

---

## 5. Out of scope（本 PR）

- 實作 cold storage upload script（Phase 2）
- scheduler tick（Phase 3）
- `audit_archiver` role 建立（USER manual ops）
- 舊 audit_logs 一次性匯出歷史資料（一次性 maintenance job）
- 政府申報書 / 兒少法 retention 上限可能不同的特殊類別 audit 處理

---

## 6. 風險

| 風險 | 緩解 |
|------|------|
| trigger relax 後 admin 不小心 DELETE | 不 grant DELETE 給 LOGIN user；只 audit_archiver NOLOGIN；DELETE 需顯式 `SET ROLE` |
| audit_archiver role 被 grant 給 LOGIN user | infra 程序：role grant 需 PR review + audit_log 紀錄（meta-audit） |
| Cold storage parquet 損壞 | sha256 manifest + R2 versioning + DR runbook 月例 verify |
| DELETE 與 INSERT 競態 | DELETE 用 `WHERE created_at < cutoff`，INSERT 用 `now()`，cutoff 至少 1 hr 前避免 boundary |
| 查歷史時 R2 parquet 不可用 | DR runbook 已有 R2 → 本地 restore drill；DuckDB 也可離線讀本地 parquet |

---

## 7. 參考

- 第五輪 P0 audit #1 原文（trigger 阻塞 + PII retention 違規）
- 第四輪 P0 #4 原文（changes JSON 含明文 PII）
- 既有 trigger migration：`alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py`
- DR runbook：`docs/sop/dr-runbook.md`
- 個資法 §11 / §15
