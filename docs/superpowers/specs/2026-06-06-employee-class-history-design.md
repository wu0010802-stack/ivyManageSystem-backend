# 員工詳情「班級歷程」分頁 — 設計文件

- 日期：2026-06-06
- 範圍：跨前後端（ivy-backend 主導 + ivy-frontend 薄殼）
- 狀態：設計定案，待轉實作計畫

---

## 1. 目標與用途

在「員工詳情」彈窗新增一個 **「班級歷程」分頁**，呈現這位員工歷年（學年 × 學期）帶過哪些班級、擔任什麼角色、同班搭檔，以及該班級期初／期末人數與淨變化。

使用者確認的用途（決定了精細度與精準度要求）：

1. **教師帶班資歷紀錄** — 歷年帶過哪些班，作為年資 / 考核 / 升遷的參考
2. **了解班級規模變化** — 看每學期期初到期末的人數增減趨勢
3. **純資訊補充** — 不需要薪資 / 法規等級的精準度

> 結論：**帶班資歷主幹要可靠；人數走「資訊等級」即可，沒有可信資料寧可留白（「—」），不硬算可能算錯的數字。**

---

## 2. 範圍

### 納入
- 員工擔任 **導師（head_teacher）或助教（assistant_teacher）** 的班級歷程
- **當前 + 所有過去學期**，依 `(school_year, semester)` 由新到舊排序
- 每筆顯示：學年/學期、班級（年級）、角色、同班搭檔、期初→期末人數、淨變化

### 排除
- **才藝老師（art_teacher）不獨立成歷程列**（才藝通常跨班授課，不算「帶過這個班」）。但「同班搭檔」欄位**會顯示**該班的才藝老師。
- 不做「轉班回放」式的精準歷史人數重建（見 §6 資料正確性）。
- 不修「PUT 改學生班級不留異動紀錄」這個既有資料缺口（見 §7，獨立 finding）。

---

## 3. 資料來源與既有機制（已實證）

| 需求 | 來源 | file:line |
|------|------|-----------|
| 班級綁學期 | 每學期同一班是獨立 `Classroom` row，欄位 `school_year` / `semester` | `models/classroom.py:80,87,93` |
| 師班綁定 | `Classroom.head_teacher_id` / `assistant_teacher_id` / `art_teacher_id`（皆 FK→employees，nullable，有 index） | `models/classroom.py:102-110` |
| 班級唯一性 | `UniqueConstraint(school_year, semester, name)` | `models/classroom.py:122` |
| 當前學期判定 | `resolve_current_academic_term()`（今天日期的純函式，不讀 is_current） | `utils/academic.py:54` |
| 學期固定邊界 | `term_bounds(school_year, semester)` → 上 8/1~隔年1/31、下 2/1~7/31 | `utils/academic.py:39` |
| 當前班即時在籍數 | `classroom_student_count_map(session, target_date)`（按當前 `Student.classroom_id` 分組，對**當前班準確**） | `services/student_enrollment.py:27` |
| 當期人數快照 | `MonthlyEnrollmentSnapshot`（year, month, classroom_id, age_group → 人數細分） | `models/gov_moe.py:150` |
| 員工詳情輸出 | `_format_employee_response` 目前只回當前 `classroom_id` / `classroom_name` | `api/employees.py:73-155` |

### dev DB 現況（2026-06-06 實測，僅供理解資料密度，非 prod）
- `classrooms` 11 筆（全在 114-2 單一學期）、`students` 176、`monthly_enrollment_snapshots` **僅 2 筆**、`student_classroom_transfers` 14、`student_change_logs` 15。
- 推論：**當期人數快照在實務上很稀疏** → 多數過去學期人數會是「—（資料不足）」，符合「接受資料不足」的決策。

---

## 4. 後端設計（ivy-backend）

### 4.1 新端點
```
GET /api/employees/{employee_id}/class-history
```
- 權限：沿用員工詳情讀取權限 **`EMPLOYEES_READ`**（admin / HR；教師端 `hasPermission` 短路 false，不開放）。
- 回傳：依 `(school_year desc, semester desc)` 排序的歷程列陣列。

### 4.2 Response schema（Pydantic，新增於 `schemas/employees.py`）
```python
class ClassHistoryCoTeacher(BaseModel):
    role: Literal["head", "assistant", "art"]
    employee_id: int
    name: str

class ClassHistoryRow(BaseModel):
    school_year: int                 # 民國學年，例 114
    semester: int                    # 1=上學期 2=下學期
    classroom_id: int
    classroom_name: str              # "蘋果班"
    grade_name: str | None           # "中班"
    role: Literal["head", "assistant"]   # 此員工在這班的角色（才藝不成列）
    co_teachers: list[ClassHistoryCoTeacher]  # 同班其他老師（含才藝）
    is_current: bool                 # 是否為當前學期的班
    start_count: int | None          # 期初人數；None=資料不足
    end_count: int | None            # 期末人數；當前學期=即時在籍數；None=資料不足
    end_count_is_live: bool          # True 時前端顯示「目前 N」
    net_change: int | None           # end-start，兩者皆有才算

class ClassHistoryResponse(BaseModel):
    rows: list[ClassHistoryRow]
```

### 4.3 主幹查詢（可靠）
1. 撈 `Classroom` where `head_teacher_id == employee_id OR assistant_teacher_id == employee_id`（**不含** art）。
2. 排序 `(school_year desc, semester desc)`。
3. 角色判定：該 row 的 `head_teacher_id == id` → `head`；否則 `assistant`。
   - 邊角：若同一員工在同一班同時是 head 與 assistant（資料異常），以 `head` 為準。
4. 同班搭檔：讀同一 row 的另外三個 teacher FK（head/assistant/art），排除自己，resolve 員工姓名（一次批次 query 避免 N+1）。
5. 年級名：join `grade`（沿用 `_format_employee_response` 既有 `班名 (年級名)` 來源邏輯，`api/employees.py:384-396`）。

### 4.4 人數（資訊等級，誠實留白）
對每一歷程列：

- **判斷是否當前學期班**：`(school_year, semester) == resolve_current_academic_term()`。
- **當前學期班**：
  - `end_count` = 即時在籍數（`classroom_student_count_map` 取該 classroom_id，對當前班準確），`end_count_is_live = True`。
  - `start_count` = 該班「開學月」的 `MonthlyEnrollmentSnapshot` 加總（跨 age_group），有就填、無則 `None`。
- **過去學期班**：
  - `start_count` = 開學月快照；`end_count` = 期末月快照；任一無則該欄 `None`，`end_count_is_live = False`。
- **月份對應**：一律以 `term_bounds(school_year, semester)` 回傳的 `(start_date, end_date)` 為準，各取其「西元年-月」去查 `MonthlyEnrollmentSnapshot`（該表 key 為西元 year/month）。**不自行硬算民國→西元換算**，交給 `term_bounds` 處理，避免上學期跨年（8 月在本西元年、隔年 1 月在次西元年）算錯。
  - 上學期：開學月 = `start_date` 的年月（8 月）、期末月 = `end_date` 的年月（隔年 1 月）。
  - 下學期：開學月 = 2 月、期末月 = 7 月（同一西元年）。
- `net_change` = `end_count - start_count`（兩者皆非 None 才算，否則 None）。
- **不做轉班回放**：不使用 `StudentClassroomTransfer` 回放歷史歸屬（見 §6 為何會算錯）。

### 4.5 pytest（`tests/test_employees_class_history.py`）
- 主幹：含 head 班、含 assistant 班、**排除 art 班**；多學期排序由新到舊。
- 角色判定正確（head vs assistant；head 優先的邊角）。
- 同班搭檔：含才藝、排除自己、姓名正確、無搭檔時為空陣列；驗證無 N+1（query 次數上界）。
- 人數三態：
  - 當前學期班 → `end_count_is_live = True` 且等於即時在籍數。
  - 過去學期有快照 → 讀到快照值。
  - 過去學期無快照 → `start_count/end_count/net_change` 皆 None。
- `net_change` 僅在兩數皆有時計算。
- 權限：無 `EMPLOYEES_READ` → 403。
- 空歷程：沒帶過任何班 → `rows: []`。

> SQLite 測試注意：本功能不碰 `permission_names.contains` 這類 PG/SQLite 分歧路徑，可用既有 SQLite in-memory 測試框架；`MonthlyEnrollmentSnapshot` 為一般欄位，SQLite 可建表測。

---

## 5. 前端設計（ivy-frontend，薄殼）

- **`EmployeeView.vue`**：在「出勤紀錄」分頁（`:1259-1287`）之後新增 `<el-tab-pane label="班級歷程" name="classHistory">`。
- 沿用既有 **lazy-load + `loadedTabs` 快取** 模式：
  - `onDetailTabChange`（`:610-621`）加 `else if (name === 'classHistory') await fetchClassHistory()`。
  - 新增 ref `classHistory` + `fetchClassHistory()`（比照 `:594-608`）。
  - `handleDetail`（`:732-734`）重置區清空該 ref。
- **`src/api/employees.ts`**：新增 `listEmployeeClassHistory(id)` → `GET /employees/{id}/class-history`，用 OpenAPI 型別（`import type { AxiosResp } from './_generated/typed'`）。
- **el-table 六欄**：
  1. 學年/學期（當前列加「現在」tag）
  2. 班級（年級）
  3. 角色（導師=藍 tag / 助教=紫 tag）
  4. 同班搭檔（`助教 李美 · 才藝 陳華` 文字串接）
  5. 期初 → 期末（`end_count_is_live` 時顯示「目前 N」；None 顯示「— 資料不足」）
  6. 淨變化（▲綠 +N / ▼紅 -N / —）
- **空狀態**：沒帶過班時顯示正向空狀態（比照既有 tab）。
- 後端改 router 後跑 `dump_openapi.py` + `npm run gen:api`，只 commit 前端 `schema.d.ts`。

---

## 6. 資料正確性說明（為何人數只能資訊等級）

歷史學期的精準人數**無法可靠重建**，原因（已實證）：

1. **初次入學分班不寫 `StudentClassroomTransfer`**（`create_student`，`api/students.py:853`）— transfer 表本身不完整。
2. **PUT 改班不寫 transfer 也不寫 change log**（`update_student`，`api/students.py:967`）— 靜默改班（見 §7）。
3. **transfer 回放在「事後查過去學期」會 fallback 現態**（`enrollment_rates.py:115` / `monthly_calculator.py:104`）— 對「快照日期之後才被搬走的學生」會把人算到錯的班，是**錯誤而非近似**。

年終模組之所以可信，是因為它在**學期當下** compute-and-store（那時 fallback 現態 = 正確班），不是事後回查。我們的情境是事後回查，故：

- 採 **contemporaneous snapshot**（`MonthlyEnrollmentSnapshot`）作為過去學期人數來源 —— 它是當月寫入的真實快照。
- 當前學期班的即時在籍數準確（當前 `classroom_id` 分組對當前班正確）。
- 沒有快照就 `—`。**不引入會算錯的回放邏輯。**

---

## 7. 範圍外 finding（記錄，不在本次修）

**PUT `update_student` 改 `classroom_id` 不留任何異動紀錄**（`api/students.py:967-969`：直接 `setattr`，只記 AuditLog diff，不寫 `StudentClassroomTransfer`、不寫 `StudentChangeLog`）。這是歷史班級歸屬無法精準回放的根因之一。

- 影響：任何「歷史班級人數 / 學生軌跡」回放都對此類改班隱形。
- 建議（未來獨立工作）：讓所有 `classroom_id` 變更統一落一筆 `StudentChangeLog`（轉入/轉出，帶 school_year+semester+classroom_id 錨點），使歷史可重建。
- **本次不處理**，本功能以「現有資料 + 誠實留白」運作。

---

## 8. 交付與收尾

- 後端分支：`feat/employee-class-history-2026-06-06-be`（worktree `.claude/worktrees/employee-class-history`，自 origin/main `f29850cf`）。
- 前端分支：實作階段另開 `feat/employee-class-history-2026-06-06-fe`（自 origin/main）。
- 前後端 **分開 commit**；後端先行（router + pytest 綠）→ 前端接上 → `gen:api:check` 無漂移。
- 整合驗證：`start.sh` 起兩端，實際開一位有帶班的員工詳情點「班級歷程」。
- 收尾：依 workspace §收尾紀律，完成 = push + CI 綠 + worktree remove。
