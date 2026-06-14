# seedgen — 可參數化全年測試資料產生器

把**本機 dev DB**（`postgresql://yilunwu@localhost:5432/ivymanagement`）全清業務資料後，
重灌成一整個學年、涵蓋所有功能面、內部一致（薪資/年終跑真引擎）、決定論的測試資料。

> ⚠️ **僅限本機 dev DB**。`guard.py` 會拒絕非 localhost / production 的 DATABASE_URL。
> 絕不會碰 Zeabur / Supabase prod。

## 快速開始

```bash
cd ~/Desktop/ivy-backend          # 或本 worktree 目錄
# 醫療欄位加密 key：本 worktree 已備 .env（僅 MEDICAL key），自動載入免 export。
# 若從別處跑：export MEDICAL_FIELD_ENCRYPTION_KEY=<同後端 .env 的值>

# 預覽（不寫 DB）：印出將清哪些表
python -m scripts.seedgen

# 實際重灌（破壞性，需 --wipe --yes）
python -m scripts.seedgen --wipe --yes

# 只驗證現有 DB（不灌）
python -m scripts.seedgen --verify
```

## 參數

| 參數 | 預設 | 說明 |
|------|------|------|
| `--year N` | 由 `--today` 推導 | 民國學年（如 114 = 2025-08~2026-07） |
| `--today YYYY-MM-DD` | **真實當天** | 模擬「今天」，決定已結/進行中/未來月份。預設取真實當天，使 app 的「當前學期/今日」視圖與 seed 資料對齊 |
| `--scale` | `standard` | `small`(3班/60生/12員) / `standard`(7班/170生/23員) / `large`(12班/420生/42員) |
| `--rng-seed N` | `20260614` | 決定論種子；同參數重跑產出同一份資料 |
| `--wipe` | off | 清除既有業務資料（需 `--yes` 才真執行） |
| `--yes` | off | 確認破壞性操作 |
| `--only m00,m01` | 全跑 | debug：只跑指定模組（不 wipe，單模組補跑） |
| `--verify` | off | 只驗證現有 DB（summary + 一致性檢查） |

## 設計重點

- **時間定位**：`--today` 預設真實當天 → 前面月份已結算（跑真薪資/年終引擎）、當月進行中
  （pending 假單/加班、薪資未結）、未來留空。**app 用真實時鐘判定「當前學期/今日」**，故
  today 設真實當天才能讓所有「當前/今日」視圖有資料。
- **內部一致**：薪資走 `engine.process_bulk_salary_calculation`、年終走 `build_settlements`
  （與 production 同路徑），數字真實對得起 Excel 對帳。
- **保留**：alembic 系統種子（permission_definitions / roles）；不種 runtime/transient 表
  （jwt_blocklist / rate_limit / *_refresh_tokens / *_cache / scheduler_* 等）。
- **已知測試帳號**（密碼 `ivytest123`，僅 dev）：`admin`(管理員) / `teacher`(班導) / `parent`(家長)。

## 架構

```
scripts/seedgen/
  __main__.py      CLI 入口（參數 → guard → wipe → 依序跑模組 → verify）
  config.py        SeedConfig（參數 + 學年/規模衍生）
  context.py       SeedContext（session + config + RNG + 已建實體 registry）
  guard.py         破壞性安全護欄（拒非 dev DB）
  wipe.py          TRUNCATE 業務+營運設定表（FK-safe）
  fake.py          決定論 faker（台灣姓名/電話/身分證）
  calendar.py      學年月份/學期/工作日推導
  reference_data.py 法定參考（勞健保級距/費率、職位底薪、考核目錄）
  verify.py        灌後 summary + 一致性檢查
  modules/m00..m14 各功能領域 seed(ctx)
```

詳見設計 spec：`docs/superpowers/specs/2026-06-14-test-data-seedgen-design.md`
與實作計畫：`docs/superpowers/plans/2026-06-14-test-data-seedgen.md`。
