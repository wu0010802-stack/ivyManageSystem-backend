# Evals & 對抗測試框架

讓 Claude(或一組規則)當對抗測試生成器,針對核心純函式自動造 edge case,並用「不變量(invariants)」檢查行為是否被打破。

## 為什麼要這個

單元測試覆蓋「我們想到的」case;eval framework 嘗試覆蓋「我們沒想到的」case。
適合對象:純函式(計算、規則 helper)、有清楚 input/output 契約、能定義一兩條不變量的程式。
不適合對象:I/O heavy、多 side-effect、權限/HTTP middleware(需另套 fuzzer)。

## 快速開始

```bash
# 1) 列出所有已註冊 target
python -m evals.cli list

# 2) 對某 target 跑 50 個攻擊 case(自動挑 LLM/heuristic)
python -m evals.cli run insurance_service -n 50

# 3) 強制走 heuristic(沒 ANTHROPIC_API_KEY 也可)
python -m evals.cli run leave_policy --mode heuristic -n 100

# 4) 強制走 LLM (Claude API)
export ANTHROPIC_API_KEY=sk-...
python -m evals.cli run insurance_service --mode llm -n 30
```

報告自動寫到 `evals/reports/<target>_<timestamp>.{md,json}`。

## 核心概念

### Target
被攻擊單位。每個 target 宣告四件事:
- `signature`: input 欄位的 schema(type、enum、boundary 提示) — 給 attacker 看的契約
- `invariants`: 必須恆真的性質,被打破就算 finding
- `seed_cases`: 已知合法輸入,給 attacker 學介面、也作為 framework 自身的 sanity check
- `runner`: callable(case_dict) → result

### Invariant
單條檢查。簽名:`check(case_input, outcome) -> Optional[str]`,回 None 表通過,字串表違反原因。

### Attacker
兩種實作:
- **HeuristicAttacker**: 不需 API key,從 `signature.boundary` 抽 ±1、0、極值、NaN、inf,加上隨機組合突變。預設 fallback。
- **LLMAttacker**: 用 Anthropic Claude(預設 `claude-sonnet-4-6`)接 prompt 生成 JSON cases。可帶 `prior_findings` 做多輪。

### Runner
跑每個 case → 跑所有 invariants → 沒被列入 `allowed_exceptions` 的 exception 自動算違反。

## 加新 target

在 `evals/targets/foo_target.py`:

```python
from evals.core.target import Invariant, Target
from services.foo import some_function

def _runner(case):
    return some_function(**case)

def _iv_nonneg(case, outcome):
    if outcome.get("ok") and outcome["result"] < 0:
        return f"got negative {outcome['result']}"
    return None

TARGET = Target(
    name="foo",
    description="...",
    signature={"fields": {"x": {"type": "int", "boundary": [0, 100]}}},
    invariants=[Invariant("nonneg", "result >= 0", _iv_nonneg)],
    seed_cases=[{"x": 50}],
    runner=_runner,
    allowed_exceptions=("ValueError",),
)
```

CLI 自動偵測(掃 `evals.targets` 套件)。

## 跑 framework 自身的測試

```bash
pytest tests/evals/ -v
```

`test_real_target_seed_cases_pass` 是 sanity gate:每個 target 的 seed 必須 0 violation,
否則代表 invariant 寫錯或 seed 不合法。

## 目前的 target 與已知發現

### Heuristic vs Offline-Claude 對比

兩個 attacker 對同一組 invariants 各跑一次,結果(2026-05-15):

| target | mode | cases | violations | unexpected exc | 抓到什麼 |
|---|---|---:|---:|---:|---|
| `leave_policy` | heuristic | 200 | 5 | 1 | `hours=0/-0.0` 通過 sick 規則(boundary 撞到);1 個 `today=9999-12-31` OverflowError |
| `leave_policy` | offline-claude | **14** | 2 | 0 | `hours=-4`(Python `-4 % 4 == 0`)與 `hours=0` 同主題,案例少但每個都精準命中 |
| `insurance_service` | heuristic | 200 | **0** | 0 | 完全沒抓到任何違反 |
| `insurance_service` | offline-claude | **16** | **4** | 0 | `salary=NaN`(×2)與 `salary=+inf`(×1)繞過 `salary < 0` guard;`dependents=1.5` 讓 health 乘子變非整數 |

**結論**:
- `insurance_service` 的 NaN/inf bypass 與非整數 dependents 是 Claude 才會聯想到的 Python 語意陷阱(NaN 比較永遠 False、`bool` 是 `int` 子類、`min(max(0, 1.5), 3)` 不檢型);heuristic 雖跑 12.5× 多 case 仍 0 命中。
- `leave_policy` 上兩 mode 互有勝場:heuristic 因海量試,撞到 `today=9999` 與 `hours=0` 邊界;offline-claude 用 14 case 精準命中 `-4 % 4 == 0` 與同類別。
- 啟示:**heuristic 適合廣面掃,LLM 適合針對 invariant 文字做語意攻擊**;兩者互補,不互斥。

### 已知 finding(已成 invariant 落地)

| target | invariant | 行為 |
|---|---|---|
| `insurance_service` | `IV10_finite_salary` | NaN/+inf salary 不會被 reject,直接走最高級距,後續結果不可信 |
| `insurance_service` | `IV11_dependents_int` | dependents=1.5 直接吃,health_employee 變非整數(業務無意義) |
| `leave_policy` | `IV6_sick_positive_hours` | 病假 hours ≤ 0(包含 -4)通過 validate;規則沒檢查時數正向性 |
| `leave_policy` | `no_unexpected_exception` | `today=date(9999,12,31)` 觸發 `OverflowError` 而非 `ValueError` |

報告全文:`evals/reports/`(檔名格式 `<target>__<attacker>__<timestamp>.{md,json}`)

## 未來方向

- **多輪 attacker**: 把 round 1 的 findings 作為 `prior_findings` 餵進去,讓 Claude 攻打沒覆蓋的維度
- **API endpoint target**: 用 FastAPI `TestClient` 包成 runner;invariants 檢查 status / response schema / 權限
- **Coverage 報告**: 整合 `coverage.py`,把 attacker 跑過的 branch hit 統計起來,quantify「對抗 vs 既有單元測試」覆蓋差
- **CI 整合**: 把 `pytest tests/evals/` 加入 CI,但 `python -m evals.cli run` 留給手動或 nightly job(避免單次 LLM cost)
