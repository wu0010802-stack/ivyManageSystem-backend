# 權限 Row-Level Scoping Phase 2.4 CLASSROOM-ATTENDANCE — 待 User 決策清單

**注意：這不是 TDD plan。** Phase 2.4 涵蓋的 2 條權限（`CLASSROOMS_READ` `ATTENDANCE_READ`）在現行 codebase 沒有 row-level scope 邏輯可加，需要先做業務決策才能寫出非 placeholder 的 plan。

---

## 為何 Phase 2.4 不能直接寫 plan

Phase 2.1-2.3 的模式是：「既有 router 有 scope 邏輯（自有 SQL filter 或 portfolio_access call），把它接上 PermissionGrant scope」。

Phase 2.4 的調查結果（per `api/classrooms.py` + 4 attendance file + `api/exports.py`）：

| File | 行數 | 既有 row-level scope? | 員工/學生? |
|------|------|---------------------|-----------|
| `api/classrooms.py` | 1279 | ❌ **完全無** — 所有 endpoint 都回全校班級 list | 班級 |
| `api/attendance/records.py` | 522 | ❌ **完全無** — 全員考勤對所有 ATTENDANCE_READ 持有人可見 | 員工 |
| `api/attendance/anomalies.py` | 402 | ❌ 同上 | 員工 |
| `api/attendance/reports.py` | 564 | ❌ 同上 | 員工 |
| `api/exports.py` | 1291 | partial — 個人月報有 `enforce_self_or_full_salary` 守衛 | 員工 |

唯一相關的「自我守衛」是 `records.py:228 require_not_self_attendance()` 與 `anomalies.py:223 assert_no_self_in_batch()`（F-041 / F-042），這是「不能改自己考勤」，**不是** row scope。

如果直接套 Phase 2.1-2.3 的 `:own_class` 語意上去，會引發三個問題：

### Q1：員工考勤的「自班 scope」是什麼語意？

員工考勤是 `Attendance + Employee` join，沒有 `classroom_id`。`ATTENDANCE_READ:own_class` 對員工考勤的合理詮釋有 3+ 種：

| 詮釋 | 對應 SQL | 業務情境 |
|------|---------|---------|
| (a) 同班同事 | `Attendance.employee_id IN (SELECT id FROM Employee WHERE id IN (classroom.head_teacher / assistant / art_teacher))` | 「我只看跟我同班的搭檔出勤」 — 對導師用，但跨班科任老師看不見 |
| (b) 同園區 | `Attendance.employee_id IN (SELECT id FROM Employee WHERE campus_id = my.campus_id)` | 多園區情境（目前單園？需確認） |
| (c) 同部門 | `Attendance.employee_id IN (SELECT id FROM Employee WHERE department_id = my.department_id)` | 需要 Employee 表有 department 欄 |
| (d) 自己的 | 同 self 守衛，無 row scope | `ATTENDANCE_READ` 沒 :all 的人只能看自己 |

**目前 code 沒任何 scope filter**，所以業主對「自班」期待是什麼**完全空白**。

### Q2：班級列表 scope 該套嗎？

`api/classrooms.py` 的 `GET /classrooms` 目前回全校班級。如果加 `:own_class` 過濾：
- teacher 看不到非自己班級 → UI 切換班級下拉選單會少
- 影響 `GET /classrooms/teacher-options`：是否教師選項也只限自班同事？
- 影響 `enrollment-composition`：teacher 只看自己班級的招生組成？

每個 endpoint 都有 UI / report 側的下游 caller。直接砍 list 範圍會破壞 UX。

### Q3：匯出端點怎麼套 scope？

`api/exports.py:GET /attendance` 是整月全校彙總 PDF。如果套 `:own_class`，要不要：
- (a) 只匯出自班同事的 row？
- (b) 全校彙總但 teacher 只能下載自己那行？
- (c) 直接禁止 teacher 匯出，只 admin/hr 可用？

---

## 建議的決策路徑

**選項 A：暫緩 Phase 2.4，不做 row-level scoping**
- 把 `CLASSROOMS_READ` `ATTENDANCE_READ` 留為「role-gate」型權限（admin/hr/有權限的 teacher 都看全部）
- 改用「require_staff_permission」防 parent role 拿到後台資料即可
- 缺點：Phase 2 spec 中宣告的 12 條 scope-aware 權限變 10 條

**選項 B：只做 `CLASSROOMS_READ` Phase 2.4a，先不碰 ATTENDANCE_READ**
- 班級列表確實有 row scope 的合理業務情境（teacher 在 UI 只看自己班）
- 員工考勤的 scope 語意需要更多業主討論，先 defer
- 工作量小，可獨立 ship

**選項 C：完整 Phase 2.4 含 ATTENDANCE_READ**
- 先和業主敲定 Q1 答案
- 寫 plan、加 `accessible_employee_ids` helper（同班同事 / 同園區）
- 風險：UX 影響大，需 frontend 配合改下游 caller

**選項 D：Hybrid — Phase 2.4a CLASSROOMS_READ + Phase 2.4b ATTENDANCE_READ 拆 PR**
- 先做 CLASSROOMS_READ（容易、已有 `accessible_classroom_ids` helper 可重用）
- ATTENDANCE_READ 留待業主確認 Q1 後另寫 plan

---

## 推薦：選項 D Hybrid

理由：
1. CLASSROOMS_READ scope 業務語意明確（teacher 只看自班）— 用 portfolio_access 已就緒
2. ATTENDANCE_READ 是高風險改動（員工考勤是 HR 敏感範圍）— 不該倉促決定
3. Phase 2 spec 「4 PR ship」可改為「5 PR」（2.4a + 2.4b），仍然小步快跑

---

## 若選 D，Phase 2.4a Plan 雛形（待 user 確認後完整化）

### 涵蓋 endpoint

`api/classrooms.py`：
- `GET /classrooms`（L440）— `CLASSROOMS_READ` → 加 `accessible_classroom_ids(code=...)` filter
- `GET /classrooms/teacher-options`（L579）— `CLASSROOMS_READ` → 同上
- `GET /classrooms/{classroom_id}`（L597）— `CLASSROOMS_READ` → assert 該 id 在 accessible 範圍
- `GET /classrooms/{classroom_id}/enrollment-composition`（L618）— `CLASSROOMS_READ` → 同上
- `GET /grades`（L1217）— `CLASSROOMS_READ` → 是否套？需確認（grades 是全校設定，teacher 改自班 enrollment 也需看到所有 grade options）

### 風險

- `GET /classrooms` 是 frontend 多個 view 的下游：學生 list / 員工分配 / 招生 funnel / 班級設定。改 row scope 必須掃 caller 確認 UX 不破。
- `teacher-options` 是「指派老師到班級」的下拉選單 — teacher 改 `:own_class` 後只看自班同事，這合理嗎？業主可能期待看全校老師（便於跨班協作）。
- `GET /grades` 是年級設定（小幼、中幼、大幼等）— 應該不套 scope（全校設定）。

### Plan 結構（仿 Phase 2.1）

1. Migration `permscope05a_classrooms_seed` — seed `CLASSROOMS_READ` scope_options
2. 確認 `GET /grades` 不套 scope（在 spec 寫明）
3. 4-5 個 endpoint 加 `accessible_classroom_ids(code=...)` filter + UI smoke test
4. Final integration test

工作量估計 4-6 個 task；遠小於 Phase 2.1。

---

## 下一步

**請 user 決策：**

1. 採選項 A / B / C / D 哪個？（推薦 D Hybrid）
2. 若選 D：Phase 2.4a 涵蓋 endpoint 與「teacher-options 是否套 scope」「grades 是否套 scope」逐項確認
3. 若選 C / D-2.4b：與業主敲定員工考勤「自班」語意（同班同事 / 同園 / 同部門 / 都不對）

決策後再產出 Phase 2.4a / 2.4b 完整 TDD plan。
