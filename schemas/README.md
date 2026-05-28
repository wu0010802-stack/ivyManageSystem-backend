# schemas/ — Pydantic response/request schemas

## 命名規範

| Suffix | 用途 | 範例 |
|--------|------|------|
| `*Out` | API response shape | `EmployeeOut` |
| `*In` | POST/PUT/PATCH request body | `EmployeeCreateIn` |
| `*Query` | GET query params (用 Pydantic Annotated[..., Query()]) | `EmployeeListQuery` |
| `*Patch` | partial update（所有欄位 Optional） | `EmployeePatch` |

## 組織原則

- 1 router = 1 schema 檔（`schemas/<router_name>.py` 對應 `api/<router_name>.py`）
- 共用 base：`schemas/_base.py` 提供 `IvyBaseModel`
  - `from_attributes=True`：可從 SQLAlchemy ORM instance 直接 `.model_validate()`
  - `populate_by_name=True`：允許 alias
  - datetime → Asia/Taipei ISO；date → ISO；Decimal → 2dp float（與既有 round_half_up 對齊）
- 不在 schema 做 PII 遮罩決策；遮罩在 router 端決定，schema 用 `Optional[str]` 接 None

## PII 防漂移 gate

所有 `*Out` schema 欄位名稱不可命中 `utils/sentry_init._PII_KEY_SUBSTRINGS` denylist；
合法 PII 欄位（admin 必看，前置 gate 由 router 控）必須加 inline:

```python
class EmployeeOut(IvyBaseModel):
    id_number: Optional[str] = None  # pii-allow: admin 必看身分證號
```

`scripts/check_pii_in_schemas.py` 走 CI gate，無 inline allow 即 fail。
`_PII_KEY_EXEMPT_SUBSTRINGS` 在 sentry_init 已有的 substring 不需 inline allow（如 `ip_address` / `health_check` / `email_template` / `growth_funnel` / `measurement_unit`）。

## response_model coverage gate

`api/` 下每個 `@router.<method>` decorator 必須有 `response_model=`；
`scripts/check_response_model_coverage.py` 走 CI gate。

例外：`.grandfather-no-response-model` 列 614 個 Phase 1 啟動時尚未覆蓋的 endpoint，
**只能變短不能變長**：

- PR 內補上某 endpoint 的 response_model → 從 grandfather 移除該行
- PR 嘗試新增條目 → CI fail（拒絕新欠帳）

## Phase 進度

| Phase | 目標 router | 預估 schema | Grandfather 預期變化 |
|-------|------------|------------|-------------------|
| 1 (本 PR) | employees / portal/students / parent_portal/auth + students / student_health | ~18 個 | 從 614 → ~560 |
| 2 (後續) | leaves / overtimes / students / classrooms / attendance/* / recruitment/* / portal/* | ~30 個 | → ~150 |
| 3 (後續) | long-tail ~90 router | ~30 個 | → 0；CI gate 變嚴 default |

## 既有 schemas（Phase 1 不重組）

- `academic_term.py` / `activity_admin.py` / `activity_public.py`
- `appraisal.py`（補 pii-allow 至 default_weight / bonus_amount）
- `calendar_admin.py` / `offboarding.py` / `parent_assistant.py`
- `recruitment_funnel.py`
- `year_end.py`（補 pii-allow 至 base_salary）

## 本 PR 新增

- `_base.py`：IvyBaseModel
- `employees.py`：EmployeeOut（首個 phased rollout target）

## 與前端 codegen 的關係

`scripts/dump_openapi.py` 從 FastAPI 抽出 OpenAPI → `openapi.json` →
ivy-frontend `npm run gen:api` → `src/api/_generated/schema.d.ts`。

未加 `response_model=` 的 endpoint 在 schema.d.ts 對應路徑會是 `unknown` —
本 PR 把 5 個高風險 router 上 response_model 後，前端對應 type 會自動下放。

PR merge 後跨 repo 需在 ivy-frontend 開小 PR regen schema.d.ts 並修可能的 caller
type 漂移；`npm run gen:api:check` 走 CI 防漂移。
