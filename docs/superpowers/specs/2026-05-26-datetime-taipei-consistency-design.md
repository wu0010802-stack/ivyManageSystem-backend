# Datetime 全系統 Asia/Taipei 一致性設計

- 日期：2026-05-26
- 領域：DevOps / 資料完整性 / Audit / 跨模組
- 狀態：spec drafted，待 user 二審後進 writing-plans

## 1. 背景與動機

### 1.1 Finding 摘要

全 backend repo `grep` 統計（2026-05-26）：

| 指標 | 數量 | 風險 |
|------|------|------|
| `services/+api/` 內 `datetime.now()` | **145 處** | 受 container tz 影響 |
| `services/+api/` 內 `datetime.utcnow()` | **8 處** | Python 3.12+ deprecated + tz contract 混亂 |
| `models/` 內 `default=datetime.now`（Python callable） | **185 處** | ORM 層 Python 進程執行，受 container tz 影響 |
| `models/+alembic/` 內 `server_default=func.now()/CURRENT_TIMESTAMP` | **135 處** | DB 層執行，受 PG `timezone` setting 影響 |
| `models/` 內 naive `Column(DateTime)` | **~200 處** | 儲存「字面值無 tz info」，semantics 完全依賴契約 |
| `models/` 內 `DateTime(timezone=True)` | **45 處** | 儲存 UTC absolute，相對安全 |
| 已 import `utils/taipei_time` 的檔案 | 30 個 | 既有覆蓋率約 38% (30 / ~79 datetime-using files) |

### 1.2 Prod 部署實況（2026-05-26 驗證）

| 證據 | 結論 |
|------|------|
| Zeabur backend service env 無 `TZ` 變數 | Linux container 預設 UTC |
| 無 Dockerfile（buildpack 部署） | image 層無從設 TZ |
| codebase grep 無 `os.environ['TZ']` / `tzset()` | startup 也沒覆寫 |
| DATABASE_URL 連 Supabase（managed PG，預設 timezone=UTC） | server_default NOW() 也回 UTC |

**結論：prod 後端 Python 進程在 UTC tz，PG 也在 UTC**。`datetime.now()` 在 prod 回的是 UTC time，naive 寫入 ~200 個 column 就是 UTC naive 字面值。

業務面「沒人抱怨」是因為（a）所有寫入和讀取都用同樣壞掉的 now()，in-app 邏輯自相容；（b）user 透過 frontend 看時間時 browser tz 補正掉表面差異；（c）跨日邊界 case 不常觸發。

### 1.3 已知壞掉的場景

- 薪資 `RECALL_WINDOW` 月初 / 月末邊界判斷
- 跨日 cutoff（如 unlock 過期、token 過期、簽核截止）
- `<5min` race 偵測（如 `api/portfolio/reports.py:651` 重送防護）
- 政府申報歸月（含 `EXTRACT(month from ...)`、`EXTRACT(day from ...)`)
- 跟 LINE webhook / 外部 API 等外部 timestamp 對帳

### 1.4 為什麼是 P0

今天沒爆是 silent corruption，任一次容器重建 / 換 region / 換平台 也不會更糟，但**也無法回頭** — 歷史已寫的 ~200 column × N rows 是 UTC naive 字面值。**愈晚修，cutover 切點愈靠後、歷史與新資料時序錯位範圍愈大**。

## 2. 範圍

### 2.1 包含

- **Phase 0（止血，無 PR）**：
  - Zeabur ivy-backend env 加 `TZ=Asia/Taipei` 並重啟容器
  - Supabase prod DB `ALTER DATABASE postgres SET timezone TO 'Asia/Taipei'`
- **Phase 1（PR1：lint gate + helper + CI matrix + 開發手冊）**：
  - `utils/taipei_time.py` 補 `now_taipei_aware()` 函式
  - `pyproject.toml`（或 `ruff.toml`）加 `select = ["DTZ"]` + per-file-ignores
  - 存量 153 處（145 `datetime.now()` + 8 `datetime.utcnow()`）用 `# noqa: DTZ005` / `# noqa: DTZ003` inline 暫留
  - **Model default reflection check**：新增 `tests/test_no_naive_datetime_in_model_defaults.py` 反射檢查所有 model column 的 `default` callable identity，禁用 `datetime.now` / `datetime.utcnow`。建立 `MODEL_DEFAULT_ALLOWLIST = {...}` 含當下 185 處 `(ModelName, column_name)` 名單，PR3 完工時 allow-list 應為空。**Why this**：Ruff DTZ 只分析 call expression（`datetime.now()`），抓不到 `default=datetime.now` 這種 callable reference，需 pytest reflection 補足
  - `.github/workflows/ci.yml` pytest job 改 matrix `[Asia/Taipei, UTC]`
  - `docs/sop/datetime-contract.md` 寫死契約
  - `tests/test_datetime_contract.py` 4 條 regression test
- **Phase 2（PR2：runtime 替換 145+8 處）**：
  - services/ + api/ 內 145 處 `datetime.now()` → `now_taipei_naive()`
  - 8 處 `datetime.utcnow()` → `now_taipei_naive()`
  - 移除對應 `noqa: DTZ005` / `noqa: DTZ003` 標記
  - `tests/test_runtime_datetime_replacement.py` 5 條核心 caller TZ=UTC 行為斷言
- **Phase 3（PR3：model default 替換 185 處）**：
  - models/ 內 185 處 `default=datetime.now` → `default=now_taipei_naive`
  - 同步清空 `MODEL_DEFAULT_ALLOWLIST`（PR3 結束時應為空 set）
  - `tests/test_model_default_datetime.py` ~15 條代表性 model 測試
  - **無 alembic schema migration**（default 是 ORM 層 Python 行為）

### 2.2 不包含（YAGNI 排除）

- ❌ **歷史資料 backfill**：~200 column × N rows 是 UTC naive 字面值，**不補**。記載 cutover_date = 2026-05-26，未來查詢若需跨 cutover 比對自行加 if/else。
- ❌ **240 個 naive column 轉 `DateTime(timezone=True)`**：schema migration risk 太大，user 已選不做。
- ❌ **既有 30 個已用 `taipei_time` 的檔案重構**：本就是健康樣本。
- ❌ **第三方 lib 內部 `datetime.now()` 行為**：scope 之外（如 `itsdangerous` token expiry）。
- ❌ **`freezegun` / `time-machine` test infra 改善**：本 spec 只解 prod 漂移問題。
- ❌ **既有 ~5000 test 全面審計**：TZ=UTC matrix 開後個案 fix 紅的 test。
- ❌ **Phase 2/3 中前端任何變動**：純後端 spec。

## 3. 設計決策

### 3.1 Naive column 契約

**所有 `Column(DateTime)`（無 `timezone=True`）儲存的字面值代表 Asia/Taipei naive datetime**。寫入用 `utils.taipei_time.now_taipei_naive()`，讀取直接視為 Asia/Taipei naive（不做 tz 轉換）。

### 3.2 Aware column 契約

**所有 `Column(DateTime(timezone=True))` 儲存 UTC absolute time**。寫入用 `now_taipei_aware()`（PG 自動轉 UTC 存）或 `func.now()`（Phase 0 後 PG 在 Asia/Taipei，存入時 PG 自動轉 UTC）。讀取時 PG 自動加 offset 顯示為 Asia/Taipei。

### 3.3 不轉 column type

User 已選不做 `naive → timezone=True` 的 schema migration。理由：
- ~200 column × N rows × N table 的 schema migration 是 deployment risk maxout
- naive + Asia/Taipei 契約足夠表達業務需求（業務只在台灣運作）
- Phase 0 ALTER DATABASE 已解 server_default 對 PG-side 的依賴

### 3.4 不 backfill 歷史資料

User 已選 cutover only：
- Phase 0 後 ~200 個 naive column 內**字面值不變**（仍是 UTC naive 字面值如 `'2026-05-26 09:00:00'`）
- 但被新契約解讀為「Asia/Taipei naive」 → 視覺上歷史時間「比實際發生早 8h」
- 文件記載 `cutover_date = "2026-05-26"`，未來業務查詢若需跨 cutover 比對自行加 if/else
- backfill 240 column × N rows 的 risk + reward 不成比例

### 3.5 lint 工具：Ruff DTZ rule set + pytest reflection check

選 `flake8-datetimez` (Ruff DTZ001-DTZ012) 而非自寫 plugin / pre-commit grep：
- 成熟、零維護、AST-aware（不會誤判註釋 / docstring）
- per-file ignore 機制可彈性處理 tests/ alembic/ utils/taipei_time.py 例外
- 存量 noqa inline 可逐 PR 清

主要 rule：
- **DTZ003**：`datetime.utcnow()`（8 處 → PR2 清）
- **DTZ005**：`datetime.now()` without tz（145 處 → PR2 清）

**Ruff DTZ 不抓的盲點**：`default=datetime.now` 是 callable reference（不是 call expression），Ruff DTZ 只分析 call。Phase 3 的 185 處 model default 需另一個檢查機制：

**Pytest reflection check**（PR1 新增 `tests/test_no_naive_datetime_in_model_defaults.py`）：
- import 所有 model 觸發 SQLAlchemy registry
- 走訪每個 `inspect(model_cls).columns`，看 `column.default.arg` 是否為 `datetime.now` 或 `datetime.utcnow`
- 命中且不在 `MODEL_DEFAULT_ALLOWLIST` → 測試失敗
- PR1 初始 allow-list 含當下 185 處 `(ModelName, column_name)`；PR3 逐處替換時同步移除條目；PR3 結束 allow-list 應為空 set

### 3.6 PR 切分：3 PR 序列

| PR | 內容 | blast radius | rollback |
|----|------|--------------|----------|
| PR1 | lint + helper + CI matrix + docs | 低（僅加 config + 新檔） | revert PR |
| PR2 | runtime 153 處 | 中（散布全 codebase） | revert PR |
| PR3 | model default 185 處 | 中（model 層集中） | revert PR |

每 PR 獨立 merge 可獨立部署，序列順序固定不可調換（PR1 → PR2 → PR3）。

### 3.7 Test 策略：CI TZ=UTC matrix run

```yaml
strategy:
  fail-fast: false
  matrix:
    container_tz: [Asia/Taipei, UTC]
env:
  TZ: ${{ matrix.container_tz }}
```

雙 tz 都過才綠。`fail-fast: false` 避免單邊紅遮蔽另一邊。

## 4. 架構與元件

### 4.1 `utils/taipei_time.py` 微擴

```python
def now_taipei_aware() -> datetime:
    """帶 ZoneInfo 的當下時間，給 timezone-aware column 用。

    Why: 既有 45 個 DateTime(timezone=True) column (audit/security/appraisal/year_end)
    存的是 UTC absolute time，寫入用 datetime.now(TAIPEI_TZ) 才能正確 round-trip。
    與 now_taipei_naive() 並列為兩個明確契約入口。
    """
    return datetime.now(TAIPEI_TZ)
```

不動 `now_taipei_naive()` / `today_taipei()` / `validate_payment_date()`。

### 4.2 `pyproject.toml`（或 `ruff.toml`）新增 lint config

```toml
[tool.ruff.lint]
select = ["DTZ"]  # flake8-datetimez

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["DTZ"]              # freezegun 用例外
"alembic/versions/**/*.py" = ["DTZ"]   # historical migration artifact
"utils/taipei_time.py" = ["DTZ"]       # 唯一合法 datetime.now(tz) 入口
```

### 4.3 `.github/workflows/ci.yml` matrix 擴展

把既有 pytest job 改 matrix：`[Asia/Taipei, UTC]`，`fail-fast: false`。新增 ruff 步驟（如尚未配置）。

### 4.4 `docs/sop/datetime-contract.md` 新檔

內容綱要：
- 兩條契約：naive col = Asia/Taipei naive；aware col = UTC absolute
- 三個 helper：`now_taipei_naive()` / `now_taipei_aware()` / `today_taipei()`
- 禁用清單：`datetime.now()` (DTZ005) / `datetime.utcnow()` (DTZ003)（除 utils/taipei_time 內部）
- CI ruff DTZ gate 說明 + per-file-ignores 例外（tests/ alembic/ utils/taipei_time.py）
- Model default reflection check 機制 + allow-list 維護規則（新增 model column 用 `default=datetime.now` → test 紅，必須改 `default=now_taipei_naive` 或顯式加入 allow-list 並附 reason）
- cutover_date = 2026-05-26 + 歷史顯示偏移說明

### 4.5 PR1 新增 `tests/test_no_naive_datetime_in_model_defaults.py`

```python
"""Phase 3 lint coverage：Ruff DTZ 抓不到 model `default=datetime.now`
(callable reference 非 call expression)，用 reflection 補。"""

from datetime import datetime
from sqlalchemy import inspect
from models.database import Base  # 觸發所有 model import

# PR1 初始：含當下 185 處 (ModelName, column_name) 名單
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = {
    ("User", "created_at"),
    ("User", "updated_at"),
    # ... 185 條，PR3 逐處替換時移除，最終應為空
}

FORBIDDEN = {datetime.now, datetime.utcnow}


def test_no_naive_datetime_in_model_defaults():
    violations = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for col in inspect(cls).columns:
            default = col.default
            if default is None or not hasattr(default, "arg"):
                continue
            if default.arg in FORBIDDEN:
                key = (cls.__name__, col.name)
                if key not in MODEL_DEFAULT_ALLOWLIST:
                    violations.append(f"{cls.__name__}.{col.name}")
    assert not violations, (
        "Model column default 用了 datetime.now / utcnow，"
        f"請改用 utils.taipei_time.now_taipei_naive():\n" + "\n".join(violations)
    )
```

### 4.6 PR2 / PR3 純機械替換

無新組件。PR2 逐處改 import + call site，伴隨移除對應 inline `noqa: DTZ003 / DTZ005`。PR3 逐處改 model default，同步從 `MODEL_DEFAULT_ALLOWLIST` 移除對應條目。

## 5. 資料流（hot fix cutover 行為）

### 5.1 Phase 0 切換前後對照

```
切換前（prod 今日狀態）
─────────────────────────────────────────────────
Python  datetime.now()                  → '2026-05-26 09:00:00' (UTC naive)
PG      NOW() / CURRENT_TIMESTAMP       → '2026-05-26 09:00:00+00'
PG col(naive)  寫入 datetime.now()       → '2026-05-26 09:00:00'  ← UTC 字面值塞 naive
PG col(aware)  寫入 datetime.now()       → '2026-05-26 09:00:00+00'  ← 正確 UTC absolute

切換後（Phase 0 完成）
─────────────────────────────────────────────────
Python  datetime.now()                  → '2026-05-26 17:00:00' (Asia/Taipei naive)
PG      NOW() / CURRENT_TIMESTAMP       → '2026-05-26 17:00:00+08'
PG col(naive)  寫入 datetime.now()       → '2026-05-26 17:00:00'  ← Asia/Taipei 字面值塞 naive ✓
PG col(aware)  寫入 datetime.now()       → '2026-05-26 17:00:00+08' ← 仍正確 UTC absolute
```

### 5.2 既有歷史資料解讀漂移

```
2026-05-26 之前的所有 naive column 既存值：
  DB 字面值：'2026-05-26 09:00:00' （實際是當天台灣時間 17:00 寫入）

  切換前 user 看到：'09:00'  ← 已經錯 8h，但 user 已習慣
  切換後 user 看到：'09:00'  ← 字面值不變，「歷史顯示不變」

  跨界對比（如「過去 7 日」查詢）：
    NOW() 變 17:00 + 7 日 = 17:00 對既存 09:00 字面值仍能比對
    但「同一個事件相對時序」會被誤判為差 8h
```

**結論**：歷史顯示「值不變」，但歷史 vs 新資料時序對比會出現「歷史看起來早 8h」的視覺錯位。**已知接受**，spec 記載 cutover_date 供未來查詢加 if/else 標註。

### 5.3 timezone-aware column 不受影響

`DateTime(timezone=True)` 儲存 UTC absolute time，ALTER DATABASE timezone 不影響其儲存值，取出時 PG 自動加 offset 顯示 — 切換前後一致正確。

### 5.4 PR2/PR3 進行中部分 caller 已改、部分未改的中間態

```
PR2 進行中：runtime 145 處替換到一半（假設 70 處已改）
─────────────────────────────────────────────────
已改：services/parent_message_service.py:164  now_taipei_naive() → '2026-05-26 17:00:00'
未改：services/contact_book_service.py:90      datetime.now()     → '2026-05-26 17:00:00'
                                                                    (Phase 0 後也是 Asia/Taipei naive)

→ 中間態行為一致。Phase 0 已把 datetime.now() 矯正為 Asia/Taipei naive，
  PR2/PR3 是把「靠 env 變數矯正」改成「靠 helper 顯式表達」— 行為不變、契約更清楚。
```

PR2/PR3 中途部署不會有時序錯亂。

## 6. 錯誤處理與風險控制

### 6.1 Phase 0 操作 risk + rollback

| 操作 | Risk | Rollback |
|------|------|----------|
| zeabur env 加 `TZ=Asia/Taipei` 重啟 | 容器 restart ~30s 服務中斷；in-flight request 中斷 | 移除 env var 重啟還原 |
| `ALTER DATABASE postgres SET timezone TO 'Asia/Taipei'` | **新 connection 才生效**，既有 connection pool 仍是舊 tz；可能需 `pg_terminate_backend()` 或等 pool refresh | `ALTER DATABASE postgres SET timezone TO 'UTC'` 還原 |

**切換順序：先改 Supabase DB → 等 5 分鐘確認 NOW() 回 Asia/Taipei → 再改 zeabur env 重啟**。反過來會出現「Python Asia/Taipei + PG UTC」的不一致期，比現狀更糟。

### 6.2 Phase 0 驗證 checklist

切換完後 Supabase SQL editor 跑：
```sql
SELECT current_setting('TIMEZONE');                    -- 應回 'Asia/Taipei'
SELECT NOW(), NOW() AT TIME ZONE 'UTC';                -- 應差 8h
INSERT INTO ... (...);                                 -- 觸發任一 server_default=NOW()
SELECT created_at FROM ... ORDER BY id DESC LIMIT 1;   -- 應為當下 Asia/Taipei 字面值
```

backend `/health/ready` 200 + log 抽樣最近 1 hr 寫入的 `created_at` 應落在合理 Asia/Taipei 時段。

### 6.3 CI failure mode

- **TZ=UTC matrix 紅**：實作有「依賴 container Asia/Taipei」隱性假設。**這是好事** — 揭露真實 bug，個案 fix。
- **Ruff DTZ 紅**：新 code 用了 `datetime.now()` (DTZ005) 或 `datetime.utcnow()` (DTZ003)。修法：用 `now_taipei_naive()` 或 `now_taipei_aware()`。
- **`test_no_naive_datetime_in_model_defaults` 紅**：新 model 加了 `default=datetime.now/utcnow` 而未進 allow-list（或 PR3 完工後 allow-list 應已空）。修法：改用 `default=now_taipei_naive`。
- **PR2/PR3 進行中既有 noqa / allow-list 殘留**：不算紅，PR3 merge 後 ruff 全綠 + allow-list 空集合才算 cutover 完成。

### 6.4 Edge case 處理

| Case | 處理 |
|------|------|
| `datetime.utcnow()` 在 Python 3.12+ deprecated | 8 處全替換為 `now_taipei_naive()`（naive column 寫入語意） |
| 第三方 lib 內部用 `datetime.now()` | 不在 scope |
| Pydantic `default_factory=datetime.now` | PR3 開頭 grep `default_factory.*datetime\.now` 確認是否有命中 |
| 既有 `datetime.now(TAIPEI_TZ).replace(tzinfo=None)` 散落寫法（30 file 已用 taipei_time） | 不替換，視為健康樣本 |
| `freezegun` test fixture | tests/ 整目錄 DTZ 例外 |
| Alembic data migration 內 timestamp | alembic/versions/ DTZ 例外 |

### 6.5 Phase 0 失敗 fallback

若 Supabase 不允許 `ALTER DATABASE`（managed PG 可能限制）：

- **Fallback A（推薦，侵入性小）**：connection pool `init_command` 對每個新 conn 跑 `SET TIME ZONE 'Asia/Taipei'`。SQLAlchemy 用 `connect_args` 或 event listener `engine.event.listen(engine, 'connect', set_timezone)`。
- **Fallback B**：放棄 server_default ALTER，PR3 內把 135 處 server_default 改寫為 `server_default=text("timezone('Asia/Taipei', now())")` — 需 alembic alter migration 135 column。

User 操作 Supabase 失敗時走 Fallback A。

## 7. 測試策略

### 7.1 CI matrix 結構

```yaml
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        container_tz: [Asia/Taipei, UTC]
    env:
      TZ: ${{ matrix.container_tz }}
    steps:
      - run: pytest -q --tb=short
```

### 7.2 PR1 必須加的 regression test

新檔 `tests/test_datetime_contract.py`：

| Test | 目的 |
|------|------|
| `test_now_taipei_naive_no_tzinfo()` | helper 回值必須是 naive |
| `test_now_taipei_naive_matches_taipei_wall_clock()` | 回值與 `datetime.now(TAIPEI_TZ).replace(tzinfo=None)` 差距 < 1s |
| `test_now_taipei_aware_has_tzinfo()` | aware helper 回值 tzinfo == TAIPEI_TZ |
| `test_ruff_dtz_config_loaded()` | 載 ruff config 斷言 DTZ 在 select 內 + 三個 per-file-ignores 存在 |

加上 §4.5 的 `test_no_naive_datetime_in_model_defaults`（獨立檔案，因含 185 條 allow-list）。5 個 test 在 `TZ=UTC` 跑也必須全綠 — 證明 helper / lint config / reflection check 本身 TZ-agnostic。

### 7.3 PR2 必須加的 regression test

新檔 `tests/test_runtime_datetime_replacement.py`：

| Test | 目的 |
|------|------|
| 任選 5 個改過的 caller（`salary_job_registry.started_at` / `parent_message_service.last_message_at` / `portfolio/reports.line_sent_at` / `contact_book_service.created_at` / `recruitment_market_intelligence.*`），mock `os.environ['TZ']='UTC'` + `time.tzset()`，斷言寫入值的 hour 落在 Asia/Taipei ±5 min | 證明替換後行為已脫離 container tz |
| 反測（canary，skip 標記，手動 unskip 驗）：故意 patch `now_taipei_naive` 為 `datetime.now`，同 5 個 test 在 TZ=UTC 必須失敗 | 證明 test 有 discriminating power |

### 7.4 PR3 必須加的 regression test

新檔 `tests/test_model_default_datetime.py`：

| Test | 目的 |
|------|------|
| 對 ~10 個改過 `default=now_taipei_naive` 的 model（fees / overtime / recruitment / config / auth / parent_binding 等代表性），TZ=UTC mock 環境下 `session.add(MyModel())` + flush + `session.refresh()` 後 `created_at` 落在 Asia/Taipei ±5 min | 證明 model default 不依賴 container tz |
| 對 ~5 個 `server_default=func.now()` model 同樣測試 | 證明 Phase 0 ALTER DATABASE 對 PR3 是「行為已矯正」（test 用 local PG，需 local PG conftest fixture init 設 `SET TIME ZONE 'Asia/Taipei'`） |
| Allow-list 必須為空：assert `MODEL_DEFAULT_ALLOWLIST == set()` | 證明 PR3 收尾把所有 185 條都清掉了 |

### 7.5 既有 test 不動

不回頭改既有 ~5000 test 的 datetime 用法。加 TZ=UTC matrix 後若有個別 test 紅，**先 fix 該 test 不 fix 整批** — 那個 test 紅就是揭露真實 bug，個案處理。

### 7.6 Manual smoke after Phase 0

切換完 Supabase + zeabur 後 user 手動驗：
1. Web admin 隨便建一筆假單，「申請時間」顯示 = 當下台灣牆鐘時間
2. 跑薪資 `/calculate-async` 一筆，`started_at` = 當下台灣牆鐘時間
3. `/health/ready` 回 200

## 8. 部署順序與 cutover

```
T0 = 2026-05-26（cutover_date）
─────────────────────────────────────────
T0      Phase 0 操作（USER manual ops，無 PR）：
        Step 1  Supabase SQL editor 跑 ALTER DATABASE postgres SET timezone TO 'Asia/Taipei'
        Step 2  等 5 分鐘，跑 SELECT NOW() 確認回 Asia/Taipei
        Step 3  Zeabur backend service 加 env TZ=Asia/Taipei
        Step 4  Zeabur 重啟 backend container（會自動因 env 變更觸發）
        Step 5  跑 §6.2 驗證 checklist

T0+1d   PR1 merge：lint + helper + CI matrix + docs
T0+3d   PR2 merge：runtime 153 處
T0+5d   PR3 merge：model default 185 處
        → cutover_completion_date = PR3 merge 日
        → ruff DTZ 全綠（無 noqa 殘留 in services/api/models）
```

## 9. 開放問題

- **PG 對 `ALTER DATABASE postgres SET timezone TO 'Asia/Taipei'` 是否允許**：Supabase managed PG 可能有限制。Phase 0 操作前 user 須在 Supabase SQL editor 試跑，失敗則走 §6.5 Fallback A。
- **Pydantic `default_factory=datetime.now`**：PR3 開頭 grep 確認是否存在，若有納入 PR3 scope。
- **Phase 2/3 PR 之間是否需要 manual smoke**：PR1 merge 後立即 PR2 開工是否風險可接受？建議 PR1 觀察 24h 確認 lint gate 沒誤殺其他 PR 後再開 PR2。
