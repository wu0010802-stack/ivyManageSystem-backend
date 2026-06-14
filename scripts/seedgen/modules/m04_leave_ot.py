"""m04_leave_ot:員工請假/加班/補打卡測試資料。

職責:
- `leave_records`:逐月為部分員工建請假單。closed 月一律 approved;
  in_progress(當月)留 pending。少量為 `compensatory` 補休,綁回來源加班記錄。
- `overtime_records`:逐月為部分員工建加班(週末)。closed 月 approved;
  當月 pending。部分標記 `use_comp_leave`(換補休),其已核補休衍生對應請假單。
- `punch_correction_requests`:少量補打卡申請(closed 月 approved、當月 pending)。

時間規則:只生到 closed + in_progress 月份(上限 ctx.config.today),不生 future。
與 m03 考勤對齊:請假/加班一律落在工作日/週末語意位置(不強制改寫考勤,僅避免
在週末灌一般請假),金額用 round_half_up(禁 builtin round)。

只透過 ctx registry 取依賴(ctx.employees / ctx.rng / ctx.config /
closed_months / current_month),不重查已建實體。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from utils.rounding import round_half_up

from ..calendar import workdays
from ..context import SeedContext

# 可扣薪假別(其餘視為不扣薪/法定)。leave_type 欄位為 String(20),
# 值域開放(model 之 LeaveType enum 僅列舉常見值,DB 存任意字串;
# 'compensatory' 補休即不在 enum 內但全系統使用)。
_DEDUCTIBLE_TYPES = {"personal", "sick"}

# 一般請假候選(假別, 原因, 時數)。涵蓋扣薪/不扣薪、整日/半日。
_LEAVE_POOL: list[tuple[str, str, float]] = [
    ("annual", "家庭旅遊", 8.0),
    ("annual", "返鄉探親", 8.0),
    ("sick", "感冒就醫", 8.0),
    ("sick", "腸胃不適", 4.0),
    ("personal", "處理私務", 4.0),
    ("personal", "銀行辦事", 4.0),
    ("menstrual", "生理假", 8.0),
]

# 補打卡原因池。
_PUNCH_REASONS = [
    "忘記打卡,有同事可作證",
    "系統當機未能打卡",
    "外出洽公返回未補打",
]


def _hourly_rate(emp) -> float:
    """以月薪推估時薪(月薪 / 30 / 8),供加班費估算。"""
    base = float(getattr(emp, "base_salary", None) or 30000)
    return base / 30.0 / 8.0


def _annual_hours_for_tenure(hire_date, ref_date) -> float:
    """勞基法§38 特休時數(年資 → 天數 × 8h)。hire_date 缺則給 56h(7 天)。"""
    if not hire_date:
        return 56.0
    years = (ref_date - hire_date).days / 365.25
    if years < 0.5:
        return 0.0
    if years < 1:
        return 24.0  # 3 天
    if years < 2:
        return 56.0  # 7 天
    if years < 3:
        return 80.0  # 10 天
    if years < 5:
        return 112.0  # 14 天
    if years < 10:
        return 120.0  # 15 天
    # 10 年以上:15 天起每年 +1,上限 30 天。
    days = min(30, 15 + int(years - 9))
    return float(days * 8)


def _seed_leave_quotas(ctx: SeedContext) -> int:
    """為每位正職員工建年度請假額度。

    annual 依年資(§38)、sick 240h(30 天半薪病假)、personal 112h(14 天事假);
    menstrual 96h 僅女性。每員工每假別一筆,對應本學年(school_year)。
    才藝時薪(hourly)兼職不建特休額度。
    唯一鍵 uq_leave_quotas_employee_school_year_type=(employee_id, school_year,
    leave_type),故同員工同假別只建一筆。
    """
    from models.leave import LeaveQuota

    session = ctx.session
    today = ctx.config.today
    n = 0
    for emp in ctx.employees:
        if getattr(emp, "employee_type", None) == "hourly":
            continue
        is_female = (getattr(emp, "gender", "") or "") in {"F", "female", "女"}
        quotas = {
            "annual": _annual_hours_for_tenure(getattr(emp, "hire_date", None), today),
            "sick": 240.0,
            "personal": 112.0,
        }
        if is_female:
            quotas["menstrual"] = 96.0
        for lt, hours in quotas.items():
            session.add(
                LeaveQuota(
                    employee_id=emp.id,
                    year=today.year,
                    school_year=ctx.config.academic_year,
                    leave_type=lt,
                    total_hours=hours,
                )
            )
            n += 1
    return n


def seed(ctx: SeedContext) -> None:
    """建立請假/加班/補打卡記錄。"""
    from models.leave import LeaveRecord
    from models.overtime import OvertimeRecord, PunchCorrectionRequest

    session = ctx.session
    rng = ctx.rng
    employees = ctx.employees
    if not employees or session is None:
        # 無員工(stub 階段 m01 尚未實作或單測)→ 不產生,保持冪等。
        return

    # 年度請假額度(每員工 setup,非逐月交易;請假頁餘額計算需要)。
    quota_n = _seed_leave_quotas(ctx)

    today = ctx.config.today
    months = list(ctx.closed_months())
    current = ctx.current_month()
    # 當月(in_progress)也要生(留 pending);若 today 落在學年內且未含於 closed。
    if current not in months:
        months.append(current)

    leave_n = 0
    overtime_n = 0
    punch_n = 0

    for y, m in months:
        is_closed = (y, m) != current
        # 該月狀態決定核准狀態:closed → approved;當月 → pending。
        leave_status = "approved" if is_closed else "pending"
        ot_status = "approved" if is_closed else "pending"
        punch_status = "approved" if is_closed else "pending"

        # 當月工作日截到 today(避免生未來日期)。
        upto = today if not is_closed else None
        month_workdays = workdays(y, m, upto=upto)
        if not month_workdays:
            continue

        # ── 1) 一般請假:約 35% 員工該月請 1~2 次假 ──────────────────────
        for emp in employees:
            if rng.random() >= 0.35:
                continue
            n = rng.randint(1, 2)
            picks = rng.sample(_LEAVE_POOL, min(n, len(_LEAVE_POOL)))
            # 從該月工作日抽不重複的請假日(請假日 = 工作日,對齊考勤語意)。
            chosen_days = rng.sample(month_workdays, min(n, len(month_workdays)))
            for (lt, reason, hours), d in zip(picks, chosen_days):
                full_day = hours >= 8
                session.add(
                    LeaveRecord(
                        employee_id=emp.id,
                        leave_type=lt,
                        start_date=d,
                        end_date=d,
                        start_time="08:00",
                        end_time="17:00" if full_day else "12:00",
                        leave_hours=hours,
                        is_deductible=(lt in _DEDUCTIBLE_TYPES),
                        deduction_ratio=1.0 if lt in _DEDUCTIBLE_TYPES else 0.0,
                        reason=reason,
                        status=leave_status,
                        approved_by="admin" if leave_status == "approved" else None,
                    )
                )
                leave_n += 1

        # ── 2) 加班(週末):約 25% 員工該月加班一次 ────────────────────
        # 列出整月所有週六候選(避開未來)。
        sats: list[date] = []
        cur = date(y, m, 1)
        while cur.month == m:
            if cur.weekday() == 5:
                if is_closed or cur <= today:
                    sats.append(cur)
            cur += timedelta(days=1)
        if sats:
            for emp in employees:
                if rng.random() >= 0.25:
                    continue
                ot_date = rng.choice(sats)
                hours = float(rng.choice([3, 4, 4, 6]))
                # 週末加班費率 1.34(對齊既有 seed 慣例);金額走 round_half_up。
                pay = round_half_up(hours * _hourly_rate(emp) * 1.34)
                use_comp = rng.random() < 0.3
                ot = OvertimeRecord(
                    employee_id=emp.id,
                    overtime_date=ot_date,
                    overtime_type="weekend",
                    start_time=datetime(ot_date.year, ot_date.month, ot_date.day, 8, 0),
                    end_time=datetime(
                        ot_date.year,
                        ot_date.month,
                        ot_date.day,
                        8 + int(hours),
                        0,
                    ),
                    hours=hours,
                    overtime_pay=pay,
                    use_comp_leave=use_comp,
                    comp_leave_granted=(use_comp and ot_status == "approved"),
                    status=ot_status,
                    approved_by="admin" if ot_status == "approved" else None,
                    reason="校外教學佈置",
                )
                session.add(ot)
                overtime_n += 1

                # 已核且選擇換補休 → 衍生一筆 compensatory 請假單(綁來源加班)。
                # 需 flush 取得 overtime id 才能設 source_overtime_id。
                if use_comp and ot_status == "approved":
                    session.flush()
                    # 補休使用日:加班日之後的某個工作日(closed 月可放當月稍後)。
                    later = [wd for wd in month_workdays if wd > ot_date]
                    comp_day = later[0] if later else ot_date
                    session.add(
                        LeaveRecord(
                            employee_id=emp.id,
                            leave_type="compensatory",
                            start_date=comp_day,
                            end_date=comp_day,
                            start_time="08:00",
                            end_time="12:00",
                            leave_hours=min(hours, 4.0),
                            is_deductible=False,
                            deduction_ratio=0.0,
                            reason="補休(加班換休)",
                            status="approved",
                            approved_by="admin",
                            source_overtime_id=ot.id,
                        )
                    )
                    leave_n += 1

        # ── 3) 補打卡:每月抽 2~3 名員工各一筆 ──────────────────────────
        sample_size = min(3, len(employees))
        if sample_size > 0:
            for emp in rng.sample(employees, sample_size):
                pc_day = rng.choice(month_workdays)
                session.add(
                    PunchCorrectionRequest(
                        employee_id=emp.id,
                        attendance_date=pc_day,
                        correction_type=rng.choice(["punch_in", "punch_out", "both"]),
                        requested_punch_in=datetime(
                            pc_day.year, pc_day.month, pc_day.day, 8, 0
                        ),
                        requested_punch_out=datetime(
                            pc_day.year, pc_day.month, pc_day.day, 17, 0
                        ),
                        reason=rng.choice(_PUNCH_REASONS),
                        status=punch_status,
                        approved_by="admin" if punch_status == "approved" else None,
                    )
                )
                punch_n += 1

    ctx.log("leave_quotas", quota_n)
    ctx.log("leave_records", leave_n)
    ctx.log("overtime_records", overtime_n)
    ctx.log("punch_correction_requests", punch_n)
