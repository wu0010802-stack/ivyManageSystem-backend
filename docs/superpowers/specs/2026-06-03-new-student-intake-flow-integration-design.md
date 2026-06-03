# 新生招生流程整合設計：訪視 → 預繳 → 暫定編班 → 註冊 → 實際分班

- **日期**：2026-06-03
- **狀態**：Design（待轉 implementation plan）
- **範圍**：跨前後端（ivy-backend 契約先行 + ivy-frontend 接上）
- **關聯**：
  - 既有招生漏斗 Phase A：`docs/superpowers/specs/2026-05-22-recruitment-funnel-phase-a-design.md`
  - 逐生在校歷程：`docs/superpowers/specs/2026-05-29-student-lifecycle-tracking-panel-design.md`
  - 永久學號編號：`docs/superpowers/specs/2026-06-01-id-numbering-scheme-design.md`

---

## 1. 背景與現況

招生漏斗（Phase A）已落地 4 階段狀態機與統計儀表板：

| 階段 | 資料表示 | 是否有 Student 記錄 |
|------|----------|---------------------|
| `visited`（訪視） | `recruitment_visits`，`has_deposit=False` | 否 |
| `deposited`（預繳） | `recruitment_visits`，`has_deposit=True` | 否 |
| `enrolled`（已註冊/報到） | 呼叫 `convert_recruitment_to_student()` 建立 `Student`（`lifecycle≠active`） | 是 |
| `active`（開學） | `Student.lifecycle_status='active'` | 是 |

現況的關鍵限制：**要進班級就必須先轉成正式 Student**（`enrolled` 階段）。沒有任何方式能在「預繳、尚未註冊」時先替孩子保留班級名額。

其它已查證事實：
- `lifecycle_status='prospect'` 為掛著但無人寫入的狀態；轉換服務只允許建在 `enrolled`/`active`。所以「訪視、預繳」階段**完全沒有 Student 記錄**，純活在 `recruitment_visits`。
- 漏斗看板（`api/recruitment/funnel.py`）與統計（`api/recruitment/stats.py`、`services/analytics/funnel_service.py`）**只讀 `recruitment_visits`**；外部「義華校官網」同步進 `recruitment_ivykids_records` 的資料僅餵熱點地圖，未進漏斗或統計。
- `classrooms` 已有 `capacity`（預設 30）、`grade_id`（FK→`class_grades`）、`school_year`、`semester`。
- `class_grades` 為乾淨年級表：`id / name / age_range / sort_order / is_graduation_grade`。
- `recruitment_visits.grade` 目前是 `String(20)` 自由填寫的「適讀班級」，**非 FK**。
- 系統**目前沒有任何「招生名額目標」概念**。
- `convert_recruitment_to_student()`（`services/recruitment_conversion.py:87`）寫死以 `resolve_current_academic_term()` 取**當前學年**配 `enrollment_school_year` + `enrollment_seq`。永久學號以 `(enrollment_school_year, enrollment_seq)` 為鍵（見 id-numbering spec），因此下學年新生若用此服務轉換會配到錯誤學年。

## 2. 招生節奏前提（決定資料模型的根本）

本園**以招「下學年新生」為主，且暫定編班的當下、下學年的班級尚未在系統建立**。

推論：保留座位**不能綁具體班級 row**（明年的「中班A」還不存在），必須綁 **年級 + 目標學年**。真正分到「中班A / 中班B」是等下學年班級建好後，走既有的 `POST /students/bulk-transfer` 完成。

## 3. 目標 / 非目標

### 目標
1. 讓「有預繳、尚未註冊」的孩子能被行政**暫定編班 = 保留一席「目標學年 × 年級」名額**（純保留，不建 Student、不進名冊、不點名、不收費）。
2. 提供**名額規劃面板**：以「目標學年 × 年級」為單位顯示 `計畫名額 / 已保留 / 已註冊 / 剩餘`，超收提醒。
3. 修正轉換服務，使下學年新生「註冊」時 `enrollment_school_year` 正確（用目標學年），`classroom_id` 暫空＝「已註冊・待實際分班」。
4. 漏斗看板「預繳」欄卡片可視化暫定編班狀態，並提供編班入口。

### 非目標（本次不做）
- **官網報名整合**：外部義華官網同步資料進漏斗/統計，列為未來 Phase。
- **班級層級 A/B 分班**：仍走既有 `bulk-transfer`，不在本次重做。
- **自動從班級容量推算計畫名額**：本次由 admin 手動設定計畫名額（見決策 D3）。未來可在下學年班級建好後改為自動加總（列未來工作）。
- 不新增 Permission（複用既有 `RECRUITMENT_*`），避免前後端權限字串集合同步負擔。

## 4. 概念模型

把整條招生線拆成兩件正交的事：

- **承諾階梯（漏斗階段）**：`訪視 → 預繳 → 註冊 → 開學`，維持既有 4 階段不動。
- **編班（橫切關注點）**：可在兩個時間點發生：
  - **預繳後、註冊前**：行政「暫定編班」＝在訪視記錄上掛 `provisional_grade_id + target_school_year`，**只保留年級名額**。
  - **註冊時**：`convert_recruitment_to_student()` 用 `target_school_year` 建 Student（`classroom_id=NULL`＝待分班）。下學年班級建好後走既有 `bulk-transfer` 實際分到 A/B 班。

「標記」即暫定編班動作本身（指定年級＋學年＝標記為「確定就讀、待編入該年級」），無額外獨立旗標（決策 D1）。

## 5. 資料模型變更

一支 alembic migration（建議 revision slug：`nsintake01`）。

### 5.1 `recruitment_visits` 新增三欄
| 欄位 | 型別 | 說明 |
|------|------|------|
| `provisional_grade_id` | `Integer` FK→`class_grades.id`，nullable，`ON DELETE SET NULL` | 暫定年級（保留座位用） |
| `target_school_year` | `Integer`，nullable | 目標學年（民國，如 115） |
| `target_semester` | `Integer`，nullable，default 1 | 目標學期（1=上學期，新生預設） |

新增索引：`(target_school_year, target_semester, provisional_grade_id)` 供名額彙總查詢使用（避免序列掃描 / N+1）。

### 5.2 新表 `grade_intake_targets`（計畫名額來源）
| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | PK | |
| `grade_id` | `Integer` FK→`class_grades.id`，`ON DELETE CASCADE` | 年級 |
| `school_year` | `Integer`，not null | 目標學年（民國） |
| `semester` | `Integer`，not null，default 1 | 學期 |
| `target_seats` | `Integer`，not null，default 0 | 計畫招生名額 |
| `created_at` / `updated_at` | `DateTime` | 台北時間 |

唯一鍵：`UNIQUE(grade_id, school_year, semester)`。

### 5.3 downgrade
完整移除上述兩處（drop 索引 → drop columns → drop table）。

## 6. 後端 API 契約

所有路徑沿用既有 `/recruitment` 前綴與 router 注入慣例。

### 6.1 暫定編班（設定 / 清除保留座位）
```
POST /recruitment/funnel/visits/{visit_id}/reserve-seat
權限：RECRUITMENT_WRITE
Request:
  {
    "provisional_grade_id": int | null,   // null = 釋放保留
    "target_school_year": int | null,     // 設定時必填
    "target_semester": int | null         // 省略則預設 1
  }
Response: 更新後的 visit 摘要（含 grade_name / target_school_year）
```
守衛：
- 設定（`provisional_grade_id` 非 null）時 **`visit.has_deposit` 必須為 True**，否則 `400`（未預繳不可保留座位）。
- 釋放（`provisional_grade_id=null`）一律允許。
- 寫 `recruitment_event_log`：`event_type='seat_reserved'`／`'seat_released'`，`metadata_json={grade_id, school_year, semester}`（非階段轉換，`from_stage/to_stage` 留同值或 null）。

### 6.2 名額規劃彙總
```
GET /recruitment/intake-plan?school_year=115&semester=1
權限：RECRUITMENT_READ
Response:
  {
    "school_year": 115, "semester": 1,
    "rows": [
      {
        "grade_id": 3, "grade_name": "中班",
        "target_seats": 30,
        "reserved_count": 12,   // 已保留、未註冊
        "enrolled_count": 5,    // 已註冊（含待分班）
        "remaining": 13,
        "over_capacity": false
      }, ...
    ]
  }
```
單一查詢彙總所有年級，避免 per-grade N+1。

### 6.3 設定計畫名額
```
PUT /recruitment/intake-targets
權限：RECRUITMENT_WRITE
Request:
  { "school_year": 115, "semester": 1,
    "targets": [ { "grade_id": 3, "target_seats": 30 }, ... ] }
Response: 更新後的 targets 清單
```
以 `(grade_id, school_year, semester)` upsert。

### 6.4 修改轉換服務 `convert_recruitment_to_student()`
- 學年來源：若 `visit.target_school_year` 非 null → 用它；否則維持 `resolve_current_academic_term()` 既有行為（向後相容，插班/當學年不受影響）。
- `enrollment_seq` 用上述決定的 `enroll_year` 取號（`next_enrollment_seq(session, enroll_year)`）。
- `classroom_id`：下學年新生由呼叫端傳 `None`＝「已註冊・待實際分班」。
- `visit.provisional_grade_id` 轉換後**保留不清**（轉換把 `visit.enrolled=True`，自然退出「已保留」計數、進入「已註冊」計數，無重複計數）。

## 7. 名額計算（單一真相）

集中於純函式服務 `services/recruitment_intake_plan.py`（接已查詢資料、不依賴 session，便於測試），由 API 層組裝查詢。

對某 `(school_year=Y, semester=S, grade=G)`：
- **已保留 `reserved_count`** ＝ `recruitment_visits` 中 `provisional_grade_id=G AND target_school_year=Y AND target_semester=S AND enrolled=False`。
- **已註冊 `enrolled_count`** ＝ `Student` join 其 `recruitment_visit`，條件 `recruitment_visit.provisional_grade_id=G AND recruitment_visit.target_semester=S AND Student.enrollment_school_year=Y AND lifecycle_status NOT IN (graduated, transferred, withdrawn)`（以 `Student.enrollment_school_year` 對齊學年、以 visit 的 `target_semester` 對齊學期，與 `reserved_count` 同基準）。
  - 以「訪視的 `provisional_grade_id`」作為年級歸屬的單一來源（Student 本身無 grade 欄、且待分班時 `classroom_id=NULL`），避免兩處定義漂移。
- **剩餘 `remaining`** ＝ `target_seats − reserved_count − enrolled_count`。
- **`over_capacity`** ＝ `reserved_count + enrolled_count > target_seats`。

**重複計數防護**：`reserved_count` 僅計 `enrolled=False`；一旦轉換為 Student（`enrolled=True`）即離開保留、進入註冊。兩集合互斥。

**已知假設**：未經訪視、由其它入口手動建立的 Student 不會被本面板的 `enrolled_count` 計入（其 `recruitment_visit` 為 null）。本園新生一律走漏斗，可接受；於面板註記。

## 8. 前端

### 8.1 漏斗看板「預繳」欄卡片（`FunnelCard.vue`）
- 已保留者顯示徽章：`🪑 暫定・<年級>・<學年>`。
- 卡片新增「編班」按鈕 → 開小對話框（選年級 + 目標學年；可釋放）。呼叫 6.1。
- 漏斗看板維持 4 欄，狀態機不動。
- board 回傳的卡片 payload 增補 `provisional_grade_id / provisional_grade_name / target_school_year`（`api/recruitment/funnel.py` board 序列化小幅擴充）。

### 8.2 新增「新生名額規劃」面板（`RecruitmentView.vue` 一個 tab）
- 學年/學期選擇器。
- 年級 × 名額 grid：每列 `年級｜計畫名額(可編輯)｜已保留｜已註冊｜剩餘`，超收標紅。
- 編輯計畫名額 → 呼叫 6.3。
- 資料來源 6.2。

### 8.3 前端 api 層
- `src/api/recruitmentFunnel.ts` 增 `reserveSeat(visitId, payload)`。
- 新增 `src/api/recruitmentIntakePlan.ts`：`getIntakePlan(schoolYear, semester)` / `setIntakeTargets(payload)`。
- 後端改 router/schema 後跑 `dump_openapi.py` + `npm run gen:api`，型別走 `_generated`。

## 9. 邊界情況與守衛
- **未預繳保留**：6.1 守衛 `has_deposit=True`，否則 400。
- **預繳退回訪視**（`deposited→visited`）：若該 visit 已保留座位，應一併釋放 `provisional_*` 或阻擋並提示先釋放。採「退回時自動釋放並記 event」。
- **轉換後計數**：見 §7 互斥保證。
- **跨年級實際分班偏差**：若日後 Student 被實際分到與當初保留年級不同的班，§7 仍以訪視 `provisional_grade_id` 計數（Phase 1 為班級建立前的規劃，學生尚未分班，影響可忽略；於面板註記）。
- **計畫名額未設定**：視為 `target_seats=0`，`remaining` 可為負並標超收（提示 admin 設定）。

## 10. 測試計畫
- **純函式**（`services/recruitment_intake_plan.py`）：給定 visits + students + targets → 正確 `reserved/enrolled/remaining/over_capacity`；含「轉換後不重複計數」案例。
- **轉換服務**：`visit.target_school_year=115` → `student.enrollment_school_year=115`，且 seq 取自 115 序列；`target_school_year=None` → 維持當前學年（回歸）。
- **守衛**：`has_deposit=False` 呼叫 reserve-seat → 4xx。
- **退回釋放**：`deposited→visited` 後 `provisional_*` 已清。
- 既有招生漏斗測試需維持綠燈。

## 11. 分期
- **Phase 1（後端，契約先行）**：migration `nsintake01` + §6 三端點 + §6.4 轉換服務修正 + §7 純函式 + §10 pytest。
- **Phase 2（前端）**：§8 卡片徽章/編班對話框 + 名額規劃面板 + api 層 + codegen。
- 前後端各自獨立 commit；後端先合併並 `alembic upgrade heads` 後前端再接。

## 12. 未來工作
- 官網報名整合（外部義華同步資料進漏斗/統計，或自建公開新生報名/參觀預約表單）。
- 下學年班級建立後，名額規劃面板的「計畫名額」可選擇自動加總該年級各班 `capacity`。
- 「已註冊・待實際分班」清單 → 一鍵帶入 `bulk-transfer` 分到實際 A/B 班的引導流程。

## 13. 決策記錄
- **D0｜暫定編班＝保留座位，不建 Student**：不進名冊、不點名、不收費（user 選定）。
- **D1｜「標記」＝暫定編班動作本身**：無額外獨立旗標。
- **D2｜下學年新生「註冊」時即建立正式 Student**：`enrollment_school_year`＝目標學年、`classroom_id` 暫空＝待分班，而非等班級建好才建檔。
- **D3｜計畫名額由 admin 手動設定**（`grade_intake_targets` 小表），不自動從班級容量推。
- **D4｜招生節奏前提**：以招下學年新生為主、班級當下未建 → 保留座位綁「年級＋目標學年」而非班級 FK。
- **D5｜官網報名整合本次不做**，列未來 Phase。
