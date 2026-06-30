# 新增員工表單重新設計（兩段式建檔）— 設計文件

- 日期：2026-06-03
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 狀態：設計定稿，待寫實作計畫

## 1. 背景與目標

現行「新增員工」是一張 41 欄的單一長表單，其中薪資/投保/銀行/特殊狀況共 18 個專業欄位，
對第一線建檔（行政/園長）太重，且建檔當下通常拿不到也不該碰這些敏感資料。

目標：把建檔拆成**兩段式**，降低第一線建檔負擔，同時順手清掉孤兒欄位、補上幾個常用欄位。

## 2. 範圍

### Phase 1（本次）
- 前端「新增員工」改為**兩段式**：第一段「基本建檔」只露身分/職務/聯絡/政府申報；
  薪資/投保/銀行（第二段）沿用既有「高風險編輯 tab + 變更摘要確認」流程，於建檔後補。
- **新增 3 個輕量欄位**：性別 `gender`、Email `email`、加保生效日 `insurance_effective_date`。
- **移除孤兒欄位**：部門 `department`（純前端、後端無欄位，建檔時硬塞 `'Teaching'` 不會被儲存）。
- **新增「待補薪資」提示**：員工列表對「正職且 `base_salary==0`」顯示 tag，提醒 HR 補第二段。

### Phase 2（後續另開，不在本次）
- 大頭照上傳：需決定儲存後端 + 上傳端點 + model 欄位 + 前端 uploader，屬獨立子系統。

### 明確不做
- 不硬刪 legacy `title` 欄位：它由 `job_title_id` 在 `create_employee` 自動同步，
  且被 `TeacherOut` / `Employee.title_name` 等多處讀取。它**早已不是表單輸入欄**（僅顯示用），
  「移除」訴求已達成，DB 欄位保留。
- 不改薪資引擎計算路徑；`insurance_effective_date` 為**純記錄欄位**，不進任何計算/proration。

## 3. 兩段式建檔設計（前端為主，後端幾乎免動）

後端現況已允許「只建人、不填薪資」：`create_employee` 的 `validate_minimum_wage` /
`validate_insurance_salary` 僅在金額 > 0 時觸發，`base_salary` 預設 0 不會擋。
因此兩段式**不需要新的後端端點**，主要是前端對話框的呈現切分：

- **第一段（新增對話框 `openCreate`）**：只渲染 `EmployeeFormBasic` 的區段
  （核心 / 職務細節 / 個資聯絡 / 工作時間 / 教保身分）。建檔時**隱藏** `EmployeeFormSalary` 整個 tab。
- **第二段（建檔後）**：開啟該員工的編輯，顯示既有薪資/投保/銀行高風險 tab（含變更摘要確認對話框），由 HR/會計補。
- **銜接提示**：列表對「正職 + 底薪為 0」顯示「待補薪資」tag，避免漏補。

## 4. 欄位異動清單

| 動作 | 欄位 | 段別 | 後端 | 備註 |
|------|------|------|------|------|
| ＋新增 | 性別 `gender` | 第一段·身分 | 加 column + schema | `String(10)` nullable，前端下拉 男/女/其他 |
| ＋新增 | Email `email` | 第一段·聯絡 | 加 column + schema（**PII**） | `String(100)` nullable；`# pii-allow` 註解 + 跑 PII 檢查腳本 |
| ＋新增 | 加保生效日 `insurance_effective_date` | 第二段·投保 | 加 column + schema + `_DATE_FIELDS` | `Date` nullable，純記錄不入計算 |
| ✕移除 | 部門 `department` | — | 無（本就無欄位） | 刪前端 input + reactive 預設 + interface + `employeeFields.ts` + `employeeFormSections.ts` |
| 不動 | legacy `title` | — | 保留 | 已非表單欄，僅由 job_title 同步顯示 |

## 5. 後端變更（ivy-backend）

1. **Model** `models/employee.py`：`Employee` 加 3 欄
   - `gender = Column(String(10), nullable=True, comment="性別")`
   - `email = Column(String(100), nullable=True, comment="電子郵件")`
   - `insurance_effective_date = Column(Date, nullable=True, comment="加保生效日（記錄用，不入計算）")`
2. **Schema** `api/employees.py`：`EmployeeCreate` + `EmployeeUpdate` 各加上述 3 欄
   （皆 Optional；`gender`/`email` 為 str，`insurance_effective_date` 為 str 由 `_DATE_FIELDS` 解析）。
3. **Out / 讀回路徑** `schemas/employees.py` 的 `EmployeeOut` + `api/employees.py` 的
   `_format_employee_response`：加 3 欄（`email` 標 `# pii-allow: 員工聯絡 Email`，`gender` 視 PII 腳本結果決定是否標）。
   *讀回路徑與寫入路徑分離，漏改 `EmployeeOut`/format 會導致存得進、讀不回。*
4. **日期解析**：`_DATE_FIELDS` 加入 `insurance_effective_date`（line 54）。
5. **持久化**：`Employee(**emp_data)` 自動帶入，無需逐欄 wiring。
6. **PII 合規**：`email`（必）與 `gender`（視情況）加 `# pii-allow`；跑 `scripts/check_pii_in_schemas.py`；
   確認前後端 Sentry denylist（後端 `utils/sentry_init` / 前端 `src/utils/sentry.ts`）對 `email` 子字串已涵蓋遮罩、無 exempt 誤放。
7. **Migration** `alembic/versions/`：新檔，`down_revision = "studnum01"`（目前唯一 head）。
   `upgrade` 加 3 個 nullable column；`downgrade` 對應 drop。純加欄、無 backfill、可逆。
8. **測試** `tests/test_employees*.py`：建檔帶新 3 欄 → 讀回驗證；`insurance_effective_date` 日期解析；
   不帶薪資仍可建檔（兩段式回歸）。

## 6. 前端變更（ivy-frontend）

1. **移除 department**：`EmployeeFormBasic.vue`（input + interface）、`EmployeeView.vue:386`
   （`form.department='Teaching'` 預設）、`constants/employeeFields.ts`、`constants/employeeFormSections.ts`。
2. **新增 3 欄輸入**（`EmployeeFormBasic.vue`）：
   - 性別：核心/身分區，`el-select`（男/女/其他）
   - Email：個資聯絡區，`el-input`（補 email 格式驗證）
   - 加保生效日：放第二段 `EmployeeFormSalary.vue` 投保群組，`el-date-picker`
3. **兩段式**：`EmployeeView.vue` 的新增對話框只渲染基本區段、隱藏薪資 tab；
   區段登記表 `employeeFormSections.ts` 補 `gender`/`email`/`insurance_effective_date`。
4. **待補薪資 tag**：員工列表對 `employee_type==='regular' && !base_salary` 顯示提示 tag。
5. **型別**：後端改完跑 `dump_openapi.py` → 前端 `npm run gen:api`，更新 `schema.d.ts`。
6. **測試**：`EmployeeFormBasic` 渲染新欄 + 不再有部門；建檔 payload 含新欄。

## 7. 實作順序（依 workspace SOP，後端先行）

1. 後端：model + schema(Create/Update/Out) + `_format_employee_response` + `_DATE_FIELDS` + PII 註解 + migration + pytest，跑 `pytest` 綠 + `alembic upgrade heads`。
2. 前端：`gen:api` → 移除 department → 新增 3 欄 → 兩段式對話框 → 待補薪資 tag → Vitest。
3. 整合：`start.sh` 起兩端，實際建一個新人走兩段式。
4. 分開 commit（後端一筆、前端一筆）。

## 8. 風險與回滾

- Migration 純加 nullable 欄，低風險、可逆。
- 兩段式為前端呈現切分，不改後端契約行為，既有建檔/編輯流程不受影響。
- PII 單側遺漏為主要風險點 → 第 5.6 節強制檢查涵蓋。
