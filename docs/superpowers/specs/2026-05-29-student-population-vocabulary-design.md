# 在學學生口徑統一（shared population vocabulary）

**日期**：2026-05-29
**範圍**：後端（本次只交付 spec + characterization test；full build defer）
**對應 audit finding**：P1 #10「Dashboard 與 analytics 對『在學學生』用三套不同口徑」

## 校正 audit 前提

逐條驗證後，audit 的「dashboard / funnel / monthly_pnl 三口徑」描述**部分不準**：

- **monthly_pnl 根本不算學生數** —— 只算 `classroom_count` + `insured_employee_count`
  （`services/finance/monthly_pnl_service.py:135-136`）。第三口徑不存在。
- **funnel 的 "active" 是「期間入學 cohort」**（`enrollment_date` 落區間且 lifecycle 已過
  enrolled，含 graduated/withdrawn 供 retention 計算，`services/analytics/funnel_service.py:128-172`）
  —— 與「當前在學人數」是**不同問題**，非不一致。

## 真正的問題：缺共用 population 詞彙

當前學生 population 在各 surface **各自手刻 lifecycle filter**，至少 6 種：

| Surface | Filter | 對應 population（lifecycle） |
|---------|--------|------------------------------|
| `dashboard_query_service.py:199` 首頁「在學人數」 | `is_active == True` | {active, on_leave} |
| `gov_moe/monthly_calculator.py:179` **教育部月報「在學」** | `~lifecycle_status.in_({prospect})` | 所有非 prospect（含 enrolled/graduated/withdrawn/transferred） |
| `analytics/churn_service.py:104` | `lifecycle_status == 'active'` | {active}（排除休學） |
| `analytics/churn_service.py:211` | `lifecycle_status.in_(active, on_leave)` | {active, on_leave} |
| `fees/generation.py:90` | `lifecycle_status.in_(active, enrolled)` | {active, enrolled} |
| `appraisal/status_aggregator.py:185` | `lifecycle_status == 'active'` | {active} |

lifecycle 狀態（`models/classroom.py:29-35`）：prospect / enrolled / active / on_leave(休學) /
transferred / withdrawn / graduated。`is_active` 由 `set_lifecycle_status` 連動維護
（active+on_leave→True，其餘→False）。

### 哪些是「同問題不同答案」（bug）vs「不同問題」（合理）

- **bug 候選（同稱「在學人數」卻不同）**：`dashboard`（{active,on_leave}）vs
  `gov_moe 月報`（~prospect，幾乎全部）。**後者是法定申報（生師比 / 補助）**，與園長首頁
  數字可大幅不同 → 對外帳實不符風險。這是 audit 真正該指的點。
- **合理的不同問題**：`churn`（churn 率本就不該含休學）、`appraisal`（考核資格不含休學/離校）、
  `fees`（billing 需含 enrolled 報到未開學）。這些**不該硬統一**。

## 本次交付（scope：先證明，不硬蓋）

1. **characterization test** `tests/test_student_population_divergence.py`：
   seed 每個 lifecycle 狀態各 1 名學生（is_active 依 sync 規則），忠實重現上述 6 個
   filter，斷言它們對**同一批學生**回不同 population，並標出「dashboard 在學 ≠ gov 月報在學」
   的具體差異。pin 住現況、把「可證明的問題」交給 reviewer。

2. **本 spec**：記錄定義分歧 + 區分 bug vs 合理差異。

## Follow-up（未做，待 user 決定後再 build）

- 抽 `services/analytics/student_population.py`：**named population 查詢**
  （如 `currently_attending()` = {active,on_leave}、`active_only()` = {active}、
  `billable()` = {active,enrolled}、`gov_reportable(as_of)` = 法定口徑），各 surface
  改為**明示宣告**用哪個 population，定義集中一處可稽核。
- 對齊 `dashboard` 與 `gov_moe 月報` 的「在學」口徑（需業主確認法定定義）。
- cross-service 一致性測試：同一 as-of-date 下，宣稱同 population 的端點回相同數字。
- **prod 資料 drift 檢查（USER 手動）**：supabase MCP 本次連線不可用。請於 prod 跑
  ```sql
  SELECT lifecycle_status, is_active, count(*) FROM students
  GROUP BY 1,2 ORDER BY 1,2;
  ```
  確認 `is_active` 與 lifecycle 是否有 drift 列（`is_active=true` 但 lifecycle∉{active,on_leave}，
  或反之）。drift>0 表示有 code 繞過 `set_lifecycle_status`（CLAUDE.md #9），那是另一層 bug。
