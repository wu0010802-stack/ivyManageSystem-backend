---
title: 補休到期與特休週年制 — Leave Quota Lifecycle Design
date: 2026-05-26
status: draft (amended 2026-05-26 — payout path 改 Alt A)
owner: yilunwu
related:
  - api/leaves_quota.py
  - services/term_subscribers/leave_quota_cutover.py
  - services/salary/unused_leave_pay.py
  - models/leave.py
  - models/overtime.py
  - services/recruitment_term_advance_scheduler.py
amendment_log:
  - 2026-05-26 v2：payout path 從 SpecialBonusItem 改為新建 unused_leave_payout_log 表
    原因：SpecialBonusItem 真實 schema 是「綁定 YearEndCycle 的年度附加項」
    （year_end_cycle_id NOT NULL FK / bonus_type PG ENUM / period_label String），
    與本 spec 需要的「按月可寫的 generic 附加項」cadence 不符。直接寫 SalaryRecord.unused_leave_payout
    沿用 offboarding path，per-event 帳本走獨立 log 表。
  - 2026-05-26 v2：scheduler 改 asyncio polling pattern（沿用 recruitment_term_advance_scheduler）
    取代 apscheduler cron，與 codebase 既有所有 scheduler 一致。
  - 2026-05-26 v2：加入 alembic merge heads 前置步驟（current heads: audrsk01 + mergeheads03）。
---

# 補休到期與特休週年制設計

## 1. Overview

本設計解決兩個既有合規 gap：

1. **補休無壽命上限**：`_grant_comp_leave_quota` 在加班核准時 upsert `LeaveQuota.compensatory` 聚合 row，無到期 stamping。員工可主張任意時點折算，雇主於離職時可能被一次折算多年累積補休 + §22-2 加計利息，違反勞基法 §32-1「未補休時數應依加班費標準折發工資」。
2. **特休採曆年 12/31 fallback**：`_calc_annual_leave_hours` 的 default `date(year, 12, 31)` 與勞基法施行細則 §24 四選一（曆年/教學年度/會計年度/週年制）需勞資協商之要求未對齊。學年制 cutover handler 已部分落實但 12/31 fallback 仍存在。

設計範圍：
- **補休**：per-OT grant ledger + 加班發生日 +1 年到期 + 到期自動折算工資寫入次月薪資
- **特休**：改週年制（hire_date 觸發）+ 每日 scheduler cutover + 未休自動折算
- **其他法定假別**（事/病/家庭照顧/生理）：維持學年制 cutover handler 既有邏輯，本 spec 不動
- **離職 path**（`services/salary/unused_leave_pay.py` + `SalaryRecord.unused_leave_payout`）：保留不動，與本 spec 新 path 共存

## 2. 前提假設

1. **勞資協商已簽署**：補休 1 年到期、特休改週年制屬勞動條件變更，需勞基法 §70-1 + 工會法相關協商程序。System 上線前 HR 必完成書面協議，本系統作為合規執行工具，不取代協商程序。
2. **`SalaryRecord.unused_leave_payout` 欄已存在**（offb0001 migration 加，由離職 path 寫入）。本 spec 新 path 共用此欄，靠新建 `unused_leave_payout_log` 表的 `source_type` 區分來源（離職/特休週年/補休到期）。
3. **不動 `special_bonus_items` 表**：該表綁定 `year_end_cycle_id NOT NULL FK`，是「年度 cycle 內附加項」語意，不適合本 spec「每月 scheduler 寫入」cadence。本 spec 改走新建獨立 log 表 + 直寫 SalaryRecord path。
4. **Employee 有合理 `hire_date`**（NOT NULL，現有資料已驗證）。
5. **Scheduler 採 asyncio polling pattern**：沿用 `services/recruitment_term_advance_scheduler.py` / `services/graduation_scheduler.py` 結構（`while not stop_event.is_set(): await asyncio.wait_for(stop_event.wait(), timeout=check_interval)`），由 `main.py` lifespan 啟動。
6. **時薪計算公式採通說**：月薪 ÷ 30 ÷ 8（勞動部 §38 解釋令採此基準），非 21.75 工作日制。
7. **Alembic 當前兩 head**（`audrsk01` + `mergeheads03`）：新 migration 前先加一條 merge migration 統一 head。

## 3. Data Model

### 3.1 新表 `overtime_comp_leave_grants`（補休 grant ledger）

```sql
CREATE TABLE overtime_comp_leave_grants (
    id BIGSERIAL PRIMARY KEY,
    overtime_record_id INTEGER NOT NULL UNIQUE
        REFERENCES overtime_records(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL
        REFERENCES employees(id) ON DELETE CASCADE,
    granted_hours FLOAT NOT NULL,           -- = OvertimeRecord.hours
    granted_at DATE NOT NULL,               -- = OvertimeRecord.overtime_date
    expires_at DATE NOT NULL,               -- = granted_at + 1 year
    consumed_hours FLOAT NOT NULL DEFAULT 0, -- 已被核准補休假單扣抵
    status VARCHAR(20) NOT NULL DEFAULT 'active', -- active / expired / revoked
    expired_at TIMESTAMP NULL,              -- scheduler stamp 結算時間
    payout_salary_record_id INTEGER NULL
        REFERENCES salary_records(id) ON DELETE SET NULL,
    payout_log_id BIGINT NULL
        REFERENCES unused_leave_payout_log(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_grant_emp_status_expires
    ON overtime_comp_leave_grants (employee_id, status, expires_at);

CREATE INDEX ix_grant_status_expires_active
    ON overtime_comp_leave_grants (expires_at)
    WHERE status = 'active';  -- scheduler 每日撈即將到期用
```

**Invariants**：
- `consumed_hours <= granted_hours`（CHECK constraint）
- `status='expired'` 時 `expired_at` NOT NULL 且 `payout_log_id` NOT NULL
- `status='revoked'` 時 `expired_at` 可 NULL（人工撤銷無 payout）
- 每筆 OT 至多一筆 grant（UNIQUE 約束）

### 3.2 `LeaveQuota` 修改（特休週年制）

```sql
ALTER TABLE leave_quotas
    ADD COLUMN period_start DATE NULL,
    ADD COLUMN period_end DATE NULL;

CREATE UNIQUE INDEX uq_leave_quotas_emp_period_annual
    ON leave_quotas (employee_id, period_start, leave_type)
    WHERE period_start IS NOT NULL AND leave_type = 'annual';
```

**語意**：
- 既有 `school_year` 欄位**保留**給其他法定假別（事/病/家庭照顧/生理）用
- 特休（`leave_type='annual'`）從 cutover handler 移除，由新 scheduler 負責
- `period_start IS NULL` = legacy 學年制 row；`period_start IS NOT NULL` = 週年制 row

### 3.3 新表 `unused_leave_payout_log`（per-event 帳本）

```sql
CREATE TABLE unused_leave_payout_log (
    id BIGSERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL
        REFERENCES employees(id) ON DELETE RESTRICT,
    source_type VARCHAR(30) NOT NULL,        -- comp_grant_expiry / annual_anniversary / offboarding
    source_ref_id INTEGER NULL,              -- comp_grant_expiry: 不填（多筆 grant 一次結算，靠 overtime_comp_leave_grants.payout_log_id 反查）
                                             -- annual_anniversary: leave_quotas.id（cutover 的 quota row）
                                             -- offboarding: employee_offboarding_records.id
    hours FLOAT NOT NULL,                    -- 結算未休時數
    hourly_wage NUMERIC(10, 2) NOT NULL,     -- 結算時點時薪（snapshot）
    amount NUMERIC(10, 2) NOT NULL,          -- = round_half_up(hours * hourly_wage)
    wage_basis_date DATE NOT NULL,           -- 取時薪的基準日（scheduler 跑當日）
    salary_record_id INTEGER NULL            -- 寫入哪筆 SalaryRecord（null = 該月薪資未 calculate）
        REFERENCES salary_records(id) ON DELETE SET NULL,
    salary_period_year INTEGER NOT NULL,     -- 預期入帳年（idempotency 用）
    salary_period_month INTEGER NOT NULL,    -- 預期入帳月（idempotency 用）
    meta JSONB NOT NULL DEFAULT '{}',        -- 來源 specific detail（grant_ids / period / breakdown）
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_payout_log_emp_period
    ON unused_leave_payout_log (employee_id, salary_period_year, salary_period_month);

CREATE INDEX ix_payout_log_salary_record
    ON unused_leave_payout_log (salary_record_id)
    WHERE salary_record_id IS NOT NULL;

CREATE UNIQUE INDEX uq_payout_log_anniversary
    ON unused_leave_payout_log (employee_id, source_type, source_ref_id)
    WHERE source_type = 'annual_anniversary';  -- 同 cutover quota 只能結算一次
```

**`meta` JSONB 結構（per source_type）**：

```json
// source_type='comp_grant_expiry'
{
  "expired_grant_ids": [123, 124, 125],
  "hours_breakdown": [
    {"grant_id": 123, "overtime_date": "2025-04-01", "unexpired_hours": 4.0}
  ]
}

// source_type='annual_anniversary'
{
  "period_start": "2025-08-15",
  "period_end": "2026-08-15",
  "entitled_hours": 80.0,
  "used_hours": 64.0
}

// source_type='offboarding'（離職 path 寫，沿用既有 unused_leave_pay.py 計算）
{
  "offboarding_record_id": 456,
  "termination_date": "2026-04-15"
}
```

**Invariants**：
- `source_type='comp_grant_expiry'`：靠 `overtime_comp_leave_grants.payout_log_id` 反查找關聯 grants（一筆 log 對多筆 grant）
- `source_type='annual_anniversary'`：partial unique index 擋同員工同 quota 二次結算
- `source_type='offboarding'`：由 offboarding step 寫入，本 spec 不主動寫但定義 schema 供既有 path 共用

### 3.4 Migration 順序

**Step 0：merge alembic heads（前置）**

Current heads: `audrsk01` + `mergeheads03`。先加一條 merge migration:

```python
# revision: mergeheads04
# down_revision: ('audrsk01', 'mergeheads03')
def upgrade(): pass
def downgrade(): pass
```

**Step 1：`compexpr01` 主 migration**（down_revision='mergeheads04'）

依序執行：
1. CREATE TABLE `unused_leave_payout_log`（含 indexes）
2. CREATE TABLE `overtime_comp_leave_grants`（含 indexes + FK 到 `unused_leave_payout_log`）
3. ALTER TABLE `leave_quotas` 加 `period_start` / `period_end` 兩欄 + partial unique index
4. Backfill：既有 OT (`use_comp_leave=True AND comp_leave_granted=True AND is_approved=True`) → grant rows
   - `granted_hours = ot.hours`、`granted_at = ot.overtime_date`
   - `expires_at = upgrade_date + INTERVAL '3 months'`（一次性寬限期，覆寫原 overtime_date+1y）
   - 寬限期長度由 ENV `LEAVE_BACKFILL_GRACE_MONTHS`（default 3）控制
   - `consumed_hours = 0`（無法 trace 既有假單對應哪筆 OT，全部歸零；既有 LeaveQuota 聚合 row 仍存在不影響員工已用顯示）
   - `status = 'active'`
5. Backfill：既有 `LeaveQuota WHERE leave_type='annual'` →
   - `period_start = 員工 hire_date 最近一個週年`（過去最近的 anniversary date）
   - `period_end = period_start + 1 year`
   - `total_hours` 不變（既有結餘保留）
6. Downgrade：對稱 drop（不嘗試 reverse backfill grants → OT，純 schema 還原）

## 4. State Machine & Scheduler

### 4.1 新檔 `services/leave_quota_expiry_scheduler.py`

沿用 `services/recruitment_term_advance_scheduler.py` 結構（asyncio polling loop）：

```python
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.leave_quota_expiry_enabled)


async def run_leave_quota_expiry_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢補休到期 + 特休週年 cutover。

    照 graduation_scheduler / recruitment_term_advance_scheduler pattern：
    - session_scope() 寫入 → log
    - try_scheduler_lock 防多 instance 重複跑
    - last_run_date 記憶體 guard 避免同一天多次跑（log spam）
    """
    from models.base import session_scope
    from utils.advisory_lock import try_scheduler_lock
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries

    check_interval = get_settings().scheduler.leave_quota_expiry_check_interval
    logger.info("leave quota expiry scheduler 啟動 (interval=%ss)", check_interval)

    last_run_date: date | None = None

    while not stop_event.is_set():
        try:
            today = _today_taipei()
            if last_run_date != today:
                with session_scope() as session:
                    with try_scheduler_lock(
                        session,
                        scheduler_name="leave_quota_expiry",
                    ) as acquired:
                        if acquired:
                            comp_summary = expire_comp_leave_grants(today, session)
                            cutover_summary = cutover_annual_leave_anniversaries(today, session)
                            logger.info(
                                "leave quota expiry tick: %s | %s",
                                comp_summary,
                                cutover_summary,
                            )
                            last_run_date = today
        except Exception:
            logger.exception("leave quota expiry scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
```

`main.py` lifespan 加 register（沿用 recruitment_term_advance 模式）：

```python
# main.py 約 line 324 附近，recruitment_term_advance 之後
from services import leave_quota_expiry_scheduler as _lqe_sched
leave_quota_expiry_stop_event = asyncio.Event()
if _lqe_sched.scheduler_enabled():
    leave_quota_expiry_task = asyncio.create_task(
        _lqe_sched.run_leave_quota_expiry_scheduler(leave_quota_expiry_stop_event)
    )
    logger.info("leave quota expiry scheduler 已啟用")
```

`config/scheduler.py` 加兩個欄位：

```python
leave_quota_expiry_enabled: bool = False  # 預設關閉，HR 簽勞資協議後手動 enable
leave_quota_expiry_check_interval: int = 3600  # 1 小時輪詢一次（last_run_date guard 確保每日只跑一次）
```

### 4.2 `expire_comp_leave_grants(today, session)`

新檔 `services/leave_quota_expiry/comp_leave_expiry.py`：

```python
def expire_comp_leave_grants(today: date, session: Session) -> dict:
    """撈到期 grant 結算寫 SalaryRecord.unused_leave_payout + log。

    跳過已離職員工（由 offboarding path 處理）。
    """
    expired_grants = (
        session.query(OvertimeCompLeaveGrant)
        .join(Employee)
        .filter(
            OvertimeCompLeaveGrant.status == 'active',
            OvertimeCompLeaveGrant.expires_at <= today,
            Employee.is_active.is_(True),
        )
        .all()
    )

    grants_by_emp: dict[int, list[OvertimeCompLeaveGrant]] = {}
    for g in expired_grants:
        grants_by_emp.setdefault(g.employee_id, []).append(g)

    total_paid = Decimal("0")
    paid_emp_count = 0

    for emp_id, grants in grants_by_emp.items():
        try:
            with session.begin_nested():
                unexpired_hours = sum(g.granted_hours - g.consumed_hours for g in grants)
                if unexpired_hours <= 0:
                    for g in grants:
                        g.status = 'expired'
                        g.expired_at = datetime.now()
                    continue

                emp = session.get(Employee, emp_id)
                hourly_wage = _resolve_hourly_wage(emp, today)
                amount = round_half_up(
                    calculate_unused_leave_compensation(unexpired_hours, hourly_wage)
                )

                period_year, period_month = _next_month(today)
                log = UnusedLeavePayoutLog(
                    employee_id=emp_id,
                    source_type='comp_grant_expiry',
                    source_ref_id=None,  # 反查靠 grant.payout_log_id
                    hours=unexpired_hours,
                    hourly_wage=Decimal(str(hourly_wage)),
                    amount=amount,
                    wage_basis_date=today,
                    salary_period_year=period_year,
                    salary_period_month=period_month,
                    meta={
                        'expired_grant_ids': [g.id for g in grants],
                        'hours_breakdown': [
                            {
                                'grant_id': g.id,
                                'overtime_date': g.granted_at.isoformat(),
                                'unexpired_hours': g.granted_hours - g.consumed_hours,
                            }
                            for g in grants
                        ],
                    },
                )
                session.add(log)
                session.flush()  # 取 log.id

                # 反向掛 SalaryRecord：layer 1 寫入
                # - 該月 SalaryRecord 不存在 → log.salary_record_id=NULL 由 layer 2 在月結時拉
                # - 該月 SalaryRecord 存在且未 finalize → 直寫 + 綁定
                # - 該月 SalaryRecord 已 finalize → log.salary_record_id=NULL 由下個 open month layer 2 接手（或 HR 手動處理）
                salary_record = _find_or_none_salary_record(
                    emp_id, period_year, period_month, session
                )
                if salary_record is not None and not salary_record.is_finalized:
                    salary_record.unused_leave_payout = (salary_record.unused_leave_payout or 0) + amount
                    log.salary_record_id = salary_record.id

                for g in grants:
                    g.status = 'expired'
                    g.expired_at = datetime.now()
                    g.payout_salary_record_id = salary_record.id if salary_record else None
                    g.payout_log_id = log.id

                total_paid += amount
                paid_emp_count += 1
        except Exception:
            logger.exception("expire_comp_leave failed for emp=%d", emp_id)

    return {
        'paid_employees': paid_emp_count,
        'total_amount': float(total_paid),
        'expired_grant_count': len(expired_grants),
    }
```

### 4.3 `cutover_annual_leave_anniversaries(today, session)`

新檔 `services/leave_quota_expiry/annual_cutover.py`：

```python
def cutover_annual_leave_anniversaries(today: date, session: Session) -> dict:
    """跑「今日滿週年」員工：結算上一週年未休 + 建新一週年 row。

    2/29 fallback：閏年到期日落非閏年自動順延至 2/28（_is_anniversary_today_sql 處理）。
    """
    candidates = session.query(Employee).filter(
        Employee.is_active.is_(True),
        _is_anniversary_today_sql(Employee.hire_date, today),
        Employee.hire_date <= today - timedelta(days=180),  # 不足半年無特休
    ).all()

    paid_count = 0
    cold_start_count = 0
    total_paid = Decimal("0")

    for emp in candidates:
        try:
            with session.begin_nested():
                current = session.query(LeaveQuota).filter(
                    LeaveQuota.employee_id == emp.id,
                    LeaveQuota.leave_type == 'annual',
                    LeaveQuota.period_start.isnot(None),
                    LeaveQuota.period_start <= today,
                    LeaveQuota.period_end > today,
                ).first()

                if current is not None:
                    used = _approved_annual_used_in_period(
                        emp.id, current.period_start, today, session
                    )
                    unused = max(0.0, current.total_hours - used)
                    if unused > 0:
                        hourly_wage = _resolve_hourly_wage(emp, today)
                        amount = round_half_up(
                            calculate_unused_leave_compensation(unused, hourly_wage)
                        )
                        period_year, period_month = _next_month(today)
                        log = UnusedLeavePayoutLog(
                            employee_id=emp.id,
                            source_type='annual_anniversary',
                            source_ref_id=current.id,  # 同 quota 二次結算靠 partial unique idx 擋
                            hours=unused,
                            hourly_wage=Decimal(str(hourly_wage)),
                            amount=amount,
                            wage_basis_date=today,
                            salary_period_year=period_year,
                            salary_period_month=period_month,
                            meta={
                                'period_start': current.period_start.isoformat(),
                                'period_end': current.period_end.isoformat(),
                                'entitled_hours': current.total_hours,
                                'used_hours': used,
                            },
                        )
                        session.add(log)
                        session.flush()

                        salary_record = _find_or_none_salary_record(
                            emp.id, period_year, period_month, session
                        )
                        if salary_record is not None and not salary_record.is_finalized:
                            salary_record.unused_leave_payout = (
                                (salary_record.unused_leave_payout or 0) + amount
                            )
                            log.salary_record_id = salary_record.id

                        paid_count += 1
                        total_paid += amount
                else:
                    cold_start_count += 1

                # 建新一週年 row（cold-start 直接走這裡）
                new_period_end = _add_one_year_with_feb29_handling(today)
                hours = _calc_annual_leave_hours(
                    emp.hire_date, year=today.year, reference_date=today
                )
                session.add(LeaveQuota(
                    employee_id=emp.id,
                    year=today.year,
                    school_year=None,
                    period_start=today,
                    period_end=new_period_end,
                    leave_type='annual',
                    total_hours=hours,
                    note=f"週年制配額（hire_date 基準 {emp.hire_date.isoformat()}）",
                ))
        except IntegrityError:
            session.rollback()  # 同 period_start row 已存在或同 quota 已結算
        except Exception:
            logger.exception("cutover_annual failed for emp=%d", emp.id)

    return {
        'paid_employees': paid_count,
        'cold_start_employees': cold_start_count,
        'total_amount': float(total_paid),
        'total_anniversaries': len(candidates),
    }
```

### 4.4 Helper Functions

```python
def _resolve_hourly_wage(emp: Employee, ref_date: date) -> float:
    if emp.employee_type == 'hourly':
        return float(emp.hourly_rate or 0)
    # 月薪：取 ref_date 當下生效薪資 / 30 / 8
    monthly = _resolve_monthly_base_at(emp.id, ref_date)
    return monthly / 30 / 8

def _next_month(today: date) -> tuple[int, int]:
    """跨年 12→1 wrap 自動處理"""
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1

def _add_one_year_with_feb29_handling(d: date) -> date:
    """2/29 + 1y 落非閏年自動順延 2/28"""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 2/29 → 非閏年
        return d.replace(year=d.year + 1, day=28)

def _is_anniversary_today_sql(hire_date_col, today: date):
    """SQL 表達式：員工 hire_date 月日 == today 月日，含 2/29 fallback"""
    # PostgreSQL: EXTRACT(MONTH FROM hire_date) = today.month AND EXTRACT(DAY FROM hire_date) = today.day
    # 2/29 員工在非閏年的 2/28 也算 anniversary
    if today.month == 2 and today.day == 28 and not _is_leap_year(today.year):
        return or_(
            and_(extract('month', hire_date_col) == 2, extract('day', hire_date_col) == 28),
            and_(extract('month', hire_date_col) == 2, extract('day', hire_date_col) == 29),
        )
    return and_(
        extract('month', hire_date_col) == today.month,
        extract('day', hire_date_col) == today.day,
    )
```

### 4.5 Idempotency

- **補休**：grant 一旦 `status='expired'` 不會再被選；scheduler 同日重跑同筆 grant 不重複結算
- **特休**：partial unique index `uq_leave_quotas_emp_period_annual` 擋同員工同 period_start row；scheduler 同日重跑見既有 row → IntegrityError → skip
- **Failure recovery**：savepoint per employee，單筆失敗 log + continue 不影響整批；scheduler 整體失敗 next day 自動 catch up（撈 `expires_at <= today` 不限定當日）

## 5. API、Salary Engine、UI Integration

### 5.1 後端 API 新增

**新檔 `api/leave_quota_expiry.py`**：

| Method | Path | 權限 | 用途 |
|---|---|---|---|
| GET | `/leave-quota-expiry/upcoming` | `LEAVES_READ` | 列即將到期補休 grant（query `days=30`），HR 提前通知 |
| GET | `/leave-quota-expiry/anniversaries` | `LEAVES_READ` | 列下 30 天滿週年員工，HR 提前準備 |
| GET | `/leave-quota-expiry/payout-history` | `SALARY_READ` | 列 UNUSED_*_PAYOUT 結算歷史（含 meta 證據鏈展開） |
| POST | `/leave-quota-expiry/run-now` | `SALARY_WRITE` | 手動 trigger scheduler handle（idempotent 重跑安全） |

### 5.2 既有 API 修改

**`api/overtimes.py`**：
- `_grant_comp_leave_quota(session, ot, result)`：除既有 LeaveQuota upsert，**新增**建 `OvertimeCompLeaveGrant` row
- `_revoke_comp_leave_grant(session, ot)`：除既有邏輯，**新增**將對應 grant `status='revoked'`（不刪除留 audit）
- 既有 `LeaveQuota.compensatory` upsert 邏輯**保留但語義降級為快取**，實際結餘以 grant ledger SUM 為準

**`api/leaves.py`**（補休假單核准/駁回）：
- 補休假單核准時 FIFO 從最早 `expires_at` 的 active grant 扣 `consumed_hours`
- 補休假單駁回/撤銷時退回對應 grant 的 `consumed_hours`
- 新 helper `_compensatory_balance(emp_id, session)` 統一查詢入口：`SUM(granted_hours - consumed_hours) WHERE status='active' AND employee_id=:eid`

**`services/term_subscribers/leave_quota_cutover.py`**：
- 移除 `annual` 從 `QUOTA_LEAVE_TYPES` 處理迴圈（特休改週年制 scheduler 負責）
- 其他 5 種法定假別 + compensatory carry-over 邏輯**保留**
- compensatory carry-over 改為「累加當下 active grants sum」而非舊聚合 row 數值

### 5.3 Salary Engine 整合

寫入 path 改為兩層：

**Layer 1：Scheduler 直寫**（同月薪資已 calculate 過）
- `expire_comp_leave_grants` / `cutover_annual_leave_anniversaries` 跑當下，若 `SalaryRecord(emp_id, period_year, period_month)` 已存在，直接 `unused_leave_payout += amount` 並把 log.salary_record_id 反向綁定
- 此 path 不會撞 finalize_guard，因 scheduler 寫的是「**未來月**」（today.month + 1）；除非該 SalaryRecord 已 finalize（極少見），否則正常 += 累加

**Layer 2：Salary Engine `calculate(year, month, emp_id)` 撈未綁定 log**
- `services/salary/engine.py` 月結時加 step：撈該員工該月 `unused_leave_payout_log WHERE salary_record_id IS NULL AND salary_period_year=year AND salary_period_month=month` 加總後寫入 + 反向綁定 log

```python
# services/salary/engine.py 月結 calculate 內加 step
pending_logs = session.query(UnusedLeavePayoutLog).filter(
    UnusedLeavePayoutLog.employee_id == emp_id,
    UnusedLeavePayoutLog.salary_period_year == year,
    UnusedLeavePayoutLog.salary_period_month == month,
    UnusedLeavePayoutLog.salary_record_id.is_(None),
).all()

if pending_logs:
    additional = sum(log.amount for log in pending_logs)
    salary_record.unused_leave_payout = (salary_record.unused_leave_payout or 0) + additional
    session.flush()
    for log in pending_logs:
        log.salary_record_id = salary_record.id
```

**重 calculate idempotency 策略**：
- 重 calc 不會重複加 — `pending_logs` 只撈 `salary_record_id IS NULL` 的 log，已綁定的 log 不在範圍
- 若 HR 手動清掉 SalaryRecord 重 calc：log.salary_record_id 因 `ON DELETE SET NULL` 自動歸 null → 重 calc 時會被重撈 → 正確還原金額
- 若 HR 手動修 SalaryRecord.unused_leave_payout 數字：log 仍綁定，不會被覆蓋（HR 手動值優先）

**三來源共存**：
- 離職 path（offboarding step）：直接寫 SalaryRecord.unused_leave_payout（既有不動），可同時寫一筆 `source_type='offboarding'` log 留證據鏈（spec §3.3 已預留 schema，本 plan 暫不改 offboarding code，留 Phase 2）
- 特休週年 path（scheduler）：寫 `source_type='annual_anniversary'` log + 直寫/月結拉
- 補休到期 path（scheduler）：寫 `source_type='comp_grant_expiry'` log + 直寫/月結拉

### 5.4 前端整合

**新元件 `LeaveQuotaExpiryTab.vue`**（放 `LeavesView` 新 sub-tab）：
- 三 sub-section：即將到期補休清單 / 即將滿週年員工 / 折算歷史
- 操作：匯出 CSV（給員工通知用）、跳轉到對應 SpecialBonusItem 編輯

**LeavesView 補休餘額顯示修改**：
- 員工自助頁顯示「補休結餘 X 小時（最早到期 YYYY-MM-DD，Z 小時）」
- 點開 detail drawer 列每筆 grant 的 granted_at / expires_at / 剩餘小時

**SalaryView 薪資單顯示**：
- `unused_leave_payout` 既有欄位，UI 加 tooltip 展開 `special_bonus_items` 證據鏈

### 5.5 Permission

無新 Permission enum，沿用既有：`LEAVES_READ` / `SALARY_READ` / `SALARY_WRITE` / `LEAVES_WRITE`。

## 6. Rollout

```
T-14 天：HR 啟動勞資協商
       準備「補休 1 年到期 + 特休改週年制」協議書，工會/勞資會議簽署
       （此步驟 system 不參與，純 ops，但 spec 必須明寫前提依賴）

T-7 天：上線前夕
       HR 跑 GET /leave-quota-expiry/upcoming?days=999 模擬 → 出待結算總金額
       核對預算（避免上線當月薪資突增驚動財務）

T0：Migration compexpr01 上線
     - 建 overtime_comp_leave_grants 表 + LeaveQuota 加欄 + special_bonus_items enum 擴
     - Backfill：既有 OT → grant rows (expires_at = T0 + LEAVE_BACKFILL_GRACE_MONTHS 月)
     - Backfill：既有 annual LeaveQuota → period_start = hire_date 最近週年 / period_end = +1y
     - 既有 unused 餘額不立即結算

T0：scheduler 啟動但 cold period
     - daily 02:30 跑但 _expire 部分過濾 expires_at >= T0+3個月 才結算
     - cutover 部分立即生效（員工到 hire_date 那天即 cutover）

T+1～14 天：HR 通知員工
     - 透過 LeaveQuotaExpiryTab 匯出「未休補休 + 寬限期到期日」CSV
     - 各班會公告排假鼓勵消化

T+90 天：寬限期結束
     - scheduler 第一波結算大批 expired comp grants
     - SalaryRecord.unused_leave_payout 首次出現補休折算金額
     - HR 在 SpecialBonusItem 頁面審核
```

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| 勞資協商未簽署前 system 已上線 → 違法變更勞動條件 | T-14 天前置 ops + spec §2 前提依賴明寫；scheduler 預設 `leave_quota_expiry_enabled=False`，HR 確認後手動 set True |
| 大批員工同月滿週年（系統建立時集中入職）→ 薪資突增 | T-7 模擬報表給 HR 預警；可選 phased rollout（先試一個月再開全部） |
| Scheduler 連續多日失敗 → grants 過期但未結算 | savepoint per employee + 重啟自動 catch up（撈 `expires_at <= today` 不限定當日） |
| 補休 FIFO 扣抵與既有 LeaveQuota 聚合 row 不一致 | grant ledger 為 source of truth，`_compensatory_balance` 統一 helper；既有 LeaveQuota.compensatory.total_hours 降級為派生快取 |
| 2/29 員工 cutover 跨閏年 → 一年 13 / 11 個月 | 統一 fallback 落 2/28（spec §4.4 明寫），員工合計每 4 年仍精確 |
| 既有 carry-over 過的補休（多年累積）寬限 3 個月過短 → 員工反彈 | 寬限期 3 個月為 spec 默認，migration script 接 ENV `LEAVE_BACKFILL_GRACE_MONTHS`（default 3）讓 HR 可調 |
| Scheduler 跑當下員工剛離職 → 重複折算（離職 path 也算一遍） | 兩 path 都加 `WHERE Employee.is_active=True` filter；離職 path 先 set `is_active=False` 後 trigger 結算 |
| Salary record 已 finalize 後 scheduler 直寫 → finalize_guard 衝突 | scheduler 寫的 salary_period_year/month 為「未來月份」（today 月+1），未來月薪資尚未 calculate 自然未 finalize；若極端撞牆則 fallback 走 layer 2（log 留 salary_record_id=NULL，下個月 calculate 自動拉） |
| LeaveQuota 既有查詢 path（22+ caller）依賴聚合 row → 改 grant ledger 後型別/語意變化 | 既有 row 不刪、`total_hours` 保留語意為「historical aggregate snapshot」；新查詢入口 `_compensatory_balance(emp_id)` 統一走 grant ledger，22+ caller 漸進切換 |
| 多 instance 部署 scheduler 重複跑 → 同 grant 多次結算 | `try_scheduler_lock(scheduler_name='leave_quota_expiry')` advisory lock（沿用 activity_waitlist_sweep pattern） |

## 8. Testing Strategy

| 層 | 測試 |
|---|---|
| Unit | `_resolve_hourly_wage` 月/時薪兩 path / FIFO 扣抵正確性 / 2/29 fallback / 跨年 `_next_month` wrap / `_add_one_year_with_feb29_handling` |
| Service | scheduler `expire_comp_leave_grants` 多員工多 grant 場景 / `cutover_annual_leave_anniversaries` cold-start + 第二輪 / savepoint 單筆失敗其他繼續 / IntegrityError skip 路徑 / `last_run_date` 同日 guard |
| Integration | grant ledger 與 `_compensatory_balance` 聚合一致性 / 補休假單核准→扣抵 grant→駁回→退回 round trip / scheduler idempotent 同日重跑無副作用 / OT revoke → grant 'revoked' / OT delete CASCADE grant |
| Migration | `mergeheads04` upgrade/downgrade / `compexpr01` upgrade/downgrade 對稱 / 既有 OT backfill 為 grant rows 行數一致 / 既有 annual quota period_start/end 正確 / ENV `LEAVE_BACKFILL_GRACE_MONTHS` 覆蓋驗證 |
| Payout Log | scheduler 寫入 layer 1（SalaryRecord 已存在 → 直寫 + 綁定）/ scheduler 寫入 layer 2（SalaryRecord 不存在 → log.salary_record_id NULL）/ salary engine `calculate` 撈 pending logs 加總正確 / 重 calc 時不重複加（已綁定 log 跳過） / `ON DELETE SET NULL` 解除綁定後重 calc 還原金額 / annual_anniversary partial unique idx 擋二次結算 |
| E2E (Playwright) | HR 在 LeaveQuotaExpiryTab 看到即將到期 → scheduler 跑 → SalaryRecord.unused_leave_payout 含該金額 |

## 9. Open Questions

不阻擋 spec 通過，留 implementation 時釐清：

1. LeavesView 補休結餘顯示「最早到期日」是否需推播提醒（LINE Bot）— Phase 2 範圍
2. HR 是否要能手動 extend 單筆 grant 的 expires_at（特殊情況 e.g. 員工長期請病假無法消化）— defer，目前用 HR 手動改 SalaryRecord.unused_leave_payout 數字 + 撤銷對應 log 代替
3. 既有 `LeaveQuota.compensatory` 聚合 row 是否最終可刪除（待 22+ caller 全切換到 `_compensatory_balance` 後再評估） — defer
4. 跨月薪資（finalize 已關 + 已 close）的 scheduler 寫入流程：scheduler 確實只寫未來月份，但極端情況（員工到職日 = 月底）`_next_month` 可能撞 close — 加 guard 寫不進去時 log warn 並依賴 layer 2 自動接手
5. 離職 path 是否也補一筆 `source_type='offboarding'` log 留證據鏈 — defer Phase 2，本 spec 不動 offboarding code
