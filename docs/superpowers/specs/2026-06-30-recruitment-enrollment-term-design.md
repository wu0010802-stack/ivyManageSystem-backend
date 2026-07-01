# 招生入學「分學期」端到端設計

- 日期：2026-06-30
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 狀態：設計已與業主確認，待寫實作計畫

## 1. 背景與目標

幼兒園招生面向未來：一個小孩可能在 **114 上學期、114 下學期、或 115 學期** 進來。系統目前對「學期」的支援是半套——底層其實已有欄位，但登記訪視時填不到、明細也篩不到，且看板/統計是用「訪視月份」推算學期，而非小孩**預計入學的學期**（兩者常不同，例如 114 下來參觀、115 上才入學）。

目標：把「入學學期」做成貫穿招生入學全流程的一級概念——**登記 → 篩選 → 看板/統計 → 轉成學生 → 在籍統計** 都以入學學期為維度。

### 業主已確認的決策

1. **學生本人入學學期**：在 `Student` 表新增 `enrollment_semester` 欄位（與既有 `enrollment_school_year` 對稱、永久不變），並一次性 backfill 既有學生。
2. **舊訪視資料**：一次性 backfill `target_school_year`/`target_semester`，用「訪視月份」推算（與現行看板行為一致，看板不會空掉），個案再人工修正。
3. **新增訪視**：「入學學期」欄位**必填**，預設帶**當前學期**，可改。

## 2. 核心概念

把已存在但被埋住的 `RecruitmentVisit.target_school_year` / `target_semester`（目前只能透過 `/reserve-seat` 流程設定）**升格為「入學學期」一級欄位**，避免雙資料源。

學期格式沿用全系統慣例：`{ school_year: 民國年, semester: 1=上學期 | 2=下學期 }`。
- 學年起訖：上學期 8/1~隔年 1/31，下學期 2/1~同年 7/31（見 `utils/academic.term_bounds`）。
- 當前學期：`utils/academic.resolve_current_academic_term()`（純函式，依今日日期推算）。

## 3. 後端設計（ivy-backend）

### 3.1 資料模型

**`models/recruitment.py` — `RecruitmentVisit`**
- 重用既有 `target_school_year`（Integer）、`target_semester`（Integer，1/2）作為「入學學期」。
- DB 層維持 nullable（容納舊資料與漸進 backfill），但**新建一律必填**（在 schema/API 層強制）。
- 既有索引 `ix_rv_target_grade(target_school_year, target_semester, provisional_grade_id)` 沿用。

**`models/classroom.py` — `Student`**
- 新增 `enrollment_semester = Column(Integer, nullable=True, comment="入學學期 1=上/2=下；入學配發一次、終身不變")`，緊鄰既有 `enrollment_school_year`。

**保留不動（限定範圍，避免動到既有統計）**
- `transfer_term`（布林）與 `expected_start_label`（文字）保留現狀；「入學學期」以新結構化欄位為準，`transfer_term` 退居輔助標記。
- `RecruitmentPeriod` 的 `transfer_term_count` / `effective_deposit_count` 統計邏輯本次不重構（列為 backlog）。

### 3.2 API 契約

Schema 位置：`api/recruitment/shared.py`

- `RecruitmentVisitCreate`：新增 `target_school_year: int`、`target_semester: int`（**必填**；`semester ∈ {1,2}`，以 validator 守衛）。
- `RecruitmentVisitUpdate`：新增 `target_school_year: int | None`、`target_semester: int | None`（選填）。
- `RecruitmentRecordOut`：回傳 `target_school_year`、`target_semester`（明細需顯示）。

端點：

- `GET /recruitment/records`（`api/recruitment/records.py`）
  - 新增 query：`school_year: int | None`、`semester: int | None`。
  - 依 `target_school_year`（必要時加 `target_semester`）篩選；`semester` 省略＝整學年（上+下）。
  - 與既有 month/grade/source 等篩選並存（AND）。
- `POST /recruitment/records` / `PUT /recruitment/records/{id}`：持久化新欄位。
- `GET /recruitment/funnel/board`（`api/recruitment/funnel.py`）
  - **分組語意改為依「入學學期」**：以 `RecruitmentVisit.target_school_year == school_year`（且 `target_semester == semester`，若有帶）圈定範圍，**取代**現行 `month.in_(school_term_to_roc_months(...))`。
  - 4 階段推導（`derive_stage`）不變；階段仍以 student 存在性 + `lifecycle_status` 為準。
- `GET /recruitment/stats`（`api/recruitment/stats.py`）
  - 加入學期維度篩選（`school_year`/`semester`），統計可按入學學期彙總；既有分月視圖保留。

### 3.3 轉成學生

`services/recruitment_conversion.py`
- 現行已讀 `visit.target_school_year` / `target_semester` 決定 `enroll_year`/`enroll_sem`，並寫入 `StudentChangeLog` 與 `student.enrollment_school_year`。
- **新增**：同時 `student.enrollment_semester = enroll_sem`，使學生本人欄位與招生來源一致。

### 3.4 Migration（Alembic，含完整 downgrade）

1. **加欄位**：`students.enrollment_semester`（nullable Integer）。
2. **Backfill 訪視**：對 `recruitment_visits` 中 `target_school_year IS NULL` 的列，用 `month`（民國格式 "115.03"）反推所屬學年/學期填入 `target_school_year`/`target_semester`。
   - 反推規則對齊 `funnel.py` 既有「月份 → 所屬學年」邏輯，確保 backfill 後看板與現況一致。
3. **Backfill 學生**：`students.enrollment_semester` 依序取：
   1. `StudentChangeLog` 中該生 `event_type=="入學"` 事件的 `semester`；
   2. 無則由 `enrollment_date` 經 `term_bounds`/`resolve_academic_term_for_date` 推；
   3. 再無則留 NULL。
- 以 **Python data migration** 處理日期/字串推算（非純 SQL），避免 `op.execute` 內嵌冒號被當 bind param 等坑。
- `downgrade`：移除 `enrollment_semester` 欄位；訪視 backfill 不可逆（資料補值），於 docstring 註明。

### 3.5 後端測試

- term 反推 helper 單測（"115.03" → 正確學年/學期；跨年邊界 1 月、8 月、2 月）。
- `GET /recruitment/records?school_year=&semester=`：篩選正確、semester 省略＝整學年。
- `GET /recruitment/funnel/board`：依入學學期分組（含「訪視在 A 學期、入學在 B 學期」的個案歸到 B）。
- `POST/PUT /records`：必填驗證（缺 target_* → 422）、持久化。
- conversion：轉成學生後 `student.enrollment_semester` 正確寫入。
- migration backfill 正確性（訪視 NULL 補值、學生由 changelog/日期推算）。
- 測試掛 `test_db_session` fixture，避免打到 dev DB。

## 4. 前端設計（ivy-frontend，全 TS）

### 4.1 登記/編輯訪視
- `src/constants/recruitment.ts` `VisitFormState`：新增 `target_school_year` / `target_semester`，預設帶當前學期。
- `src/components/recruitment/RecruitmentRecordDialog.vue`：加「入學學期」選擇器（學年下拉 + 學期切鈕，沿用 `IntakePlanPanel` 既有樣式），**必填、預設當前學期**，選項範圍 `當前學期 −1 ~ +3`（可回補過去、規劃未來梯次）。
- `src/api/recruitment.ts`：create/update payload 帶 `target_school_year` / `target_semester`。

### 4.2 明細篩選
- `src/components/recruitment/AdmissionsRecordsPanel.vue` / `RecruitmentDetailTab.vue`：篩選列加「學期」下拉（學年+學期），`getRecruitmentRecords` 帶 `school_year`/`semester`。
- 明細表格新增「入學學期」欄。

### 4.3 看板 + 統計
- `src/components/recruitment/funnel/FunnelBoard.vue`：選擇器沿用，**文案/提示改為「入學學期」**（語意已由後端切換）。
- `src/components/recruitment/RecruitmentStatsPanel.vue`：加學期篩選，統計可按入學學期看。

### 4.4 學生 / 在籍統計
- `Student` 型別新增 `enrollment_semester`；學生詳情顯示「入學學期」。
- `src/components/student/workbench/EnrollmentPanel.vue`：確認在籍統計以入學學年/學期維度（已有 term 選擇器，對齊新欄位）。

### 4.5 前端測試
- dialog 預設當前學期 + 必填驗證（vitest）。
- 明細篩選 wiring（帶對 query params）。
- 學生顯示入學學期。

## 5. 跨端同步收尾

- 後端改完 schema → `python scripts/dump_openapi.py` + 前端 `npm run gen:api` → **只 commit 前端 `schema.d.ts`**（`openapi.json` 不入 repo）。
- 權限：沿用既有 recruitment 權限，無新增。
- PII denylist：本次無新增 PII 欄位，前後端 denylist 不變。
- 前後端**分開 commit**（不同 repo），訊息描述同一功能。

## 6. 明確排除（YAGNI / Backlog）

- 不重構 `transfer_term` / `RecruitmentPeriod` 的轉期統計（保留現狀）。
- 不自動由「入學學期 vs 訪視月份學期」推導 `transfer_term`（列 backlog）。
- 舊訪視 backfill 採「訪視月份」推算，不嘗試猜測未來入學學期。

## 7. 實作順序（SOP：後端先行）

1. 後端：model + migration（含 backfill）+ schema + router + conversion + pytest。
2. `dump_openapi` → 前端 `gen:api` → commit `schema.d.ts`。
3. 前端：constants + dialog + API + 明細篩選 + 看板/統計文案 + 學生顯示 + vitest。
4. 整合驗證（`start.sh` 起兩端，實點一次：登記帶學期 → 明細篩 → 看板分 → 轉學生 → 學生詳情）。
5. 前後端分開 commit。
