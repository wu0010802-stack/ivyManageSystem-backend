# IDOR Audit Phase 1（盤查）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**：對 ivy-backend 全部 API 做 IDOR 漏洞靜態盤查，產出 `docs/superpowers/audits/2026-04-28-idor-findings.md`，每筆 finding 含位置、級別、PoC、建議修法、是否需測試。**本 plan 不修任何程式碼**，僅產文件；修補另起 Phase 2 plan。

**Architecture**：以 grep + 人工複審的混合法掃描全部 router。每個威脅模型一個或多個 task，逐模組讀檔、邊讀邊把發現寫入 audit 報告，每完成一個威脅模型 commit 一次。報告採累積式增量，所有 task 共用同一份 markdown 檔。

**Tech Stack**：Python（ivy-backend：FastAPI、SQLAlchemy、Pydantic）、Markdown 報告、git commit。

**Spec**：`docs/superpowers/specs/2026-04-28-idor-audit-design.md`（commit `abc219dc`）

---

## File Structure

| 路徑 | 動作 | 責任 |
|---|---|---|
| `docs/superpowers/audits/2026-04-28-idor-findings.md` | Create | Audit 報告主檔，所有 finding 累積寫入 |
| `docs/superpowers/audits/_grep-hits.md` | Create（暫存） | Step 2 grep 結果暫存，Phase 1 結束時刪除或保留為 appendix |

**不修改任何 `api/`、`utils/`、`tests/` 下的檔案**。

---

## 通用 Convention（所有 task 共用）

### Finding 編號規則

從 `F-001` 開始連號，跨 task 連續。每個 task 最後在報告開頭的「Index」區塊更新。

### Finding 寫入格式

每筆 finding 一節：

```markdown
### F-### [Critical|High|Medium|Low] <模組>: <一句描述>

- **位置**：`api/foo/bar.py:LINE` `METHOD /path/{param}`
- **威脅模型**：a / b / c / d / e
- **PoC**：<受害者身分 + 攻擊者身分 + 動作 + 後果，一句話>
- **根因**：<為何漏掉，例：直接 `session.get(Foo, foo_id)` 未檢查 owner>
- **建議修法**：<一句話，例：用 `assert_employee_self_or_perm()` 守門>
- **是否需新測試**：yes / no
- **修補狀態**：⏳ Pending
```

### 風險分級口訣（執行時隨手對照）

| 級別 | 條件 |
|---|---|
| **Critical** | 跨家庭/跨員工 PII 或財務外洩；或未認證即可存取需認證資料 |
| **High** | 跨班教師存取學生敏感資料；員工敏感欄位（薪資、身分證、勞保、銀行帳號）跨人 |
| **Medium** | 同角色同範疇但不該看的細節（員工看同事請假） |
| **Low** | 低敏感；或現有 Permission bitflag 已大致擋住但欄位有疏漏 |

### 確認非漏洞時

**不要**寫成 finding。在執行 task 的對話中簡短報告「`api/X.py` 已確認 OK，理由：…」即可。

### Commit 訊息

每個 task 完成後 commit，訊息格式：

```
docs(audit): IDOR Phase 1 task N - <area> findings F-NNN ~ F-MMM
```

---

## Task 1: 建立 audit 報告骨架

**Files:**
- Create: `docs/superpowers/audits/2026-04-28-idor-findings.md`

- [ ] **Step 1: 確認 audits 目錄**

Run: `ls docs/superpowers/`
Expected: 看到 `specs` 目錄；`audits` 目錄不存在（將由寫檔自動建立）。

- [ ] **Step 2: 建立 audit 報告骨架**

寫入下列內容到 `docs/superpowers/audits/2026-04-28-idor-findings.md`：

````markdown
# IDOR 全面盤查 — Findings Report

**日期**：2026-04-28
**Spec**：`docs/superpowers/specs/2026-04-28-idor-audit-design.md`
**狀態**：🚧 In Progress

> 對 ivy-backend 全部 API 路由的 IDOR 靜態盤查結果。每筆 finding 含位置、威脅模型、PoC、建議修法。
> Phase 2（修補）另起 plan。

---

## Index

（Task 完成後在此累積；格式：`- [F-NNN](#f-nnn) [<級別>] <模組>: <一句描述>`）

---

## Statistics

（Phase 1 結束時填入；按級別 × 威脅模型 × 模組統計。）

---

## Findings

（以下按 task 執行順序累積寫入。）

````

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 1 - scaffold findings report"
```

---

## Task 2: 靜態 pattern grep（建立候選清單）

**Files:**
- Create: `docs/superpowers/audits/_grep-hits.md`

- [ ] **Step 1: Grep 所有接 `*_id` path/query 參數的 endpoint**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend && \
grep -rn -E "def [a-zA-Z_]+\([^)]*[a-z_]+_id[: ]" api/ --include="*.py" | \
grep -v "__pycache__" > /tmp/idor_hit_param.txt && \
wc -l /tmp/idor_hit_param.txt
```

Expected: 數十至數百 hits。

- [ ] **Step 2: Grep 直接用 `session.get(Model, *_id)` 取物件**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend && \
grep -rn -E "session\.get\([A-Z][a-zA-Z]+, [a-z_]*id" api/ services/ repositories/ --include="*.py" | \
grep -v "__pycache__" > /tmp/idor_hit_get.txt && \
wc -l /tmp/idor_hit_get.txt
```

Expected: 中等量 hits（這類最容易藏 IDOR）。

- [ ] **Step 3: Grep 公開 router（未掛 auth）**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend && \
grep -rn -E "(APIRouter\(prefix|@\w+\.(get|post|put|patch|delete))" api/ --include="*.py" | \
grep -v "__pycache__" | head -200 > /tmp/idor_hit_router.txt && \
echo "---" && \
grep -rln "Depends(get_current_user)" api/ --include="*.py" > /tmp/idor_hit_authed.txt && \
echo "Routers without get_current_user dependency:" && \
comm -23 <(grep -rl "router = APIRouter\|@router\." api/ --include="*.py" | sort -u) <(sort -u /tmp/idor_hit_authed.txt)
```

Expected: 列出所有未引入 `get_current_user` 的 router 檔；**這些是公開端點候選**（含 line_webhook、activityPublic、auth/login、parent_portal/auth 等）。

- [ ] **Step 4: 寫入暫存 grep 結果**

把 `/tmp/idor_hit_*.txt` 內容整理成 `docs/superpowers/audits/_grep-hits.md`，分三段：

````markdown
# IDOR Grep Hits（Phase 1 暫存）

> Task 2 產出，供後續 task 複審使用。Phase 1 結束時可刪除或併入 audit 報告 appendix。

## A. 接 `*_id` 參數的 endpoint

```
<貼上 /tmp/idor_hit_param.txt 內容>
```

## B. `session.get(Model, *_id)` 候選

```
<貼上 /tmp/idor_hit_get.txt 內容>
```

## C. 未掛 `get_current_user` 的 router 檔

```
<列出 step 3 comm 結果>
```
````

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/_grep-hits.md
git commit -m "docs(audit): IDOR Phase 1 task 2 - static grep candidate hits"
```

---

## Task 3: parent_portal/* 盤查（威脅模型 c — 公開面向最危險）

**Files to read:**
- `api/parent_portal/_shared.py`（先理解 helper、ParentBinding 邏輯）
- `api/parent_portal/auth.py`
- `api/parent_portal/profile.py`
- `api/parent_portal/binding_admin.py`
- `api/parent_portal/home.py`
- `api/parent_portal/attendance.py`
- `api/parent_portal/leaves.py`
- `api/parent_portal/activity.py`
- `api/parent_portal/announcements.py`
- `api/parent_portal/events.py`
- `api/parent_portal/fees.py`

**Files to write:**
- Append findings to `docs/superpowers/audits/2026-04-28-idor-findings.md`

- [ ] **Step 1: 讀 `_shared.py` 與 `auth.py` 建立基線理解**

讀完後在執行對話中簡述：parent_portal 如何識別家長身分？ParentBinding 表結構？helper（例：`_get_bound_student_ids`）長相？

關鍵問題：
- 每個帶 `student_id` 的 endpoint 是否一律過 binding 比對？
- 還是有些直接用 `session.get(Student, student_id)`？

- [ ] **Step 2: 逐檔讀其餘 9 個 parent_portal 檔，記錄候選**

每讀一檔，對每個帶 `student_id` 或其他資源 id 的 endpoint，檢查：
1. 是否經過 ParentBinding 比對（白名單 student_ids）？
2. 若資源是學生子物件（活動報名、出缺、請假、繳費），是否驗證 owner_student_id ∈ 綁定學生？
3. 公開 endpoint（如 `auth/login`）是否能列舉家長帳號 / 觀察 timing 差異？

把疑慮 endpoint 抄到 scratch（執行對話中），不需另開檔。

- [ ] **Step 3: 對每個確認的漏洞寫 finding**

從 `F-001` 開始連號，append 到 `docs/superpowers/audits/2026-04-28-idor-findings.md` 的 `## Findings` 段尾，使用本 plan「通用 Convention → Finding 寫入格式」之 template。

家長端典型威脅模型一律標 **c**；級別預設 **Critical**（除非僅為公告/活動清單等低敏感）。

- [ ] **Step 4: 更新 Index**

把這批 finding 補到報告開頭的 `## Index` 段。

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 3 - parent_portal findings"
```

---

## Task 4: portal/* 盤查（威脅模型 a + b — 教師端混合）

**Files to read（依序）：**
- `api/portal/_shared.py`
- `api/portal/profile.py`（員工自查 — 威脅 a）
- `api/portal/salary.py`、`api/portal/overtimes.py`、`api/portal/leaves.py`、`api/portal/punch_corrections.py`、`api/portal/anomalies.py`、`api/portal/attendance.py`、`api/portal/dismissal_calls.py`（威脅 a 為主）
- `api/portal/students.py`、`api/portal/student_attendance.py`、`api/portal/assessments.py`、`api/portal/incidents.py`、`api/portal/announcements.py`、`api/portal/calendar.py`、`api/portal/schedule.py`、`api/portal/activity.py`（威脅 b 為主）

- [ ] **Step 1: 讀 `_shared.py` 建立基線**

關鍵問題：
- 教師端如何判斷「自己」？`current_user.user_id` → `Employee`？
- 是否有「老師班級」helper（取目前學期任教班級）？
- 員工自查（薪資/加班/請假/補打卡）是否一律用 `current_user.user_id` 推導 employee_id，**而不是**接受 client 傳的 employee_id？

- [ ] **Step 2: 逐檔讀員工自查類（profile, salary, overtimes, leaves, punch_corrections, anomalies, attendance, dismissal_calls）**

對每個 endpoint：
1. 帶不帶 `employee_id` path 參數？若帶，是否強制 == 當前員工 id？
2. 帶不帶 record id（leave_id / overtime_id / punch_correction_id）？是否驗證 record.employee_id == 當前員工？
3. 是否有「主管查屬下」的越權路徑？

威脅 a，級別 Critical（薪資、補打卡）/ High（請假、加班）/ Medium（出缺、考勤明細）。

- [ ] **Step 3: 逐檔讀學生資料類（students, student_attendance, assessments, incidents, announcements, calendar, schedule, activity）**

對每個帶 `student_id` 的 endpoint：
1. 是否用「老師目前學期任教班級的學生白名單」過濾？
2. 是否有 admin/園長 perm 可越過班級限制（這是合理）？
3. 副班導／班導區分是否影響？

威脅 b，級別 High（健康、事件、評估）/ Medium（出缺）/ Low（行事曆）。

- [ ] **Step 4: 寫 finding（連號接續 task 3）**

Append 到 `docs/superpowers/audits/2026-04-28-idor-findings.md`。

- [ ] **Step 5: 更新 Index 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 4 - portal teacher-side findings"
```

---

## Task 5: 員工財務 / 人事頂層 router 盤查（威脅模型 a — 非 portal 路徑）

> 與 task 4 區別：portal/* 是教師自助；本 task 是 admin / HR / 會計使用的頂層 router，可能看任意員工的資料，需確認 Permission bitflag 是否到位、且**沒繞過**。

**Files to read:**
- `api/salary.py`
- `api/overtimes.py`
- `api/leaves.py`
- `api/leaves_quota.py`
- `api/leaves_workday.py`
- `api/punch_corrections.py`
- `api/employees.py`
- `api/employees_docs.py`
- `api/bonus_preview.py`
- `api/insurance.py`
- `api/salary_fields.py`
- `api/shifts.py`

- [ ] **Step 1: 讀 salary、employees、employees_docs、insurance、bonus_preview**

對每個帶 `employee_id` 或 record id 的 endpoint：
1. Permission bitflag 是否覆蓋（例：`Permission.READ_SALARY`、`READ_EMPLOYEE_SENSITIVE`）？
2. 欄位級洩漏：低權限角色拿到的 response 是否仍含敏感欄位（身分證、銀行帳號、薪資金額）？
3. 是否有 endpoint 標榜「自己用」但接受 employee_id 參數而沒 == 自己？（這是 a 經典）

威脅 a / e，級別 Critical（薪資金額、銀行帳號、身分證跨人）/ High（敏感人事欄位）。

- [ ] **Step 2: 讀 overtimes、leaves、leaves_quota、leaves_workday、punch_corrections、shifts**

對每個 record CRUD endpoint：
1. PUT/DELETE：是否驗證 record.employee_id == 當前員工，或當前員工持有審核權？
2. List：是否依 perm 限制 scope（自己 vs 全員）？
3. 是否有「審核者」端點被一般員工撞到？

- [ ] **Step 3: 寫 finding**

Append 到報告。

- [ ] **Step 4: 更新 Index 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 5 - employee financial/HR top-level findings"
```

---

## Task 6: 學生資料頂層 router 盤查（威脅模型 b — 非 portal 路徑）

**Files to read:**
- `api/students.py`
- `api/student_assessments.py`
- `api/student_attendance.py`
- `api/student_change_logs.py`
- `api/student_communications.py`
- `api/student_enrollment.py`
- `api/student_health.py`
- `api/student_incidents.py`
- `api/student_leaves.py`
- `api/portfolio/observations.py`
- `api/classrooms.py`
- `api/meetings.py`

- [ ] **Step 1: 讀 students.py、classrooms.py 建立基線**

關鍵問題：
- 班級成員 list 是否依 perm 限制範圍？
- 學生 detail endpoint：哪些欄位需要 `Permission.READ_STUDENT_SENSITIVE`？

- [ ] **Step 2: 逐檔讀 student_*.py 子模組**

對每個帶 `student_id` 或 record id 的 endpoint：
1. 班導 / 副班導是否限制在自己班學生？
2. record CRUD：是否驗證 record.student_id 在當前 user 可視範圍？
3. 健康、聯絡簿、事件等敏感資料的 perm 是否到位？

威脅 b / e，級別 High（健康、聯絡資訊、事件）/ Medium（出缺、評估）/ Low（行事曆）。

- [ ] **Step 3: 讀 portfolio/observations.py、meetings.py**

特別注意：portfolio 已有 `utils/portfolio_access.py`，先確認所有 portfolio endpoint 都有調用該 helper；若有遺漏，列為 finding。

- [ ] **Step 4: 寫 finding 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 6 - student-data top-level findings"
```

---

## Task 7: activity / recruitment / 公開報名盤查（威脅模型 c + d）

**Files to read:**
- `api/activity/_shared.py`
- `api/activity/courses.py`
- `api/activity/registrations.py`
- `api/activity/attendance.py`
- `api/activity/inquiries.py`
- `api/activity/pos.py`
- `api/activity/pos_approval.py`
- `api/activity/public.py`（公開）
- `api/activity/settings.py`
- `api/activity/stats.py`
- `api/activity/supplies.py`
- `api/recruitment/shared.py`
- `api/recruitment/competitors.py`
- `api/recruitment/hotspots.py`
- `api/recruitment/market.py`
- `api/recruitment/periods.py`
- `api/recruitment/records.py`
- `api/recruitment/stats.py`
- `api/recruitment_gov_kindergartens.py`
- `api/recruitment_ivykids.py`
- `api/line_webhook.py`

- [ ] **Step 1: 讀 activity/public.py（公開端點）**

這支提供家長未登入即可使用的公開報名介面。重點檢查：
1. 接受哪些參數？是否有 enumerate-able id？
2. 是否能透過枚舉 student_id 取回個資（姓名、繳費狀態）？
3. registration update 是否驗證家長身分？

> 補充：業主明確表示家長透過 `public_update` 自助加課佔名額（含搶位）為**預期行為**，這部分**不算 IDOR 漏洞**；但若該流程能讓任意人**讀取**他人個資（姓名、聯絡方式、繳費狀態），仍應列為 finding。

威脅 c + d，多為 Critical（公開可枚舉個資）。

- [ ] **Step 2: 讀 activity 其餘子模組（管理端）**

對每個帶 registration_id / course_id / student_id 的 endpoint：
1. 是否限制只能操作自己班 / 自己負責活動？
2. POS 結帳是否驗證 staff 身分？

威脅 b / e。

- [ ] **Step 3: 讀 recruitment/* 與 line_webhook.py**

關鍵問題：
- recruitment 端點是否全部要求登入？
- line_webhook 是否驗證 LINE 簽章（不是 IDOR 但同屬未認證信任）？順手記錄非 IDOR 但相關發現於 audit 末尾「附帶觀察」段。

- [ ] **Step 4: 寫 finding 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 7 - activity/recruitment/public findings"
```

---

## Task 8: admin / 財務報表 router 盤查（威脅模型 e — 高權限欄位級）

**Files to read:**
- `api/fees.py`
- `api/insurance.py`
- `api/gov_reports.py`
- `api/reports.py`
- `api/exports.py`
- `api/analytics.py`
- `api/audit.py`
- `api/approvals.py`
- `api/approval_settings.py`

- [ ] **Step 1: 對每個 report / export endpoint 檢查**

1. Permission bitflag 是否到位（READ_FINANCE / EXPORT 之類）？
2. 接收的 filter 參數（employee_id、student_id、classroom_id）是否限制在 user 可視範圍？
3. 大量 export 是否被低權限觸發（例：HR 看到全員薪資）？

- [ ] **Step 2: audit.py / approvals.py 特別檢查**

審核端點：是否任意員工能改別人的審核狀態？AuditLog 查詢是否限制 scope？

- [ ] **Step 3: fees.py 重點檢查**

費用 CRUD 是否限制在學生 owner 範圍？退費操作的 perm？

- [ ] **Step 4: 寫 finding 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 8 - admin/finance reports findings"
```

---

## Task 9: 其餘模組盤查（補完）

**Files to read:**
- `api/announcements.py`
- `api/attachments.py`
- `api/events.py`
- `api/notifications.py`
- `api/config.py`
- `api/dev.py`
- `api/dismissal_calls.py`
- `api/dismissal_ws.py`（WebSocket — 重點看連線時 user scope）
- `api/attendance/anomalies.py`、`api/attendance/records.py`、`api/attendance/reports.py`、`api/attendance/upload.py`
- `api/auth.py`
- `api/health.py`

- [ ] **Step 1: 逐檔快速掃**

每檔重點：
- 接受 id 的 endpoint 有沒有越權風險？
- 通知中心：能否讀別人的通知？
- 出勤上傳：能否覆蓋別人的紀錄？
- WS：連線後是否限制只接收自己 / 自己班的事件？

- [ ] **Step 2: 寫 finding 並 commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md
git commit -m "docs(audit): IDOR Phase 1 task 9 - remaining modules findings"
```

---

## Task 10: 統計與 SECURITY_AUDIT.md 整合

**Files:**
- Modify: `docs/superpowers/audits/2026-04-28-idor-findings.md`（填入 Statistics、改狀態）
- Modify: `SECURITY_AUDIT.md`（新增 IDOR section 連結）
- Delete: `docs/superpowers/audits/_grep-hits.md`（暫存檔，留下會誤導）

- [ ] **Step 1: 統計表填入 audit 報告**

於 `## Statistics` 段填入：

````markdown
## Statistics

### 按級別

| 級別 | 筆數 |
|---|---|
| Critical | N |
| High | N |
| Medium | N |
| Low | N |
| **總計** | N |

### 按威脅模型

| 模型 | 描述 | 筆數 |
|---|---|---|
| a | 員工互查 | N |
| b | 跨班教師 | N |
| c | 家長跨家庭 | N |
| d | 未認證公開 | N |
| e | 高權限欄位級 | N |

### 按模組（Top 10）

| 模組 | 筆數 |
|---|---|
| ... | ... |
````

- [ ] **Step 2: 報告狀態改為「✅ Phase 1 Complete」**

把報告開頭的 `**狀態**：🚧 In Progress` 改為 `**狀態**：✅ Phase 1 Complete`，並在「Statistics」之後加一段：

```markdown
---

## Phase 2 規劃

依本報告 Critical / High / Medium / Low 分批修補。Phase 2 plan：`docs/superpowers/plans/2026-04-XX-idor-fix-phase2.md`（尚未撰寫）。
```

- [ ] **Step 3: 更新 `SECURITY_AUDIT.md`**

讀現有 `SECURITY_AUDIT.md` 末尾，新增一節：

```markdown
## IDOR 全面盤查（2026-04-28）

詳見：
- 設計：`docs/superpowers/specs/2026-04-28-idor-audit-design.md`
- 盤查報告：`docs/superpowers/audits/2026-04-28-idor-findings.md`

### 摘要
- Critical：N 筆，Phase 2 修補中
- High：N 筆，Phase 2 修補中
- Medium：N 筆，待業主排程
- Low：N 筆，待業主排程
```

（N 替換為實際數字。）

- [ ] **Step 4: 刪除暫存 grep 檔**

```bash
rm docs/superpowers/audits/_grep-hits.md
```

- [ ] **Step 5: Final commit**

```bash
git add docs/superpowers/audits/2026-04-28-idor-findings.md SECURITY_AUDIT.md
git rm docs/superpowers/audits/_grep-hits.md
git commit -m "docs(audit): IDOR Phase 1 task 10 - finalize report and integrate SECURITY_AUDIT.md"
```

- [ ] **Step 6: 回報結束**

在 chat 中對使用者報告：
1. 各級別 finding 數量
2. 最該優先處理的 3 筆 critical
3. 建議下一步：撰寫 Phase 2 修補 plan（另起 brainstorming → writing-plans 流程？或直接根據 audit 寫 fix plan？）

---

## Phase 1 完成判準

- [x] `docs/superpowers/audits/2026-04-28-idor-findings.md` 存在且狀態為「✅ Phase 1 Complete」
- [x] 報告涵蓋 spec section 0「範圍邊界」列出的全部模組
- [x] Statistics 段有實際數字
- [x] `SECURITY_AUDIT.md` 已新增 IDOR section
- [x] 暫存 `_grep-hits.md` 已刪除
- [x] 全部 task 各自 commit、訊息符合格式

---

## 不在本 plan 範圍

- ❌ 修補任何程式碼（屬 Phase 2）
- ❌ 撰寫 pytest（屬 Phase 2）
- ❌ 抽 `utils/idor_guards.py` helper（屬 Phase 2）
- ❌ 重構既有 `finance_guards` / `portfolio_access`（spec 已排除）
- ❌ 前端 IDOR（spec 已排除：前端只是 UX）
