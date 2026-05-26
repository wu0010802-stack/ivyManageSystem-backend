---
title: 補休到期與特休週年制 — Leave Quota Lifecycle Design
date: 2026-05-26
status: draft
owner: yilunwu
related:
  - api/leaves_quota.py
  - services/term_subscribers/leave_quota_cutover.py
  - services/salary/unused_leave_pay.py
  - models/leave.py
  - models/overtime.py
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
2. **`special_bonus_items` 表已存在**且支援自訂 type enum 擴充（沿用 CLAUDE.md #10 考核年終獎金模式）。
3. **`SalaryRecord.unused_leave_payout` 欄已存在**（offb0001 migration 加，由離職 path 寫入）。本 spec 新 path 共用此欄，靠 `special_bonus_items.meta` 區分來源。
4. **Employee 有合理 `hire_date`**（NOT NULL，現有資料已驗證）。
5. **Scheduler infrastructure 已就位**：apscheduler 已在 `main.py` 註冊（`recruitment_term_advance_scheduler` 已用此 pattern）。
6. **時薪計算公式採通說**：月薪 ÷ 30 ÷ 8（勞動部 §38 解釋令採此基準），非 21.75 工作日制。

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
    payout_special_bonus_item_id INTEGER NULL
        REFERENCES special_bonus_items(id) ON DELETE SET NULL,
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
- `status='expired'` 時 `expired_at` NOT NULL 且 `payout_special_bonus_item_id` NOT NULL
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

### 3.3 `special_bonus_items` enum 擴充

新增兩個 type：
- `UNUSED_ANNUAL_LEAVE_PAYOUT` — 特休週年 cutover 折算
- `UNUSED_COMP_LEAVE_PAYOUT` — 補休 +1 年到期折算

`meta` (JSONB) 結構：

```json
// UNUSED_COMP_LEAVE_PAYOUT
{
  "expired_grant_ids": [123, 124, 125],
  "hours_breakdown": [
    {"grant_id": 123, "overtime_date": "2025-04-01", "unexpired_hours": 4.0}
  ],
  "hourly_wage_basis": 200.0,
  "wage_basis_date": "2026-04-01",
  "total_unexpired_hours": 4.0
}

// UNUSED_ANNUAL_LEAVE_PAYOUT
{
  "cutover_quota_id": 456,
  "period_start": "2025-08-15",
  "period_end": "2026-08-15",
  "entitled_hours": 80.0,
  "used_hours": 64.0,
  "unused_hours": 16.0,
  "hourly_wage_basis": 220.0,
  "wage_basis_date": "2026-08-15"
}
```

### 3.4 Migration `compexpr01`

依序執行：
1. CREATE TABLE `overtime_comp_leave_grants`
2. ALTER TABLE `leave_quotas` 加 `period_start` / `period_end` 兩欄 + partial unique index
3. 擴 `special_bonus_items` type enum 增兩值
4. Backfill：既有 OT (`use_comp_leave=True AND comp_leave_granted=True`) → grant rows
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

```python
# 沿用 services/recruitment_term_advance_scheduler.py pattern

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

def register(scheduler: AsyncIOScheduler):
    scheduler.add_job(
        handle,
        CronTrigger(hour=2, minute=30),  # 避開 02:00 student_lifecycle GC
        id="leave_quota_expiry",
        replace_existing=True,
    )

def handle():
    with SessionLocal() as session:
        with session.begin():
            today = date.today()
            _expire_comp_leave_grants(today, session)
            _cutover_annual_leave_anniversaries(today, session)
```

### 4.2 `_expire_comp_leave_grants(today, session)`

```python
def _expire_comp_leave_grants(today: date, session: Session) -> None:
    expired_grants = (
        session.query(OvertimeCompLeaveGrant)
        .join(Employee)
        .filter(
            OvertimeCompLeaveGrant.status == 'active',
            OvertimeCompLeaveGrant.expires_at <= today,
            Employee.is_active.is_(True),  # 跳過已離職（由 offboarding path 處理）
        )
        .all()
    )

    grants_by_emp = group_by(expired_grants, key=lambda g: g.employee_id)

    for emp_id, grants in grants_by_emp.items():
        try:
            with session.begin_nested():  # savepoint per employee
                unexpired_hours = sum(g.granted_hours - g.consumed_hours for g in grants)
                if unexpired_hours <= 0:
                    # 全用完仍要 mark expired 避免下次重撈
                    for g in grants:
                        g.status = 'expired'
                        g.expired_at = datetime.now()
                    continue

                emp = session.get(Employee, emp_id)
                hourly_wage = _resolve_hourly_wage(emp, today)
                payout = calculate_unused_leave_compensation(unexpired_hours, hourly_wage)
                payout = round_half_up(payout)  # 對齊 [[project-money-rounding-half-up-rollout]]

                period_year, period_month = _next_month(today)
                sbi = SpecialBonusItem(
                    employee_id=emp_id,
                    type='UNUSED_COMP_LEAVE_PAYOUT',
                    amount=payout,
                    period_year=period_year,
                    period_month=period_month,
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
                        'hourly_wage_basis': hourly_wage,
                        'wage_basis_date': today.isoformat(),
                        'total_unexpired_hours': unexpired_hours,
                    },
                )
                session.add(sbi)
                session.flush()
                for g in grants:
                    g.status = 'expired'
                    g.expired_at = datetime.now()
                    g.payout_special_bonus_item_id = sbi.id
        except Exception:
            logger.exception("expire_comp_leave failed for emp=%d", emp_id)
            # savepoint 已回滾，其他員工繼續
```

### 4.3 `_cutover_annual_leave_anniversaries(today, session)`

```python
def _cutover_annual_leave_anniversaries(today: date, session: Session) -> None:
    # 2/29 fallback：閏年到期日落非閏年自動順延至 2/28
    candidates = session.query(Employee).filter(
        Employee.is_active.is_(True),
        _is_anniversary_today_sql(Employee.hire_date, today),
        Employee.hire_date <= today - timedelta(days=180),  # 不足半年無特休
    ).all()

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
                    # 結算上一週年未休
                    used = _approved_annual_used_in_period(
                        emp.id, current.period_start, today, session
                    )
                    unused = max(0.0, current.total_hours - used)
                    if unused > 0:
                        hourly_wage = _resolve_hourly_wage(emp, today)
                        payout = round_half_up(
                            calculate_unused_leave_compensation(unused, hourly_wage)
                        )
                        period_year, period_month = _next_month(today)
                        session.add(SpecialBonusItem(
                            employee_id=emp.id,
                            type='UNUSED_ANNUAL_LEAVE_PAYOUT',
                            amount=payout,
                            period_year=period_year,
                            period_month=period_month,
                            meta={
                                'cutover_quota_id': current.id,
                                'period_start': current.period_start.isoformat(),
                                'period_end': current.period_end.isoformat(),
                                'entitled_hours': current.total_hours,
                                'used_hours': used,
                                'unused_hours': unused,
                                'hourly_wage_basis': hourly_wage,
                                'wage_basis_date': today.isoformat(),
                            },
                        ))

                # 建新一週年 row（cold-start 直接走這裡）
                new_period_end = _add_one_year_with_feb29_handling(today)
                hours = _calc_annual_leave_hours(
                    emp.hire_date, year=today.year, reference_date=today
                )
                session.add(LeaveQuota(
                    employee_id=emp.id,
                    year=today.year,
                    school_year=None,            # 週年制不用 school_year
                    period_start=today,
                    period_end=new_period_end,
                    leave_type='annual',
                    total_hours=hours,
                    note=f"週年制配額（hire_date 基準 {emp.hire_date.isoformat()}）",
                ))
        except IntegrityError:
            # 已存在同 period_start row（scheduler 同日重跑）→ skip
            session.rollback()
        except Exception:
            logger.exception("cutover_annual failed for emp=%d", emp.id)
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

`services/salary/engine.py` 月結 `calculate(year, month, emp_id)` 加 step：

```python
# 重 calculate idempotency：先抓出本月既有來自三個 path 的 unused_leave_payout
# 1. 離職 path 直接寫 SalaryRecord.unused_leave_payout（offboarding step）
# 2. 特休週年 path → SpecialBonusItem(type=UNUSED_ANNUAL_LEAVE_PAYOUT)
# 3. 補休到期 path → SpecialBonusItem(type=UNUSED_COMP_LEAVE_PAYOUT)
# 重 calc 時離職 path 部分由 offboarding snapshot 保留，scheduler path 由 query SBI 重算

offboarding_amount = _get_offboarding_unused_leave_payout(emp_id, year, month, session)

scheduler_payouts = session.query(SpecialBonusItem).filter(
    SpecialBonusItem.employee_id == emp_id,
    SpecialBonusItem.type.in_([
        'UNUSED_ANNUAL_LEAVE_PAYOUT',
        'UNUSED_COMP_LEAVE_PAYOUT',
    ]),
    SpecialBonusItem.period_year == year,
    SpecialBonusItem.period_month == month,
).all()

salary_record.unused_leave_payout = (
    offboarding_amount
    + sum(sbi.amount for sbi in scheduler_payouts)
)
```

`unused_leave_payout` 欄三來源共存：離職 path / 特休週年 path / 補休到期 path。**寫入策略為「全量覆蓋」非累加**，避免重 calculate 時重複加。離職 path 與 scheduler path 互斥（離職員工 `is_active=False` 不會被 scheduler 撈），實務上同月最多兩個 path 同時觸發（特休週年 + 補休到期）。HR 改 SpecialBonusItem amount 後須重 calculate 才同步（沿用考核年終 pattern）。

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
| 勞資協商未簽署前 system 已上線 → 違法變更勞動條件 | T-14 天前置 ops + spec §2 前提依賴明寫；migration 不會偷渡執行，需 HR 確認後手動跑 |
| 大批員工同月滿週年（系統建立時集中入職）→ 薪資突增 | T-7 模擬報表給 HR 預警；可選 phased rollout（先試一個月再開全部） |
| Scheduler 連續多日失敗 → grants 過期但未結算 | savepoint per employee + 重啟自動 catch up（撈 `expires_at <= today` 不限定當日） |
| 補休 FIFO 扣抵與既有 LeaveQuota 聚合 row 不一致 | grant ledger 為 source of truth，`_compensatory_balance` 統一 helper；既有 LeaveQuota.compensatory.total_hours 降級為派生快取 |
| 2/29 員工 cutover 跨閏年 → 一年 13 / 11 個月 | 統一 fallback 落 2/28（spec §4.4 明寫），員工合計每 4 年仍精確 |
| 既有 carry-over 過的補休（多年累積）寬限 3 個月過短 → 員工反彈 | 寬限期 3 個月為 spec 默認，migration script 接 ENV `LEAVE_BACKFILL_GRACE_MONTHS`（default 3）讓 HR 可調 |
| Scheduler 跑當下員工剛離職 → 重複折算（離職 path 也算一遍） | 兩 path 都加 `WHERE Employee.is_active=True` filter；離職 path 先 set `is_active=False` 後 trigger 結算 |
| Salary record 已 finalize 後 scheduler 寫 special_bonus_items → finalize_guard 衝突 | scheduler 寫的 period_year/month 為「未來月份」（today 月+1），未來月薪資尚未 calculate 自然未 finalize |
| LeaveQuota 既有查詢 path（22+ caller）依賴聚合 row → 改 grant ledger 後型別/語意變化 | 既有 row 不刪、`total_hours` 保留語意為「historical aggregate snapshot」；新查詢入口 `_compensatory_balance(emp_id)` 統一走 grant ledger，22+ caller 漸進切換 |

## 8. Testing Strategy

| 層 | 測試 |
|---|---|
| Unit | `_resolve_hourly_wage` 月/時薪兩 path / FIFO 扣抵正確性 / 2/29 fallback / 跨年 `_next_month` wrap / `_add_one_year_with_feb29_handling` |
| Service | scheduler `_expire_comp_leave_grants` 多員工多 grant 場景 / `_cutover_annual_leave_anniversaries` cold-start + 第二輪 / savepoint 單筆失敗其他繼續 / IntegrityError skip 路徑 |
| Integration | grant ledger 與 `_compensatory_balance` 聚合一致性 / 補休假單核准→扣抵 grant→駁回→退回 round trip / scheduler idempotent 同日重跑無副作用 / OT revoke → grant 'revoked' / OT delete CASCADE grant |
| Migration | `compexpr01` upgrade/downgrade 對稱 / 既有 OT backfill 為 grant rows 行數一致 / 既有 annual quota period_start/end 正確 / ENV `LEAVE_BACKFILL_GRACE_MONTHS` 覆蓋驗證 |
| Salary Engine | special_bonus_items `UNUSED_*_PAYOUT` 加總正確進 `SalaryRecord.unused_leave_payout` / 三來源共存（離職 + 週年 + 補休到期）總和正確 / 重 calculate 同步 |
| E2E (Playwright) | HR 在 LeaveQuotaExpiryTab 看到即將到期 → 月結時 SpecialBonusItem 出現 → SalaryRecord.unused_leave_payout 含該金額 |

## 9. Open Questions

不阻擋 spec 通過，留 implementation 時釐清：

1. LeavesView 補休結餘顯示「最早到期日」是否需推播提醒（LINE Bot）— Phase 2 範圍
2. HR 是否要能手動 extend 單筆 grant 的 expires_at（特殊情況 e.g. 員工長期請病假無法消化）— defer，目前用 SpecialBonusItem 手動調金額代替
3. SpecialBonusItem 的 `period_year` / `period_month` 欄位實際 schema 確認（spec 假設獨立兩欄，實作時需驗證）
4. 既有 `LeaveQuota.compensatory` 聚合 row 是否最終可刪除（待 22+ caller 全切換到 `_compensatory_balance` 後再評估） — defer
5. 跨月薪資（finalize 已關 + 已 close）的 SpecialBonusItem 寫入流程：scheduler 確實只寫未來月份，但極端情況（員工到職日 = 月底）`_next_month` 可能撞 close — 加 guard 寫不進去時 log warn 並延到下個 open month
