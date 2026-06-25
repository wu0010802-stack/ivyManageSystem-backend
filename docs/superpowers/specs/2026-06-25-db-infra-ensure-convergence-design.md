# DB 基礎建設 idempotent ensure_* 收斂設計（MID-3 mutation 半）

**狀態**：設計（待 staging 實 DB 驗證才實作/啟用）
**背景**：設計審查 2026-06-25 主題 B。偵測半已落地（`startup/infra_check.py` +
`tests/test_db_infra_check.py`，啟動唯讀偵測缺漏 → Sentry）。本文件為 **mutation 半**
的設計與安全分析，供有 staging PG 時執行。

## 問題

prod 曾以 `create_all + alembic stamp head` 建立 → 跳過所有 migration 的 `op.execute`
基礎建設（DB role / SECURITY DEFINER function / immutability trigger / RLS policy + FORCE
/ partial unique index），卻被 stamp 成 head → 系統「以為」fully migrated。任何未來
fresh DB（DR 還原、新 region/staging）走相同 `empty → create_all + stamp` 路徑會**靜默
重演 divergence**。現役 prod 已手動補過最危險的部分，但路徑本身仍是 footgun。

## 目標

把 `op.execute`-only 基礎建設收斂成一組 **idempotent `ensure_*` 啟動步驟**，在
`run_alembic_upgrade()` 後（以及任何 `create_all` 後）呼叫，讓 `empty→create_all+stamp`
與 `versioned upgrade` 兩條 bootstrap 路徑收斂到同一終態。`infra_check`（偵測半）作為
執行後的驗證訊號。

## 基礎建設盤點（2026-06-25，subagent 完整性核實）

| 類型 | 數量 | 來源 migration |
|------|------|----------------|
| DB role | 4（`ivy_parent_role`/`ivy_admin_role`/`ivy_audit_writer`/`audit_archiver`，+ login roles） | `parlsr001`、audit 系列 |
| SECURITY DEFINER function | 4（`audit_log_immutable_fn`/`medication_log_immutable_fn`/`parent_owns_attachment`/`public_count_enrolled`） | audit/medication/parlsr/actvcnt |
| immutability trigger | 3（`trg_audit_log_immutable_delete`/`_update`/`trg_medication_log_immutable`） | audit/medication 系列 |
| RLS policy（家長隔離） | 33（`parent_isolate_*` + `parent_self_guardian`，多數 f-string 迴圈產生） | `parlsr002`–`parlsr011` |
| FORCE-RLS 表 | 33 | 同上 |
| partial unique index | ~13 關鍵（金流/醫療/單例，另約 15 個次要） | 17 個 migration（見偵測清單） |

## 冪等性策略（per type）

- **Functions**：`CREATE OR REPLACE FUNCTION` — 天然冪等，可直接重跑。
- **RLS enable/force**：`ALTER TABLE … ENABLE/FORCE ROW LEVEL SECURITY` — 重跑無害（冪等）。
- **Partial unique index**：`CREATE UNIQUE INDEX IF NOT EXISTS … WHERE` — 冪等，**但建前
  必先 dedup**（已有重複資料會建失敗；現有 migration 如 `recvisuq01`/`activity_regs_unique`
  已示範 pre-flight dedup）。
- **Policies**：無 `OR REPLACE`/`IF NOT EXISTS` → 重跑 `42710 duplicate_object`。必先
  `DROP POLICY IF EXISTS <name> ON <table>` 再 `CREATE`（downgrade 已有此模式可複用）。
- **Triggers**：同上，本專案用 DROP-then-CREATE；必先 `DROP TRIGGER IF EXISTS`。
- **Roles**：`CREATE ROLE` 重跑 `42710`；用 `DO $$ BEGIN … EXCEPTION WHEN duplicate_object
  THEN null; END $$` 包裹（`parlsr001` 已是此模式）。

## 順序相依（重套必照此序）

1. **Roles 先於一切**（policy 的 `TO ivy_parent_role`、GRANT 都依賴 role 存在）。
2. **Functions 先於 Policies/Triggers**：`parent_owns_attachment` 被 policy USING 引用；
   immutable functions 被對應 trigger 引用。
3. **GRANT → ENABLE RLS → FORCE RLS → CREATE POLICY**（與 migration 同序）。
4. Partial unique index 與上述獨立，但**建前先 dedup**。

## 實作方向

- 新模組 `startup/ensure_infra.py`，函式 `ensure_db_infra(session)` 依上述序重套，全程
  idempotent guard。各物件定義從對應 migration 抽出為單一事實來源（policy/trigger 用
  f-string 迴圈，沿用 migration 既有的 table-list 變數）。
- 在 `startup.migrations.run_alembic_upgrade()` 後呼叫；初期**置於 env flag 後**
  （如 `ENSURE_DB_INFRA_ON_STARTUP=1`），預設關閉，僅在 staging 驗證綠後 prod 開啟。
- 執行後由 `infra_check.check_db_infra_present()` 驗證收斂（缺漏應歸零）。

## 驗證需求（為何不在 SQLite 環境直接 ship）

RLS / role / SECURITY DEFINER 為 **PG 專屬**，SQLite 測試環境無法 TDD；且在已手動 patch
的 prod boot path 重套 DDL 風險高。**啟用前須**：
1. staging 起一個 **fresh** PG（`empty → create_all + stamp`）→ 跑 `ensure_db_infra` →
   `check_db_infra_present` 應回 `[]`（收斂成功）。
2. staging 起一個 **既有** PG（已有全部 infra）→ 跑 `ensure_db_infra` → 不報錯、無副作用
   （冪等）→ `check_db_infra_present` 仍 `[]`。
3. 對家長 RLS 跑既有 e2e/RLS 探測（`_probe_parent_rls_ready`）確認隔離仍生效。
4. 確認 dedup pre-flight 對既有重複資料不誤刪。

通過後再把 flag 在 prod 開啟。
