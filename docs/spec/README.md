# 介面契約規格索引（docs/spec/）

本目錄存放 ZeroSpec 格式的 **SPEC（Interface Specification）** 文件，描述後端核心模組的對外介面契約、業務規則與不變式，作為開發、code review、與跨域對齊的 Source of Truth。

> SPEC 與 `docs/superpowers/specs/` 內的 **設計規格（design doc）** 不同：
> - **設計規格**（superpowers/specs/）：特定時間點的決策與遷移計畫，append-only，不隨 code 演進更新
> - **SPEC**（本目錄）：反映**當前** code 行為的介面契約，隨 code 異動同步維護

---

## Document Index

| 編號 | 主題 | Status | Version | Scope 摘要 |
|------|------|--------|---------|------------|
| [SPEC-001](./SPEC-001_salary-engine-core.md) | 薪資 Engine 主流程 | Draft | v0.1 | `services/salary/{engine,totals,breakdown,finalize_guard,bulk_preload,utils,constants}.py` + `api/salary/*` |
| [SPEC-002](./SPEC-002_salary-festival-bonus.md) | 薪資節慶獎金與會議扣款 | Draft | v0.1 | `services/salary/festival.py` + 節慶相關呼叫；`api/salary/festival.py` |
| [SPEC-003](./SPEC-003_salary-hourly-wage.md) | 時薪基準與最低工資 | Draft | v0.1 | `services/salary/{hourly,minimum_wage,constants}.py` |
| [SPEC-004](./SPEC-004_salary-deduction-insurance.md) | 薪資扣繳與保費 | Draft | v0.1 | `services/salary/{deduction,insurance_salary,supplementary_premium}.py` + `services/insurance_service.py` + `api/insurance.py` |
| [SPEC-005](./SPEC-005_salary-severance-unused-leave.md) | 離職結算(資遣費、未休假折現、任職比例) | Draft | v0.1 | `services/salary/{severance,unused_leave_pay,proration}.py` + `services/offboarding/` + `api/offboarding.py` |
| [SPEC-006](./SPEC-006_salary-appraisal-year-end.md) | 年終考核獎金 | Draft | v0.1 | `services/salary/appraisal_year_end.py` + `services/{appraisal,year_end}/` + `api/{appraisal,year_end}/` |
| [SPEC-007](./SPEC-007_permission-system.md) | 權限系統 | Draft | v0.1 | `utils/{permissions,auth}.py` + `models/{auth,permission_models}.py` + `api/{auth,permissions_admin}.py` |
| [SPEC-008](./SPEC-008_notification-dispatch.md) | 通知統一 Dispatch | Draft | v0.1 | `services/notification/` 全套(dispatch / channel_matrix / event_types / renderers / _channels) |
| [SPEC-009](./SPEC-009_attendance.md) | 考勤與打卡 | Draft | v0.1 | `api/attendance/` + `api/punch_corrections.py` + `services/attendance_parser.py` + `utils/attendance_{calc,guards,leave_merge}.py` + `models/attendance.py` |
| [SPEC-010](./SPEC-010_leaves-overtimes.md) | 請假與加班 | Draft | v0.1 | `api/{leaves*,overtimes,student_leaves}.py` + `services/leave_*` + `services/overtime_conflict_service` + `services/approval/cross_type_offset` + `utils/leave_*` |
| [SPEC-011](./SPEC-011_analytics.md) | 招生漏斗與流失預警 | Draft | v0.1 | `api/analytics.py` + `services/analytics/` + `services/recruitment_*` + `services/report_cache_service.py` + `models/{report_cache,recruitment}.py` |
| [SPEC-012](./SPEC-012_parent-portal-pii.md) | 家長入口與 PII Retention | Draft | v0.1 | `api/parent_portal/` + `api/{guardians_admin,line_webhook}.py` + `services/{pii_retention_scheduler,parent_*,line_login_service}.py` + `utils/student_lifecycle.py` + `models/{guardian,parent_*}.py` |

---

## How to Choose（情境 → 對應 SPEC）

| 你正在做的事 | 應該讀 / 更新的 SPEC |
|--------------|----------------------|
| 改薪資 engine 主流程、加 `SalaryRecord` 欄位、調 gross_salary 公式 | SPEC-001 |
| 改節慶獎金邏輯、發放月、`meeting_absence_deduction` 規則 | SPEC-002（必同步 SPEC-001 主流程） |
| 改時薪計算基底、加班費時薪、最低工資守門 | SPEC-003（影響 SPEC-001 / SPEC-004） |
| 改考勤扣款公式、勞健保級距、補充保費、`total_deduction` 範圍 | SPEC-004（必同步 SPEC-001） |
| 改離職結算流程、資遣費新舊制、未休特休折現、proration | SPEC-005 |
| 改年終考核 payout、`appraisal_year_end_bonus` 拉取規則 | SPEC-006（必同步 SPEC-001 的 2 月特殊處理） |
| 加新 Permission、改 role 模板、調 JWT guard、`require_permission` 套用點 | SPEC-007 |
| 加新通知事件、改 channel matrix、加 channel adapter、改 after_commit hook | SPEC-008 |
| 改打卡解析、排班比對、遲到/早退判定、補卡 state machine | SPEC-009（必同步 SPEC-001 / SPEC-004 扣款） |
| 改假別 / 配額、§32 II 加班雙重上限、leave↔OT 跨類抵扣 | SPEC-010（必同步 SPEC-001 / SPEC-005） |
| 改招生漏斗階段、A/C/D at-risk 訊號、cache TTL、學期推進 | SPEC-011 |
| 改家長入口端點、LIFF 認證、refresh token rotation、PII Retention 政策 | SPEC-012（必同步 SPEC-007 / SPEC-008） |

---

## Maintenance Rules

### 何時更新 SPEC（觸發條件）

- 新增、移除、改名對外介面（HTTP endpoint / 內部 public function）
- 修改 Request / Response schema（欄位、型別、required/optional）
- 修改權限要求（`require_permission()` 引數）
- 修改業務規則或狀態機（含 default 值、邊界條件、計算公式）
- **Bug fix 改變對外行為**（呼叫端會觀察到不同輸出）

### 更新方式

- **小幅 delta**：修改變動章節 + 在 `## Changelog` 加一列 `vX.Y | YYYY-MM-DD | 變更摘要`
- **大幅重寫**：bump version 到 `v1.0`（穩定）或 `v2.0`（破壞性）；舊版本保留為單一 SPEC 內 history，不分檔
- **Bug fix variant**：在 Changelog 用 `Bugfix:` 前綴，記錄 Before / After / Unchanged / Impact Scope

### 新 SPEC

- 編號連續遞增（下一個為 SPEC-013）
- 檔名：`SPEC-{3 位數}_{小寫連字號描述}.md`（例 `SPEC-013_fees-billing.md`）
- 新增後**必須**在本索引 `## Document Index` 加一列、必要時在 `## How to Choose` 加映射

### 標記規範

- `[unverified]`：欄位 / 行為缺乏程式碼證據，需查 caller 後再確認
- `[needs review]`：業務規則邊界、權限矩陣需人工 sign-off

---

## Status 含義

| Status | 含義 |
|--------|------|
| **Draft** | 初版草稿，已對齊當前 code，但仍待跨域 review / 邊界條件確認 |
| **Accepted** | 已驗證並穩定運行；變更須走 Changelog delta |
| **Deprecated** | 已被新 SPEC 取代；保留歷史索引，正文加 `> [!WARNING]` 註明替代者 |

---

## 與其他文件的關係

- **`docs/superpowers/specs/`**：歷史 design doc 與遷移計畫（不隨 code 演進更新）；本目錄 SPEC 的 `Related` 欄位常引用其中對應 design 規格
- **`docs/superpowers/plans/`**：實作 plan / phase rollout 紀錄
- **`docs/superpowers/audits/`**：security audit findings
- **`docs/sop/`**：營運操作手冊（datetime contract、DR、Zeabur 部署、storage 部署）
- **`CLAUDE.md`** / **`AGENTS.md`**：開發協作指南；本目錄 SPEC 為其引用之介面 SoT
