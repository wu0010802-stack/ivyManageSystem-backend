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
