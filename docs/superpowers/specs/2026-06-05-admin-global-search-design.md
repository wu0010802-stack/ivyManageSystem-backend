# 後台全域搜尋（完整版）— 設計文件

- 日期：2026-06-05
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 狀態：設計待實作

## 1. 背景與目標

後台目前已有 Ctrl+K 全域搜尋（`ivy-frontend/src/components/GlobalSearch.vue`），但它是「半成品」拼湊而成：

- **員工**：client-side 過濾 `employeeStore.employees`（依賴整份員工清單已載入）。
- **公告**：`getAnnouncements({})` 把**整包公告載回前端**再 client-side `includes` 過濾。
- **頁面**：**寫死 17 個** `ALL_PAGES`（系統實際有 100+ 路由，缺才藝 / 學費 / 接送 / 政府申報 / 考核年終 等一大票）。
- **學生**：唯一走後端 `getStudents({search})` 的類別。
- **點擊只跳列表頁**（例如點某學生 → 跳 `/students`，不會跳到該學生檔案頁 `/students/profile/:id`）。
- **只涵蓋 4 類**（員工 / 學生 / 公告 / 頁面）。

對照之下，教師端 Portal 反而有正規的後端跨實體搜尋 `api/portal/search.py`（跨 5 實體、逐類權限/班級 scope 過濾、PII 遮罩、寫稽核 log），做得比後台完整。

**目標**：把後台全域搜尋做「完整」——涵蓋 8 類實體 + 頁面導航、後端逐類權限把關、點擊跳到該筆紀錄，並把上述「權限只在前端擋 / 公告整包載回前端 / 頁面寫死」三個既有問題一併解掉。

**使用者已確認的決策**：
- 主軸＝擴充後台 Ctrl+K 全域搜尋（不動教師端 Portal 搜尋）。
- 涵蓋範圍＝核心對象（人）+ 行政物件，共 **8 類**：學生、員工、家長/監護人、班級、學費繳費紀錄、才藝報名、招生紀錄、公告，外加**頁面導航**。
- 架構＝**方案 A**：新增一支正規後端搜尋 endpoint（仿 `portal/search.py`），前端重寫面板消費它。
- 跳轉行為＝**跳列表頁 + 帶關鍵字預篩**；學生跳檔案詳情頁、家長跳其綁定學生的檔案頁。
- 終態學生 / 離職員工**預設不出現**在全域搜尋（查歷史走各自頁面的進階篩選）。

## 2. 既有現況（探查結論）

### 2.1 後端各列表的搜尋能力

| 實體 | 列表端點 | 已有模糊搜尋 | 搜尋欄位 | 出處 |
|------|---------|------------|---------|------|
| 學生 | `GET /students` | ✅ | `name` / `student_id` / `parent_name`（ilike） | `api/students.py:445-516` |
| 員工 | `GET /employees` | ✅ | `name` / `employee_id`（ilike） | `api/employees.py:272-286` |
| 家長 | — | ❌（無獨立列表，僅 `/students/{id}/guardians`） | — | `api/students.py:1512` |
| 班級 | `GET /classrooms` | ❌ | 僅 school_year/semester 等過濾 | `api/classrooms.py:451-487` |
| 學費 | `GET /fees/records` | ⚠️ 部分 | `student_name` / `classroom_name`（分開參數） | `api/fees/records.py:64-110` |
| 才藝報名 | `GET /activity/registrations` | ✅ | `_build_registration_filter_query()` 內搜 | `api/activity/registrations.py:276-324` |
| 招生 | `GET /recruitment/records` | ✅ | `keyword`：`child_name` / `address` / `notes` / `parent_response`（ilike） | `api/recruitment/records.py:59-94` |
| 公告 | `GET /announcements` | ❌ | 僅 page/page_size | `api/announcements.py:140-189` |

> 結論：班級、公告**無搜尋能力**；家長、學費**無統一搜尋**。本設計**不去逐一改既有列表端點**，而是在新的搜尋 endpoint 內**自組各實體 query**（避免擴散副作用、避免動到正在被多處消費的列表 API）。

### 2.2 範本 `api/portal/search.py`（要仿照的成熟 pattern）

- 端點 `GET /api/portal/search?q=`，輸入 `min_length=0/max_length=100`，但**實際資料 ≥ 2 字才回**。
- 一次查 5 實體（students / guardians / messages / contact_book / announcements），各限 5 筆。
- **班級 scope**：`is_unrestricted(user)` → 不過濾；否則 `_get_teacher_classroom_ids()` 取班級清單，空清單則各查詢跳過；每條 query 加 `Student.classroom_id.in_(classroom_ids)`。
- **PII 遮罩**：guardian 用 `mask_phone()`；訊息/聯絡簿用 `_strip_html()` 剝 HTML、截斷 snippet。
- **稽核**：結尾 `write_explicit_audit(action="READ", entity_type="portal_search", summary=..., changes={q, result_counts})`——因為回傳跨班/跨人 PII，敏感 GET 須顯式補審計（一般 middleware 只審寫操作）。
- response 為扁平 dict（`{q, students:[...], guardians:[...], ...}`）。

### 2.3 前端導航目標（每類點擊後跳哪）

| 實體 | 詳情頁 | 列表頁 | 列表頁讀 query 現況 |
|------|-------|-------|-------------------|
| 學生 | ✅ `/students/profile/:id`（`route.query`：from / classroom_id / tab） | `/students`（StudentWorkbenchView） | StudentListPanel **已支援** `search` |
| 員工 | ❌ | `/employees`（EmployeeHubView，query `section`） | 列表支援 search，但 query→filter 串鏈待接 |
| 家長 | ❌（透過學生檔案頁看） | — | 導航到綁定學生 `/students/profile/:student_id` |
| 班級 | ❌（Modal 編輯） | `/classrooms`（ClassroomView） | 無 query 串鏈 |
| 學費 | ❌ | `/fees`（StudentFeeView） | 無 query 串鏈 |
| 才藝報名 | ❌（Modal 詳情） | `/activity/registrations`（ActivityRegistrationView） | 無 query 串鏈 |
| 招生 | ❌ | `/recruitment`（RecruitmentView） | 無 query 串鏈 |
| 公告 | ❌（Modal 詳情） | `/announcements`（AnnouncementView） | 無 query 串鏈 |

> 只有「學生」有真正的詳情頁；其餘 7 類點擊跳列表頁。學生工作台已有 `route.query` 預篩範本可複製。

## 3. 架構決策

### 決策 A：新增獨立搜尋 endpoint `GET /api/search?q=`，不動既有列表 API

新建 `ivy-backend/api/search.py`（單檔；不大），於 `main.py` 註冊 router。一次查 8 類、各自組 query，逐類做權限把關。理由：

- 安全集中於後端（取代「前端 filter 可被繞過」）。
- 單次往返（取代前端併發 8 支 list API）。
- 不擴散到既有列表端點（班級/公告無搜尋、學費未統一），避免動到被多處消費的 API。
- 完全沿用 `portal/search.py` 已驗證的權限/scope/遮罩/稽核 pattern。

### 決策 B：逐類權限把關 + 沿用既有 READ 權限（不新增任何權限）

endpoint 進入點用 staff-only 守衛（非家長角色）。**每一類實體各自檢查對應 READ 權限**，沒有就**完全不查、回空陣列**：

| 類別 | 需要權限 |
|------|---------|
| 學生 | `STUDENTS_READ` |
| 員工 | `EMPLOYEES_READ` |
| 家長 | `GUARDIANS_READ` |
| 班級 | `CLASSROOMS_READ` |
| 學費 | `FEES_READ` |
| 才藝報名 | `ACTIVITY_READ` |
| 招生 | `RECRUITMENT_READ` |
| 公告 | `ANNOUNCEMENTS_READ` |

權限判定用既有 helper（後端 `utils/permissions` 的 `has_permission` / scope 解析）。**不新增 `Permission` enum 值、不動前端 `PERMISSION_NAMES`**——沿用既有 READ 權限即可，因此本功能**無 PII denylist / 權限同步面**的跨端變更。

### 決策 C：班級 scope 沿用 `is_unrestricted` + classroom 過濾（fail-safe）

後台 layout 對 teacher 角色本就擋住，但 endpoint 層仍 fail-safe：非 `is_unrestricted(user)` 時，學生/家長/學費/才藝等「綁學生」的類別套 `accessible_classroom_ids()` 過濾（沿用 `utils/portfolio_access.py`）。終態學生比照 portal 排除。

### 決策 D：終態學生 / 離職員工預設不納入

- 學生查詢預設 `is_active == True` 且非終態（GRADUATED / WITHDRAWN / TRANSFERRED），與 portal 一致。
- 員工查詢預設在職（依 `employees.py` list 端點既有的在職判定欄位；以該欄位為準，不自創）。
- 要查歷史走各自頁面的進階篩選（本功能不處理）。

### 決策 E：稽核——比照 portal 寫 explicit audit

結尾 `write_explicit_audit(action="READ", entity_type="admin_global_search", summary=f"後台全域搜尋（q={q[:32]}）", changes={q[:64], result_counts})`。理由同 portal：回傳跨人 PII 的敏感 GET 須留軌跡。

### 決策 F：前端頁面清單改為「自動產生」

取代寫死的 17 個 `ALL_PAGES`。來源優先序：
1. 若側邊欄選單（`AdminSidebar`）已有結構化的 `{label, path, permission}` 清單 → 重用它（單一真相）。
2. 否則從 router 的 routes（含 meta.title）產生。

每筆套 `canAccessRoute(path)` 過濾（沿用既有 `src/utils/auth.ts`）。**實作時先確認** AdminSidebar 是否導出可重用的選單定義（決定走 1 或 2）。

## 4. API 契約

### Request
```
GET /api/search?q=<string>
```
- `q`：trim 後 **< 2 字** → 回各類空陣列（不查 DB）。
- 守衛：staff-only（非家長）。

### Response（Pydantic `response_model`，供 gen:api 產型別）

```jsonc
{
  "q": "王",
  "students": [
    { "id": 12, "name": "王小明", "student_id": "S114001", "classroom_name": "向日葵班" }
  ],
  "employees": [
    { "id": 5, "name": "王老師", "employee_id": "E007", "job_title": "教師" }
  ],
  "guardians": [
    { "id": 8, "name": "王大華", "phone_masked": "09**-***-678", "child_name": "王小明", "student_id": 12 }
  ],
  "classrooms": [
    { "id": 3, "name": "向日葵班", "school_year": 114, "semester": 1 }
  ],
  "fees": [
    { "record_id": 99, "student_name": "王小明", "classroom_name": "向日葵班", "period": "114-09", "status": "unpaid" }
  ],
  "activity_registrations": [
    { "id": 41, "student_name": "王小明", "course_name": "美術課", "status": "approved" }
  ],
  "recruitment": [
    { "id": 7, "child_name": "王小寶", "status": "visited", "target_school_year": 115 }
  ],
  "announcements": [
    { "id": 2, "title": "親師座談會通知", "created_at": "2026-06-01T09:00:00+08:00" }
  ]
}
```

- 各類**最多 8 筆**（per-category limit；常數可調）。
- 無權限的類別 → **空陣列**（前端該區塊自然不顯示）。
- 各 item 欄位**精簡**只放「列表呈現 + 導航所需」；不回完整 PII（家長電話已遮罩，學費不回金額）。
- 確切欄位以實作時各 model 可取得者為準（探查已確認 model 有對應欄位；缺欄位時退化為可得最近欄位並於 PR 註記）。

## 5. 前端設計

### 5.1 新 api wrapper `src/api/search.ts`
```ts
import type { AxiosResp } from './_generated/typed'
export function globalSearch(q: string) {
  return api.get('/search', { params: { q } }) // 回 AxiosResp<Schema...>
}
```
（型別走 OpenAPI codegen，後端加 `response_model` 後 `npm run gen:api` 自動下放。）

### 5.2 重寫 `GlobalSearch.vue`
- 單一 debounce（300ms）呼叫 `globalSearch(q)`；`< 2 字` 不打 API。
- 渲染至多 8 個實體區塊 +「頁面」區塊；無結果 / 無權限的區塊不顯示。
- 鍵盤導航（↑↓ / Enter / Esc）沿用既有 flatItems 串接邏輯，擴成 9 區塊。
- placeholder：「搜尋學生、員工、家長、班級、學費、才藝、招生、公告、頁面…」。
- 高亮沿用 `src/utils/highlight.ts`。
- **頁面區塊**：依決策 F 自動產生 + `canAccessRoute` 過濾。

### 5.3 `select(item)` 導航對應
| type | 導航 |
|------|------|
| student | `/students/profile/:id` |
| guardian | `/students/profile/:student_id` |
| employee | `/employees?section=employees&search=<name>` |
| classroom | `/classrooms?search=<name>` |
| fees | `/fees?search=<student_name>` |
| activity | `/activity/registrations?search=<student_name>` |
| recruitment | `/recruitment?keyword=<child_name>` |
| announcement | `/announcements?search=<title>` |
| page | `router.push(path)` |

### 5.4 列表頁「讀 query 預篩」串鏈（逐頁補）
學生工作台已支援。其餘列表頁各補一小段：`onMounted` / `watch(route.query)` 讀對應 query key → 設定該頁既有的搜尋 filter 值並觸發查詢。逐頁確認 query key 與既有 filter 欄位對齊（部分頁有 tab/section，需先切到正確分頁）。**此為最瑣碎、最易漏的部分，逐頁列入實作 checklist。**

## 6. 測試計畫

### 後端 `tests/test_search.py`
- 字元門檻：`q` < 2 字 → 全空、不查 DB。
- 逐類權限把關：持 `STUDENTS_READ` 但無 `FEES_READ` → 有學生結果、學費為空；反之亦然（逐類至少各一案）。
- 班級 scope：非 unrestricted 角色只回自己班級的學生/家長。
- 終態：終態學生不出現。
- PII：guardian 回傳的是 `phone_masked`，非原始電話。
- 稽核：搜尋後 audit 表多一筆 `admin_global_search` READ，changes 含 q 與各類筆數。
- 跨類 limit：每類最多 8 筆。

### 前端 vitest
- `GlobalSearch.vue`：mock `globalSearch` 回多類結果 → 各區塊渲染；空權限類別不顯示；鍵盤導航跨區塊；`< 2 字` 不呼叫 API；`select` 各 type 導航 path 正確。
- 列表頁讀 query 預篩：至少對「學生以外」新接的頁各一個 `route.query.search` → filter 生效的測試。

## 7. 風險 / 邊界

1. **列表頁 query→filter 串鏈**：6 個列表頁要各接一段，且部分有 tab/section，最易漏 → 逐頁 checklist + 各一個測試。
2. **契約改動須跑 codegen**：後端加 `response_model` 後跑 `python scripts/dump_openapi.py` + 前端 `npm run gen:api`，只 commit `schema.d.ts`（`openapi.json` 不入 repo）。
3. **N+1**：學生 classroom_name、家長 child_name 等用 join / 一次性 in-query 取得，勿逐筆查（仿 portal）。
4. **效能**：8 條 query 各 limit 8 + 前端 debounce；輸入 < 2 字短路不打 DB。
5. **PII**：家長電話遮罩、學費不回金額；新增 entity 欄位時若涉敏感欄位，比照遮罩。
6. **前後端分開 commit**（跨 repo SOP），後端先（含 migration？本功能**無 migration**、無 schema 變更）、前端後（含 gen:api）。
7. **教師不受影響**：teacher 走 Portal 搜尋，後台 endpoint 對其 fail-safe（scope 過濾 + 後台 layout 本就擋住）。

## 8. 不做的事（YAGNI）

- 不動教師端 `portal/search.py`。
- 不為家長 / 班級等新增「詳情頁」（跳列表頁 + 預篩即可）。
- 不做模糊比對 / typo 容錯（沿用 ilike 子字串）。
- 不做搜尋歷史 / 熱搜排行 / 個人化。
- 不改既有列表端點的搜尋參數（在新 endpoint 內自組 query）。
- 不新增權限、不動 PII denylist。

## 9. 落地順序（給後續 plan）

1. 後端：`api/search.py` + Pydantic response schemas + `main.py` 註冊 + `tests/test_search.py`，`pytest` 綠。
2. 後端：`dump_openapi.py`；前端 `gen:api` 更新 `schema.d.ts`。
3. 前端：`src/api/search.ts` + 重寫 `GlobalSearch.vue` + 頁面清單自動產生 + vitest。
4. 前端：逐頁接「讀 query 預篩」串鏈 + 各一測試。
5. 整合驗證：`start.sh` 起兩端，實際 Ctrl+K 點一輪 8 類 + 導航。
6. 前後端分開 commit（feature branch 各一支，off origin/main）。
