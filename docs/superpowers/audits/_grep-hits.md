# IDOR Grep Hits（Phase 1 暫存）

> Task 2 產出，供後續 task 複審使用。Phase 1 結束時可刪除或併入 audit 報告 appendix。

統計：
- 全部 router 檔：95
- 引用任一 auth 函式（`get_current_user` / `require_permission` / `require_staff_permission` / `require_parent_role` / `require_admin` / `verify_line_signature` / `verify_ws_token`）：91
- 真正未引用任何 auth 函式（公開候選）：4 個非 `__init__.py`（見 C 段）
- 路徑帶 `*_id` 的 endpoint：155
- 用 `session.query(Model).get(id)` 直接撈物件：16 處（不檢查 owner 的 high-risk pattern）

---

## A. 路徑帶 `*_id` 的 endpoint（155 筆）

按模組分組（複審時逐個跑）。

### parent_portal（家長端，威脅 c）

```
api/parent_portal/events.py:145:@router.post("/{event_id}/ack")
api/parent_portal/announcements.py:142:@router.post("/{announcement_id}/read")
api/parent_portal/activity.py:291:@router.post("/registrations/{registration_id}/confirm-promotion")
api/parent_portal/activity.py:338:@router.get("/registrations/{registration_id}/payments")
api/parent_portal/binding_admin.py:38:@router.post("/{guardian_id}/binding-code")
api/parent_portal/fees.py:139:@router.get("/records/{record_id}/payments")
api/parent_portal/leaves.py:164:@router.get("/{leave_id}")
api/parent_portal/leaves.py:185:@router.post("/{leave_id}/cancel")
```

### portal（教師端，威脅 a+b）

```
api/portal/activity.py:290:@router.get("/activity/attendance/sessions/{session_id}")
api/portal/activity.py:321:@router.put("/activity/attendance/sessions/{session_id}/records")
api/portal/anomalies.py:91:@router.post("/anomalies/{attendance_id}/confirm")
api/portal/dismissal_calls.py:176:@router.post("/dismissal-calls/{call_id}/acknowledge")
api/portal/dismissal_calls.py:192:@router.post("/dismissal-calls/{call_id}/complete")
api/portal/overtimes.py:156:@router.delete("/my-overtimes/{overtime_id}")
api/portal/announcements.py:74:@router.post("/announcements/{announcement_id}/read")
api/portal/schedule.py:353:@router.post("/swap-requests/{request_id}/respond")
api/portal/schedule.py:474:@router.post("/swap-requests/{request_id}/cancel")
api/portal/leaves.py:373:@router.post("/my-leaves/{leave_id}/attachments")
api/portal/leaves.py:440:@router.delete("/my-leaves/{leave_id}/attachments/{filename}")
api/portal/leaves.py:484:@router.get("/my-leaves/{leave_id}/attachments/{filename}")
api/portal/leaves.py:699:@router.post("/my-leaves/{leave_id}/substitute-respond")
```

### 員工財務 / 人事頂層（威脅 a / e）

```
api/auth.py:773:@router.put("/users/{user_id}/reset-password")
api/auth.py:810:@router.put("/users/{user_id}")
api/auth.py:879:@router.delete("/users/{user_id}")
api/leaves_quota.py:790:@quota_router.put("/leaves/quotas/{quota_id}")
api/leaves.py:752:@router.put("/leaves/{leave_id}")
api/leaves.py:906:@router.delete("/leaves/{leave_id}")
api/leaves.py:994:@router.put("/leaves/{leave_id}/approve")
api/leaves.py:1826:@router.get("/leaves/{leave_id}/attachments/{filename}")
api/overtimes.py:830:@router.put("/overtimes/{overtime_id}")
api/overtimes.py:1007:@router.delete("/overtimes/{overtime_id}")
api/overtimes.py:1054:@router.put("/overtimes/{overtime_id}/approve")
api/punch_corrections.py:105:@router.put("/punch-corrections/{correction_id}/approve")
api/salary.py:518:@router.get("/salaries/calculate-jobs/{job_id}")
api/salary.py:899:@router.put("/salaries/{record_id}/manual-adjust")
api/salary.py:1087:@router.get("/salaries/{record_id}/audit-log")
api/salary.py:1134:@router.get("/salaries/{record_id}/breakdown")
api/salary.py:1229:@router.get("/salaries/{record_id}/field-breakdown")
api/salary.py:1268:@router.get("/salaries/{record_id}/export")
api/salary.py:1755:@router.delete("/salaries/{record_id}/finalize")
api/salary.py:1861:@router.get("/salaries/snapshots/{snapshot_id}")
api/salary.py:1912:@router.get("/salaries/snapshots/{snapshot_id}/diff")
api/employees.py:247:@router.get("/employees/{employee_id}")
api/employees.py:367:@router.put("/employees/{employee_id}")
api/employees.py:482:@router.delete("/employees/{employee_id}")
api/employees.py:508:@router.post("/employees/{employee_id}/offboard")
api/employees.py:553:@router.get("/employees/{employee_id}/final-salary-preview")
api/employees_docs.py:147:@router.get("/employees/{employee_id}/educations")
api/employees_docs.py:166:@router.post("/employees/{employee_id}/educations")
api/employees_docs.py:196:@router.put("/employees/{employee_id}/educations/{edu_id}")
api/employees_docs.py:233:@router.delete("/employees/{employee_id}/educations/{edu_id}")
api/employees_docs.py:257:@router.get("/employees/{employee_id}/certificates")
api/employees_docs.py:273:@router.post("/employees/{employee_id}/certificates")
api/employees_docs.py:295:@router.put("/employees/{employee_id}/certificates/{cert_id}")
api/employees_docs.py:324:@router.delete("/employees/{employee_id}/certificates/{cert_id}")
api/employees_docs.py:348:@router.get("/employees/{employee_id}/contracts")
api/employees_docs.py:364:@router.post("/employees/{employee_id}/contracts")
api/employees_docs.py:397:@router.put("/employees/{employee_id}/contracts/{contract_id}")
api/employees_docs.py:442:@router.delete("/employees/{employee_id}/contracts/{contract_id}")
api/shifts.py:230:@router.put("/types/{type_id}")
api/shifts.py:279:@router.delete("/types/{type_id}")
api/shifts.py:591:@router.delete("/daily/{shift_id}")
api/meetings.py:348:@router.put("/meetings/{record_id}")
api/meetings.py:395:@router.delete("/meetings/{record_id}")
```

### 學生資料頂層（威脅 b）

```
api/students.py:481:@router.get("/students/{student_id}")
api/students.py:566:@router.put("/students/{student_id}")
api/students.py:612:@router.delete("/students/{student_id}")
api/students.py:662:@router.post("/students/{student_id}/graduate")
api/students.py:852:@router.get("/students/{student_id}/profile")
api/students.py:880:@router.post("/students/{student_id}/lifecycle")
api/students.py:1003:@router.get("/students/{student_id}/guardians")
api/students.py:1030:@router.post("/students/{student_id}/guardians")
api/students.py:1073:@router.patch("/students/guardians/{guardian_id}")
api/students.py:1127:@router.delete("/students/guardians/{guardian_id}")
api/student_health.py:196:@router.get("/students/{student_id}/allergies")
api/student_health.py:221:@router.post("/students/{student_id}/allergies")
api/student_health.py:265:@router.patch("/students/{student_id}/allergies/{alg_id}")
api/student_health.py:305:@router.delete("/students/{student_id}/allergies/{alg_id}")
api/student_health.py:341:@router.get("/students/{student_id}/medication-orders")
api/student_health.py:370:@router.get("/students/{student_id}/medication-orders/{order_id}")
api/student_health.py:397:@router.post("/students/{student_id}/medication-orders")
api/student_health.py:490:@router.post("/medication-logs/{log_id}/administer")
api/student_health.py:530:@router.post("/medication-logs/{log_id}/skip")
api/student_health.py:563:@router.post("/medication-logs/{log_id}/correct")
api/student_change_logs.py:505:@router.put("/{log_id}")
api/student_change_logs.py:546:@router.delete("/{log_id}")
api/student_leaves.py:189:@router.post("/{leave_id}/approve")
api/student_leaves.py:223:@router.post("/{leave_id}/reject")
api/student_assessments.py:187:@router.put("/student-assessments/{assessment_id}")
api/student_assessments.py:238:@router.delete("/student-assessments/{assessment_id}")
api/student_incidents.py:191:@router.put("/student-incidents/{incident_id}")
api/student_incidents.py:251:@router.delete("/student-incidents/{incident_id}")
api/student_communications.py:255:@router.put("/{log_id}")
api/student_communications.py:293:@router.delete("/{log_id}")
api/portfolio/observations.py:130:@router.get("/{student_id}/observations")
api/portfolio/observations.py:179:@router.post("/{student_id}/observations")
api/portfolio/observations.py:224:@router.patch("/{student_id}/observations/{obs_id}")
api/portfolio/observations.py:280:@router.delete("/{student_id}/observations/{obs_id}")
api/classrooms.py:576:@router.get("/classrooms/{classroom_id}")
api/classrooms.py:597:@router.get("/classrooms/{classroom_id}/enrollment-composition")
api/classrooms.py:699:@router.put("/classrooms/{classroom_id}")
api/classrooms.py:1149:@router.delete("/classrooms/{classroom_id}")
api/classrooms.py:1227:@router.patch("/grades/{grade_id}")
```

### activity / recruitment（威脅 c + d + e）

```
api/activity/courses.py:128:@router.get("/courses/{course_id}")
api/activity/courses.py:305:@router.put("/courses/{course_id}")
api/activity/courses.py:357:@router.get("/courses/{course_id}/waitlist")
api/activity/courses.py:412:@router.get("/courses/{course_id}/enrolled")
api/activity/courses.py:467:@router.delete("/courses/{course_id}")
api/activity/inquiries.py:56:@router.put("/inquiries/{inquiry_id}/read")
api/activity/inquiries.py:82:@router.put("/inquiries/{inquiry_id}/reply")
api/activity/inquiries.py:111:@router.delete("/inquiries/{inquiry_id}")
api/activity/registrations.py:852:@router.post("/registrations/{registration_id}/match")
api/activity/registrations.py:923:@router.post("/registrations/{registration_id}/reject")
api/activity/registrations.py:987:@router.post("/registrations/{registration_id}/rematch")
api/activity/registrations.py:1111:@router.post("/registrations/{registration_id}/force-accept")
api/activity/registrations.py:1211:@router.post("/registrations/{registration_id}/restore")
api/activity/registrations.py:1438:@router.get("/registrations/{registration_id}")
api/activity/registrations.py:1545:@router.put("/registrations/{registration_id}/payment")
api/activity/registrations.py:1690:@router.put("/registrations/{registration_id}/remark")
api/activity/registrations.py:1730:@router.put("/registrations/{registration_id}")
api/activity/registrations.py:1812:@router.post("/registrations/{registration_id}/courses")
api/activity/registrations.py:1935:@router.post("/registrations/{registration_id}/supplies")
api/activity/registrations.py:2015:@router.delete("/registrations/{registration_id}/supplies/{supply_record_id}")
api/activity/registrations.py:2168:@router.get("/registrations/{registration_id}/payments")
api/activity/registrations.py:2239:@router.post("/registrations/{registration_id}/payments")
api/activity/registrations.py:2458:@router.delete("/registrations/{registration_id}/payments/{payment_id}")
api/activity/registrations.py:2586:@router.put("/registrations/{registration_id}/waitlist")
api/activity/registrations.py:2656:@router.delete("/registrations/{registration_id}/courses/{course_id}")
api/activity/registrations.py:2839:@router.delete("/registrations/{registration_id}")
api/activity/supplies.py:118:@router.put("/supplies/{supply_id}")
api/activity/supplies.py:170:@router.delete("/supplies/{supply_id}")
api/recruitment/periods.py:321:@router.put("/periods/{period_id}")
api/recruitment/periods.py:338:@router.delete("/periods/{period_id}")
api/recruitment/periods.py:350:@router.post("/periods/{period_id}/sync")
api/recruitment/records.py:185:@router.put("/records/{record_id}")
api/recruitment/records.py:207:@router.delete("/records/{record_id}")
api/recruitment/records.py:302:@router.post("/records/{record_id}/convert")
```

### admin / 財務報表（威脅 e）

```
api/fees.py:253:@router.put("/items/{item_id}")
api/fees.py:289:@router.delete("/items/{item_id}")
api/fees.py:472:@router.put("/records/{record_id}/pay")
api/fees.py:757:@router.post("/records/{record_id}/refund")
api/fees.py:927:@router.get("/records/{record_id}/refunds")
```

### 其餘模組

```
api/announcements.py:201:@router.put("/{announcement_id}")
api/announcements.py:244:@router.delete("/{announcement_id}")
api/announcements.py:324:@router.get("/{announcement_id}/parent-recipients")
api/announcements.py:394:@router.put("/{announcement_id}/parent-recipients")
api/attachments.py:211:@router.delete("/attachments/{attachment_id}")
api/dismissal_calls.py:284:@router.post("/{call_id}/cancel")
api/events.py:137:@router.get("/events/{event_id}")
api/events.py:198:@router.put("/events/{event_id}")
api/events.py:245:@router.delete("/events/{event_id}")
api/config.py:1006:@router.put("/titles/{title_id}")
api/config.py:1042:@router.delete("/titles/{title_id}")
api/attendance/records.py:370:@router.delete("/record/{employee_id}/{date}")
api/attendance/records.py:415:@router.delete("/records/{employee_id}/{date_str}")
```

---

## B. `session.query(Model).get(id)` 候選（16 筆 — high-risk pattern）

> 這類直接撈出 ORM 物件後**沒有檢查 owner**，只要拿到 id 就能讀寫。複審時**必查**。

```
api/meetings.py:235:        emp = session.query(Employee).get(data.employee_id)
api/meetings.py:357:        record = session.query(MeetingRecord).get(record_id)
api/meetings.py:375:            emp = session.query(Employee).get(record.employee_id)
api/meetings.py:403:        record = session.query(MeetingRecord).get(record_id)
api/shifts.py:238:        st = session.query(ShiftType).get(type_id)
api/shifts.py:286:        st = session.query(ShiftType).get(type_id)
api/shifts.py:599:        ds = session.query(DailyShift).get(shift_id)
api/dev.py:501:        emp = session.query(Employee).get(employee_id)
api/portal/schedule.py:423:            req_emp = session.query(Employee).get(swap.requester_id)
api/employees.py:324:            job_title = session.query(JobTitle).get(emp_data["job_title_id"])
api/employees.py:419:                        jt = session.query(JobTitle).get(value)
api/recruitment/periods.py:328:        p = session.query(RecruitmentPeriod).get(period_id)
api/recruitment/periods.py:344:        p = session.query(RecruitmentPeriod).get(period_id)
api/recruitment/periods.py:357:        p = session.query(RecruitmentPeriod).get(period_id)
api/recruitment/records.py:192:        record = session.query(RecruitmentVisit).get(record_id)
api/recruitment/records.py:213:        record = session.query(RecruitmentVisit).get(record_id)
```

---

## C. 未引用任何 auth 函式的 router 檔（4 個 + 4 個 `__init__.py`）

```
api/activity/public.py    ← 公開報名（家長未登入）— 預期公開
api/health.py             ← health check — 預期公開
api/dismissal_ws.py       ← WebSocket，用 verify_ws_token 認證（grep 已排除）
api/parent_portal/__init__.py  ← 純 router 組合
api/portal/__init__.py         ← 純 router 組合
api/activity/__init__.py       ← 純 router 組合
api/attendance/__init__.py     ← 純 router 組合
api/recruitment/__init__.py    ← 純 router 組合
```

> 真正需審視的「公開面向 + 帶資源 id」端點集中在 `api/activity/public.py`。
