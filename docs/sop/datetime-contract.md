# Datetime Asia/Taipei 契約

> 適用於 ivy-backend 全 codebase。spec：`docs/superpowers/specs/2026-05-26-datetime-taipei-consistency-design.md`。
> cutover_date = 2026-05-26。

## 兩條契約

### Naive column = Asia/Taipei naive

所有 `Column(DateTime)`（無 `timezone=True`）儲存的字面值代表 Asia/Taipei naive datetime。

- **寫入**：用 `utils.taipei_time.now_taipei_naive()`
- **讀取**：直接視為 Asia/Taipei naive（不做 tz 轉換）

### Aware column = UTC absolute

所有 `Column(DateTime(timezone=True))` 儲存 UTC absolute time。

- **寫入**：用 `now_taipei_aware()`（PG 自動轉 UTC 存）或 `func.now()`（Phase 0 後 PG 在 Asia/Taipei，存入時 PG 自動轉 UTC）
- **讀取**：PG 自動加 offset 顯示為 Asia/Taipei

## 三個 helper（`utils/taipei_time.py`）

| Helper | 用途 |
|--------|------|
| `now_taipei_naive()` | naive column 寫入（最常用） |
| `now_taipei_aware()` | aware column 寫入（少數 audit/security/appraisal/year_end 表用） |
| `today_taipei()` | 取「今日（Asia/Taipei）」`date` 物件 |

## 禁用清單（CI Ruff DTZ 擋）

| Pattern | Ruff rule | 替代 |
|---------|-----------|------|
| `datetime.now()` (無 tz arg) | DTZ005 | `now_taipei_naive()` |
| `datetime.utcnow()` (Python 3.12+ deprecated) | DTZ003 | `now_taipei_naive()` |
| `date.today()` | DTZ011 | `today_taipei()` |
| `datetime.today()` | DTZ002 | `now_taipei_naive()` |

例外目錄（per-file-ignores）：
- `tests/` — freezegun 等 fixture 用
- `alembic/versions/` — historical migration artifact
- `utils/taipei_time.py` — 唯一合法 `datetime.now(tz)` 入口

不在 PR1-3 scope（ruff config `ignore` 排除，留 follow-up PR）：
- DTZ001（constructor without tzinfo） / DTZ007（strptime without zone） / DTZ901（datetime.min/max）

## Model column default 機制

Ruff DTZ 不分析 callable reference，所以 `default=datetime.now` 不會 trigger。
`tests/test_no_naive_datetime_in_model_defaults.py` 用 SQLAlchemy reflection 補：

- 新增 model column 用 `default=datetime.now/utcnow` 或 `onupdate=datetime.now/utcnow` → test 紅
- 必須改 `default=now_taipei_naive` / `onupdate=now_taipei_naive`
- 不可繞 `MODEL_DEFAULT_ALLOWLIST`（PR3 收尾後 allow-list 應為空 set，由 canary test `test_model_default_allowlist_is_empty` 強制）

## Cutover 影響（2026-05-26）

### Phase 0 之前
- prod 後端與 Supabase PG 雙在 UTC
- 所有 2026-05-26 之前的 naive column 字面值為 UTC naive

### Phase 0 之後（zeabur env TZ=Asia/Taipei + Supabase ALTER DATABASE）
- naive column 新寫入字面值 = Asia/Taipei naive ✓
- 既有歷史字面值「按字面解讀為 Asia/Taipei」 → 顯示比實際發生早 8h
- 字面值**未 backfill**（240 column × N rows 風險太高）
- 未來查詢若需跨 cutover 比對自行加 if/else，cutover_date = "2026-05-26"

## CI gates

| Gate | 機制 | Job 名稱 |
|------|------|---------|
| Ruff DTZ lint | `ruff check .` | `ruff-lint` |
| Pytest matrix run | `TZ=[Asia/Taipei, UTC]` 雙 run | `test` |
| Model default reflection | `tests/test_no_naive_datetime_in_model_defaults.py` | 含於 `test` |

3 個 gate 並行；任一紅即不可 merge。
