# Phase C Backend Plan — Leave Quota Lifecycle

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development

**Goal:** 後端落地 Phase C 4 個 sub-feature 的 backend 部分：(C-1) SalaryView 明細 endpoint / (C-2) LINE Bot 7 天前到期提醒 / (C-3) portal 補休預告 endpoint / (C-4) offboarding path 補寫 log。

**前提：**
- Phase A backend (`516dabf`) 已 merge — model + scheduler + 4 endpoints 已就緒
- 既有 `services/offboarding/steps/snapshot_leave.py::prefill_salary` 已寫 `SalaryRecord.unused_leave_payout`，需補 log
- 既有 `services/leave_quota_expiry_scheduler.py` asyncio polling — 加 step `remind_upcoming_comp_grants`
- 既有 `services/line_service.py::LineService.push_to_user(line_user_id, text)` 可用

---

## C-1: SalaryView 明細 endpoint (~2 task)

### Task C1-1: `GET /salary-records/{salary_record_id}/unused-leave-payout-detail`

**Files:**
- Create: `api/salary/unused_leave_detail.py`（或加進 `api/salary.py`）
- Test: `tests/test_salary_unused_leave_payout_detail_api.py`

**Endpoint signature:**
```
GET /salary-records/{salary_record_id}/unused-leave-payout-detail
Permission: SALARY_READ (HR) OR 員工本人 (employee_id == request.user.employee_id)
Response: {
  "salary_record_id": int,
  "total_amount": Decimal,  # SalaryRecord.unused_leave_payout
  "logs": [
    {
      "log_id": int,
      "source_type": str,        # comp_grant_expiry / annual_anniversary / offboarding
      "hours": float,
      "hourly_wage": Decimal,
      "amount": Decimal,
      "wage_basis_date": date,
      "meta": dict,              # grant_ids / period / breakdown
    },
    ...
  ]
}
```

**邏輯：**
1. Query `SalaryRecord` by id，404 if not found
2. Permission check：HR (SALARY_READ) 或 employee 本人
3. Query `UnusedLeavePayoutLog WHERE salary_record_id = :id ORDER BY created_at`
4. Return aggregate + logs

**Test (3 case)：**
- HR 看任何員工 → 200 + logs
- 員工本人查自己 → 200
- 員工查他人 → 403
- SalaryRecord 不存在 → 404
- SalaryRecord 存在但無 logs → 200 + logs=[]

**Step：寫 test → impl → commit `feat(salary): unused-leave-payout-detail endpoint`**

---

## C-2: LINE Bot 7 天前到期提醒 (~2 task)

### Task C2-1: `OvertimeCompLeaveGrant` 加 `reminder_sent_at` 欄 + 新 migration

**Files:**
- Modify: `models/overtime_comp_leave_grant.py`（加 `reminder_sent_at DateTime nullable`）
- Create: `alembic/versions/20260526_compexpr02_grant_reminder_sent_at.py`
- Test: 不需新 test (schema change，下個 task 整合測)

```python
# models/overtime_comp_leave_grant.py 追加
reminder_sent_at = Column(DateTime, nullable=True, comment="LINE 推播提醒已發送時間（防重複）")
```

```python
# migration
op.add_column("overtime_comp_leave_grants", sa.Column("reminder_sent_at", sa.DateTime, nullable=True))
```

down_revision = `mergeheads05`。**注意**：user 並行可能加新 migration，跑 `alembic heads` 確認 base。若衝突再加 merge migration。

### Task C2-2: scheduler 加 `remind_upcoming_comp_grants` step

**Files:**
- Create: `services/leave_quota_expiry/comp_grant_reminder.py`
- Modify: `services/leave_quota_expiry_scheduler.py`（handle 內加 `remind_upcoming_comp_grants(today, session)`）
- Test: `tests/test_remind_upcoming_comp_grants.py`

**邏輯：**
```python
def remind_upcoming_comp_grants(today, session, days_ahead=7) -> dict:
    """7 天前推 LINE 提醒員工排補休。

    撈 active grant WHERE expires_at BETWEEN today AND today + 7 days
                          AND reminder_sent_at IS NULL
                          AND Employee.is_active = True
                          AND User.line_user_id IS NOT NULL
    Group by employee_id → 每員工發一則 LINE text
    Stamp grant.reminder_sent_at = now
    Return {reminded_employees: int, skipped_no_line: int}
    """
```

**Text message template:**
```
您好，您有 X 小時補休將於 YYYY-MM-DD 到期。
逾期未休將自動折算工資。建議盡早申請補休假單。
```

**Test (4 case)：**
- 0 upcoming grants → no-op
- grant within 7 days + line_user_id 存在 → push + reminder_sent_at set
- grant within 7 days + line_user_id NULL → skipped_no_line+1
- grant within 7 days + reminder_sent_at 已 set → 不重推
- mock `LineService.push_to_user` 驗呼叫

**Step：寫 test → impl → wire scheduler → commit `feat(leave-expiry): LINE Bot 補休 7 天前到期提醒`**

---

## C-3: portal 補休預告 endpoint (~2 task)

### Task C3-1: `GET /portal/me/leave-quota-expiry`

**Files:**
- Modify: `api/portal.py`（加新 endpoint）
- Test: `tests/test_portal_leave_quota_expiry.py`

**Endpoint signature:**
```
GET /portal/me/leave-quota-expiry
Permission: portal user (員工自己，require_portal_user)
Response: {
  "compensatory_balance": float,           # _compensatory_balance(emp_id)
  "earliest_expiring_grant": {             # 最早到期 grant
    "expires_at": date,
    "unexpired_hours": float,
  } | null,
  "next_anniversary": date | null,         # 員工下個 hire_date 週年
  "expected_payout_month": "YYYY-MM" | null,  # 下個結算月 (_next_month from next_anniversary or earliest_expiring)
}
```

**邏輯：**
1. From request.user 取 employee_id
2. `_compensatory_balance(employee_id, session)` 拿結餘
3. Query earliest active grant by `expires_at ASC`
4. Compute `next_anniversary` from emp.hire_date（最近未來週年）
5. `expected_payout_month`:
   - 若 earliest grant.expires_at < next_anniversary → `_next_month(earliest_expiring.expires_at)`
   - 否則 `_next_month(next_anniversary)`
   - 若兩者皆 null → null

**Test (3 case)：**
- 有 grant + 有 anniversary → 完整 response
- 無 grant (compensatory_balance=0) → earliest=null
- 員工未滿 6 個月（無 anniversary）→ next_anniversary=null

**Step：寫 test → impl → commit `feat(portal): /portal/me/leave-quota-expiry 補休結餘 + 下個結算月預告`**

---

## C-4: offboarding path 補寫 log (~2 task)

### Task C4-1: prefill_salary 同時寫 UnusedLeavePayoutLog

**Files:**
- Modify: `services/offboarding/steps/snapshot_leave.py`（`prefill_salary` 內補 log 寫入）
- Test: `tests/test_offboarding_prefill_writes_payout_log.py`

**邏輯：**
prefill_salary 既有寫 `SalaryRecord.unused_leave_payout = snapshot.payout_amount`，**新增**寫對應 `UnusedLeavePayoutLog`：

```python
log = UnusedLeavePayoutLog(
    employee_id=emp.id,
    source_type='offboarding',
    source_ref_id=record.id,
    hours=snapshot["remaining_hours"],
    hourly_wage=Decimal(str(snapshot["daily_wage"] / 8)),  # daily → hourly
    amount=Decimal(str(snapshot["payout_amount"])),
    wage_basis_date=record.resign_date,
    salary_record_id=salary_record.id,
    salary_period_year=salary_record.salary_year,
    salary_period_month=salary_record.salary_month,
    meta={
        "offboarding_record_id": record.id,
        "termination_date": record.resign_date.isoformat(),
        "snapshot_remaining_days": snapshot["remaining_days"],
    },
)
session.add(log)
```

**並同步 revoke 該員工 active grants**（避免 scheduler 重複結算）：

```python
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

active_grants = session.query(OvertimeCompLeaveGrant).filter_by(
    employee_id=emp.id, status='active'
).all()
for g in active_grants:
    g.status = 'revoked'
```

**Test (3 case)：**
- prefill_salary 跑後驗 `UnusedLeavePayoutLog source_type='offboarding'` row 存在 + amount 對齊 snapshot
- 既有 active grants 全 mark revoked
- log.salary_record_id 反向綁定正確

**Step：寫 test → impl → commit `feat(offboarding): prefill_salary 同步寫 offboarding log + revoke active grants`**

---

## Out of Scope（Phase D）
- LINE flex message（目前 text message 即可）
- 推播 i18n（目前繁中即可）
- portal 補休歷史明細頁
- 離職員工特休週年 cutover 跳過（已由 `Employee.is_active=True` filter 防護）

---

## 工作流程
- 順序 C4 → C2 → C1 → C3（C4 先做有 offboarding log 後 C1 才能在 SalaryView 顯示完整 source）
- 每 task：dispatch implementer → spec+quality combined review (haiku for trivial, sonnet for integration)
- 全完 final review → merge → cleanup
