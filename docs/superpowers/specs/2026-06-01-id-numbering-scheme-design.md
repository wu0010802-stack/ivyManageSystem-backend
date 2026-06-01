# 學號與員工編號邏輯設計（Student / Employee ID Numbering）

- 日期：2026-06-01
- 範圍：跨前後端（ivy-backend 為主，ivy-frontend 少量表單調整）
- 模組：學生（students / classroom）、員工（employees）、招生轉化（recruitment）
- 對應 CLAUDE.md：跨端 SOP、`api-contract` skill、Migration 先行（#6）

## 背景與目標

系統有兩個對外的人類可讀識別碼：

- **學號** `Student.student_id`（String(20), unique）：目前**自動產生**，格式
  `{學年}-{班級代號}-{NN}`（例 `114-A-05`），在入學（招生轉化）時產生一次，
  之後**不再更新**。
- **工號** `Employee.employee_id`（String(20), unique）：目前**全手動填寫**，
  無格式規範（既有資料 `E001` / `T001` / `ADMIN001` 混用）、無自動生成。

問題來自業務現實：**老師會換班級、學生會升年級**。需要重新設計這兩個編號的邏輯，
讓「會變動的關係」與「編號」之間的關係明確、可維護。

### 目標

1. 學號對外顯示能**反映學生當前的學年與年級層級**（升年級時跟著變）。
2. 但學生在系統內部要有一個**永不改變的穩定識別**，使所有歷史紀錄、報表、稽核
   都不受顯示碼變動影響。
3. 工號改為**自動產生、格式統一**，且工號**到職即固定**、與班級/職務無關。

### 非目標（Out of Scope）

- 不改任何外鍵關聯方式（全系統已用內部整數主鍵 `id`，本案不動）。
- 不改年級（`ClassGrade`）的結構（不新增 code 欄位，年級字由名稱首字推導）。
- 不重編既有員工的手填工號（`E001` 等保留原值），只有**新進員工**自動給號。
- 不做「自動給號可手動覆寫」（使用者選擇純自動、統一格式）。
- 不提供「對外顯示碼」的全域唯一性 DB 約束（顯示碼為計算值，見「已接受取捨」）。

## 關鍵決策（已與使用者確認）

| 決策 | 選擇 | 理由 |
|------|------|------|
| 學號性質 | **反映當前年級**（升年級才變） | 業主希望一眼看出學生現在哪一屆哪一級 |
| 套用範圍 | **只有學生**；老師工號穩定不變 | 工號是人事編號，與班級無關；換班只改 `classroom_id` |
| 學號形狀 | **學年 + 年級層級 + 流水**（例 `115-中-05`） | 最貼合「升年級而變動」；同年級內換班不變 |
| 流水號穩定性 | **跟著小孩的永久編號，一旦給就不變** | 留級/學期中轉入都沿用同號；對外顯示由系統即時組出 |
| 儲存策略 | **存永久 key、顯示即時計算（架構 B）** | 給出相同對外行為，但實作最單純、最安全（見下節） |
| 工號產生 | **自動產生、統一格式** | 取代手填、消除格式不一 |
| 工號格式 | **`{民國到職年}{當年3碼流水}`**（例 `114001`） | 到職年永不變，符合「工號穩定」原則 |

## 架構決策：為何用「永久號 + 計算顯示」（B）而非「儲存可變字串」（A）

對外要的行為是「學號顯示反映當前年級、升年級才變、且同一小孩流水號不變
（`114-小-05 → 115-中-05`）」。有兩種實作能產生**完全相同**的對外行為：

- ❌ **A：把可變字串存進 `student_id`**：升年級時 `UPDATE student_id`。
  代價是要加重產 hook、改兩處唯一性檢查、建歷史表、處理「釋放的舊號」碰撞、
  以及未分班學生沒有年級無法組字串 → 複雜且脆弱 → **否決**。
- ✅ **B（採用）：存穩定 canonical key + `student_id` 維持為「顯示快取欄位」**：
  新增 `enrollment_school_year` + `enrollment_seq`（永久、入學時給一次）作為**身分認定鍵**；
  `student_id` **仍是儲存的 String 欄位**，但語意改為「由 canonical key + 當前班級
  即時組出的 denormalized 顯示快取」，透過 SQLAlchemy `before_flush` event listener
  在學生新建 / 班級異動時自動重算。
  - **為何不把 `student_id` 改成純 Python property**：前置調查發現 `Student.student_id`
    在 SQL 層被大量使用 —— `order_by(Student.student_id)` ×5、**以學號搜尋
    `Student.student_id.ilike(...)` ×3**、select ×1。純 property 無法 `order_by` / `ilike`，
    且「打學號搜尋」必須是 SQL 可查（學號是 join 班級+年級才組得出，無法在 SQL 端重建）。
    保留為儲存欄位即可讓這些用法**零改動**。
  - **比架構 A 安全在哪**：身分認定永遠是 canonical key，永不變；`student_id` 只是
    cosmetic 快取，即使某次重算被漏掉也只是顯示字串過時，**不會有資料錯亂、不需歷史表、
    不需處理「釋放的舊號」碰撞**。
  - 前置調查已確認全系統外鍵都用內部整數 `id`，是 B 能乾淨成立的前提。

## 學生學號設計

### 資料層（永久，不變）

`Student` 新增兩個欄位：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `enrollment_school_year` | Integer, **nullable** | 發號學年（民國，如 114）；入學配發永久號時記錄一次 |
| `enrollment_seq` | Integer, **nullable** | 永久流水號；於該發號學年內遞增、入學時給一次、終身不變 |

- 複合唯一約束 `UniqueConstraint("enrollment_school_year", "enrollment_seq")`
  —— 這是學生真正穩定的對外唯一鍵。Postgres 將 NULL 視為相異，故未配號的列
  （legacy / 測試 fixture）不互相衝突。
- **兩欄刻意 nullable**：未走配號器的列（既有測試 fixture `Student(student_id="S1")`、
  尚未配號的特殊資料）`enrollment_seq` 為 NULL，重算 listener 會略過它們，
  **完全不影響既有測試與資料**；只有配號器配過的列才進入新邏輯。
- 永久號**配發時機**：學生**報到分班**時。目前有兩條入口：
  - `recruitment_conversion.convert_recruitment_to_student()`：現走
    `next_student_id_code` 自動產生 → 改為呼叫配發器取得 `enrollment_seq`。
  - `POST /students`：現為**手填** `student_id` + 唯一性檢查 → 改為由配發器自動配號。

  兩條入口統一走同一個配發器。招生中（prospect、尚未分班）的學生**尚未配號**，
  與現況一致。
- 配發採既有 `pg_advisory_xact_lock` 並發保護：在「發號學年」維度上鎖，
  掃描該學年現有 `enrollment_seq` 取 `max + 1`。
- 升年級 / 留級 / 轉班 / 轉入轉出 → **永久號全程不動**。

### 顯示層（對外「學號」= denormalized 顯示快取）

- `Student.student_id` **維持為儲存的 String 欄位**（不改成 property，理由見架構決策），
  但語意改為「顯示快取」，值由純函式即時組出後寫入：

  ```
  在班級時： f"{classroom.school_year}-{grade_char}-{enrollment_seq:02d}"   例 115-中-05
  無班級時： f"{enrollment_school_year}-{enrollment_seq:02d}"                例 114-05
  ```

- `grade_char` = 年級名稱首字：大班→大、中班→中、小班→小、幼幼班→幼
  （由 `classroom.grade.name[0]` 推導；不動 `ClassGrade` 結構）。
- **重算機制：SQLAlchemy `before_flush` event listener**。對 session 內
  「新建」或「`classroom_id`/`enrollment_seq` 有異動」且 **`enrollment_seq` 非 NULL**
  的 `Student`，重新計算 `student_id` 並寫回。lookup 班級/年級用 `session.no_autoflush`
  避免 flush 遞迴。
  - 涵蓋所有 ORM 寫入路徑：報到分班（insert）、`bulk_transfer`（屬性 set）、
    `PUT /students` 改班（屬性 set）。**未來新增的 ORM 改班路徑自動涵蓋**。
  - 唯一不經 ORM 的 bulk path：`classroom_carry_over._carry_over_same_year` 用
    `query.update(synchronize_session=False)`（繞過 listener）—— 但它是**同學年、
    同年級**搬遷，顯示值不變，**本就不需重算**，故此 gap 無害（spec 明確記載）。
- 顯示**只看學生當前所在班級**（班級本身帶 `school_year` 與 `grade`）：
  - 升年級＝被移到新班級（新學年、新年級）→ listener 重算，顯示自動跟著變。
  - 畢業 / 轉出（停留或最後落在終態班級）→ 學號**凍結**在最後班級的年級。
  - 未分班 / 招生中 → 退化為 `{發號學年}-{永久號}`。

> 序列化：`student_id` 仍是欄位，`StudentListItemOut` / `StudentDetailOut`
> 等 Pydantic response（`schemas/students.py:67,113`）與所有報表/證書/搜尋
> **零改動**，自動顯示新格式。

### 唯一性檢查的簡化

現有兩處用 `Student.student_id == X` 做全域唯一性檢查 → 改由「永久號配發器 +
複合唯一鍵」自動保證：

- `api/students.py:816`（`POST /students` 唯一性檢查）→ 移除字串比對，改走配發器。
- `services/recruitment_conversion.py:106`（轉化時唯一性檢查）→ 同上。

**移除 `student_id` 欄位上的 `unique=True`**（這是刻意的）：已接受的「跨屆留級生
極罕見顯示同碼」在 unique 約束下會於升年級批次時 crash；唯一性移到複合鍵。
`repositories/student.py:17` 的 `get_by_student_id()` 為 dead code，順手評估移除。
實作時再掃一次是否有其他「假設 `student_id` 唯一」之處（例 `.filter(student_id==X).one()`）。

### 既有資料遷移（Alembic）

1. 新增 `enrollment_school_year`、`enrollment_seq` 欄位（nullable）。
2. 移除 `students.student_id` 的 `unique` 約束；新增複合唯一約束
   `uq_students_enrollment_year_seq (enrollment_school_year, enrollment_seq)`。
3. Backfill（data migration）：對既有學生
   - `enrollment_school_year`：解析 `student_id` 前綴 `^(\d{3})-` 取學年；
     失敗則用 `enrollment_date` 推算學年；再失敗用當前學年。
   - `enrollment_seq`：**在每個 `enrollment_school_year` 內依 `id` 排序重新配 1,2,3…**
     （現有 NN 是 per「學年+班級」唯一、跨班會重號，故必須在學年維度重排）。
   - 重算 `student_id` 快取為新格式（依當前班級或退化規則）。**既有列印學號會改變**
     （見風險）。
4. `downgrade`：刪兩欄 + 還原 `student_id` unique；舊 `{學年}-{班代}-{NN}` 字串
   無法完全還原（class_code 資訊已不在 student_id 內），於 migration docstring 明確記載
   downgrade 後 `student_id` 為新格式快取、非原值（不可逆部分）。

> Migration 先行（CLAUDE.md #6）：須在前端拉新行為前合併並 `alembic upgrade heads`。

## 員工工號設計

- 自動產生，格式 **`{民國到職年:03d}{當年流水:03d}`**，例 `114001`。
- 年份來源：`Employee.hire_date`（line 178）的民國年；`hire_date` 為空時退回
  `created_at` 的民國年。（民國年換算沿用 codebase 既有 helper。）
- 流水：於該「到職民國年」維度內遞增（`max + 1`），同樣以 advisory lock 保並發。
- 到職即固定、與班級/職務/職稱無關 → 換班、轉職類、改 `classroom_id` 皆不影響。
- 既有手填工號（`E001` 等）**保留不動**；只有新進員工自動給號。
  Backfill 既有資料**不做**（避免衝擊既有列印/對帳）。
- `EmployeeCreate` schema **移除手填 `employee_id`**；`create_employee` 改為
  伺服器端產生並回傳工號。`api/employees.py:407-419` 的重複檢查改為配發器保證。
- 前端「新增員工」表單（`ivy-frontend`）同步調整：移除/唯讀 `employee_id` 欄，
  建立後顯示系統產生的工號。

> 工號流水若需與 `id` 解耦（避免顯示碼洩漏總人數），以「該到職年內計數」而非全域
> `max(id)`；本設計採前者。

## 受影響的程式碼點（清單）

後端（ivy-backend）：
- `models/classroom.py`：`Student` 加兩欄 + 複合唯一鍵；移除 `student_id` 的 `unique`。
- `models/employee.py`：`employee_id` 仍為欄位，但由 server 端配發。
- 新增 `services/student_numbering.py`：`grade_char` / `compute_student_display_id` /
  `next_enrollment_seq` 純函式 + 配發器。
- 新增 `services/employee_numbering.py`：`next_employee_id(session, hire_year_roc)`。
- 新增 `before_flush` listener（註冊於 model 載入時，例 `models/student_events.py`
  並由 `models/__init__.py` import），重算 `student_id` 快取。
- `services/recruitment_conversion.py`、`api/students.py`：唯一性檢查改走配發器；
  `StudentCreate` 移除必填 `student_id`。
- `api/employees.py`：`EmployeeCreate` 移除 `employee_id`、`create_employee` 改自動給號；
  `EmployeeUpdate` 移除 `employee_id`（工號不可改）。
- `repositories/student.py`：評估移除 dead `get_by_student_id`。
- `alembic/versions/`：學生欄位 + 約束調整 + backfill migration（單一 head）。

前端（ivy-frontend）：
- 新增員工表單：移除/唯讀工號輸入；建立後顯示產生值。
- 學號/工號顯示處不需改取用路徑（仍為 `student_id` / `employee_id` 字串）。

## 測試

- **純函式**：學號顯示組字串（在班級 / 無班級 / 終態凍結）、年級字推導、
  民國年換算、工號組字串。
- **配發器**：`enrollment_seq` 與工號流水的並發遞增、advisory lock 行為、
  跨學年 / 跨到職年重置。
- **升年級顯示變化**：學生換班級後 `student_id` 計算值正確改變、`enrollment_seq` 不變。
- **遷移腳本**：既有 `{學年}-{班代}-{NN}` 跨班重號的學年內重排正確、無衝突；
  不可解析值的 fallback。
- **回歸**：確認報表 / 證書 / 家長端取用 `student_id` 仍正常（序列化）。
- 修 bug 先補可重現的回歸測試（CLAUDE.md 規範）。

## 風險與已接受取捨

1. **顯示碼罕見不唯一**：永久號在「發號學年內」流水（小號碼如 `05`）。
   極罕見情況——兩位**不同屆**、`enrollment_seq` 都為 05、又**剛好同一年升到同一年級**
   （例如有人留級）——對外會都顯示 `115-中-05`。內部複合 key 與整數 `id` 仍能區分，
   **資料不會錯**，僅顯示碼罕見地重複。使用者已接受（替代方案：全園流水則號碼變大
   如 `1024`，失去小號碼，未採用）。
2. **既有列印學號會改變**：遷移會把所有現有 `student_id` 重算為新格式
   （`{學年}-{年級字}-{流水}`）。已發出的紙本/通知上的舊學號與系統不再一致，
   須事先告知家長與行政。歷史紀錄關聯不受影響（全走內部 `id`）。
3. **重算 listener 的 bulk-update gap**：`classroom_carry_over` 用 `query.update()`
   繞過 listener，但為同學年同年級搬遷、顯示值不變，**無害**（已記載）。若未來新增
   「會改學年/年級」的 bulk-update 路徑，須在該處顯式呼叫重算 helper。
4. **遷移 downgrade 不可逆部分**：`student_id` 還原為新格式快取、非原 `{學年}-{班代}-{NN}`
   字串（class_code 已不在 student_id 內）；於 migration docstring 明確記載。

## 待釐清 / Follow-up

- 民國年換算：codebase 慣用 `西元年 - 1911`（見 `utils/academic.py:_resolve_by_date`）；
  學年用 `resolve_current_academic_term()`。工號的「到職民國年」直接 `hire_date.year - 1911`
  （日曆年，非學年）；實作時沿用此慣例、勿重造。
- 工號流水單位確認：採「到職民國年內計數」（已定），實作時驗證跨年重置為 `001`。
- 是否需要「依 `enrollment_school_year` 找學生」的查詢入口（目前無需求，不做）。
