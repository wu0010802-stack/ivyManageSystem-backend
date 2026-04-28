# IDOR 全面盤查 — Findings Report

**日期**：2026-04-28
**Spec**：`docs/superpowers/specs/2026-04-28-idor-audit-design.md`
**Plan**：`docs/superpowers/plans/2026-04-28-idor-audit-phase1.md`
**狀態**：🚧 In Progress

> 對 ivy-backend 全部 API 路由的 IDOR 靜態盤查結果。每筆 finding 含位置、威脅模型、PoC、建議修法。
> Phase 2（修補）另起 plan。

---

## Index

- [F-001](#f-001) [High] parent_portal/auth: `bind` 未檢查 guardian 已被他人認領，可被覆寫綁定
- [F-002](#f-002) [Low] parent_portal/fees: `GET /fees/records/{record_id}/payments` 404 vs 403 可枚舉費用記錄存在性
- [F-003](#f-003) [Low] parent_portal/activity: `GET /activity/registrations/{registration_id}/payments` 404 vs 403 可枚舉報名記錄存在性
- [F-004](#f-004) [Low] parent_portal/leaves: `GET /{leave_id}` 與 `POST /{leave_id}/cancel` 404 vs 403 可枚舉請假申請存在性
- [F-005](#f-005) [Medium] portal/leaves: `_check_substitute_leave_conflict` 透過 409 detail 洩漏代理人請假/加班區間與狀態，可被探測同事行程
- [F-006](#f-006) [Low] portal/dismissal_calls: `acknowledge` 與 `complete` 404 vs 403 可枚舉接送通知存在性
- [F-007](#f-007) [Low] portal/incidents: `POST /incidents` 404 vs 403 可枚舉學生 ID 存在性
- [F-008](#f-008) [Low] portal/assessments: `POST /assessments` 404 vs 403 可枚舉學生 ID 存在性
- [F-009](#f-009) [Low] portal/announcements: `POST /announcements/{announcement_id}/read` 缺少可見性檢查並可枚舉公告存在性
- [F-010](#f-010) [Low] portal/activity: `GET /activity/attendance/sessions/{session_id}` 即使該場次不含自班學生仍回傳場次中介資料，可枚舉場次存在性與課程名稱
- [F-011](#f-011) [Low] portal/leaves: 補休申請 `source_overtime_id` 400 vs 403 可枚舉加班記錄存在性
- [F-012](#f-012) [High] employees: `GET /employees/{employee_id}/final-salary-preview` 缺 `_enforce_self_or_full_salary`，可看任意員工最終薪資結算
- [F-013](#f-013) [High] salary: `GET /salaries/festival-bonus` 與 `/salaries/festival-bonus/period-accrual` 未限縮查詢者，回傳全員節慶獎金與期中累積金額
- [F-014](#f-014) [High] employees_docs: `GET /employees/{employee_id}/contracts` 回傳 `salary_at_contract`，繞過 `_enforce_self_or_full_salary`
- [F-015](#f-015) [High] punch_corrections: `PUT /punch-corrections/{correction_id}/approve` 缺自我核准守衛，可自審補打卡（直接影響本人薪資扣款）
- [F-016](#f-016) [Medium] bonus_preview: `GET /bonus-preview/dashboard` 與 `POST /bonus-impact-preview` 以 `STUDENTS_READ`/`STUDENTS_WRITE` 控管，但回傳每位教師估算節慶獎金金額
- [F-017](#f-017) [Medium] employees: `GET /employees` 與 `GET /employees/{id}` 回傳 `base_salary` / `hourly_rate` 給僅持 `EMPLOYEES_READ` 之自訂角色（無 `SALARY_READ`）
- [F-018](#f-018) [High] students/classrooms: `GET /students` / `GET /students/{id}` / `GET /students/{id}/profile` / `GET /classrooms/{id}` 在 `STUDENTS_READ` / `CLASSROOMS_READ` 下回傳 `allergy` / `medication` / `special_needs`，繞過 `STUDENTS_HEALTH_READ` 與 `STUDENTS_SPECIAL_NEEDS_READ`
- [F-019](#f-019) [High] student_communications: 全 CRUD 無班級 scope；持 `STUDENTS_READ`/`STUDENTS_WRITE` 之自訂角色可讀／改／刪別班學生家長溝通紀錄
- [F-020](#f-020) [High] student_attendance: `batch` / `by-student` / `monthly` / `export` 無班級 scope；可任意改寫別班出席（影響家長端／薪資未連動但屬學生記錄完整性）
- [F-021](#f-021) [High] student_leaves: `POST /{leave_id}/approve` 與 `/reject` 無班級 scope，可審核別班家長端請假並寫入 `StudentAttendance`
- [F-022](#f-022) [Medium] student_change_logs: list/summary/export/CRUD 無班級 scope；持 `STUDENTS_READ` 可讀全校異動軌跡（含轉班/退學/休學原因）
- [F-023](#f-023) [Medium] student_incidents/assessments: list 端點 `student_id` 與 `classroom_id` 都未帶時跳過 `_require_classroom_access`，回傳全校事件／評量
- [F-024](#f-024) [Medium] students/records: `GET /students/records` 時間軸（`services/student_records_timeline`）無 viewer-side 班級過濾，回傳全校事件＋評量＋異動
- [F-025](#f-025) [Medium] students: `GET /students/{student_id}/guardians` 缺班級 scope，可跨班讀家長聯絡資料
- [F-026](#f-026) [Medium] activity/registrations: `GET /registrations` / `GET /registrations/{id}` / `GET /registrations/pending` 在 `ACTIVITY_READ` 下回傳 `parent_phone` / `birthday` / `email` / `student_id` / `classroom_id`，繞過 `GUARDIANS_READ` / `STUDENTS_READ`
- [F-027](#f-027) [Medium] activity/registrations: `GET /students/search` 僅以 `ACTIVITY_WRITE` 守門，回傳全校在校生 `student_id` 學號 / `birthday` / `parent_phone`，繞過 `STUDENTS_READ`
- [F-028](#f-028) [Low] activity/pos: `GET /pos/outstanding-by-student` / `GET /pos/recent-transactions` 在 `ACTIVITY_READ` 下回傳全校學生 `student_name` / `birthday` / `class_name`，可被一線櫃檯偷帶走
- [F-029](#f-029) [Low] activity/public: `POST /public/update` 換手機號 409 `此手機號碼已被其他報名使用` 形成 phone enumeration oracle
- [F-030](#f-030) [Medium] activity/public: `POST /public/register` 多重未認證枚舉 oracle（學生姓名/生日 + 家長電話）
- [F-031](#f-031) [High] reports/finance-summary: `GET /finance-summary/detail` 與 `/finance-summary/export` 在 `Permission.REPORTS` 下回傳逐員 `gross_salary` / `net_salary` / `employer_benefit` / `real_cost`，繞過 `SALARY_READ`
- [F-032](#f-032) [High] exports: `GET /exports/employee-attendance?employee_id=...` 缺自我守衛，可任意員工 id 拉同事個人逐日打卡明細
- [F-033](#f-033) [Medium] exports/gov_reports: GET 匯出（students / attendance / leaves / overtimes / shifts / employee-attendance / 政府申報四端點）未呼叫 `write_explicit_audit`，PII 與身分證匯出無稽核軌跡
- [F-034](#f-034) [Medium] fees: `GET /records?student_id=...` 跨班讀全校學生繳費紀錄，僅以 `FEES_READ` 守門
- [F-035](#f-035) [Low] audit-logs: `GET /audit-logs/export` 自身未呼叫 `write_explicit_audit`，匯出全系統操作軌跡的事件本身無痕

---

## Statistics

> （Phase 1 結束時填入；按級別 × 威脅模型 × 模組統計。）

---

## Findings

### F-001 [High] parent_portal/auth: `bind` 未檢查 guardian 已被他人認領，可被覆寫綁定

- **位置**：`api/parent_portal/auth.py:358` `POST /api/parent/auth/bind`
- **威脅模型**：c
- **PoC**：家長 B 的綁定碼若外洩（行政誤傳、家長截圖外傳、訊息攔截），家長 A 用未綁定 LINE 帳號 + 該碼呼叫 `/auth/bind`，因 `bind` 直接 `guardian.user_id = user.id`（line 358）覆寫舊值，A 立即取得 B 小孩的 Guardian 綁定，可看 B 小孩全部 PII（姓名、班級、出席、健康、費用、聯絡資訊）。
- **根因**：`bind` 缺少 `if guardian.user_id and guardian.user_id != user.id: 拒絕` 守衛；`bind-additional` 在同一檔 line 414 已有這層檢查，bind 漏掉。
- **建議修法**：在 `bind` line 358 之前加上等價於 `bind-additional` line 414 的檢查：若 `guardian.user_id` 非空且不屬於即將綁定的 user，rollback 並 400/409 拒絕；同時記錄 audit log（綁定碼可能外洩）。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-002 [Low] parent_portal/fees: `GET /fees/records/{record_id}/payments` 404 vs 403 可枚舉費用記錄存在性

- **位置**：`api/parent_portal/fees.py:151-158` `GET /api/parent/fees/records/{record_id}/payments`
- **威脅模型**：c
- **PoC**：家長 A 暴力遞增 `record_id` 呼叫此 endpoint。不存在 → 404，存在但不屬於 A → 403（由 `_assert_student_owned` 拋出）。可區分兩種狀態，從而枚舉系統內 fee record id 範圍 / 任意 student 是否有費用記錄。雖未直接洩漏金額，但能側面推斷 record id 序號分布與其他家庭是否有逾期費用。
- **根因**：先 `session.query(StudentFeeRecord).filter(id==record_id).first()` 再 `_assert_student_owned`，兩種失敗路徑回傳不同 status code。
- **建議修法**：當 record 不存在或非自己小孩時，一律回 403（或一律 404），不揭露差異；helper 內部統一處理。
- **是否需新測試**：no（Low；列為 Phase 2 順手處理）
- **修補狀態**：⏳ Pending

### F-003 [Low] parent_portal/activity: `GET /activity/registrations/{registration_id}/payments` 404 vs 403 可枚舉報名記錄存在性

- **位置**：`api/parent_portal/activity.py:347-359` `GET /api/parent/activity/registrations/{registration_id}/payments`（同樣 pattern 也存在於 `POST /activity/registrations/{registration_id}/confirm-promotion` line 301-313）
- **威脅模型**：c
- **PoC**：家長 A 暴力遞增 `registration_id`。不存在 → 404，存在但非 A 小孩 → 403。可枚舉 ActivityRegistration id 序號、推斷其他家庭是否有 active 報名。
- **根因**：先 `session.query(ActivityRegistration).first()` 再 `_assert_student_owned`，404/403 路徑分岔。
- **建議修法**：同 F-002，一律 403（或一律 404）。建議集中於共用 helper（`_get_owned_registration_or_403`）。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-004 [Low] parent_portal/leaves: `GET /{leave_id}` 與 `POST /{leave_id}/cancel` 404 vs 403 可枚舉請假申請存在性

- **位置**：`api/parent_portal/leaves.py:164` `GET /api/parent/leaves/{leave_id}` 與 `api/parent_portal/leaves.py:185` `POST /api/parent/leaves/{leave_id}/cancel`
- **威脅模型**：c
- **PoC**：家長 A 暴力遞增 `leave_id`。不存在 → 404，存在但非 A 小孩 → 403（`_assert_student_owned` 拋出）。可枚舉 StudentLeaveRequest id 序號分布、推測其他家庭是否有 pending/approved 請假紀錄。
- **根因**：與 F-002/F-003 相同 — 先 query 再 ownership check，兩種失敗路徑分岔。
- **建議修法**：同 F-002/F-003，一律 403（或一律 404）；建議集中於共用 helper。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-005 [Medium] portal/leaves: `_check_substitute_leave_conflict` 409 detail 洩漏代理人請假/加班區間與狀態，可被探測同事行程

- **位置**：helper 在 `api/leaves.py:450-502`；portal 入口在 `api/portal/leaves.py:224` `POST /api/portal/my-leaves`（建立假單時若帶 `substitute_employee_id` 觸發）
- **威脅模型**：a
- **PoC**：員工 A 想刺探員工 B 的請假/加班排程：A 反覆呼叫 `POST /api/portal/my-leaves`，故意填入 `substitute_employee_id=B`，搭配不同 `start_date`/`end_date`/`start_time`/`end_time`。一旦命中 B 的請假或加班時段，伺服器會回 409，detail 內含 `代理人在 {start_date} ~ {end_date} 已有{待審核|已核准}請假記錄` 或 `代理人在 {date} 有{狀態}加班記錄`，等於把 B 的具體日期區間與審批狀態回傳給 A。重複二分搜尋即可還原 B 整段排假/加班行事曆。
- **根因**：`_check_substitute_leave_conflict` 為了讓 UI 顯示「代理人衝突」直接把對方的日期、狀態回灌進錯誤訊息；portal 端沒有把訊息泛化或抹除日期/狀態欄位。
- **建議修法**：對於從 portal 發起的代理人衝突檢查，回傳泛化訊息（例：`此代理人於該時段不可用，請改派他人`），不揭露對方假單/加班的日期、起訖時間與審批狀態；管理端 `api/leaves.py` 維持原訊息但限定僅在持有 `LEAVES_READ` 等高權限呼叫情境下顯示。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-006 [Low] portal/dismissal_calls: `acknowledge` 與 `complete` 404 vs 403 可枚舉接送通知存在性

- **位置**：`api/portal/dismissal_calls.py:128-131` `_db_transition_call`；端點 `POST /api/portal/dismissal-calls/{call_id}/acknowledge`（line 176）與 `POST /api/portal/dismissal-calls/{call_id}/complete`（line 192）
- **威脅模型**：b
- **PoC**：教師 A 暴力遞增 `call_id`。不存在 → 404 `找不到通知`；存在但屬於別班 → 403 `無權操作此通知`；存在且屬於本班但狀態不對 → 422。三種狀態碼分岔等於可枚舉 `StudentDismissalCall.id` 是否存在以及對應班級是否屬於 A，再交叉比對 `/portal/my-students` 即能還原當日其他班的接送通知數量分布。
- **根因**：先 `session.query(...).first()` 判 404，再以 `call.classroom_id not in classroom_ids` 判 403，兩條路徑回不同 status code。
- **建議修法**：當通知不存在或屬於非本班時統一回 404（或統一 403），勿揭露差異；建議在 helper 內集中處理。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-007 [Low] portal/incidents: `POST /incidents` 404 vs 403 可枚舉學生 ID 存在性

- **位置**：`api/portal/incidents.py:90-94` `POST /api/portal/incidents`
- **威脅模型**：b
- **PoC**：教師 A 暴力遞增 `payload.student_id`。學生不存在 → 404 `STUDENT_NOT_FOUND`；學生存在但不屬於 A 班 → 403 `無權為此學生填寫事件紀錄`。可枚舉 `Student.id` 序號分布；若再交叉 `/my-students`，可推估其他班學生的 id 範圍與在學狀態。
- **根因**：先 `session.query(Student).filter(Student.id == payload.student_id).first()` 判存在性，再以 `student.classroom_id not in classroom_ids` 判班級歸屬，兩條路徑分流回不同 status code。此外此處也未過濾 `Student.is_active`，已畢業/退學學生若仍存在 row 也會走 403 路徑。
- **建議修法**：兩種失敗一律回同一 status code（建議 404，與 STUDENT_NOT_FOUND 一致），並加上 `Student.is_active.is_(True)` 條件避免揭露已停用學生。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-008 [Low] portal/assessments: `POST /assessments` 404 vs 403 可枚舉學生 ID 存在性

- **位置**：`api/portal/assessments.py:84-88` `POST /api/portal/assessments`
- **威脅模型**：b
- **PoC**：與 F-007 相同模式：教師 A 列舉 `payload.student_id`。學生不存在 → 404；存在但不屬於本班 → 403 `無權為此學生填寫評量記錄`。可枚舉學生 id 序號分布。
- **根因**：與 F-007 相同：先 query 再班級歸屬檢查，兩路徑回不同 status code；亦未過濾 `Student.is_active`。
- **建議修法**：同 F-007，一律回同一 status code 並加上 `is_active` 條件。建議與 F-007 共用同一個 `_assert_teacher_owns_student` helper（IDOR design 第 4 節已預留）。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-009 [Low] portal/announcements: `POST /announcements/{announcement_id}/read` 缺少可見性檢查並可枚舉公告存在性

- **位置**：`api/portal/announcements.py:74-101` `POST /api/portal/announcements/{announcement_id}/read`
- **威脅模型**：a
- **PoC**：員工 A 暴力遞增 `announcement_id`。公告不存在 → 404 `ANNOUNCEMENT_NOT_FOUND`；公告存在但非 targeted 給 A → 200 並寫入 `AnnouncementRead(announcement_id, employee_id=A)`。差異化的 status code 可枚舉所有 `Announcement.id`；同時 A 可任意把不該屬於自己的指定公告標為已讀，使自己 `/portal/unread-count` 失真（`total - read` 可能歸零，遮蔽真正待讀公告，間接干擾己身公告 UX，但不直接竄改他人狀態）。
- **根因**：`mark_announcement_read` 只查 `Announcement` 是否存在，沒有比對 `AnnouncementRecipient`（即 `get_portal_announcements` 用的 `visible_filter`）；可見性檢查只做在 list 端點，未做在 mark-read 端點。
- **建議修法**：寫入 `AnnouncementRead` 前先套用相同的 `visible_filter`（無 recipients 或 recipients 含當前 emp_id）；不可見即回 404，與不存在時相同訊息，避免列舉。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-010 [Low] portal/activity: `GET /activity/attendance/sessions/{session_id}` 場次中介資料無視自班學生即外洩

- **位置**：`api/portal/activity.py:290-318` `GET /api/portal/activity/attendance/sessions/{session_id}`
- **威脅模型**：b
- **PoC**：教師 A（即使無管轄班級或本場次無自班學生）暴力遞增 `session_id`。不存在 → 404 `找不到場次`；存在 → 200 並回傳完整 session 物件含 `course_name`、`session_date`、`notes`、`created_by`、`created_at`（見 `api/activity/_shared.py:1385-1397` `_build_session_detail_response`），即使 `students` 因 `classroom_ids_filter` 過濾為空也照樣外露課程名稱與日期等中介資料。教師端可由此推算每堂課的開課時程與授課動態，包含其他班別參與的課程。
- **根因**：權限只擋 `students` 列表（用 `classroom_ids_filter`），對 session 根節點欄位（`course_name`、`notes` 等）未做門檻；當教師完全沒有自班學生在該場次時應視為無權查閱。
- **建議修法**：當 `classroom_ids_filter` 套用後 `students` 為空且該教師對課程無其他存取權限時，直接回 404（或 403），勿外露課程／場次中介資料。或在進入 helper 前先驗證教師有至少一筆自班 enrollment 落在此 session 對應 course。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-011 [Low] portal/leaves: 補休申請 `source_overtime_id` 400 vs 403 可枚舉加班記錄存在性

- **位置**：`api/portal/leaves.py:273-283` `POST /api/portal/my-leaves`（leave_type=compensatory 流程）
- **威脅模型**：a
- **PoC**：員工 A 用 `leave_type=compensatory` 暴力遞增 `source_overtime_id` 提交申請。不存在 → 400「來源加班記錄不存在」；存在但非 A 的 → 403「來源加班記錄不屬於本人」。可枚舉 OvertimeRecord id 序號分布，推測同事是否有加班紀錄。
- **根因**：先 `session.query(OvertimeRecord).filter(id==source_overtime_id).first()` 再檢查 owner，兩種失敗路徑 status code + detail 差異化。
- **建議修法**：合併為單一 status code（例：一律 400「來源加班記錄無效或無權使用」），不揭露存在性差異。
- **是否需新測試**：no
- **修補狀態**：⏳ Pending

### F-012 [High] employees: `GET /employees/{employee_id}/final-salary-preview` 缺 `_enforce_self_or_full_salary`，可看任意員工最終薪資結算

- **位置**：`api/employees.py:553-644` `GET /api/employees/{employee_id}/final-salary-preview`
- **威脅模型**：a
- **PoC**：任何持有 `Permission.SALARY_READ` 但非 admin/hr 角色的使用者（例：自訂角色僅給 SALARY_READ 供查自己歷史薪資；或主管被臨時授予 SALARY_READ）對任意 `employee_id` 呼叫此端點。response 直接回傳該員工 `contracted_base_salary` / `base_salary`（含月中離職折算） / `festival_bonus` / `gross_salary` / `total_deduction` / `labor_insurance` / `health_insurance` / `pension` / `net_salary` / `unused_annual_leave_compensation` / `net_salary_with_unused_annual`，等於別人離職當月的完整薪資結算。salary.py 內所有 `record_id` 與 `employee_id` 端點（`/breakdown`、`/audit-log`、`/field-breakdown`、`/export`、`/history`、`/snapshots/{id}`、`/simulate`）都調用 `_enforce_self_or_full_salary`，唯獨此 employees 路徑下的端點漏掛。
- **根因**：endpoint 只用 `require_staff_permission(Permission.SALARY_READ)` 做權限門檻，未呼叫 `_enforce_self_or_full_salary(current_user, employee_id)` 限縮為「admin/hr 看全部，其他角色只能看自己」。`_salary_engine.preview_salary_calculation(employee_id, ...)` 回傳的 breakdown 也沒有 viewer-side 過濾。
- **建議修法**：在進入 `_salary_engine.preview_salary_calculation` 前先呼叫 `from api.salary import _enforce_self_or_full_salary; _enforce_self_or_full_salary(current_user, employee_id)`；或抽出 `utils/salary_access.py` 共用 helper 後同時供 salary.py / employees.py 使用，避免端點散落各處時容易漏掛。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-013 [High] salary: `GET /salaries/festival-bonus` 與 `/salaries/festival-bonus/period-accrual` 未限縮查詢者，回傳全員節慶獎金與期中累積金額

- **位置**：`api/salary.py:534-584` `GET /api/salaries/festival-bonus`；`api/salary.py:587-720` `GET /api/salaries/festival-bonus/period-accrual`
- **威脅模型**：a
- **PoC**：任何持有 `Permission.SALARY_READ` 但非 admin/hr 角色（例：自訂角色僅給 SALARY_READ 用以查自己歷史；或主管被臨時授予 SALARY_READ）呼叫上述任一端點。處理流程是「`session.query(Employee).filter(_active_employees_in_month_filter(year, month)).all()` → 對每筆計算 `engine.calculate_festival_bonus_breakdown(...)` 或 `engine.calculate_period_accrual_row(...)` → 整批回傳」，過程完全不引用 `_resolve_salary_viewer_employee_id` 也未呼叫 `_enforce_self_or_full_salary`。攻擊者一次請求即可拿到全體在職員工該月的：節慶獎金（festivalBonus）、超額獎金、會議缺席扣款、bonusBase（職稱基數）、targetEnrollment、period-accrual 每月明細與累積總和（含預估淨領金額 `net_estimate`）。對比同檔 `/salaries/records`（line 723）正確使用 `_resolve_salary_viewer_employee_id` 過濾 query；festival-bonus 兩條路徑屬同型敏感資料卻未加守衛。
- **根因**：兩端點皆在 admin/hr 預設情境下開發，假設只有他們會使用；未防範「ad-hoc 授予 SALARY_READ 的非 admin/hr 角色」會打到同一 endpoint。
- **建議修法**：兩端點 query 員工前先 `viewer = _resolve_salary_viewer_employee_id(current_user)`；若 `viewer is not None`（非 admin/hr），用 `Employee.id == viewer` 進一步過濾員工 query，使該角色僅能看到自己一筆。或全面拒絕（403）非 admin/hr 對「彙總式」端點的存取，要求改用 `/salaries/records?employee_id=self` 查單筆。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-014 [High] employees_docs: `GET /employees/{employee_id}/contracts` 回傳 `salary_at_contract`，繞過 `_enforce_self_or_full_salary`

- **位置**：`api/employees_docs.py:348-361` `GET /api/employees/{employee_id}/contracts`（response 由 `_contract_to_dict`，line 123-134 組裝，含 `salary_at_contract`）
- **威脅模型**：a
- **PoC**：任何持 `Permission.EMPLOYEES_READ` 之使用者（預設配置：admin/hr）對任意 `employee_id` 呼叫即可取得該員工每一段合約的 `salary_at_contract`（合約簽訂時月薪）。salary.py 已用 `_enforce_self_or_full_salary` 限縮非 admin/hr 看自己；employees_docs 則完全交給 `EMPLOYEES_READ`。員工合約上的薪資是高敏感資訊（影響退休金、資遣費基數），等同薪資資料，若僅被授予 EMPLOYEES_READ（例：人資助理/自訂查資料的 viewer 角色）即可看遍全員合約金額。同樣 pattern 還會洩漏合約類型 / 起訖日（敏感人事決策軌跡，可推算他人是否續約、轉正、即將到期）。
- **根因**：employees_docs 將 contracts/educations/certificates 三類一律用 `EMPLOYEES_READ` 控管，未區分 sensitive（contracts → 含薪資）與 non-sensitive（educations / certificates）；亦未調用既有的 `_enforce_self_or_full_salary` 阻擋非 admin/hr 看他人合約。
- **建議修法**：（1）`list_contracts` / `update_contract` / `delete_contract` 新增 `_enforce_self_or_full_salary(current_user, employee_id)`，比照 salary.py 的「admin/hr 看全部，其他僅看自己」。（2）或在 response 層動態 mask `salary_at_contract` 給沒有 `SALARY_READ` 的 viewer。建議用 (1)，避免維護兩套遮罩規則；附帶把 contracts 的 perm 提升為 `EMPLOYEES_READ + SALARY_READ` 雙重門檻。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-015 [High] punch_corrections: `PUT /punch-corrections/{correction_id}/approve` 缺自我核准守衛，可自審補打卡（直接影響本人薪資扣款）

- **位置**：`api/punch_corrections.py:105-254` `PUT /api/punch-corrections/{correction_id}/approve`
- **威脅模型**：a
- **PoC**：員工 A 為主管或具 `Permission.APPROVALS` 之角色，先以 portal 建立自己的補打卡申請（`PunchCorrectionRequest.employee_id = A`），再以管理端 API `PUT /punch-corrections/{id}/approve` 對自己這筆呼叫 `approved=true`。endpoint 通過 `_check_approval_eligibility` 檢查（同為 supervisor → supervisor 路徑視配置可能放行），但**完全沒有「approver_eid == correction.employee_id 拒絕」這層守衛**。對比 `api/leaves.py:1018` 與 `api/overtimes.py:1079` 都有相同 idiom：`approver_eid = current_user.get("employee_id"); if approver_eid and ot/leave.employee_id == approver_eid: 403`，唯獨 punch_corrections 漏掛。核准補打卡會 (1) 建立 / 改動 Attendance 的 punch_in/out_time、(2) 把 is_missing_punch_in / out 設成 False、(3) `mark_salary_stale` 觸發後續重算，等同 A 可單人完成「補打卡 → 自審 → 漂白遲到/缺卡 → 取消扣款」的閉環，違反勞動法的職務分工原則，也是金流 A 錢路徑（補打卡內容直接屬於 task brief 的 Critical 級別判定）。
- **根因**：punch_corrections.py:105 `approve_punch_correction` 在 line 128 角色資格檢查之後，未補上自我核准防護，與同期重構過的 leaves / overtimes 不一致；屬遺漏修補。
- **建議修法**：在 `approve_punch_correction` 取得 correction 物件後（line 119 之後）加入：
  ```python
  approver_eid = current_user.get("employee_id")
  if approver_eid and correction.employee_id == approver_eid:
      raise HTTPException(status_code=403, detail="不可自我核准補打卡申請")
  ```
  並比照 leaves/overtimes 補測試（`tests/test_punch_corrections_self_approve.py`）。長期建議抽出 `utils/approval_helpers.py:require_not_self_approval(current_user, submitter_employee_id, action="...")` 共用 helper，避免日後新增審核端點再次漏寫。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-016 [Medium] bonus_preview: `GET /bonus-preview/dashboard` 與 `POST /bonus-impact-preview` 以 `STUDENTS_READ`/`STUDENTS_WRITE` 控管，但回傳每位教師估算節慶獎金金額

- **位置**：`api/bonus_preview.py:184-334` `POST /api/bonus-impact-preview`（perm: `STUDENTS_WRITE`）；`api/bonus_preview.py:342-472` `GET /api/bonus-preview/dashboard`（perm: `STUDENTS_READ`）
- **威脅模型**：a
- **PoC**：supervisor 預設模板含 `STUDENTS_READ` 與 `STUDENTS_WRITE` 但**不含 `SALARY_READ`**。supervisor 呼叫 `/bonus-preview/dashboard` 即可拿到每班導／副班導的 `estimated_bonus`（即將發放的節慶獎金估算金額）、`base_amount`（職稱基數），以及全校 `estimated_total_bonus` 與每位主管/辦公室人員（category="主管"/"辦公室"）的 `current_bonus`。`bonus-impact-preview` 進一步揭露各教師在學生數異動下的 `current_bonus → projected_bonus` 變化，可被 supervisor 用來反推他人薪資的變動性與職稱基數差異。雖然這是「估算金額」而非「實發金額」，但節慶獎金本就是員工月薪可顯現的一部分，且職稱基數能反推是否帶 A/B/C 級獎金、主管紅利身份等敏感人事資料。
- **根因**：兩端點為了讓 supervisor 評估「招生變動 → 獎金影響」與「全校達成率」，把獎金金額納入 response，但 perm 只用 `STUDENTS_READ`/`STUDENTS_WRITE`，未要求同時持 `SALARY_READ`。預期 supervisor 看「自己 + 自班教師」沒問題，但 response 未限縮班級或 employee_id 範圍。
- **建議修法**：（1）若維持給 supervisor 用，把 response 中各員工 `estimated_bonus` / `base_amount` 用 mask（例：四捨五入到千、或以 `<3000`/`3000-5000`/`5000+` 區間取代具體數字）。（2）或要求 perm 加上 `SALARY_READ`，使僅 admin/hr/被明確授權者可看細節；supervisor 仍可用班級總計版本（`estimated_total_bonus` + 達成率，不含個別員工金額）。建議 (1)：保留 supervisor 用以評估招生決策的 UX，又不洩漏個別員工估算金額。
- **是否需新測試**：no（Medium；列為 Phase 2 優先處理）
- **修補狀態**：⏳ Pending

### F-017 [Medium] employees: `GET /employees` 與 `GET /employees/{id}` 回傳 `base_salary` / `hourly_rate` 給僅持 `EMPLOYEES_READ` 之自訂角色（無 `SALARY_READ`）

- **位置**：`api/employees.py:167-194` `GET /employees`；`api/employees.py:247-290` `GET /employees/{employee_id}`；響應由 `_format_employee_response`（line 40-93）組裝
- **威脅模型**：a
- **PoC**：在預設角色配置下 admin/hr 才有 EMPLOYEES_READ，且兩者也都有 SALARY_READ；此 finding 主要適用於**自訂角色**情境：若管理員建立「人資助理」「員工目錄查詢者」等只授予 `EMPLOYEES_READ`（不授 SALARY_READ）的角色，該使用者呼叫 `GET /employees` 即可取得每位員工的 `base_salary`（月薪）、`hourly_rate`（時薪）、`insurance_salary_level`（投保級距）、`pension_self_rate`（勞退自提率）、`dependents`（眷屬人數，影響保費）。`bank_code` / `bank_account_name` / 完整 `bank_account` / 完整 `id_number` 已正確被 `SALARY_WRITE` gate 遮蔽，但**底薪/時薪/投保級距/自提率四個欄位完全沒被 mask**。`hire_date` / `birthday` / `phone` / `address` / `emergency_contact_*` 也都會洩漏，但相對而言薪資金額更直接。
- **根因**：`_format_employee_response` 只區分「`SALARY_WRITE` → 銀行/身分證遮罩」一條規則，沒有第二層「`SALARY_READ` → 薪資金額遮罩」。背後假設「能看員工的就能看薪資」，但 EMPLOYEES_READ 與 SALARY_READ 為獨立 bit，配置上可被拆開。
- **建議修法**：在 `_format_employee_response` 多接受 `can_view_salary` 參數，由 caller 用 `has_permission(perms, Permission.SALARY_READ)` 判斷；若 `can_view_salary=False`，把 `base_salary` / `hourly_rate` / `insurance_salary_level` / `pension_self_rate` 改為 `None` 或 `"***"`。同時更新 `_format_employee_response` 文件註記「薪資欄位遮罩規則：需 SALARY_READ」。亦可一併把 `hire_date` 之外的 PII（phone/address/emergency_contact_*）對「沒有自己 employee_id 對映」的查詢者遮罩，但這超出 Threat a 範圍，建議拆 finding 處理。
- **是否需新測試**：no（Medium；現行預設角色不會觸發；列為 Phase 2 與自訂角色 RBAC 改造一併處理）
- **修補狀態**：⏳ Pending

### F-018 [High] students/classrooms: `GET /students` / `GET /students/{id}` / `GET /students/{id}/profile` / `GET /classrooms/{id}` 在 `STUDENTS_READ` / `CLASSROOMS_READ` 下回傳 `allergy` / `medication` / `special_needs`，繞過 `STUDENTS_HEALTH_READ` 與 `STUDENTS_SPECIAL_NEEDS_READ`

- **位置**：
  - `api/students.py:368` `GET /students`（response 含 `allergy` / `medication` / `special_needs` / `emergency_contact_*`，line 427-432）
  - `api/students.py:481-515` `GET /students/{student_id}`（同上）
  - `api/students.py:852-874` `GET /students/{student_id}/profile`（呼叫 `services/student_profile.py:_serialize_basic`，line 288-289 `allergy` / `medication`，line 286 `special_needs`）
  - `api/classrooms.py:576-594` `GET /classrooms/{classroom_id}`（`_serialize_classroom_detail` line 384-386 在 `students` list 內含 `allergy` / `medication` / `special_needs`）
- **威脅模型**：b
- **PoC**：與 F-014 / F-017 同型 — 預設角色配置（admin/hr/supervisor 通常同時持 `STUDENTS_READ` 與 `STUDENTS_HEALTH_READ` / `STUDENTS_SPECIAL_NEEDS_READ`）下不易觸發；風險在**自訂角色**：若管理員建立「學生目錄查詢者」「教務助理」等只授予 `STUDENTS_READ`（不授 `STUDENTS_HEALTH_READ` / `STUDENTS_SPECIAL_NEEDS_READ`）的角色，該角色呼叫 `GET /students` 即可拿到全校學生 `allergy`（過敏原）、`medication`（用藥）、`special_needs`（特殊需求）。同樣 `GET /classrooms/{id}` 在僅持 `CLASSROOMS_READ` 的角色下會把該班所有學生的健康／特殊需求欄位連帶外洩。權限系統把這三個欄位獨立成 bit `STUDENTS_HEALTH_READ`（1<<44）、`STUDENTS_SPECIAL_NEEDS_READ`（1<<47）就是為了把健康／IEP 與一般學生資料拆開，但這四個 endpoint 完全未檢查；存取該欄位的細粒度 RBAC 完全失效。
- **根因**：`students.py` 與 `classrooms.py` 的 GET 端點直接 dump 整個 ORM row（含三個敏感欄位）回傳，沒有比照 `student_health.py` 走 `STUDENTS_HEALTH_READ` 路徑、也沒有像 `_format_employee_response` 在 response 層做欄位遮罩。`api/student_health.py:196` 起的端點明確以 `STUDENTS_HEALTH_READ` 守門 + `assert_student_access` 班級 scope；同一個資料卻在 students/classrooms 端被以更低門檻直接外洩。
- **建議修法**：（1）在 `_format_student_response`（students.py 沒抽出，需新建）與 `_serialize_classroom_detail` / `_serialize_basic` 多接 `can_view_health` / `can_view_special_needs` 參數，由 caller 以 `has_permission(perms, Permission.STUDENTS_HEALTH_READ)` 與 `STUDENTS_SPECIAL_NEEDS_READ` 判斷；無權者把三欄位改回 `None`。（2）長期建議把 `allergy` / `medication` / `special_needs` 從 `Student` 主表移出，改走 `StudentAllergy`/`StudentMedicationOrder`/`SpecialNeed` 子表（`student_health.py` 已是這個方向），然後 `Student.allergy/medication` 在 schema 層 deprecate，避免雙寫。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-019 [High] student_communications: 全 CRUD 無班級 scope；持 `STUDENTS_READ`/`STUDENTS_WRITE` 之自訂角色可讀／改／刪別班學生家長溝通紀錄

- **位置**：`api/student_communications.py:126-209` `GET /api/students/communications`；`api/student_communications.py:212-252` `POST`；`api/student_communications.py:255-290` `PUT /{log_id}`；`api/student_communications.py:293-318` `DELETE /{log_id}`
- **威脅模型**：b
- **PoC**：教師 A 透過自訂角色被授予 `STUDENTS_READ` / `STUDENTS_WRITE`（或 supervisor 跨班），呼叫 `GET /api/students/communications?classroom_id={B 班 id}` 即可取得 B 班所有學生與家長間的溝通紀錄（含 `topic` / `content` / `follow_up`，這是親師之間最敏感的對話文本，含家庭狀況、孩子行為描述、衝突細節）；`PUT /{log_id}` 與 `DELETE /{log_id}` 完全沒檢查 log 屬於哪個學生／哪個班，可任意改寫他人 log 的 `content`、刪除別班的紀錄。整個 router 完全沒有 `assert_student_access` 或 `_require_classroom_access` 呼叫，與 `student_health.py` / `portfolio/observations.py` 形成強烈反差。
- **根因**：作者僅以 `STUDENTS_READ`/`STUDENTS_WRITE` 為 perm gate，並假設「能看到溝通紀錄頁的就是行政 / supervisor」，沒有考慮自訂角色或多班教師跨班讀取他班 log。`PUT/DELETE` 的 path param 為 `log_id`，沒有 `student_id` 段，也沒從 log 反查 student → classroom 做 owner 驗證。
- **建議修法**：
  1. list 與 export：對非 admin/hr/supervisor，以 `accessible_classroom_ids(session, current_user)` 限縮 `Student.classroom_id.in_(allowed)`；若 `classroom_id` query 帶值且不在 allowed 內回 403。
  2. POST / PUT / DELETE：取出 `log` 後 `assert_student_access(session, current_user, log.student_id)`；若不存在 / 無權一律 404，避免 404 vs 403 枚舉。
  3. 與 student_health.py 共用 `utils/portfolio_access.py` helper，避免每個 router 重寫。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-020 [High] student_attendance: `batch` / `by-student` / `monthly` / `export` 無班級 scope；可任意改寫別班出席

- **位置**：
  - `api/student_attendance.py:229-275` `GET /student-attendance?classroom_id=...`（僅讀）
  - `api/student_attendance.py:278-339` `POST /student-attendance/batch`（**寫**：upsert 任意 `student_id` 出席）
  - `api/student_attendance.py:342-398` `GET /student-attendance/by-student?student_id=...`（讀單生紀錄）
  - `api/student_attendance.py:401-415` `GET /student-attendance/monthly?classroom_id=...`
  - `api/student_attendance.py:418-488` `GET /student-attendance/export`
  - `api/student_attendance.py:205-226` `GET /student-attendance/overview`
- **威脅模型**：b
- **PoC**：所有端點僅以 `STUDENTS_READ` / `STUDENTS_WRITE` 把關，完全沒有 `_require_classroom_access` 或 `assert_student_access`。攻擊路徑：
  1. 跨班讀：教師 A 持 `STUDENTS_READ`（自訂角色或 supervisor），`GET /student-attendance?classroom_id={B}` 即取 B 班逐生出席；`GET /student-attendance/export` 無 classroom_id 時匯出全園多 sheet。
  2. **跨班寫**：教師 A 持 `STUDENTS_WRITE`，`POST /student-attendance/batch` 帶任意 `student_id`，可把別班學生狀態改為「缺席」「請假」，並覆蓋既有 `recorded_by`。儘管薪資不直接連動，這仍會：(a) 影響家長端可見的歷史出席（家長對班導投訴時拿到不一致紀錄）、(b) 觸發 `invalidate_student_attendance_report_caches` 導致全校報表重算、(c) 與 `api/student_leaves.py` 的 `_apply_attendance_for_leave`（line 131-167）打架，覆寫家長端假單寫入的紀錄而不留痕。
- **根因**：與 student_communications.py 同型 — 整個 router 從頭到尾沒做班級 scope；list 端點允許任意 `classroom_id`、batch 端點允許任意 `student_id`。
- **建議修法**：
  1. `GET /student-attendance` / `monthly` / `export`：對非 admin/hr/supervisor 走 `accessible_classroom_ids`，限制 `classroom_id` 必須在 allowed 內，否則 403；export 全園模式需 `STUDENTS_READ` 且 admin/hr/supervisor。
  2. `POST /batch`：對 payload 中所有 `student_id` 批次走 `filter_student_ids_by_access`，發現非 allowed 即整批回 403（避免「partial write，部分成功」）。
  3. `GET /by-student`：以 `assert_student_access` 取代裸 query。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-021 [High] student_leaves: `POST /{leave_id}/approve` 與 `/reject` 無班級 scope，可審核別班家長端請假並寫入 `StudentAttendance`

- **位置**：`api/student_leaves.py:189-220` `POST /api/student-leaves/{leave_id}/approve`；`api/student_leaves.py:223-255` `POST /api/student-leaves/{leave_id}/reject`；`api/student_leaves.py:100-128` `GET /api/student-leaves`
- **威脅模型**：b
- **PoC**：教師 A 透過自訂角色取得 `STUDENTS_READ` / `STUDENTS_WRITE`（或 supervisor），呼叫 `POST /api/student-leaves/{leave_id}/approve` 對任意 leave_id 即可：(1) 把別班家長端送的 `StudentLeaveRequest.status` 改為 `approved`、設 `reviewed_by=A`、(2) 透過 `_apply_attendance_for_leave`（line 131-167）對該生請假區間每個應到日 upsert `StudentAttendance` row（覆蓋既有 status / remark），**留下「教師 A 審核了 B 班學生請假」的稽核軌跡**，且在覆蓋既有 attendance 時連帶觸發家長端可見的出席異動。`reject` 同樣可任意操作；既有 approved 紀錄會走 `_revert_attendance_for_leave`（line 170-187），把 attendance row delete 掉。`GET /api/student-leaves` 列表也沒帶班級過濾，可枚舉全校 pending 假單。
- **根因**：endpoint 只把關 `STUDENTS_READ`/`STUDENTS_WRITE`，沒對 leave 取出後的 `student.classroom_id` 做班級 scope；列表沒有對非特權角色強制 `classroom_id` query 必須在 allowed 範圍。學生請假審核屬「改別班學生紀錄」，比 attendance batch 更直接 — 動作會被 LINE 通知家長（`_notify_parent_leave_result_safe`），且寫入 attendance 同時還影響薪資代班相關的後續出席報表。
- **建議修法**：
  1. `approve` / `reject`：取出 `item` 後立刻 `assert_student_access(session, current_user, item.student_id)`；非自班 一律 403。
  2. `GET`：對非 admin/hr/supervisor，若沒帶 `classroom_id` 自動限縮為 `accessible_classroom_ids`；帶了但不在 allowed 內回 403。
  3. 補測試 `tests/test_student_leaves_cross_classroom.py`：建立兩班、模擬 B 班教師對 A 班 leave_id approve → 預期 403；同時驗 `StudentAttendance` 未被寫入。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-022 [Medium] student_change_logs: list/summary/export/CRUD 無班級 scope；持 `STUDENTS_READ` 可讀全校異動軌跡

- **位置**：`api/student_change_logs.py:165-208` `GET /api/students/change-logs/summary`；`api/student_change_logs.py:211-316` `GET /api/students/change-logs`（list）；`api/student_change_logs.py:319-446` `GET /api/students/change-logs/export`；`api/student_change_logs.py:449-577` `POST` / `PUT /{log_id}` / `DELETE /{log_id}`
- **威脅模型**：b
- **PoC**：持 `STUDENTS_READ` 的自訂角色（或 supervisor 跨班）呼叫 `GET /api/students/change-logs?classroom_id={B}` 取得 B 班全部異動紀錄（轉班、退學、休學、復學的 `event_type` + `reason`+`notes` + `from_classroom_id` + `to_classroom_id`），這對家長 / 學生屬於敏感人事決策軌跡（退學原因、家庭狀況等）。`GET /export` 可一鍵下載全校 5000 筆 CSV。`PUT /{log_id}` / `DELETE /{log_id}` 雖只允許改/刪手動補登（`source != 'lifecycle'`）的紀錄，但仍未驗證 log 對應的學生是否屬於 caller 班級 — 教師 A 可改寫 B 班的 manual log 內容、刪除 B 班的補登紀錄。
- **根因**：與 student_communications.py / student_attendance.py 同型，整個 router 沒做班級 scope。
- **建議修法**：
  1. list / summary / export：對非 admin/hr/supervisor，以 `accessible_classroom_ids` 限縮 `classroom_id` / `from_classroom_id` / `to_classroom_id`（任一在 allowed 即可）；帶了不允許的 classroom_id 回 403。
  2. POST / PUT / DELETE：取出 log 後 `assert_student_access(session, current_user, log.student_id)`。
- **是否需新測試**：no（Medium；列為 Phase 2 與 F-019/F-020/F-021 同步處理）
- **修補狀態**：⏳ Pending

### F-023 [Medium] student_incidents/assessments: list 端點 `student_id` 與 `classroom_id` 都未帶時跳過 `_require_classroom_access`，回傳全校事件／評量

- **位置**：
  - `api/student_incidents.py:85-140` `GET /api/student-incidents`（line 102-116 條件分支：`if student_id` 才走 `_require_classroom_access`，`if classroom_id` 才走；兩者皆 None 時直接 query 全校）
  - `api/student_assessments.py:81-133` `GET /api/student-assessments`（同上 pattern，line 97-112）
- **威脅模型**：b
- **PoC**：教師 A 持 `STUDENTS_READ`（自訂角色），呼叫 `GET /api/student-incidents`（不帶 `student_id` 也不帶 `classroom_id`）即取得全校事件紀錄列表（含 `severity`、`description`、`action_taken`、`parent_notified`），同樣對 `student-assessments` 也成立。helper `_require_classroom_access` 只在帶過濾參數時被觸發；no-filter 路徑等於跳過所有班級 scope，全部回傳。`POST` / `PUT` / `DELETE` 分支已正確調用 helper（見 incidents.py line 156-161 / 211-212 / 270-273），這 finding 僅針對 list 端點。
- **根因**：list 端點假設前端 UI 一定會帶 `classroom_id`，但 API 層沒強制；非特權角色（教師）打 list 時若不帶任一過濾，就跳過 helper。應在 list 入口先檢查角色 — 非 admin/hr/supervisor 至少必須帶 `classroom_id` 或 `student_id` 之一，且帶的值必須在 `accessible_classroom_ids` 內；或 default 強制 filter `Student.classroom_id.in_(allowed)`。
- **建議修法**：在 list 端點起始處：
  ```python
  role = current_user.get("role", "")
  if role not in ("admin", "hr", "supervisor"):
      allowed = accessible_classroom_ids(session, current_user)
      if not allowed:
          return {"total": 0, "items": []}
      query = query.filter(Student.classroom_id.in_(allowed))
  ```
  保留現有 `if student_id` / `if classroom_id` 分支的 helper 呼叫做雙重防線。incidents 與 assessments 共用同一個 helper（`utils/portfolio_access.py:accessible_classroom_ids`）即可。
- **是否需新測試**：no（Medium；結合 F-024 一起測）
- **修補狀態**：⏳ Pending

### F-024 [Medium] students/records: `GET /students/records` 時間軸無 viewer-side 班級過濾，回傳全校事件＋評量＋異動

- **位置**：`api/students.py:444-478` `GET /api/students/records`（呼叫 `services/student_records_timeline.list_timeline`，line 213-301）
- **威脅模型**：b
- **PoC**：endpoint 接受 `student_id`、`classroom_id`、`type`（incident / assessment / change_log）等過濾參數，但 `list_timeline` 完全沒有 `current_user` 參數，內部沒有 `accessible_classroom_ids` 過濾。教師 A 持 `STUDENTS_READ`（自訂角色）呼叫 `GET /api/students/records`（不帶 classroom_id），即同時取得全校事件＋評量＋異動三類紀錄的合併時間軸；帶 `classroom_id={B}` 也照樣回傳 B 班全部紀錄。屬「跨班看別班學生紀錄」典型 IDOR；與 F-023 同型但作用範圍更廣（一個端點打三個資料源）。
- **根因**：`list_timeline` 為純資料服務，未帶 `current_user`；endpoint 為了簡化沒做 viewer-side filter，仰賴前端只帶自班 classroom_id。
- **建議修法**：
  1. `list_timeline` 加 `accessible_classroom_ids: list[int] | None` 參數（None=全放行 admin）；內部 `_fetch_incidents` / `_fetch_assessments` 透過 `Student.classroom_id.in_(allowed)`、`_fetch_change_logs` 透過 `StudentChangeLog.classroom_id.in_(allowed)` 過濾。
  2. `GET /students/records` endpoint 在進入 service 前 `from utils.portfolio_access import is_unrestricted, accessible_classroom_ids; allowed = None if is_unrestricted(current_user) else accessible_classroom_ids(session, current_user)`，傳給 service。
- **是否需新測試**：no（Medium）
- **修補狀態**：⏳ Pending

### F-025 [Medium] students: `GET /students/{student_id}/guardians` 缺班級 scope，可跨班讀家長聯絡資料

- **位置**：`api/students.py:1003` `GET /students/{student_id}/guardians`
- **威脅模型**：b
- **PoC**：教師 A 班導 1 班，但持 `GUARDIANS_READ`（custom role 或 supervisor）。對任意 student_id 呼叫此端點，可讀該學生家長姓名、與學生關係、電話、Email 等聯絡 PII，跨班搜集家庭聯絡資料。
- **根因**：endpoint 僅 `require_staff_permission(GUARDIANS_READ)`，缺 `assert_student_access` 或 `accessible_classroom_ids` 過濾；同 pattern 已在 F-019/F-022 出現。
- **建議修法**：在 endpoint 入口加 `assert_student_access(session, current_user, student_id)`；或將 `GUARDIANS_READ` 視為 admin-only 並收歸 supervisor 角色（依業主 policy 二擇一）。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-026 [Medium] activity/registrations: `GET /registrations` / `/{id}` / `/pending` 在 `ACTIVITY_READ` 下回傳 `parent_phone` / `birthday` / `email` / `student_id` / `classroom_id`，繞過 `GUARDIANS_READ` / `STUDENTS_READ`

- **位置**：
  - `api/activity/registrations.py:1283-1435` `GET /registrations`（response item line 1395-1424：`parent_phone`、`birthday`、`student_id`、`classroom_id`）
  - `api/activity/registrations.py:1438-1542` `GET /registrations/{registration_id}`（response line 1517-1540：同上 + `email`）
  - `api/activity/registrations.py:733-803` `GET /registrations/pending` 透過 `_serialize_pending_item`（line 711-730：`parent_phone`、`birthday`、`email`、`classroom_id`）
- **威脅模型**：e
- **PoC**：在預設角色配置下 admin/hr 通常同時持 ACTIVITY_READ + STUDENTS_READ + GUARDIANS_READ，本 finding 主要適用於**自訂角色**情境：若管理員建立「活動行政」「才藝助教」等只授予 `ACTIVITY_READ`（不授 `STUDENTS_READ` / `GUARDIANS_READ`）的角色，該角色呼叫 `GET /registrations?limit=200` 即可一頁拉走當學期所有報名學生的 `parent_phone`（家長手機）+ `birthday`（生日）+ `student_id`（在校 Student.id）+ `classroom_id`（班級 FK）+ `email`，等同跨班把全校報名才藝家庭的 PII 攜帶外帶。`/registrations/{id}` 與 `/registrations/pending` 同型外洩。`parent_phone` 在系統其他位置（`api/students.py:1003 /students/{id}/guardians`、F-025）已被視為 GUARDIANS_READ 級別敏感欄位；此處走 ACTIVITY_READ 即繞過。本 finding 與 F-017（`base_salary` 在 EMPLOYEES_READ 下外洩）、F-022（change_logs 在 STUDENTS_READ 下無班級 scope）同型——都是「同一份 PII 由次要 router 用較低門檻外洩」。
- **根因**：`registrations.py` GET 端點直接 dump 整個 ORM row 到 response，未區分基本資料（`student_name` / `class_name` / `course_count` 等運維所需）與聯絡 PII（`parent_phone` / `email` / `birthday`）。無 viewer-side 欄位 mask、亦未檢查呼叫者是否同時持 `GUARDIANS_READ` / `STUDENTS_READ`。
- **建議修法**：（1）在 list / detail / pending response 組裝處接受 `can_view_pii` 參數（caller 用 `has_permission(perms, Permission.GUARDIANS_READ)` 與 `STUDENTS_READ` 任一判斷），無權者把 `parent_phone` / `birthday` / `email` 改為 `None` 或 `"***"`；`student_id` / `classroom_id` 對非 STUDENTS_READ 也建議遮蔽。（2）或要求端點 perm 為 `ACTIVITY_READ + GUARDIANS_READ`，但會影響既有「活動行政」角色職責，需業主決策。建議 (1)，與 F-017 改造一致並可共享 helper。
- **是否需新測試**：no（Medium；現行預設角色不會觸發；列為 Phase 2 與 F-017 自訂角色 RBAC 改造一併處理）
- **修補狀態**：⏳ Pending

### F-027 [Medium] activity/registrations: `GET /students/search` 僅以 `ACTIVITY_WRITE` 守門，回傳全校在校生 `student_id` 學號 / `birthday` / `parent_phone`，繞過 `STUDENTS_READ`

- **位置**：`api/activity/registrations.py:806-848` `GET /api/activity/students/search`
- **威脅模型**：e
- **PoC**：endpoint perm 僅 `ACTIVITY_WRITE`。非 admin/hr 但持 ACTIVITY_WRITE 的角色（例：自訂「活動行政」「才藝櫃檯」）以 `q=王`、`q=09` 等寬鬆關鍵字呼叫，response（line 836-846）即逐筆回傳 `student_id`（學號）、`name`、`birthday`、`classroom_id`、`classroom_name`、`parent_phone`，相當於在無 STUDENTS_READ 權限下取得全校在校生目錄與家長聯絡資料。同模組正規端點（`api/students.py:368 GET /students`、F-018/F-019）至少需 `STUDENTS_READ`，且部分敏感欄位（健康/特殊需求）需更高位元；此 search 端點走 ACTIVITY_WRITE 等於建立側信道。`limit=50` 上限雖小，配合多次不同關鍵字仍可窮舉。
- **根因**：endpoint 為了讓後台「待審核 → 手動 match」流程能搜學生，把學生搜尋 colocate 在 activity router 內，僅以 ACTIVITY_WRITE 守門，未要求同時持 STUDENTS_READ。亦未對非 admin/hr/supervisor 強制最小關鍵字長度（≥2 已有，但 phone like `09` 即可命中大量學生）或限縮為僅匹配 pending registration 上下文（要求帶 `registration_id` 才回傳）。
- **建議修法**：（1）perm gate 改為 `ACTIVITY_WRITE + STUDENTS_READ`（`require_staff_permissions_all` helper）；（2）或要求 query 必須帶 `registration_id`，handler 校驗該 reg 為 pending 且呼叫者具 ACTIVITY_WRITE 才開放；（3）response 對非 STUDENTS_READ 角色把 `birthday` / `parent_phone` mask 為 `None`，僅回 `id` / `student_id` / `name` / `classroom_name` 供 match。建議 (1) 最小改動。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-028 [Low] activity/pos: `GET /pos/outstanding-by-student` / `GET /pos/recent-transactions` 在 `ACTIVITY_READ` 下回傳全校學生 `student_name` / `birthday` / `class_name`

- **位置**：
  - `api/activity/pos.py:283-414` `GET /api/activity/pos/outstanding-by-student`（group response line 376-409：`student_name`、`birthday`、`class_name`，每組附 registrations[].class_name + courses 明細）
  - `api/activity/pos.py:762-900` `GET /api/activity/pos/recent-transactions`（response line 871-893 包含 `student_names` 列表 + `items[].student_name` / `class_name`）
  - `api/activity/pos.py:913-1111` `GET /api/activity/pos/semester-reconciliation`（response line 1070-1092：`student_name` / `class_name`，全學期）
- **威脅模型**：e
- **PoC**：與 F-026 同型但範圍稍窄。自訂「POS 櫃檯」角色僅有 ACTIVITY_READ（用於日結對帳）即可呼叫 `outstanding-by-student?q=&limit=500` 一頁取走全校未繳清才藝報名的學生姓名+生日+班級，或 `pos/semester-reconciliation` 取整學期全校才藝清單。`recent-transactions` 雖以日期過濾但每日交易仍含 `student_name` + `class_name`。實務上 ACTIVITY_READ 通常授給活動行政，但生日資料對社工釣魚有實際價值。
- **根因**：POS 端點以 `ACTIVITY_READ` 為單一閘門，未疊加 `STUDENTS_READ` 或欄位遮罩。設計上假設操作者同時為老闆/會計（同時持兩個 perm），未防範自訂 RBAC 拆分。
- **建議修法**：與 F-026 共用 viewer-side 欄位 mask helper。對非 STUDENTS_READ 角色：`outstanding-by-student` 將 `birthday` 改為 `None`（保留 `student_name` + `class_name` 供櫃檯叫號識別）；`semester-reconciliation` 同理；`recent-transactions` 已較少敏感欄位，可保留。或把這三個端點 perm gate 升為 `ACTIVITY_READ + STUDENTS_READ`，逼業主明確授權。
- **是否需新測試**：no（Low；列為 Phase 2 與 F-017/F-026 同步處理）
- **修補狀態**：⏳ Pending

### F-029 [Low] activity/public: `POST /public/update` 換手機號 409 `此手機號碼已被其他報名使用` 形成 phone enumeration oracle

- **位置**：`api/activity/public.py:796-809` `POST /api/activity/public/update`（`new_parent_phone` 衝突檢查）
- **威脅模型**：c
- **PoC**：攻擊者先用任意自家或社工取得的某筆有效報名（`name + birthday + parent_phone` 三欄通過驗證）取得對該 reg 的 update 權限。接著以 `new_parent_phone={target_TW_mobile}` 嘗試更新。若 target 號碼**未**綁定任何 active registration → 200 更新成功；若 target 號碼**已**綁定（任何家長正在使用）→ 409 `此手機號碼已被其他報名使用，請聯繫校方協助處理`。攻擊者可由此**枚舉任意 09 開頭 10 碼台灣手機是否在系統內出現過**（含家長、員工兼家長、轉班過的家庭等），用於：(1) 確認某支號碼是否與本園有教養關係；(2) 配合社工釣魚，先以 phone hits 縮小目標再實施其他攻擊。雖有 `_public_register_limiter`（5/min）限速，仍允許每分鐘 5 個 probe，每天約 7,200 個（一支 IP）；對 09 + 10 碼的 ~10^7 空間在已知/常見手機號白名單裡可有實質效果。
- **根因**：為了把「兩個家長共用一支號碼」的對帳混亂擋掉，更新流程加了全域 `is_active` phone 唯一性檢查，但 409 detail 直接告知攻擊者「此手機號碼已被其他報名使用」。可區分 200 / 409 即洩漏存在性。
- **建議修法**：（1）保留 phone 唯一性檢查，但 409 detail 改為通用訊息：`此手機號碼變更失敗，請聯繫校方協助處理` 或 `更新成功`（silent accept 但實際不寫入 phone，並另發 admin alert）；前者較不破壞 UX 但仍然部分洩漏（依然可區分 200 / 409）；後者完全消除 oracle 但 UX 變差。建議走 silent accept + 轉送至「校方審核佇列」。（2）強化 rate limit：`_public_register_limiter` 從 5/min 降為 5/hour（或 phone 變更專屬 limiter，3/day per IP），讓窮舉成本提高。建議 (1) + (2) 同時做。
- **是否需新測試**：no（Low）
- **修補狀態**：⏳ Pending

### F-030 [Medium] activity/public: `POST /public/register` 多重未認證枚舉 oracle（學生姓名/生日 + 家長電話）

- **位置**：`api/activity/public.py:467-500` `POST /api/activity/public/register`
- **威脅模型**：c + d
- **PoC**：未登入攻擊者直接呼叫 `POST /public/register` 並提交任意 `(student_name, birthday)`：
  1. 若該組合在當學期已有有效報名 → 400「此學生本學期已有有效報名」
  2. 若不存在 → 流程繼續（最終會在後續 3 欄驗證失敗，但此差異化已洩漏存在性）
  另用 `parent_phone` 探測：若該電話有 pending 報名 → 400「您的報名仍在確認中」。
  攻擊者**不需事先持有任何有效身分組合**即可枚舉系統內學生姓名+生日 / 家長電話的存在性，與 F-029（需先持有效三欄）相比，威脅面更廣。
- **根因**：`existing` 與 `pending_dup` 兩個檢查在 `_verify_parent_identity` 之前執行；當這兩支查詢命中時直接 raise 400，與其他失敗路徑（驗證錯誤、無資料）回傳的 status / detail 不同，形成存在性 oracle。
- **建議修法**：將 `existing` / `pending_dup` 檢查移到 `_verify_parent_identity` 之後（即先確認家長合法身分再檢查重複報名），或合併為 generic「請聯絡園所」訊息隱藏存在性差異。亦應評估提高 `_public_register_limiter` 嚴格度（目前 5/min/IP，攻擊者輪換 IP 可達 7,200/day）。
- **是否需新測試**：yes
- **修補狀態**：⏳ Pending

### F-031 [High] reports/finance-summary: `GET /finance-summary/detail` 與 `/finance-summary/export` 在 `Permission.REPORTS` 下回傳逐員 `gross_salary` / `net_salary` / `employer_benefit` / `real_cost`，繞過 `SALARY_READ`

- **位置**：
  - `api/reports.py:270-278` `GET /api/reports/finance-summary/detail`（`build_finance_detail` 回 `detail["salary"]` 含 `employee_name` / `gross_salary` / `net_salary` / `employer_benefit` / `real_cost` / `is_finalized` 逐員 row）
  - `api/reports.py:281-403` `GET /api/reports/finance-summary/export`（Sheet 5「薪資明細」逐員列出同一組欄位 + 是否封存）
  - `api/reports.py:222-238` `GET /api/reports/dashboard`（同樣 `Permission.REPORTS`，但只回月度合計，本 finding 不涵蓋）
- **威脅模型**：e
- **PoC**：預設角色配置中，**supervisor 持 `Permission.REPORTS` 但無 `SALARY_READ`**（見 `utils/permissions.py:149-184`，supervisor template 無 SALARY_READ；對比 hr 同時持 SALARY_READ + REPORTS）。supervisor（園長/主任）登入後直接呼叫 `GET /api/reports/finance-summary/detail?year=2026&month=4`，response payload `salary[]` 即逐員回傳 `employee_name`（姓名）、`gross_salary`（應發）、`net_salary`（實發）、`employer_benefit`（雇主保費+勞退）、`real_cost`（園方真實支出）、`is_finalized`。或呼叫 `/api/reports/finance-summary/export?year=2026&month=4` 一鍵下載 Excel，第 5 個 sheet「薪資明細」就是逐員實發名冊。等於用 REPORTS 位元繞過 SALARY_READ 的閘門 — 後者本來把 supervisor 隔離在薪資金額之外，前者卻把同一份欄位以「彙總/明細」名義打開。本 finding 與 F-017（`base_salary` 在 EMPLOYEES_READ 下外洩）、F-026（`parent_phone` 在 ACTIVITY_READ 下外洩）同型：「同一份敏感欄位由次要 router 用較低門檻外洩」，但**這支是 e 威脅中影響範圍最大的薪資金額**，且 export 直接落 Excel，可離線分發。
- **根因**：`reports.py` 的設計把「跨來源金流彙總」當作 admin/老闆視角，給了單一的 `Permission.REPORTS` 閘門；但 supervisor 角色預設就有 REPORTS（用於招生/出勤年度報表），而 supervisor 並非預設可看薪資金額的角色。`build_finance_detail` 服務層內未做 viewer-side 欄位 mask，由呼叫端決定回傳；endpoint 層又把所有 REPORTS 持有者一視同仁。Excel 匯出的 Sheet 5 也因此把同一份欄位連帶外洩。
- **建議修法**：兩擇一或併用——
  1. **endpoint 加雙 perm 閘門**：`/finance-summary/detail` 與 `/finance-summary/export` 改為同時要求 `Permission.REPORTS + Permission.SALARY_READ`（用 `require_staff_permissions_all` helper），確保只有 hr/admin/有 SALARY_READ 的自訂角色才能下鑽到逐員薪資明細。Dashboard / 月度彙總（`/dashboard`、`/finance-summary` 不含 detail）保持 `Permission.REPORTS`。
  2. **viewer-side mask**：在 `build_finance_detail` 加 `can_view_salary_detail: bool` 參數，無權者把 `salary[]` 整個轉為「合計只回總額不回逐員」或把 `employee_name`/各金額欄位 mask 為 `None`；export 同理。建議 (1) 較簡潔且符合既有 RBAC 設計（薪資金額屬 SALARY_READ 範圍）。
  併用時：endpoint 加 SALARY_READ 雙閘門 + service 層仍接受 viewer 旗標，避免未來新增 caller 時忘記加 perm。
- **是否需新測試**：yes（`tests/security/test_idor_admin_endpoints.py`：建立 supervisor 帳號（含 REPORTS、無 SALARY_READ），呼叫 `/finance-summary/detail` 應 403；含 SALARY_READ 才應 200，且 `salary[]` 含逐員金額。export 同型驗證）
- **修補狀態**：⏳ Pending

### F-032 [High] exports: `GET /exports/employee-attendance?employee_id=...` 缺自我守衛，可任意員工 id 拉同事個人逐日打卡明細

- **位置**：`api/exports.py:852-1178` `GET /api/exports/employee-attendance?employee_id=...&year=...&month=...`
- **威脅模型**：a + e
- **PoC**：endpoint 僅 `require_staff_permission(Permission.ATTENDANCE_READ)`，未檢查 `employee_id` 是否為呼叫者自己、也未要求高權限角色（admin/hr/supervisor）才能查他人。預設角色中 supervisor / hr 都持 ATTENDANCE_READ，**任何持 ATTENDANCE_READ 的自訂角色（例：班導兼出勤助理、教務助理）對任意員工 id 即可下載個人月報 Excel**：含逐日打卡時間 (`punch_in_time` / `punch_out_time`)、工時、遲到/早退分鐘、請假類型 / 假時、加班類型 / 時數、備註，等同同事每日上下班時間表 + 請假明細 + 加班記錄全帶走。對比同 router 的 `/exports/attendance`（line 308-483）走全員彙總統計、未洩漏單人逐日明細，這支端點是**單人逐日**版的「個人考勤側信道」。
  與 F-015（補打卡跨員工自審）、F-012/F-014（薪資跨員工讀取）成同一類威脅 a；同時也是 e（內部高權限角色之間：supervisor/hr 應該能看，但純 ATTENDANCE_READ 不該能下鑽到同事每日進出時間 — 那是出勤管理員職責，不是 supervisor）。
- **根因**：endpoint 只把 ATTENDANCE_READ 當作門檻，沒接 `_enforce_self_or_full_attendance` 等 helper（系統其他位置如 `api/attendance/*`、`api/employees.py:final-salary-preview` 經 F-012 修補後已建立 self-or-perm pattern，本 endpoint 漏掉）。`/exports/leaves` / `/exports/overtimes` 雖然走全員 list 不收 `employee_id` 過濾、屬不同 risk profile，但本端點明確接收 `employee_id` query 卻未做 owner 比對。
- **建議修法**：在 line 864（`session.query(Employee).filter(Employee.id == employee_id).first()`）取出 emp 後立刻：
  ```python
  perms = current_user.get("permissions", 0)
  is_self = current_user.get("employee_id") == emp.id
  has_full_view = has_permission(perms, Permission.SALARY_READ) or current_user.get("role") in ("admin", "hr")
  if not (is_self or has_full_view):
      raise HTTPException(status_code=403, detail="不得匯出他人個人出勤月報")
  ```
  或直接調用 `_enforce_self_or_full_salary` 等價的 attendance 版 helper（建議在 `utils/idor_guards.py` 抽 `assert_self_or_attendance_admin`）。Phase 2 一併修。
- **是否需新測試**：yes（`tests/security/test_idor_employee_financial.py` 或新建 `test_idor_admin_endpoints.py`：建立 teacher/supervisor 帳號（含 ATTENDANCE_READ），用其 token 呼叫他人 `employee_id` 預期 403；同帳號自身 employee_id 預期 200；admin/hr 預期 200）
- **修補狀態**：⏳ Pending

### F-033 [Medium] exports/gov_reports: GET 匯出（students / attendance / leaves / overtimes / shifts / employee-attendance / 政府申報四端點）未呼叫 `write_explicit_audit`，PII 與身分證匯出無稽核軌跡

- **位置**：
  - `api/exports.py:241-302` `GET /exports/students`（全校學生 PII：student_id、生日、家長電話、地址）
  - `api/exports.py:308-483` `GET /exports/attendance`
  - `api/exports.py:560-622` `GET /exports/leaves`
  - `api/exports.py:628-697` `GET /exports/overtimes`
  - `api/exports.py:703-749` `GET /exports/holidays`
  - `api/exports.py:755-813` `GET /exports/shifts`
  - `api/exports.py:852-1178` `GET /exports/employee-attendance`（個人逐日打卡）
  - `api/exports.py:497-546` `GET /exports/calendar`
  - `api/gov_reports.py:308-924` `GET /gov-reports/labor-insurance` / `/health-insurance` / `/withholding` / `/pension`（含全員 `id_number` 身分證）
  - **對照組**：`api/exports.py:132-235` `GET /exports/employees` 已正確調用 `write_explicit_audit(action="EXPORT", entity_type="employee", ...)`（line 153-165），是唯一有顯式稽核痕跡的匯出端點。
- **威脅模型**：e
- **PoC**：依 `utils/audit.py:240-311` AuditMiddleware 設計，**只審計 POST/PUT/PATCH/DELETE，GET 請求不寫 AuditLog**；GET 匯出路徑（含 PII / 身分證 / 銀行帳號等敏感資料）必須由 endpoint 顯式調用 `write_explicit_audit`（line 192-237），否則沒有不可推卸的稽核軌跡。`exports.py` 只有 `/exports/employees`（員工名冊）做了，其他 8 支端點全沒做；`gov_reports.py` 所有四支政府申報端點（每支都會輸出全員 `id_number` 身分證）也沒做，僅以 `logger.warning` 留軌跡（log 為運維工具，retention 短且非業務證據）。
  攻擊面：持有 SALARY_READ 的會計（hr）對 `/gov-reports/withholding?year=2026` 下載含全員身分證 + 全年所得，**事後無 AuditLog 可追**；持 STUDENTS_READ 的角色（supervisor/自訂）對 `/exports/students` 下載含全校生日 + 家長手機 + 地址，**亦無紀錄**。事故發生（PII 外洩、勒索郵件）時，無法從 `AuditLog` 表回溯誰於何時下載過該檔，只能挖 nginx access log（依部署而定，可能未保留 query string 或已被 rotate）。
  本 finding 屬「稽核完整性」缺口而非直接 IDOR 越權，但落在威脅 e 的範疇：**內部高權限角色之間缺少彼此可驗證的痕跡，會計可一夜下載身分證後否認**。
- **根因**：`write_explicit_audit` 是後加的 helper（見 `utils/audit.py:192-237` docstring 說明「為 GET 匯出 / 敏感讀取顯式寫 AuditLog」），但只回填到 `/exports/employees` 一處，其他匯出端點實作時未補上。Excel 匯出 + StreamingResponse 的設計本身不會自動觸發 audit。
- **建議修法**：在每支匯出端點 return 之前統一調用：
  ```python
  write_explicit_audit(
      request,
      action="EXPORT",
      entity_type="<student|attendance|leave|overtime|shift|gov_report>",
      summary=f"匯出{...}（{count} 筆）",
      changes={"count": count, "year": year, "month": month, ...},
  )
  ```
  並在 `utils/audit.py:ENTITY_LABELS` 補 `gov_report`、`shift_assignment` 等 label。`/gov-reports/*` 還可在 `changes` 內標 `is_full_id_number=True`、`force=True`（force 模式更敏感），讓 SOC 能優先告警。
- **是否需新測試**：no（Medium；建議 Phase 2 與其他 audit 完整性修補一起做。新測試型如：呼叫 `/exports/students`，斷言 AuditLog 表新增一筆 `action='EXPORT'`、`entity_type='student'`，且 `changes.count` 等於 returned 筆數）
- **修補狀態**：⏳ Pending

### F-034 [Medium] fees: `GET /records?student_id=...` 跨班讀全校學生繳費紀錄，僅以 `FEES_READ` 守門

- **位置**：`api/fees.py:402-464` `GET /api/fees/records?student_id=...`（line 409 接受 `student_id` query；line 419-427 透過 `_apply_fee_record_filters` 過濾，無班級 scope）
- **威脅模型**：e + b
- **PoC**：endpoint 僅 `require_staff_permission(Permission.FEES_READ)`。預設 supervisor 持 FEES_READ + STUDENTS_READ（合法看全校）；但**自訂角色（如「財務助理」「會計記帳」）若只授予 FEES_READ 不授 STUDENTS_READ**，呼叫 `GET /api/fees/records?student_id={X}` 即可拿到任意學生跨學期所有費用紀錄（`student_name`、`classroom_name`、`fee_item_name`、`amount_due` / `amount_paid` / `status` / `payment_date`），等於用 FEES_READ 拿到 STUDENTS_READ 級的學生目錄資訊（姓名、班級、繳費狀況）。同 finding 也適用 `/api/fees/records`（不帶 student_id 但帶 `period` 或 `classroom_name`）— 寬鬆過濾下可一頁拉全校 200 筆繳費明細。本 finding 與 F-027（`api/activity/students/search` 在 ACTIVITY_WRITE 下回傳全校學生目錄）、F-028（POS 端點在 ACTIVITY_READ 下回 student PII）同型，但 fees 涉及金額；對家庭隱私敏感。
  另：班導/副班導若被授 FEES_READ（而非 STUDENTS_READ）也能透過此端點側面查到不屬於自班的學生繳費紀錄 — 屬 b 威脅。
- **根因**：`fees.py` 把 FEES_READ 視為單一閘門，未疊加 STUDENTS_READ 或班級 scope（`accessible_classroom_ids`）。退費 / 繳費端點已有 finance_guards 三重門檻（reason / 累積閾值 / 自我守衛）保護金流寫入面，但**讀取面**對 viewer 角色未做欄位區隔。
- **建議修法**：兩擇一——
  1. **perm gate 升級**：endpoint 改為 `FEES_READ + STUDENTS_READ`（`require_staff_permissions_all`），符合「看學生繳費紀錄需同時看得到學生本人」的實務設計。
  2. **班級 scope 收斂**：對非 admin/hr/supervisor 強制走 `accessible_classroom_ids`，限縮 `classroom_name` filter；帶不在 allowed 內的 classroom 回 403。
  建議 (1)：(2) 因 list 端點以 `classroom_name` 字串過濾（非 FK），實作較複雜；(1) 一行改動且更明確。
- **是否需新測試**：no（Medium；列為 Phase 2 與 F-017 / F-026 / F-027 自訂角色 RBAC 改造一併處理）
- **修補狀態**：⏳ Pending

### F-035 [Low] audit-logs: `GET /audit-logs/export` 自身未呼叫 `write_explicit_audit`，匯出全系統操作軌跡的事件本身無痕

- **位置**：`api/audit.py:141-215` `GET /api/audit-logs/export`
- **威脅模型**：e
- **PoC**：endpoint 受 `Permission.AUDIT_LOGS` 守門（預設僅 admin 持有），單看 perm 沒問題。但**匯出全系統操作軌跡（含他人薪資修改、員工資料異動、學費繳費等敏感寫入歷史）**這個動作本身**未被審計**：handler 只 stream CSV、不調用 `write_explicit_audit`，AuditMiddleware 又只審計 POST/PUT/DELETE。結果：admin A 下載全系統 10000 筆 AuditLog（含其他 admin/hr 的所有操作軌跡，最敏感 — 全公司動向都在裡面）、轉檔離線分發、之後否認，**`AuditLog` 表內找不到 A 下載過 export 的痕跡**。屬「meta-audit」缺口：稽核工具自身不被稽核。
  雖然多 admin 互信，且預設角色配置下只有 admin 可觸發，但對 SOC / 法遵需求（誰看了全系統紀錄需要可追）構成弱點。比較對照組：`api/exports.py:/exports/employees` 已有 `write_explicit_audit`，是對「員工名冊匯出」的 meta-audit。AuditLog 自身匯出反而沒有，明顯不一致。
- **根因**：`audit.py` 為早期實作（在 `write_explicit_audit` helper 之前），未回填。匯出上限 10000 筆已限制單次 blast radius，但對單一匯出事件本身的可追性無幫助。
- **建議修法**：在 line 172 取得 `items` 後、return StreamingResponse 之前調用：
  ```python
  write_explicit_audit(
      request,
      action="EXPORT",
      entity_type="audit_log",  # 需在 utils/audit.py:ENTITY_LABELS 補 audit_log: "操作紀錄"
      summary=f"匯出操作審計紀錄（{len(items)} 筆，篩選：entity_type={entity_type or '*'}, action={action or '*'}, username={username or '*'}）",
      changes={
          "count": len(items),
          "filters": {
              "entity_type": entity_type, "action": action, "username": username,
              "entity_id": entity_id, "ip_address": ip_address,
              "start_at": start_at.isoformat() if start_at else None,
              "end_at": end_at.isoformat() if end_at else None,
          },
      },
  )
  ```
  注意 entity_type 取 `audit_log` 而非 `audit`，避免與 path pattern 衝突。
- **是否需新測試**：no（Low）
- **修補狀態**：⏳ Pending
