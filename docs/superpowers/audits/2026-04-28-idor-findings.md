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
