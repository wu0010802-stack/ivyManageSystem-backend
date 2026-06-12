# 資料庫優化（索引／N+1／Schema 三線）設計

- 日期：2026-06-12
- 狀態：已核准（方案 A）
- 範圍：ivy-backend + dev DB（`ivymanagement`），不碰 prod

## 背景與摸底

- dev DB：166 張表、38 MB，最大表 `student_attendances`（35,242 列）
- **14 個 FK 沒有覆蓋索引**（catalog query 實測）
- 全庫 900 個索引／166 表，疑有重複或前綴冗餘
- 已知 backlog：年終 build N+1（2026-06-04 QA P1）

## 目標

1. **索引健檢**：補缺索引的 FK、移除結構上確定冗餘的索引、為熱查詢補複合索引
2. **慢查詢／N+1**：修 ORM N+1（selectinload／批次查詢），回應 schema 不動
3. **Schema 健檢**：型別、約束、ON DELETE 策略全面審查；高風險項只進報告

## 執行架構（方案 A）

1. **三線並行診斷**：3 個 read-only subagent（索引／N+1／Schema），輸出強制含
   file:line + mechanism + 實證證據 + 自評 false-positive 風險
2. **實證驗證**：所有 finding 對真 PG dev DB 用 `EXPLAIN ANALYZE`／catalog query 驗證
   （不靠 grep 推論）
3. **修補**：worktree 開分支；索引一支 Alembic migration（接現有 head、含完整
   downgrade、dev DB 實跑 up/down/up）；N+1 逐項 TDD 修
4. **收尾**：migration-reviewer 審查、pytest 全綠、dev DB `alembic upgrade heads`、
   併 local main（不 push，照既有 push gate）

## 關鍵決策

- **「未使用索引」保守處理**：dev 的 `idx_scan=0` 不代表 prod 不用；只刪結構上
  確定冗餘的（同表同前綴重複），其餘列報告
- **Schema 變更以報告為主**：只有零風險項（純加 CHECK、資料已驗證無 NULL 的
  NOT NULL）才直接修，其餘待業主裁決
- **N+1 修補不改行為**：只改載入策略或批次化，靠既有測試＋新增查詢次數斷言守護
- 改 schema 一律走 Alembic，不透過 postgres MCP 直接 DDL（workspace 規範）

## 產出

- 修補分支：`fix/db-optimization-2026-06-12-be`（併 local main）
- 報告：workspace `.scratch/db-optimize-2026-06-12/REPORT.md`（含待裁決項）
