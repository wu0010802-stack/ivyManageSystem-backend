# Evals & 對抗測試框架 — 設計

**日期**: 2026-05-14
**分支**: `feat/eval-framework-2026-05-14`
**動機**: 既有 pytest 覆蓋我們「想到的」case;希望讓 Claude 或規則生成器自動造邊界/突變 case,找潛在 edge case 與 invariant 違反。

## 範圍

**做什麼**:
- 後端純函式 target 的對抗測試管線(target → attacker → runner → reporter)
- 兩個示範 target:`services.leave_policy`、`services.insurance_service`
- 兩種 attacker:heuristic(規則式邊界 + mutation)、LLMAttacker(Claude API)
- CLI(`python -m evals.cli list / run`)
- pytest 鎖住 framework 行為與 target 的 seed 健全性

**不做(YAGNI)**:
- 前端 fuzz(需 browser,單獨議題)
- HTTP endpoint fuzz(需 FastAPI TestClient 包裝,留作未來;見 README)
- 多輪 feedback loop(MVP 只 1 輪;架構已預留 `prior_findings` 介面)
- Coverage 整合(留作未來)

## 架構

```
evals/
├── core/
│   ├── target.py        # Target / Invariant / CaseResult / EvalReport / run_one_case
│   ├── llm_attacker.py  # Attacker ABC + HeuristicAttacker + LLMAttacker + build_attacker
│   ├── runner.py        # run_eval + collect_violations
│   └── reporter.py      # report_to_json / report_to_markdown / save_report
├── strategies/          # 預留,目前未用(strategies 邏輯都吸到 attacker 內)
├── targets/
│   ├── leave_policy_target.py
│   └── insurance_target.py
├── prompts/             # 預留(目前 prompt template 直接在 LLMAttacker)
├── reports/             # 產出
├── cli.py
└── README.md

tests/evals/
└── test_framework.py    # 14 個 test:framework 自測 + target seed sanity
```

## 設計選擇

### 為何 invariants 而非 oracle 對比?

純函式 oracle(差分測試)需要第二實作;對 `InsuranceService.calculate` 這種「就是真實邏輯本身」的計算,沒有獨立 oracle。改用「不變量」更務實:
- IV1 例:`salary < 0 應 raise ValueError` — 從 docstring/合約推得
- IV4 例:`total_employee == sum(三項)` — 結構性恆等式
- IV7 例:單調性(`salary` 上升 `insured_amount` 不減) — 通用屬性測試

### 為何 attacker 分兩種?

| 維度 | HeuristicAttacker | LLMAttacker |
|---|---|---|
| 不需 API key | ✅ | ❌ |
| 確定性 | ✅(seed-based) | ❌ |
| 跑得快 | ✅(<100ms / 100 case) | ❌(~10s) |
| 結構性突變 | ✅(boundary、enum、mutation) | ✅ |
| 語意性突變 | ❌(不懂 invariant 文字) | ✅(讀 invariant 描述、針對性攻擊) |
| 學習 prior_findings | 部分 | ✅(prompt 帶入) |

CI 預設用 heuristic(快、無外部依賴、可重現);nightly / 手動深度跑用 LLM。

### Target signature 的設計

JSON schema-like 但極簡:`{"fields": {<name>: {"type": "int|float|string|bool|date|dict", "boundary": [...], "enum": [...]}}}`。
理由:
- 簡單到可讀(LLM prompt 用)
- HeuristicAttacker 可機械生成邊界值
- 不引進 `jsonschema` 依賴

`boundary: [None, ...]` 對應「允許 None 為合法值」(分項投保的語意)。

### 例外處理約定

`Target.allowed_exceptions` 列名稱(`("ValueError",)`)。Runner 抓到符合者算 OK;不符合者算 `no_unexpected_exception` 違反。invariants 也能自己看 `outcome["exception"]` 判斷「該 raise 但沒 raise」(如 IV1)或反向。

## Findings

### 第一輪(heuristic only,2026-05-14)

| target | finding | 風險 |
|---|---|---|
| `leave_policy` | `today=date(9999, 12, 31)` 觸發 `OverflowError` 而非 `ValueError` | 低:生產 today 來自 server clock。若 caller 傳入 user 控制值應 cap |
| `insurance_service` | `salary=inf` 不會 raise,直接走最高級距 | 灰色:商業上是否該 reject? 待產品決策 |

### 第二輪(offline-claude 上線後,2026-05-15)

新增 attacker:`OfflineClaudeAttacker`(Claude 對著 invariants 文字思考的預生成 case 庫)。
新增 invariant:`IV6_sick_positive_hours`、`IV10_finite_salary`、`IV11_dependents_int`。

**對比矩陣**(heuristic 200 / offline-claude 14~16 cases):

| target | mode | violations | 備註 |
|---|---:|---:|---|
| leave_policy | heuristic | 5 | hours=0/-0.0 + OverflowError today=9999 |
| leave_policy | offline-claude | 2 | hours=-4 與 hours=0(精準命中) |
| insurance_service | heuristic | **0** | NaN/non-int 完全沒生成 |
| insurance_service | offline-claude | **4** | NaN salary ×2、+inf salary ×1、dependents=1.5 ×1 |

**關鍵新發現**:
- `insurance_service.calculate(salary=NaN, ...)` 繞過 `salary < 0` guard(NaN 比較永遠 False),走到最高級距,輸出含 NaN 傳染後 round 出來的怪數
- `salary=+inf` 同樣繞過 guard,走 cap;結果是有限值但語意不對
- `dependents=1.5` 直接吃,`health_employee = base * 2.5`(非整數眷屬乘子)

**結論**: heuristic 廣面掃適合常見邊界(0、邊界 ±1、boundary 內);LLM-style attacker 適合語意陷阱(NaN/-0.0/bool/float-where-int)。兩者互補。

## 驗收

- `pytest tests/evals/` 14/14 通過
- `python -m evals.cli list` 列出 2 target
- 兩個 target 各跑 100/200 case,framework 抓到 3 個 unexpected_exception(已驗證為真實 surface)
- 報告 JSON 可 reload、Markdown 可讀

## 後續(寫進 README,不在本次)

1. 多輪 attacker(feedback loop)
2. HTTP endpoint target(FastAPI TestClient runner)
3. Coverage 整合
4. CI nightly job 跑 LLM 模式
