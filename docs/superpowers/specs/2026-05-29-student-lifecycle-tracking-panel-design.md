# 學生在校歷程追蹤面板（Student Lifecycle Tracking Panel）

| 項目 | 內容 |
|------|------|
| 日期 | 2026-05-29 |
| 作者 | yilunwu + Claude |
| 範圍 | 內部端（admin / HR / 班導），read-only |
| 估時 | 5-7 天（後端 2 + 前端 3 + 測試/拋光 2） |
| 風險 | 低 — 純讀，零 schema migration，不動 state machine |

## 1. 動機與目的

招生漏斗（4 階段 Kanban）與學生生命週期（7 status state machine）目前是兩條獨立的線：

- Funnel 視角是「全校招生」(`visited → deposited → enrolled → active`)
- Lifecycle 視角是「個別學生狀態機」(`prospect → enrolled → active → on_leave / graduated / withdrawn / transferred`)
- 班級異動（`StudentClassroomTransfer`）又是另一條
- 繳費流水（`StudentFeePayment`）又是另一條

HR 與班導需要「站在學生本人角度」看一條完整的故事線：**這個學生從第一次來參觀到目前的所有歷史**。目前沒有任何一個視圖呈現這條故事線。

本 spec 設計一個 **read-only 的「在校歷程」追蹤面板**，掛在 `StudentProfileView` 的新 tab，提供「Stepper 鳥瞰」+「Timeline 細節」兩層資訊。

**明確不在範圍：**

- ❌ 不改 funnel Kanban（招生視角不動）
- ❌ 不改 lifecycle 狀態機合法轉移表
- ❌ 不寫新 schema migration（純讀現有資料）
- ❌ 不做家長 portal 端（家長端後續視回饋再評估，需考慮 PII 與 actor 隱藏）
- ❌ 不做點 dot 觸發動作（先 read-only，動作 dialog 留給後續迭代）
- ❌ 不做批次升班儀式（另一個獨立議題）

## 2. 概念模型

### 2.1 兩層 Stepper

**外層 Stepper（5 點，每個學生只走一次）**

```
參觀(visited) → 預繳(deposited) → 報到(enrolled) → 在學(active) → 終態
                                                                ↓
                                                      畢業 / 退學 / 轉學（三選一）
```

- 對應後端 `funnel stage` 或 `lifecycle_status`
- 「在學」節點之後展開內層 Stepper
- 終態節點顏色依實際終態：綠（畢業）/ 紅（退學）/ 橘（轉學）；未發生時顯示「預計畢業 YYYY/MM」灰底
- **休學疊加狀態**：`lifecycle_status == on_leave` 時，「在學」dot 旁加 ⏸ 徽章 + Timeline 上方紅字「YYYY/MM/DD 起休學中」

**內層 Stepper（在「在學」節點下展開）**

- 依 `ClassGrade.sort_order` 由小到大列出園所**實際啟用的年級**（`is_active=true`）
- 起點：學生**首次進入的年級**（非每個學生都從幼幼班入）；入學年級之前的年級顯示為 `skipped`（灰色虛線）
- 終點：`is_graduation_grade=true` 的年級
- 目前年級：`current`（半亮 + 動畫）
- 已過年級：`done`（全亮）
- 未來年級：`future`（暗）

### 2.2 Timeline（雙層 Stepper 下方）

可摺疊。時間倒序。每筆一行，含：時間、類別徽章、摘要文字、操作者（如有）、reason（如有）。

支援篩選類別（multi-select）：

| 類別 | 來源 model | 摘要範例 |
|------|-----------|---------|
| `funnel_event` | `RecruitmentEventLog` | 「轉換為正式生（小班 A）」 |
| `change_log` | `StudentChangeLog` | 「升狀態為 active」 |
| `classroom_transfer` | `StudentClassroomTransfer` | 「轉班：小班 A → 中班 A」 |
| `payment` | `StudentFeePayment` | 「繳交註冊費 NT$5,000」 |
| `incident` | `StudentIncident` | 「跌倒輕傷（操場）」 |
| `assessment` | `StudentAssessment` | 「期中發展評量（中班 A）」 |

## 3. 後端設計

### 3.1 新檔：`services/student_lifecycle_overview.py`

純函式集中。三個 public function：

```python
@dataclass
class StepInfo:
    key: str  # visited / deposited / enrolled / active / terminal
    label: str  # 中文標籤
    status: Literal["done", "current", "future"]
    occurred_at: Optional[date]
    meta: Optional[dict]  # e.g. deposit amount

@dataclass
class GradeStepInfo:
    grade_id: int
    name: str
    sort_order: int
    status: Literal["done", "current", "future", "skipped"]
    entered_at: Optional[date]
    expected_at: Optional[date]  # for future steps
    classroom_name: Optional[str]  # 該年級當時所屬班名

@dataclass
class TerminalInfo:
    kind: Literal["graduated", "withdrawn", "transferred", "none"]
    actual_date: Optional[date]
    expected_date: Optional[date]  # 在學時推算「預計畢業日」

@dataclass
class LifecycleOverview:
    student_id: int
    current_stage: str  # visited/deposited/enrolled/active/graduated/withdrawn/transferred/on_leave
    on_leave_badge: bool
    on_leave_since: Optional[date]
    outer_steps: list[StepInfo]
    inner_grade_steps: list[GradeStepInfo]
    terminal: TerminalInfo

def compute_outer_steps(
    student: Student,
    funnel_events: list[RecruitmentEventLog],  # 同一 student_id 的事件
    change_logs: list[StudentChangeLog],
) -> list[StepInfo]: ...

def compute_inner_grade_steps(
    student: Student,
    all_grades: list[ClassGrade],  # 過濾 is_active=true，已 sort by sort_order
    transfers: list[StudentClassroomTransfer],
    classroom_grade_map: dict[int, int],  # classroom_id → grade_id
    classroom_name_map: dict[int, str],
) -> list[GradeStepInfo]: ...

def compute_terminal(
    student: Student,
    inner_grade_steps: list[GradeStepInfo],
    graduation_grade_sort_order: int,
) -> TerminalInfo: ...

def build_lifecycle_overview(
    session: Session,
    student_id: int,
) -> LifecycleOverview:
    """聚合 entrypoint，內部呼叫三個 compute_*。"""
```

純函式好處：可以不依賴 DB 直接 unit test 各種情境。`build_lifecycle_overview()` 負責所有 query，組好參數丟給純函式。

### 3.2 推算邏輯細節

| 推算項 | 規則 | 降級規則 |
|--------|------|----------|
| 「參觀」occurred_at | `RecruitmentEventLog` 中 `to_stage == "visited"` 的最早 `created_at::date` | 無 funnel 記錄 → status="future" |
| 「預繳」occurred_at | `RecruitmentEventLog` 中 `to_stage == "deposited"` 的最早 `created_at::date` | 無記錄 → status="future" |
| 「預繳」meta.amount | optional — 對應時間附近的 `StudentFeePayment` 第一筆 deposit；推算困難可省略，前端缺欄位顯示為「-」 | — |
| 「報到」occurred_at | `RecruitmentEventLog` 中 `event_type == "converted"` 的 `created_at::date`；無此事件用 `student.enrollment_date` | 兩者皆無 → status="future" |
| 「在學」occurred_at | `StudentChangeLog` 中 `event_type` 對應 `LIFECYCLE_TO_EVENT_TYPE["activated"]` 的最早 `event_date`；無則用 `student.enrollment_date` | 兩者皆無但 `lifecycle_status == "active"` → 用 `enrollment_date`；皆無 → status="future" |
| 首次進入年級 | 從 `StudentClassroomTransfer` 找最早的 `to_classroom_id`，map 到 grade_id；該 grade 是「入學年級」 | 無 transfer 紀錄 → 用 `student.classroom_id` 對應的 grade 當入學年級 |
| 各年級進入日期 | 對 `StudentClassroomTransfer` 按 `to_classroom.grade_id` 分組，取每組最早 `transferred_at::date` | 入學年級若無 transfer → 用 `student.enrollment_date` |
| 跳級判定 | 入學年級之後到當前年級之間，中間 `sort_order` 對應的 grade 若**從未**出現在該學生的 transfer 歷史 → 顯示 `skipped` | — |
| 入學前的年級 | sort_order < 入學年級的 sort_order → `skipped`（灰色虛線） | — |
| 預計畢業日 | 「進入當前年級的學年」+ `(graduation_grade.sort_order - current_grade.sort_order)` 年 → 該學年的 7/31（園所學年結束日預設）。若可從 `AcademicTerm` 找到對應學年的下學期 `end_date`，優先用該值 | 若已在 graduation grade → expected = 當學年下學期 end_date（或 7/31 預設）；若無 graduation grade → None |
| 休學徽章 | `lifecycle_status == "on_leave"` | `on_leave_since` 用最近一筆 `StudentChangeLog` `event_type=on_leave` 的 `event_date` |

**重點**：推算規則必須對「早期/不完整資料」降級，**不能因為缺資料就 raise**。所有「不知道」一律標 `future` + `occurred_at=None`。

### 3.3 新 endpoint：`GET /api/students/{student_id}/lifecycle-overview`

在 `api/students.py` 加：

```python
@router.get(
    "/students/{student_id}/lifecycle-overview",
    response_model=LifecycleOverviewOut,
)
def get_lifecycle_overview(
    student_id: int,
    session: Session = Depends(get_session),
    current_user = Depends(require_permission(Permission.STUDENTS_READ)),
) -> LifecycleOverviewOut:
    overview = build_lifecycle_overview(session, student_id)
    return LifecycleOverviewOut.from_dataclass(overview)
```

Pydantic schema (`schemas/student_lifecycle.py` 新檔)：

```python
class StepOut(BaseModel):
    key: str
    label: str
    status: Literal["done", "current", "future"]
    occurred_at: Optional[date]
    meta: Optional[dict] = None

class GradeStepOut(BaseModel):
    grade_id: int
    name: str
    sort_order: int
    status: Literal["done", "current", "future", "skipped"]
    entered_at: Optional[date]
    expected_at: Optional[date]
    classroom_name: Optional[str]

class TerminalOut(BaseModel):
    kind: Literal["graduated", "withdrawn", "transferred", "none"]
    actual_date: Optional[date]
    expected_date: Optional[date]

class LifecycleOverviewOut(BaseModel):
    student_id: int
    current_stage: str
    on_leave_badge: bool
    on_leave_since: Optional[date]
    outer_steps: list[StepOut]
    inner_grade_steps: list[GradeStepOut]
    terminal: TerminalOut
```

### 3.4 擴充 `GET /api/students/{student_id}/timeline`

`services/student_records_timeline.py` 既有 `RECORD_TYPES = {"incident", "assessment", "change_log"}`，擴充為：

```python
RECORD_TYPES = {
    "incident",
    "assessment",
    "change_log",
    "funnel_event",      # NEW
    "classroom_transfer", # NEW
    "payment",           # NEW
}
```

加三個 `_fetch_*` + `_build_*_item` 函式：

- `_fetch_funnel_events` / `_build_funnel_event_item`：query `RecruitmentEventLog WHERE student_id = ?`
- `_fetch_classroom_transfers` / `_build_classroom_transfer_item`：query `StudentClassroomTransfer WHERE student_id = ?`
- `_fetch_payments` / `_build_payment_item`：query `StudentFeePayment WHERE student_fee_record_id IN (SELECT id FROM student_fee_records WHERE student_id = ?)`；摘要文字「繳交 {fee_item_name} NT${amount}」

API 層 `api/student_change_logs.py`（或 timeline endpoint 所在處）將 `types` query param 的 enum 加上新值。

**權限**：既有 `STUDENTS_READ`，但 `payment` 類型若 viewer 沒有 `FEES_READ` 應退化為「繳費記錄已隱藏」filler row 或直接 filter out（spec 採後者，由 endpoint 層判斷並從 enabled set 移除 `payment`）。

### 3.5 已存在不重複實作

| 已有 | 路徑 |
|------|------|
| `services/student_lifecycle.transition()` | `services/student_lifecycle.py` |
| `services/recruitment_funnel.transition_visit()` | `services/recruitment_funnel.py` |
| `services/student_records_timeline.list_timeline()` | `services/student_records_timeline.py` |
| `models/classroom.ClassGrade.{sort_order, is_graduation_grade}` | `models/classroom.py:58` |
| `models/student_log.StudentChangeLog` | `models/student_log.py:56` |
| `models/student_transfer.StudentClassroomTransfer` | `models/student_transfer.py:17` |
| `models/recruitment.RecruitmentEventLog` | `models/recruitment.py:271` |
| `models/fees.StudentFeePayment` | `models/fees.py:185` |

## 4. 前端設計

### 4.1 新元件：`src/components/students/StudentLifecyclePanel.vue`

```vue
<script setup lang="ts">
import type { components } from '@/api/_generated/schema'
import { ref, computed, onMounted } from 'vue'
import { getLifecycleOverview } from '@/api/studentLifecycle'
import { getStudentTimeline } from '@/api/studentTimeline'
import OuterStepperRow from './lifecycle/OuterStepperRow.vue'
import InnerGradeStepperRow from './lifecycle/InnerGradeStepperRow.vue'
import LifecycleTimelineList from './lifecycle/LifecycleTimelineList.vue'

type Overview = components['schemas']['LifecycleOverviewOut']

const props = defineProps<{ studentId: number }>()
const overview = ref<Overview | null>(null)
const timelineExpanded = ref(true)
const enabledTypes = ref<string[]>([
  'funnel_event', 'change_log', 'classroom_transfer', 'payment', 'incident', 'assessment'
])

onMounted(async () => {
  overview.value = await getLifecycleOverview(props.studentId)
})
</script>
```

子元件拆分（每檔聚焦單一職責，方便測試）：

- `OuterStepperRow.vue` — 5 點外層 stepper，含「在學」徽章與終態顏色
- `InnerGradeStepperRow.vue` — 年級內層 stepper，含 skipped 灰虛線
- `LifecycleTimelineList.vue` — Timeline + 類別 multi-select filter

### 4.2 新 API wrapper：`src/api/studentLifecycle.ts`

```ts
import api from './index'
import type { ApiResponse, AxiosResp } from './_generated/typed'

const base = (studentId: number) => `/students/${studentId}/lifecycle-overview`

export const getLifecycleOverview = (studentId: number) =>
  api.get<ApiResponse<'/students/{student_id}/lifecycle-overview', 'get'>>(
    base(studentId)
  ) as AxiosResp<'/students/{student_id}/lifecycle-overview', 'get'>
```

### 4.3 既有 `src/api/studentTimeline.ts` 擴充

新增 `types` 參數選項（前端 type 用 OpenAPI codegen 自動更新）。無需手動加 enum。

### 4.4 掛點：`src/views/StudentProfileView.vue`

加新 tab「在校歷程」(label) / `lifecycle` (key)：

```vue
<el-tab-pane label="在校歷程" name="lifecycle">
  <StudentLifecyclePanel :student-id="studentId" />
</el-tab-pane>
```

**權限門**：tab 顯示條件 `hasPermission('STUDENTS_READ')`（多半已隱含於進入 `StudentProfileView`）。

### 4.5 視覺規範

- Stepper：用 Element Plus `<el-steps>` 但客製顏色：
  - `done` = 主色綠 `var(--el-color-primary)`
  - `current` = 漸層淡綠 + pulse 動畫
  - `future` = 灰 `var(--el-color-info-light-7)`
  - `skipped` = 虛線灰 `var(--el-color-info-light-9)`
- 終態 dot：
  - 畢業綠 `#67c23a`
  - 退學紅 `#f56c6c`
  - 轉學橘 `#e6a23c`
  - 未發生（在學中預測）灰 `var(--el-color-info-light-7)`
- 休學 ⏸ 徽章用 `<el-tag type="warning" effect="plain">`
- 響應式：< 768px 時雙層 stepper 改為垂直堆疊（內層 stepper 縮為「目前年級 + 進度條」）

## 5. 測試規格

### 5.1 後端 pytest (`tests/test_student_lifecycle_overview.py` 新檔)

純函式測試（不需 DB session）：

- `test_compute_outer_steps_only_visited` — 只有參觀記錄
- `test_compute_outer_steps_visited_to_active_full_path` — 全程記錄
- `test_compute_outer_steps_graduated_full_terminal`
- `test_compute_outer_steps_withdrawn_from_active`
- `test_compute_outer_steps_transferred_from_active`
- `test_compute_outer_steps_legacy_student_no_funnel_events` — 早期學生無 funnel，僅有 enrollment_date
- `test_compute_inner_grades_full_journey_from_yo_yo` — 幼幼班入學一路升到大班
- `test_compute_inner_grades_mid_year_enrollment` — 5 歲入大班，小/中班顯示 skipped
- `test_compute_inner_grades_with_class_repeat` — 同年級兩次 transfer（轉班但年級沒變）
- `test_compute_inner_grades_no_transfer_history_fallback` — 純用 student.classroom_id 推
- `test_compute_inner_grades_gradeless_classroom` — classroom.grade_id IS NULL 降級
- `test_compute_terminal_expected_graduation_date` — 在學中推算「預計畢業 YYYY/MM」
- `test_compute_terminal_at_graduation_grade_expected_end_of_term`
- `test_compute_terminal_no_graduation_grade_returns_none_expected`
- `test_on_leave_badge_with_since_date`

整合測試（有 DB session）：

- `test_build_lifecycle_overview_end_to_end_active_student`
- `test_build_lifecycle_overview_handles_orphan_funnel_events`

Timeline 擴充測試（接在 `tests/test_student_records_timeline.py`）：

- `test_timeline_includes_funnel_events`
- `test_timeline_includes_classroom_transfers`
- `test_timeline_includes_payments`
- `test_timeline_filters_payment_when_no_fees_permission`
- `test_timeline_combined_sources_sorted_correctly`

### 5.2 前端 vitest (`src/components/students/__tests__/`)

- `StudentLifecyclePanel.spec.ts`
  - 外層 stepper 5 點正確渲染對應 status 顏色
  - 內層 stepper 依 grade_id 升冪排列
  - skipped 年級顯示虛線
  - 終態 3 種顏色正確
  - on_leave_badge=true 時顯示 ⏸
- `InnerGradeStepperRow.spec.ts`
  - 入學前年級 skipped 樣式
  - 跳級情境 skipped 樣式
- `LifecycleTimelineList.spec.ts`
  - 類別 multi-select 篩選正確
  - 時間倒序

## 6. 任務拆解（給 writing-plans 用）

T0. 後端 services：`student_lifecycle_overview.py` 純函式（3 compute_* + 1 build_*）+ 純函式 pytest
T1. 後端 schemas：`schemas/student_lifecycle.py` Pydantic models
T2. 後端 API：`api/students.py` 加新 endpoint + 整合 pytest
T3. 後端 timeline 擴充：`services/student_records_timeline.py` 加 3 source + 對應 fetch/build + pytest
T4. 前端 API wrapper：`src/api/studentLifecycle.ts` + 跑 `npm run gen:api` 更新 schema
T5. 前端元件：`InnerGradeStepperRow.vue` + spec
T6. 前端元件：`OuterStepperRow.vue` + spec
T7. 前端元件：`LifecycleTimelineList.vue` + spec
T8. 前端元件：`StudentLifecyclePanel.vue` 整合 + spec
T9. 前端整合：`StudentProfileView.vue` 加新 tab
T10. 後端 commit；前端 commit；OpenAPI drift 檢查；手測（admin/HR/班導 3 種角色 × 不同學生情境）

## 7. 已知限制與後續迭代（不在本 spec 範圍）

1. **家長 portal 看自家孩子歷程**：後續評估，需考慮 PII（不顯示 actor 員工姓名 / 內部 reason 文字 / funnel 階段技術名詞）
2. **點 dot 觸發動作**：先 read-only；後續可加「點在學 dot → 申請休學 dialog」「點轉學 dot → bulk-transfer flow」
3. **「升班儀式」批次工具**：每年 8 月學年推進精靈是獨立議題
4. **終態副作用 SOP 串接**：退費試算、解綁 LIFF、活動報名取消是另一個獨立 spec
5. **缺繳費 deposit 金額對應**：spec 3.2 註明「預繳金額」meta 用 best-effort 推算，找不到就不顯示。後續若要精準，需在 funnel `deposited` event 寫入時把對應 payment.id 寫進 `RecruitmentEventLog.metadata_json`

## 8. Open Questions（spec 寫完前已 resolve）

| 問題 | 結論 |
|------|------|
| 升班怎麼進 stepper？ | 雙層 stepper：在學節點展開年級內層 |
| 終態顏色未發生時如何呈現？ | 灰 + 「預計畢業 YYYY/MM」label |
| 家長 portal 是否包含？ | 否，後續評估 |
| MVP 範圍多深？ | Stepper + Timeline read-only，不做動作 dialog |
| 「報到」與「在學」分兩節點還是合併？ | 分兩節點（funnel `enrolled` = 報到分班生學號；lifecycle `active` = 學期開學）|
| 休學 (`on_leave`) 怎麼呈現？ | active dot 旁 ⏸ 徽章 + timeline 上方紅字提示 |
