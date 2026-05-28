# API response_model 全覆蓋 + CI drift gate

**日期**：2026-05-28
**範圍**：後端 ivy-backend（api/ 全 router + schemas/）+ 前端 ivy-frontend（schema.d.ts regen + 既有 caller 重對齊）
**狀態**：spec — phased rollout，每 Phase 各自 spec → plan → implement 循環
**所屬序列**：goal /audit-findings C 項（A→B→C 串行；B 已 ship 待 user merge）

---

## 1. 背景與問題

```bash
find schemas/ -name "*.py" -not -name "__init__.py" | wc -l   # → 9
find models/ -name "*.py" -not -name "__init__.py" | wc -l    # → 46
find api/ -name "*.py" -not -name "__init__.py" | wc -l       # → 166
grep -rln "response_model=" api/ | wc -l                       # → 22 (~13% coverage)
```

具體 anti-pattern：
- `api/employees.py`：return naked dict containing SQLAlchemy entity attrs → 曾出現 `_sa_instance_state` 洩漏（已修，但 base pattern 仍在）
- `api/leaves.py`（2487 行）：0 個 `response_model=`，前端拿到 OpenAPI codegen 全 `unknown`
- `api/portal/students.py`、`api/parent_portal/auth.py` 等家長端 endpoint 直回 raw dict / row → PII 無 schema 守門
- 前端「OpenAPI codegen 大量 unknown」根因：response_model 缺，FastAPI 推不出 type → schema.d.ts 對應 endpoint 是 `unknown`

**目標**：
1. 為所有 public router 加 `response_model=`，杜絕 raw dict / `__dict__` 洩漏
2. 統一 schemas/ 命名與組織（`*Out` suffix、按模組分檔）
3. CI 加 gate：新 router 必須有 `response_model=` 才能合併；schema/model 漂移檢測（同時也讓 schema.d.ts 漂移 gate 更嚴）
4. 自動加 PII 防漂移檢查（Out schema 不可含 denylist 欄位）

---

## 2. 範圍與分期

### 全 sub-project 工程量估算

| 工作項 | 數量 | 每項估時 | 總估時 |
|--------|------|---------|--------|
| 新增 `*Out` schema | ~60-80 個 | 10 min | 10-13 h |
| 接 `response_model=` 到 router | 144 router files (避開已有 22) | 8 min | 19 h |
| 既有 endpoint return shape 重整 (raw dict → Out instance) | 預估 200+ endpoint | 5 min | 17 h |
| 前端 schema.d.ts regen + 修 type 漂移 | ~30 caller 重對齊 | 15 min | 7.5 h |
| 既有 pytest 修 response 斷言（shape change） | ~50 test 預期受影響 | 10 min | 8 h |
| CI gate 設計與實作 | 1 | 4 h | 4 h |
| PII 漂移檢查 | 1 | 3 h | 3 h |
| Code review / 修 bug | - | - | 10 h |
| **總計** | | | **~80 h（2-3 週工程）** |

→ **單一 PR 不可能**。**單一 sub-project spec 內也不可能完整實作**。本 spec 切 3 phase，每 phase 各自獨立 spec → plan → implement → PR。

### Phase 1：基建 + 高風險 endpoint + CI gate（本 spec 的 Phase 1）

**Goal**：建立 infrastructure 並把最關鍵的 PII-含 endpoint 上 schema。CI gate 從 Phase 1 起 enforce 新 router 必須有 response_model。

**In scope**：
- `schemas/` 重組為 by-module（`schemas/employees.py` / `schemas/leaves.py` / `schemas/students.py` / `schemas/portal_students.py` / `schemas/parent_portal_auth.py`），加 base helper
- 為 **Phase 1 目標 router** 全 endpoint 上 `response_model=`：
  - `api/employees.py`（員工 PII + 薪資）
  - `api/portal/students.py`（家長端 LIFF 學童）
  - `api/parent_portal/auth.py`（家長登入）
  - `api/parent_portal/students.py`（家長端學童詳情）
  - `api/student_health.py`（醫療 PII）
- CI gate：
  - `scripts/check_response_model_coverage.py` — grep `@router.get|post|put|delete|patch` 後 N 行內無 `response_model=` 即 fail（grandfather list 列 144 既有路徑，逐 phase 移除）
  - `scripts/check_pii_in_schemas.py` — Pydantic schema field 名 vs `_PII_KEY_SUBSTRINGS` 比對，命中即 fail（防新 schema 不小心 expose PII）
  - 加入 `.github/workflows/ci.yml`
- pytest 既有 response 斷言修正（影響範圍待 implementation phase 評估）
- 前端 `npm run gen:api:check` 走 CI 確認 schema.d.ts 漂移檢測仍綠（已存在，本 phase 不動）

**Out of scope（Phase 1 不做）**：
- 其餘 139 router files（Phase 2/3）
- 完整 Pydantic v2 model_config 全套件（datetime serialization tz 等）
- 前端 .vue 元件 explicit type narrow（schema.d.ts 自動下放即可，elem-level type 改善另案）

**Phase 1 完成定義**：
- 5 個高風險 router 100% `response_model=`
- ~25 個 `*Out` schema 落地
- CI gate 啟動 + grandfather list 含 139 router
- pytest 既有測試零 regression
- 前端 schema.d.ts regen 後 `npm run gen:api:check` 綠

### Phase 2：中高流量 router（後續 spec）

**Goal**：覆蓋 user 高流量 admin 端，徹底解掉 OpenAPI codegen unknown 問題。

範圍：~50 router files。重點：
- `api/leaves.py` / `api/overtimes.py` / `api/students.py` / `api/classrooms.py`
- `api/attendance/*` / `api/recruitment/*` / `api/portal/*`（除 students 已 Phase 1）
- 對應 schemas

每補一個 router 同時從 grandfather list 移除。

### Phase 3：剩餘 long tail（後續 spec）

**Goal**：清空 grandfather list；CI gate 變嚴格的 default。

範圍：所有 Phase 1/2 未覆蓋。多半是內部管理或低頻 endpoint，工程量小但數量多。

完成後 grandfather list 全空 → CI gate 對全 router 一視同仁 enforce。

---

## 3. 設計（Phase 1 細節）

### §1 schemas/ 重組

新規範：
- 1 module = 1 file（`schemas/<router_name>.py`）
- 命名：`*Out`（response）、`*In`（request body）、`*Query`（query params）、`*Patch`（partial update）
- Base class：`schemas/_base.py` 提供 `class IvyBaseModel(BaseModel)` with `model_config = ConfigDict(from_attributes=True, populate_by_name=True)`
- datetime / Decimal 統一序列化：`IvyBaseModel` 加 `@field_serializer` for datetime → ISO with tz (Asia/Taipei)；Decimal → float 2dp

Phase 1 新建檔：

```
schemas/
  _base.py                  ← IvyBaseModel + 共用 serializer
  employees.py              ← EmployeeOut / EmployeeListOut / EmployeeDetailOut / EmployeePatchIn / SalaryRecordOut（從 employees.py 抽出）
  portal_students.py        ← PortalStudentOut / PortalStudentListOut
  parent_portal_auth.py     ← LineLoginRequestIn / LineLoginRequestOut / SessionRefreshIn / SessionRefreshOut
  parent_portal_students.py ← ParentStudentDetailOut
  student_health.py         ← StudentHealthOut / StudentHealthRecordOut
```

舊 `schemas/activity_admin.py` / `schemas/recruitment_funnel.py` 等 9 個既有檔保留不動（Phase 1 不重組已有結構）。

### §2 PII 防漂移 schema-level gate

新建 `scripts/check_pii_in_schemas.py`：

```python
"""禁止 Pydantic Out schema 暴露 PII 欄位（denylist substring 命中即 fail）。

Sentry _PII_KEY_SUBSTRINGS 對齊。允許 In/Patch schema（request body 本就含 PII），
僅檢查 Out schema 與其 nested model field names。
"""

import ast, sys
from pathlib import Path
from utils.sentry_init import _PII_KEY_SUBSTRINGS, _PII_KEY_EXEMPT_SUBSTRINGS

def check_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Out"):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fname = item.target.id.lower()
                    if any(s in fname for s in _PII_KEY_EXEMPT_SUBSTRINGS):
                        continue
                    for denied in _PII_KEY_SUBSTRINGS:
                        if denied in fname:
                            errors.append((item.lineno, f"{node.name}.{item.target.id} 含 PII '{denied}'"))
    return errors

if __name__ == "__main__":
    fail = False
    for f in Path("schemas").glob("*.py"):
        for ln, msg in check_file(f):
            print(f"{f}:{ln}: {msg}")
            fail = True
    sys.exit(1 if fail else 0)
```

Caveat：某些 admin endpoint 合法需要回 PII（薪資管理需 employee 月薪）。允許機制：
- schema field 加 `# pii-allow: <reason>` inline comment → script 用 ast.unparse + tokenize 取 trailing comment 過濾
- 或建一個 `OUT_PII_ALLOWLIST: dict[tuple[file, classname, fieldname], str]` 由人手 review 後加入

決定：用 inline comment（go-to convention），減少維護一份外部 allowlist 的負擔。

### §3 response_model coverage gate

新建 `scripts/check_response_model_coverage.py`：

```python
"""每個 @router.<method> decorator 後 4 行內必須出現 response_model=。
grandfather list 列已知缺失路徑；新增 router 不可加入 grandfather list（CI 拒絕新增）。"""

import ast, sys
from pathlib import Path

GRANDFATHER = set()  # 由 .grandfather-no-response-model 文件載入

def check_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                   and dec.func.attr in {"get", "post", "put", "delete", "patch"}:
                    has_rm = any(kw.arg == "response_model" for kw in dec.keywords)
                    if not has_rm:
                        key = f"{path}:{node.name}"
                        if key not in GRANDFATHER:
                            errors.append((dec.lineno, f"missing response_model"))
    return errors
```

Grandfather 文件 `.grandfather-no-response-model`（純文字，one path:func per line）：

```
api/leaves.py:list_leaves
api/leaves.py:create_leave
... (Phase 1 完成時這檔應從 ~200 條開始)
```

CI 步驟：
1. 對所有 router 跑 coverage check
2. 對 schemas/ 跑 PII denylist check
3. 兩個有任一 fail 即 PR 不可 merge

新增 router endpoint 時：
- 如果新 router 有 response_model → CI 自動過
- 如果新 router 沒 response_model 又不在 grandfather → CI fail（拒絕新欠帳）
- 如果 PR 把 grandfather 條目移除（補完 response_model）→ CI 過（合理進度）
- 如果 PR 嘗試新增 grandfather 條目 → CI 額外 check (`git diff` 對 grandfather 比較行數，新增即 fail)

### §4 Phase 1 router 範圍與 endpoint 詳列

| Router | 大致 endpoint 數 | 新 Out schema 數 |
|--------|----------------|-----------------|
| `api/employees.py` | ~18 | EmployeeOut / EmployeeListOut / EmployeeDetailOut / SalaryRecordOut / SalaryHistoryOut / EmployeeProbationAlertOut |
| `api/portal/students.py` | ~12 | PortalStudentOut / PortalStudentListOut / PortalStudentScheduleOut |
| `api/parent_portal/auth.py` | ~8 | LineLoginRequestOut / SessionOut / SessionRefreshOut / LogoutOut |
| `api/parent_portal/students.py` | ~6 | ParentStudentDetailOut / ParentStudentScheduleOut |
| `api/student_health.py` | ~10 | StudentHealthOut / StudentHealthHistoryOut / VaccinationRecordOut |
| **Total** | ~54 endpoint | ~18 Out schema |

每個 endpoint：
1. 新建/找到對應 Out schema
2. 把 `return {...}` 改成 `return XxxOut(**dict)` 或 `return XxxOut.model_validate(orm_instance)`
3. router decorator 加 `response_model=XxxOut`（或 `list[XxxOut]`）
4. 跑既有 pytest 確認 response shape 一致（shape 不變僅 type narrow）

---

## 4. 風險與緩解

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| 既有前端 caller 依賴 raw dict 多餘欄位（schema 漏列） | 高 | 中 | 第一輪 Out schema 用 strict shape 反而抓出 caller bug；接前端 schema.d.ts 後若 caller 用了未列欄位會 typecheck fail |
| 既有 pytest 寫死 `response.json() == {...}` 嚴格 shape | 高 | 低 | implementation phase 修；多半是補 / 改既有 assert 接受新 schema serialization 順序 |
| Decimal/datetime 序列化 round-trip 不對 | 中 | 中 | IvyBaseModel base serializer 統一 + 補 round-trip test |
| Pydantic v2 `model_config` 與既有 schemas 衝突 | 低 | 低 | 9 個既有 schemas 已是 v2；漸進兼容無風險 |
| Grandfather list 過大導致 git merge conflict 高 | 中 | 低 | 文件 sort + 一行一條，merge conflict 少且易解 |
| PII denylist substring 誤判合法欄位 | 中 | 中 | inline comment exempt 機制 + per-Phase review |
| Phase 1 完成後 CI gate 反而拖慢 Phase 2/3 進度 | 低 | 低 | grandfather list 「只能移除不能增加」的 invariant 強制清債節奏 |

---

## 5. 測試

- 純函式：`scripts/check_*.py` 各自 5 test（pass/fail/exempt/grandfather/edge）
- Phase 1 router：既有 pytest 應大部分綠；改動處補 1-2 條 response_model serialize shape test
- PII gate：fixture schema 故意含 `email_address` 欄位 → 預期 fail；改加 `# pii-allow: 員工 contact 必填` → 預期 pass

---

## 6. 提交策略（Phase 1）

單 PR in `ivy-backend` repo（worktree `feat/response-model-phase1-2026-05-28-backend`）。

預期 12-15 commits（estimate）：

1. docs(spec)
2. feat(schemas): IvyBaseModel + datetime/Decimal serializer
3. feat(schemas): employees Out classes
4. feat(api): api/employees.py 全 endpoint 接 response_model
5. feat(schemas): portal_students + parent_portal_*
6. feat(api): api/portal/students.py response_model
7. feat(api): api/parent_portal/auth.py + students.py response_model
8. feat(schemas): student_health
9. feat(api): api/student_health.py response_model
10. test(api): 修既有 pytest 與新增 round-trip test
11. feat(ci): grandfather list + check_response_model_coverage.py
12. feat(ci): check_pii_in_schemas.py + inline-comment exempt
13. ci(github): .github/workflows/ci.yml 加 coverage + PII gate
14. docs: schemas/README.md 命名/結構規範
15. chore(frontend): cross-repo 確認 schema.d.ts regen 後 type 漂移在可接受範圍

跨 repo 影響：
- ivy-frontend `npm run gen:api:check` 在後端 merge 後第一次跑可能 fail（schema 增變動）→ 開 PR 同步 regen schema.d.ts（小 PR，自動 ship）

---

## 7. 完成定義（Phase 1 DoD）

**Backend**：
- [ ] 5 個高風險 router 100% `response_model=`
- [ ] ~18 個 `*Out` schema + IvyBaseModel 落地
- [ ] CI grandfather list 含 139 router endpoints
- [ ] `scripts/check_response_model_coverage.py` + `scripts/check_pii_in_schemas.py` 落地
- [ ] `.github/workflows/ci.yml` 兩 gate 加入 PR-blocking
- [ ] pytest 全套 baseline ±0（不引入新 fail）
- [ ] schemas/README.md 寫成

**Frontend（同 day cross-repo PR）**：
- [ ] `npm run gen:api` regen schema.d.ts
- [ ] `npm run gen:api:check` 綠
- [ ] caller import shape mismatch 全修（預估 ~5-10 個檔）
- [ ] typecheck 0 / vitest 全綠 / build 過

**User 部分**：
- [ ] backend merge + push
- [ ] frontend regen PR 開 + merge
- [ ] 後續 Phase 2 啟動時間規劃

---

## 8. Out of Scope / 後續 Phase

- **Phase 2**：50 個中流量 router（leaves / overtimes / students / classrooms / attendance / recruitment）
- **Phase 3**：剩餘 ~90 個 router；清空 grandfather；gate 變嚴
- **OpenAPI tags / examples 完整化**：Pydantic field-level `examples=`，提升 /docs 可讀性（與 response_model 解耦）
- **OpenAPI security schemes**：endpoint-level auth 標註（已有 Depends 守門，文件層補注另案）
- **GraphQL 評估**：response shape 多型需求若日後浮現（admin 端要 partial fields 而 LIFF 端要不同 partial），先 REST + Out schema 過渡，GraphQL 為 long-term option
