# 薪資設定單一事實來源 + 啟動完整性檢查 + 殘留查詢路徑收斂 設計

> 日期：2026-06-25
> 範圍：ivy-backend（薪資設定 defaults / seed / model / 啟動檢查 / engine 殘留查詢路徑）
> 起因：使用者詢問「底薪、獎金比例等參數寫在原始碼還是 DB」，調查後發現現況為「DB 權威 + 原始碼 fallback」混合模式，但存在三項設計債：同一數值三處手抄、保險級距兩份手抄、無啟動時設定完整性驗證。
> 前置基礎：**本設計建立在 [`2026-06-05-period-aware-salary-config-resolver-design.md`](./2026-06-05-period-aware-salary-config-resolver-design.md) 已落地的成果之上**，補齊其三個尚未實作的衍生目標，不重做 resolver 本體。

---

## 0. 與 2026-06-05 resolver spec 的關係（先讀，避免重做）

2026-06-05 period-aware resolver spec 的**核心已落地**（2026-06-25 核實 main HEAD `d12cc50f`）：

| 已落地 ✅ | 證據 |
|---|---|
| `config_resolver.py`（`resolve_config` / `resolve_brackets` / `PayrollConfigMissingError`） | `services/salary/config_resolver.py:15-79`，fail-loud `raise` 已就位 |
| 實算路徑走 resolver | `_calculate_and_build_breakdown`（`engine.py:3135`）→ `config_for_month` → `_apply_configs_for_month`（`:598`）→ `_select_active_at`（`:508`）→ `resolve_config`；BonusConfig/InsuranceRate/AttendancePolicy/PositionSalaryConfig 皆涵蓋 |
| 獎金系數 fail-loud | BonusConfig 已在實算 resolver 路徑內，缺當年度即 raise |
| PositionSalaryConfig 加 `config_year` | `models/config.py:341` + `alembic/versions/cfgyear01_add_config_year.py` |
| **fail-loud 邊界 = 「表空 fallback、表有料缺當年度 raise」** | `_select_active_at`（`engine.py:522-524` 表空回 None 用內建常數）/ `load_brackets_from_db`（`insurance_service.py:871-879` 表空 fallback、`:856-863` 缺年度 raise） |

**重要**：現行 fail-loud 邊界恰好就是本輪決策要的「表空 fallback、表有料獨缺當年度 raise」。原 spec 文字寫「純 fail-loud」，但實作時刻意改成此混合邊界以保全新部署/測試的零數字漂移。**本設計沿用此既有邊界，不更動它。**

尚未落地、即為本設計範圍：

| 未落地 ❌ | 現況 |
|---|---|
| 消除「三處重複」（constants / seed / model default 抽共用） | 無共用模組；`startup/seed.py:129-154` 硬寫數字；三處各自獨立；無一致性測試 |
| 保險級距兩份手抄一致性守衛 | `INSURANCE_TABLE_2026`（`insurance_service.py:102`）vs migration `_BRACKETS_2026` 仍兩份手抄；無比對測試 |
| 啟動驗證「薪資 config 表當年度齊全」 | `infra_check.py` 只查 DB infra；`check_insurance_brackets_seeded`（`insurance_service.py:23`）只查級距整表非空、不查當年度、不涵蓋其他 config 表 |
| 殘留旧查詢路徑收斂 | `_load_config_from_db_locked`（`engine.py:747`）啟動/全域 baseline 載入仍用 `is_active + id.desc()` 忽略年度 |

---

## 1. 目標與非目標

**目標**
- **單一事實來源**：每個設定預設數值只定義一次，其餘處引用；手抄無法物理消除者（migration 快照）以一致性測試守衛防漂移。
- **啟動即暴露配置遺漏**：啟動時驗證各薪資 config 表「當前年度」列齊全，缺漏 → loud error log + Sentry + `/health`，但**絕不阻擋啟動**（避免 boot loop）。
- **金流無路徑繞過 fail-loud**：查證殘留 `_load_config_from_db_locked` 是否影響金流，若影響則收斂走 resolver，若純 baseline 則加註說明其安全性。

**非目標（YAGNI）**
- 不做給非工程師改設定的後台 UI（既有 `bonus_configs` 等表的編輯入口不在本設計範圍）。
- 不更動現行 fail-loud 邊界（沿用「表空 fallback、缺年度 raise」）。
- 不重做 `config_resolver` 本體（已落地）。
- 不做 config 寫入端 `finance_approve + audit` 守衛（屬 2026-06-05 spec 的 follow-up 範圍 C，另立）。
- 不碰純法規常數（加班倍率 1.34/1.67、基本工資 29500/196、補充保費門檻 ×4）——本就只在 `constants.py`、無 DB、無 fallback 問題，僅在階段一順手補「法源註解 + 既有測試覆蓋確認」，不改值不改行為。

---

## 2. 階段一：單一事實來源（主菜，純內部，僅一處 fallback 值修正）

### 2.0 已知漂移與業主裁定（2026-06-25 寫計畫時查出）

「三處手抄」已實際漂移：節慶獎金「目標人數」的 `constants.TARGET_ENROLLMENT`（fallback）
與 DB `GradeTarget.festival_*`（prod seed）不一致。兩者是同一數值的 fallback/DB 兩存
（`engine.py:382` 用 constants 初始化 `_target_enrollment`；`engine.py:655-657`/`828-830`
DB 有 GradeTarget 時覆蓋）。

| 年級 | 項目 | `constants` fallback（舊） | DB seed（prod 在用，**正確**） |
|---|---|---|---|
| 大班 | 2 教師 | 24 | **27** |
| 大班 | 1 教師 | 12 | **14** |
| 中班 | 2 教師 | 24 | **25** |
| 中班 | 1 教師 | 12 | **13** |
| 小班 | 2 教師 | 24 | **23** |

（小班 1 教師、所有 `shared_assistant`、幼幼班、以及全部 `OVERTIME_TARGET` 兩邊皆一致。）

**業主裁定（2026-06-25）**：DB 當前值（27/25/23/14/13）為正確值。`constants.TARGET_ENROLLMENT`
的節慶目標係過時 fallback，須更新對齊 DB。**prod 資料不動**（已正確）；本次只改原始碼 fallback。

**對「零行為變更」的影響**：此修正**只影響 fallback 路徑**（dev/test/fresh，無 DB GradeTarget 時）。
prod 走 DB，數字不變。但若有任何薪資 gold 測試係在「無 GradeTarget seed」下跑（靠 fallback），
其節慶獎金期望值會從舊目標(24…)位移到新目標(27…)，需重鎖 gold——實作時須先判定各 gold
測試是否 seed GradeTarget，再決定是否重鎖（見 §7）。

### 2.1 抽共用 defaults 模組（消除可物理合併的三處重複）

新增低層模組 `services/salary/config_defaults.py`，集中定義「設定預設數值」（獎金基數、主管紅利、節慶獎金、超額獎金 per-person、目標人數、費率預設等）。

**依賴方向（避免循環 import）**：
- `config_defaults.py` **不得 import** `models/` 或 `services/salary/engine.py`、`constants.py`（保持為最低層、無業務依賴的純資料模組）。
- `services/salary/constants.py` → 改為 `from .config_defaults import (...)`（或於 constants 內 re-export，保持既有 `from services.salary.constants import X` 呼叫端不破壞）。
- `startup/seed.py:seed_default_configs` → 改 `from services.salary.config_defaults import (...)`，不再硬寫數字字面值。
- `models/config.py` BonusConfig 欄位 `default=` → 改引用 `config_defaults`（model import config_defaults 是單向安全的，因 config_defaults 不 import model）。

**風險點**：`models/config.py` 的 `default=` 若改為引用常數，須確認 SQLAlchemy column default 接受該形式（純量字面值直接帶入即可；若需延遲求值用 `default=lambda: config_defaults.X`）。實作時以「值完全不變」為準繩。

### 2.2 一致性測試守衛（無法物理合併者）

**保險級距**：`INSURANCE_TABLE_2026`（`constants`/`insurance_service`）與 migration `_BRACKETS_2026`（`alembic/.../d9e0f1g2h3i4...py`）**不可物理合併**——migration 是歷史快照，不應 import 會變動的源碼常數（否則改常數會改變「重跑舊 migration」的結果，違反 Alembic 哲學）。

→ 新增測試 `tests/test_config_single_source.py`：
- 斷言 migration `_BRACKETS_2026` 與 runtime `INSURANCE_TABLE_2026` **逐筆相同**（amount/labor/health/pension 全欄）。動態 import 該 migration 模組取其 `_BRACKETS_2026`（沿用 `2026-06-24 prod 缺口` 既有動態 import migration 常數的手法）。
- 斷言 `seed_default_configs` 寫入 DB 的值 == `config_defaults` == BonusConfig model `default=`（三處一致）。2.1 抽共用後此測試會恆綠（同一來源），但保留作為「未來有人改回硬寫」的回歸護欄。

### 2.3 TDD 策略（重構安全網）

去重屬重構，非 bug fix，故用「測試當安全網、重構後保持綠」而非典型 RED→GREEN：
1. 先寫 2.2 一致性測試（當前值恰一致 → 初始即綠）。
2. 加一條「單一來源生效」證明測試：改 `config_defaults` 某值（test 內 monkeypatch 或獨立驗證 import 同一性 `constants.X is config_defaults.X`），證明 seed/model default 隨之變動。
3. 執行 2.1 重構，全程保持上述測試綠 + 既有薪資 gold 測試零位移。

---

## 3. 階段二：殘留旧查詢路徑收斂（正確性查證）

`_load_config_from_db_locked`（`engine.py:747`，由 `load_config_from_db`、`startup/bootstrap.py:153`、`main.py:193` 觸發）對 InsuranceRate/AttendancePolicy/BonusConfig 仍用 `filter(is_active==True).order_by(id.desc())`，**忽略年度欄位**，與 resolver 語意不一致。

**實作前第一步任務（查證）**：釐清這條 baseline 載入的值是否會進入任何實際金流計算結果：
- 若實算一律走 `config_for_month`（per-month resolver）覆蓋 baseline → 這條僅為 engine 初始化 placeholder，**不影響金流**：加註解說明其安全性 + 標 follow-up，**不強制重寫**（YAGNI）。
- 若有任何金流路徑（如某 simulate / 無 month context 的計算）直接讀 baseline 值 → **必須收斂**：改走 `resolve_config(..., 當前年度, year_col=...)`，對齊實算路徑與 fail-loud 邊界。

**決策準繩**：本階段的存在意義是「確認金流沒有繞過 fail-loud 的後門」。查證若證明無後門，本階段即縮為「一行註解 + follow-up 標記」；查證若發現後門，才升級為收斂工作。不預先假設工作量。

---

## 4. 階段三：啟動薪資 config 完整性檢查（新增，loud 不擋 boot）

### 4.1 行為

新增 `check_salary_configs_current_year(session) -> list[str]`（放 `startup/infra_check.py` 或 `services/salary/` 下，與既有風格一致），啟動時檢查各薪資 config 表「當前年度」列是否齊全：

| 表 | 年度欄位 |
|---|---|
| `BonusConfig` | `config_year` |
| `InsuranceRate` | `rate_year` |
| `InsuranceBracket` | `effective_year` |
| `PositionSalaryConfig` | `config_year` |
| `AttendancePolicy` | `config_year` |

**邊界（與實算 fail-loud 對齊）**：
- 某表**完全空** → **不報**（dev/test/fresh 部署靠內建常數，by design）。
- 某表**有料卻獨缺當前年度** → **列入缺漏清單**（prod 配置遺漏的指紋）。

**輸出**：缺漏清單非空 → loud error log + Sentry（沿用 `infra_check.py:173-184` 既有模式）+ 暴露到 `/health`。**絕不 raise、絕不擋 boot**（避開 boot loop）。

### 4.2 與既有檢查的關係

- 取代 / 擴充 `check_insurance_brackets_seeded`（`insurance_service.py:23`，現僅查級距整表非空）：新檢查涵蓋全部 5 表且查「當年度」而非「整表非空」。`main.py:247-252` 既有呼叫點改呼叫新函式（或在其後追加）。
- 與 `infra_check.check_db_infra_present`（DB role/RLS/trigger/index）平行並存，各司其職；可共用 `/health` 的 degraded 聚合與 Sentry 推送通道。

### 4.3 TDD（SQLite 可測）

`tests/test_salary_config_startup_check.py`：
- 全表皆有當前年度列 → 回 `[]`。
- 某表整表空 → 不列入缺漏（回 `[]` 或不含該表）。
- 某表有料但缺當前年度 → 該表名出現在缺漏清單。
- 非 PG/連線失敗 → 安全回 `[]`（不擋 boot），比照 infra_check。

---

## 5. 元件邊界小結

| 元件 | 做什麼 | 怎麼用 | 依賴 |
|---|---|---|---|
| `config_defaults.py` | 集中定義設定預設數值（單一事實來源） | constants / seed / model 引用 | 無（最低層純資料） |
| `test_config_single_source.py` | 守衛 migration 級距 vs runtime 級距、seed vs defaults vs model 一致 | CI 防漂移 | 動態 import migration |
| `check_salary_configs_current_year` | 啟動時回報缺當年度的 config 表 | `main.py` 啟動呼叫 → loud + Sentry + /health | session、5 config model |

---

## 6. 上線與部署安全

- **零 schema 變更**：本設計不新增 migration（階段三是查詢 + 啟動檢查；階段一是 code 重構）。`PositionSalaryConfig.config_year` 已存在。
- **階段一近乎零行為變更**：去重以「值完全不變」為準；**唯一例外**是 §2.0 的節慶目標人數 fallback
  修正（24→27 等），僅影響無 DB GradeTarget 的 fallback 路徑（dev/test/fresh），**prod 走 DB 數字不變**。
  驗收門檻：有 seed GradeTarget 的薪資 gold 測試零位移；靠 fallback 的 gold 依 §2.0 重鎖。
- **啟動檢查不擋 boot**：階段三 loud 但不 raise，prod cold-start 安全。
- **prod 現況**：後端服務已 RUNNING（2026-06-23 上線），push 即觸發 Zeabur 部署 + cold-start 跑 migration。本設計無 migration，部署風險限於 code 變更本身。
- 合併前 `gen:api` 不需要（無 response schema 變動；階段三若 `/health` 回傳結構新增欄位則確認前端容忍）。

---

## 7. 測試計畫總覽（TDD：先紅後綠 / 重構安全網）

- **階段一**：`test_config_single_source.py`（級距逐筆一致、三處 defaults 一致、單一來源生效證明）；
  節慶目標人數 fallback 修正後（§2.0），有 seed GradeTarget 的薪資 gold 零位移、靠 fallback 的 gold 重鎖。
- **階段二**：依查證結果——若收斂，加「baseline 缺當年度 fail-loud」測試；若證明安全，加註解，無新測試。
- **階段三**：`test_salary_config_startup_check.py`（齊全→空清單 / 表空不報 / 缺年度入清單 / 非 PG 安全回空）。
- **時區 matrix**：沿用 CI 既有 Asia/Taipei × UTC matrix（年度邊界相關）。

---

## 8. Follow-ups（本設計不含，明列）

- 範圍 C：config 寫入端 `finance_approve + audit` 守衛（承 2026-06-05 spec）。
- 階段二若證明 `_load_config_from_db_locked` 為純 baseline，其完整收斂走 resolver 列為低優先 follow-up。
- 缺設定告警升級：fail-loud / 啟動缺漏觸發時推 LINE 給行政（承 2026-06-05 spec follow-up）。
- 後台 UI 讓行政自助維護各年度 config（明確排除於本設計）。
