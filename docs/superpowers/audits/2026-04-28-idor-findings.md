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
