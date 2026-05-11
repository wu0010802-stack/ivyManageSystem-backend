# 教育部申報模組設計（MOE Reporting Module）

| 欄位 | 內容 |
|------|------|
| 日期 | 2026-05-11 |
| 範圍 | 跨前後端（ivy-backend + ivy-frontend） |
| 狀態 | design（待 user 最終 review） |
| 對應需求 | 原始 13 項建議中的 #5 / #6 / #7 / #9 |

---

## 1. 背景與目標

義華為**私立幼兒園**。業主目前每月/每學期向教育部「全國幼兒園幼生管理系統」(ece.moe.edu.tw) 與「教保服務人員管理資訊系統」**手工**填表，每位幼生、每位教保員欄位逐一輸入，耗時且容易漏填。

本模組**不做政府 API 對接**（政府不開放），而是：

1. 在系統內補齊政府要求的結構化資料欄位
2. 提供 Excel 對照清單匯出（欄位順序、標題與政府網站表單對齊）
3. 讓業主拿著 Excel 一格一格貼上政府網站，**將 30 min/幼生的工作縮短到 2 min/幼生**

涵蓋四個面向：

- **#5 身心障礙幼兒就學補助**：身障名冊管理、IEP 個別化教育計畫、特教加給/助理鐘點費申領、家長申請在學證明
- **#6 全國教保資訊網幼生資料**：學期初/異動時的幼生對照清單
- **#7 教保服務人員資料庫**：教保員、教師證號、身分別異動對照清單
- **#9 每月幼生在園/出席統計**：每月底「實際在園人數」月報，影響教育券撥款

---

## 2. 範圍與不在範圍

### 在範圍

- Student、Employee 表結構化欄位補齊
- 4 張新表：身障文件、IEP、特教加給、月報快照
- Excel 對照清單匯出器（人類可讀，標題對齊政府表單）
- IEP CRUD（含「複製上學期作為起草」）
- 特教加給/助理鐘點費 申領 CRUD
- 在學證明 PDF 產生器（通用模板）
- 首頁鑑定到期提醒 widget

### 不在範圍

- **政府 API 對接**：政府未開放
- **自動排程產生月報**：業主明確選擇手動觸發
- **準公共補助申請**：私幼不適用
- **托育補助/育兒津貼**：屬家長端，非園所端工作
- **5 歲免學費**：私幼有不同制度，本期不做
- **教育部公文掃描歸檔**：另作獨立模組

---

## 3. 實作策略：分 4 Phase 漸進交付

業主使用頻率差異大（月報每月、幼生/教保員每學期、IEP 一年兩次），分階段上線 ROI 最高。

| Phase | 範圍 | 預估工時 | 依賴 |
|-------|------|---------|------|
| Phase 1 | 共用基礎：schema 補齊 + 後台維護表單 + 首頁提醒 | 1-2 週 | 無 |
| Phase 2 | #9 月報匯出器 | 1 週 | Phase 1 |
| Phase 3 | #6 幼生 + #7 教保員 對照清單匯出器 | 1-2 週 | Phase 1 |
| Phase 4 | #5 IEP + 特教加給 + 在學證明 | 2 週 | Phase 1 |

Phase 2/3/4 之間無相互依賴，必要時可平行（不同分支）。

---

## 4. Phase 1 設計：資料模型補齊（共用基礎）

### 4.1 Student 表新增欄位

所有欄位 nullable（不破壞既有資料）；舊幼生資料漸進補齊。

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id_number` | String(20), **unique where not null** | 身分證字號（所有政府報表的 key） |
| `nationality` | String(20) default '本國' | 國籍 |
| `household_address` | String(200) | 戶籍地（既有 `address` 為通訊地） |
| `is_disadvantaged` | Boolean default False | 弱勢總開關（給統計報表用） |
| `low_income_status` | String(20) | `low` / `mid_low` / null |
| `indigenous_status` | String(20) | 阿美/泰雅/.../null |
| `disability_type` | String(50) | 智能/聽覺/視覺/肢體/語言/情緒行為/學習/自閉症/多重 |
| `disability_level` | String(10) | 輕度/中度/重度/極重度 |
| `disability_cert_no` | String(50) | 鑑定證明文號 |
| `disability_cert_expiry` | Date | 鑑定到期日（首頁提醒用） |

既有 `special_needs Text` **保留為自由備註欄**，不複製到結構化欄位。

### 4.2 Employee 表新增欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| `staff_role_category` | String(20) | `teacher_certified`/`educare_certified`/`assistant_educare`/`office`/`kitchen`/`driver`/`other` |
| `teacher_cert_no` | String(50) | 教師證/教保員證號 |
| `teacher_cert_type` | String(20) | 幼教師證/教保員證/助理教保員證 |

既有 `EmployeeCertificate` 子表保留為「所有證照清單」用；上述三欄專供政府申報「教保身分」用，唯一且結構化。

### 4.3 新增 4 張表（Phase 1 全部建立，內容遞延填）

#### `student_disability_documents`（Phase 1 用）
| 欄位 | 型別 |
|------|------|
| id PK, student_id FK | |
| doc_type | `鑑定證明` / `身障手冊` / `IEP` / `評估報告` / `其他` |
| file_path | String（既有 `attachments` 整合） |
| issued_date | Date |
| expiry_date | Date nullable |
| notes | Text |
| created_at, updated_at | DateTime |

#### `student_iep_records`（Phase 4 用，空殼先建以避免 Phase 4 migration 過重）
詳見 7.1。

#### `special_education_subsidies`（Phase 4 用，空殼）
詳見 7.2。

#### `monthly_enrollment_snapshots`（Phase 2 用，空殼）
詳見 5.1。

### 4.4 後台 UI 改動

- **`StudentForm.vue`**：新增「**政府申報資料**」摺疊區（預設摺疊），含上述 10 欄；身障類型/等級為下拉、鑑定文件附件區直接 inline 編輯
- **`EmployeeForm.vue`**：新增「**教保身分**」摺疊區，含上述 3 欄
- **學生詳情頁**：新「**鑑定文件**」分頁，列出 `student_disability_documents`
- **首頁 widget**：「身障鑑定到期提醒」— 顯示「N 名幼生鑑定 1 個月內到期」連結至清單頁

### 4.5 權限

新增兩個位元：
- `Permission.GOV_REPORTS_VIEW` — 看政府申報資料、預覽
- `Permission.GOV_REPORTS_EXPORT` — 執行匯出 Excel / 開立證明

既有 `gov_reports.py`（社保申報）權限對齊：實作時檢查並避免重複新增。

### 4.6 Migration 策略

- 一個 migration：所有 Student/Employee 欄位 + 4 張新表
- 不寫資料遷移腳本（既有 `special_needs Text` 不自動結構化）
- downgrade 完整可逆

### 4.7 測試

- pytest：欄位寫入/讀取、權限守衛、unique where not null、首頁 widget API
- vitest：兩個 form 新區塊渲染、欄位 v-model、必填驗證、widget 元件

---

## 5. Phase 2 設計：#9 月報匯出器

### 5.1 資料模型 `monthly_enrollment_snapshots`

| 欄位 | 型別 |
|------|------|
| id, year, month | PK + Int + Int |
| classroom_id | FK |
| age_group | `2-3` / `3-4` / `4-5` / `5-6` |
| total_count, male_count, female_count | Int |
| disadvantaged_count, disability_count, indigenous_count, foreign_count | Int |
| expected_attendance_days, actual_attendance_days, attendance_rate | Int / Float |
| snapshot_date | Date（產生時的計算基準日） |
| generated_at, generated_by | DateTime + String |

unique on `(year, month, classroom_id, age_group)`；重新產生覆寫但寫 audit log。

### 5.2 出席率算法

- **分母** = 本月「上課日數」× 本月「在園幼生數」
- **上課日數** = 工作日 − 國定假日（讀既有 holiday 表；若沒有則 Phase 2 內建立小 holiday 表 + 2026 假日資料）
- **分子** = 該班幼生本月實際到園日數總和（讀 `student_leave` 表反推：應到 − 請假 = 實到）
- 跨月加退園：按實際在園日比例計入

### 5.3 API

```
POST /api/gov_moe/monthly/generate
  body: { year, month }
  → 計算寫入 monthly_enrollment_snapshots（覆寫前留 audit）

GET  /api/gov_moe/monthly?year=&month=
  → 三維度資料：classroom_summary / student_detail / overview

GET  /api/gov_moe/monthly/export?year=&month=&format=xlsx
  → 下載 Excel（3 sheets）
```

### 5.4 Excel 三 Sheet 格式

**Sheet 1 — 班級總表**：`班級 | 教師 | 應到 | 實到 | 出席率 | 男 | 女 | 弱勢 | 身障 | 原民 | 外籍`

**Sheet 2 — 幼生明細**：`學號 | 姓名 | 身分證 | 班級 | 年齡層 | 在園日數 | 缺席日數 | 出席率 | 弱勢標記`

**Sheet 3 — 統計摘要**：總人數、各年齡層分布、弱勢/身障/原民/外籍占比

實作上 Phase 2 Excel 產生器與 Phase 3 共用同一份 `gov_moe/excel_writer.py`（見 6.3）。

檔名：`義華幼兒園_月報_2026-05_產生於2026-06-01.xlsx`

### 5.5 邊界處理

- **跨月加退園**：月中加入 → 從加入日起算；月中退園 → 算到退園日
- **追溯重算**：允許重新產生（覆寫），audit log 記錄「誰、何時、原始/新值」
- **班級異動**：採「該月最後一天」的班級分配（用既有 `StudentClassroomTransfer`）
- **無資料月份**：API 回 200 + 空 sheet（不報錯）

### 5.6 UI

新增 `/admin/gov-reports/monthly` 頁面：
- 月份選擇器（預設上月）
- 「產生/重算本月」按鈕（確認 modal 避免誤觸）
- 三個 tab：班級總表 / 幼生明細 / 統計摘要 預覽 table
- 「匯出 Excel」下載按鈕
- 頁尾提示：「對照 ece.moe.edu.tw → 幼生通報 → 月報」

### 5.7 測試

- 跨月加退、無資料月、班級異動、出席率邊界（0%、100%）、弱勢統計
- 重算覆寫 + audit log
- 整合：實際產生 → 匯出 → 開啟 Excel 驗證欄位

---

## 6. Phase 3 設計：#6 幼生 + #7 教保員 對照清單

兩個一起做（邏輯一致，共用 Excel writer）。

### 6.1 #6 幼生對照清單

對應 ece.moe.edu.tw「幼生管理」欄位順序。**包含本學期在園 + 本學期退/轉出者**（上學期已離不報）。

匯出欄位（1 sheet）：
```
學號 | 姓名 | 身分證統一編號 | 出生年月日 | 性別 | 國籍 |
監護人姓名 | 監護人關係 | 監護人身分證 | 監護人電話 |
戶籍地址 | 通訊地址 | 入園日期 | 班別 | 年齡層 |
低收/中低收 | 身障類型 | 身障等級 | 原住民族別 | 外籍標記
```

欄位標題加註灰色小字「對應網站：xxx」幫助業主對位。

API：
```
GET /api/gov_moe/students/export?period=2026-1&format=xlsx
GET /api/gov_moe/students/changes?since=&until=&format=xlsx
  → 期間異動清單（新進/離園/轉班/改名），讀既有 student_change_logs
  → 預設 since = today-14d, until = today
```

### 6.2 #7 教保員對照清單

對應「教保服務人員管理資訊系統」：
```
姓名 | 身分證 | 出生年月日 | 性別 | 聯絡電話 |
教保身分別 | 證號類型 | 教保員證/教師證號 |
最高學歷 | 主修 | 到職日 | 目前班級 | 在職狀態
```

API：
```
GET /api/gov_moe/staff/export?format=xlsx&include_resigned=false
GET /api/gov_moe/staff/changes?since=&until=&format=xlsx
  → 預設兩週
```

### 6.3 共用 Excel writer (`gov_moe_excel_writer.py`)

- 統一中文表頭、欄寬自動、凍結首列、空值顯示「-」
- 標題列加註「對應網站：xxx」灰色小字
- 命名規則：`義華幼兒園_{報表名}_{期間}_{產生日期}.xlsx`
- 共用權限守衛：`require(GOV_REPORTS_EXPORT)`

### 6.4 UI

`/admin/gov-reports/students` 與 `/admin/gov-reports/staff`：
- Tab 切換「**全部清單**」/「**期間異動**」
- 篩選器（在園/在職、班級、教保身分別）
- 預覽 table、「匯出 Excel」按鈕
- 頁尾說明對應到哪個政府網站入口

### 6.5 測試

- 幼生全名單匯出（含/不含已退園 + 本/上學期切分）
- 異動清單時間邊界（含起日、不含迄日次日）
- 教保員身分別 7 種都正確匯出對應中文
- 欄位順序穩定（snapshot 測試）

---

## 7. Phase 4 設計：#5 IEP + 特教加給 + 在學證明

### 7.1 IEP 個別化教育計畫

#### 7.1.1 資料模型 `student_iep_records`

| 欄位 | 型別 |
|------|------|
| id PK, student_id FK | |
| school_year | Int（如 2025） |
| semester | Int（1/2） |
| status | `draft` / `pending_review` / `approved` / `closed` |
| current_status | Text（語言/認知/動作/社會情緒 評估） |
| long_term_goals | Text |
| short_term_goals | JSON `[{goal, criteria, due_date, status}]` |
| mid_term_evaluation | Text |
| final_evaluation | Text |
| iep_team_members | JSON（家長/特教巡輔/治療師清單） |
| meeting_dates | JSON（初擬/期中/期末會議日期） |
| created_by_employee_id | FK |
| approved_by_employee_id | FK nullable |
| created_at, updated_at, deleted_at | DateTime |

unique on `(student_id, school_year, semester)`。

#### 7.1.2 API

```
GET    /api/gov_moe/iep?student_id=&school_year=&semester=
POST   /api/gov_moe/iep
PUT    /api/gov_moe/iep/{id}
PUT    /api/gov_moe/iep/{id}/submit       # → pending_review
PUT    /api/gov_moe/iep/{id}/approve      # → approved
POST   /api/gov_moe/iep/{id}/clone        # 複製到下學期
GET    /api/gov_moe/iep/{id}/export       # PDF（評鑑用 A4 制式）
```

#### 7.1.3 複製規則

`POST /iep/{id}/clone` body `{target_school_year, target_semester}`：
- 複製 `current_status`、`long_term_goals`、`short_term_goals`、`iep_team_members`
- **清空** `mid_term_evaluation`、`final_evaluation`、`meeting_dates`
- 新狀態強制 `draft`
- 若 `(target_school_year, target_semester)` 已有紀錄 → 422 error
- 寫 audit log

#### 7.1.4 UI

`/admin/gov-reports/iep`：
- 左：身障幼生清單（按學期篩選）+ 顯示「本學期 IEP 狀態」
- 右：多 tab 編輯（狀況評估 / 目標 / 評估 / 會議 / 附件）
- 工具列：複製上學期 / 提交審核 / 核准 / 匯出 PDF

### 7.2 特教加給 / 助理鐘點費

#### 7.2.1 資料模型 `special_education_subsidies`

| 欄位 | 型別 |
|------|------|
| id PK | |
| subsidy_type | `teacher_extra` / `assistant_hourly` |
| employee_id FK | 申領人 |
| related_student_ids | JSON `[int]`（服務的身障幼生） |
| period_start, period_end | Date |
| hours_or_rate | Float |
| amount_requested | Money |
| amount_approved | Money nullable |
| status | `draft` / `submitted` / `approved` / `paid` / `rejected` |
| applied_at, approved_at, paid_at | DateTime |
| approval_doc_path | String（核准公文掃描） |
| notes | Text |

#### 7.2.2 API

```
GET    /api/gov_moe/special_subsidies?employee_id=&period=&status=
POST   /api/gov_moe/special_subsidies
PUT    /api/gov_moe/special_subsidies/{id}
PUT    /api/gov_moe/special_subsidies/{id}/approve
PUT    /api/gov_moe/special_subsidies/{id}/mark_paid
GET    /api/gov_moe/special_subsidies/export?period=&format=xlsx
```

#### 7.2.3 UI

`/admin/gov-reports/special-subsidies`：
- 申領清單 table（status / 期間 / 員工 / 金額）
- 篩選：員工 / 期間 / 狀態
- 「新增申領」表單（多選身障幼生）
- 摘要區塊：本期申領總額、待核准筆數、已撥款總額

### 7.3 在學證明產生器

#### 7.3.1 通用模板（PDF）

A4 直式，含：
- 園所抬頭（從 config 讀，無則用「義華幼兒園」）
- 標題「在學證明書」
- 學生資料：姓名、學號、身分證、入園日期、目前班級
- 申請用途、開立日期、序號
- 章戳位（園長章、園所章）
- 編號：`EC-{year}-{seq}` 格式，`seq` 每年從 0001 起跳並 zero-pad 為 4 位（如 `EC-2026-0001`、`EC-2027-0001`），跨年自動重設

業主上線使用後再依實際需求調整模板（不在 design 階段定樣）。

#### 7.3.2 API

```
POST /api/gov_moe/enrollment_certificate/{student_id}/generate
  body: { issue_date, purpose, copies }
  → 回傳 PDF URL（並寫 audit log）
GET  /api/gov_moe/enrollment_certificate/history?student_id=&since=&until=
```

#### 7.3.3 UI

- 學生詳情頁右上加「**開立在學證明**」按鈕 → modal 輸入用途、份數 → 下載 PDF
- 集中頁 `/admin/gov-reports/certificates` 看所有歷史（誰、何時、給哪位學生、用途）

### 7.4 權限細節

- **IEP 編輯**：班導/副班導（限自己班級的身障幼生）；主任/admin 全部班級。外聘特教巡輔員不在系統內，由園內教師代為記錄
- **IEP 核准**：主任以上（用既有 `supervisor_role`）
- **特教加給 新增/核准**：admin 才可（影響金流）
- **在學證明 開立**：admin/組長都可（須 audit log）

### 7.5 測試

- IEP 草稿/提交/核准 flow
- IEP 複製規則：評估清空、status 強制 draft、重複學期回 422
- 特教加給 金額計算、status 流轉
- 在學證明 PDF 內容正確（章戳區、學生資料、序號遞增）
- audit log 完整

---

## 8. 共通設計

### 8.1 模組組織

新增 `api/gov_moe/` 子套件（與既有 `gov_reports.py` 區分，後者為社保申報，名稱不重複）：
```
api/gov_moe/
  __init__.py          # router 註冊
  monthly.py           # Phase 2
  students.py          # Phase 3 #6
  staff.py             # Phase 3 #7
  iep.py               # Phase 4
  special_subsidies.py # Phase 4
  certificates.py      # Phase 4
  excel_writer.py      # 共用 Excel 工具
```

前端 `src/api/govMoe.js`、`src/views/admin/gov-reports/` 子目錄。

### 8.2 稽核

利用既有 `AuditMiddleware`。所有 POST/PUT/DELETE 自動產生 AuditLog。**額外手動寫 audit**：
- 月報重算（生成 vs 覆寫前後值）
- 在學證明開立（記錄學生、用途、份數、開立人）
- 特教加給核准/撥款

### 8.3 Migration 策略

- **每個 phase 一個 migration**（4 個 migration）
- 每個 migration 完整可逆（downgrade 必須能還原欄位 + 表）
- 不寫資料遷移腳本（既有資料保留）
- 跑 `alembic upgrade heads` 前確認無多 head 衝突

### 8.4 Commit 策略

每個 Phase 至少 2 commit：
- `feat(backend): MOE reporting phase N - <subject>`
- `feat(frontend): MOE reporting phase N - <subject>`

Phase 4 視大小可再切（IEP / 特教加給 / 在學證明 各一組 commit）。

### 8.5 測試覆蓋目標

- 後端：每個 router 至少 5 個測試（happy path + 權限 + 邊界 + 重複/不存在 + 跨資料一致性）
- 前端：關鍵頁面 vitest（form、預覽 table、匯出按鈕）
- 整合驗證：phase 上線前用 Playwright 跑一遍主要流程

---

## 9. 風險與假設

| 項目 | 假設/風險 | 對策 |
|------|----------|------|
| 政府表單欄位 | 假設業主回報的欄位為準；可能與實際申報有差 | 欄位以 dict 配置化，方便後續調整 |
| 身分證為空舊資料 | 既有 Student 沒填身分證 | `unique where not null`、漸進補齊 |
| 既有 holiday 表 | Phase 2 出席率算法依賴 | 實作前確認；無則 Phase 2 內建小 holiday 表 |
| `student_change_logs` 結構 | Phase 3 #6 異動清單依賴 | 實作前確認該表能否滿足異動類型；不足則擴充 |
| `student_leave` vs `student_attendance` | 哪個是主來源 | 實作 Phase 2 前看程式碼確認 |
| Permission 位元 > 32-bit | 既有 IntFlag 已多 | 前端 BigInt（既有規範） |
| IEP PDF 模板 | 評鑑表單格式各縣市略異 | Phase 4 用通用樣板，上線後依實際回饋調整 |
| 在學證明章戳 | 業主章戳實體掃描檔尚無 | Phase 4 預留章戳位，業主上線後再掃描上傳 |

---

## 10. 開放問題

**無**。所有問題已於 brainstorming 階段確認：
- ✅ 路線：B（4 phase 漸進）
- ✅ id_number：unique where not null
- ✅ staff_role_category：7 類保留
- ✅ 首頁鑑定到期提醒：加
- ✅ 出席率分母：上課日 × 幼生數
- ✅ 月報排程：手動觸發
- ✅ #6 範圍：本學期在園 + 本學期退/轉出
- ✅ 異動清單：預設兩週
- ✅ IEP 複製：一律 draft + 評估清空
- ✅ 在學證明：通用模板先行

---

## 11. 後續步驟

1. 本 spec user review → 通過後呼叫 `writing-plans` skill 產生實作計畫
2. 實作計畫拆解到 phase 級別、每個 phase 內再分子任務
3. 依序：Phase 1（基礎）→ Phase 2（月報）→ Phase 3（對照清單）→ Phase 4（IEP/補助/證明）
4. 每 phase 完成走「後端 commit → 前端 commit → 整合驗證 → 業主驗收」
