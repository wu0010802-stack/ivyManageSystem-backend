"""m03_attendance:員工逐月逐工作日考勤(normal/late/early_leave/leave)、

排班(shift_assignments/daily_shifts),以及學生每日點名(student_attendances)。
in_progress 月只生到 today。

設計要點:
- 只生 closed + in_progress(current)月份,上限 `ctx.config.today`,不生 future。
  closed 月為已完成(每工作日皆有打卡);in_progress 月只生到 today。
- 員工考勤:正職(employee_type != 'hourly')逐工作日各一筆 `Attendance`;
  ~88% normal,其餘 late/early_leave/leave(全天請假)。決定論走 `ctx.rng`。
- 排班:每員工每週(週一)一筆 `ShiftAssignment`(綁 m00 落庫的 ShiftType);
  另抽樣少量 `DailyShift`(調班/排休)以涵蓋每日排班表。
- 學生點名:在籍學生逐工作日一筆 `StudentAttendance`,
  status 值域 出席/缺席/病假/事假/遲到(CHECK ck_student_attendances_status)。

唯一鍵:
- Attendance: (employee_id, attendance_date)
- ShiftAssignment: (employee_id, week_start_date)
- DailyShift: (employee_id, date)
- StudentAttendance: (student_id, date)
逐月、逐工作日建一筆,天然不撞唯一鍵。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from models.attendance import Attendance, AttendanceStatus
from models.classroom import StudentAttendance
from models.shift import DailyShift, ShiftAssignment, ShiftType

from ..calendar import workdays
from ..context import SeedContext

if TYPE_CHECKING:  # pragma: no cover
    pass


# 學生點名 status 值域(對齊 CHECK ck_student_attendances_status)。
_STUDENT_STATUS_NORMAL = "出席"


def _parse_hhmm(value: str | None, default: tuple[int, int]) -> tuple[int, int]:
    """把 "HH:MM" 解析成 (時, 分),失敗回 default。"""
    if not value:
        return default
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


def _months_to_seed(ctx: SeedContext) -> list[tuple[int, int]]:
    """回傳要生的月份:closed 月 + 進行中(current)月。不含 future。"""
    months = list(ctx.closed_months())
    cur = ctx.current_month()
    if cur not in months:
        months.append(cur)
    return months


def _workdays_for_month(ctx: SeedContext, year: int, month: int) -> list[Any]:
    """該月工作日;若為進行中(current)月,截到 today。"""
    upto = ctx.config.today if (year, month) == ctx.current_month() else None
    return workdays(year, month, upto=upto)


def _regular_employees(ctx: SeedContext) -> list[Any]:
    """正職(非時薪)員工:走打卡/排班路徑。

    時薪(才藝,employee_type == 'hourly')不逐日打卡,故排除。
    防呆:employee_type 為 None 時視為正職。
    """
    result: list[Any] = []
    for emp in ctx.employees:
        etype = getattr(emp, "employee_type", None)
        if etype == "hourly":
            continue
        result.append(emp)
    return result


def _seed_employee_attendance(
    ctx: SeedContext, employees: list[Any], months: list[tuple[int, int]]
) -> int:
    """逐月逐工作日為每位正職員工建一筆 Attendance。回傳新增筆數。"""
    session = ctx.session
    rng = ctx.rng
    added = 0
    for emp in employees:
        ws_h, ws_m = _parse_hhmm(getattr(emp, "work_start_time", None), (8, 0))
        we_h, we_m = _parse_hhmm(getattr(emp, "work_end_time", None), (17, 0))
        for year, month in months:
            for d in _workdays_for_month(ctx, year, month):
                roll = rng.random()
                status = AttendanceStatus.NORMAL.value
                is_late = is_early = False
                late_min = early_min = 0
                punch_in = punch_out = None

                if roll < 0.04:
                    # 遲到
                    status = AttendanceStatus.LATE.value
                    is_late = True
                    late_min = rng.choice([5, 10, 15, 20, 30])
                    punch_in = datetime(d.year, d.month, d.day, ws_h, ws_m) + timedelta(
                        minutes=late_min
                    )
                    punch_out = datetime(
                        d.year, d.month, d.day, we_h, we_m
                    ) + timedelta(minutes=rng.randint(0, 10))
                elif roll < 0.08:
                    # 早退
                    status = AttendanceStatus.EARLY_LEAVE.value
                    is_early = True
                    early_min = rng.choice([10, 20, 30])
                    punch_in = datetime(d.year, d.month, d.day, ws_h, ws_m) - timedelta(
                        minutes=rng.randint(0, 5)
                    )
                    punch_out = datetime(
                        d.year, d.month, d.day, we_h, we_m
                    ) - timedelta(minutes=early_min)
                elif roll < 0.12:
                    # 全天請假(員工請假同步寫入考勤;leave_record_id 由 m04 視情況回填)
                    status = AttendanceStatus.LEAVE.value
                    # 請假日不打卡
                else:
                    # 正常:準時或提早數分鐘到、稍晚數分鐘走
                    punch_in = datetime(d.year, d.month, d.day, ws_h, ws_m) - timedelta(
                        minutes=rng.randint(0, 5)
                    )
                    punch_out = datetime(
                        d.year, d.month, d.day, we_h, we_m
                    ) + timedelta(minutes=rng.randint(0, 10))

                session.add(
                    Attendance(
                        employee_id=emp.id,
                        attendance_date=d,
                        punch_in_time=punch_in,
                        punch_out_time=punch_out,
                        status=status,
                        is_late=is_late,
                        is_early_leave=is_early,
                        is_missing_punch_in=False,
                        is_missing_punch_out=False,
                        late_minutes=late_min,
                        early_leave_minutes=early_min,
                    )
                )
                added += 1
    return added


def _week_mondays(months: list[tuple[int, int]], upto: Any) -> list[Any]:
    """回傳 months 期間內、首日 ≤ upto 的所有週一日期(去重排序)。"""
    if not months:
        return []
    first_year, first_month = min(months)
    # 從首月 1 日所在週的週一開始。
    cursor = datetime(first_year, first_month, 1).date()
    cursor -= timedelta(days=cursor.weekday())  # 退到該週週一
    mondays: list[Any] = []
    while cursor <= upto:
        mondays.append(cursor)
        cursor += timedelta(days=7)
    return mondays


def _seed_shifts(
    ctx: SeedContext, employees: list[Any], months: list[tuple[int, int]]
) -> tuple[int, int]:
    """建每週排班(ShiftAssignment)+ 少量每日調班(DailyShift)。

    Returns:
        (shift_assignments 筆數, daily_shifts 筆數)
    """
    session = ctx.session
    rng = ctx.rng
    shift_types = session.query(ShiftType).filter(ShiftType.is_active.is_(True)).all()
    if not shift_types:
        # m00 應已落庫 shift_types;缺則跳過排班(不致命)。
        return (0, 0)

    # 班導用「正值(班導)」班別優先,其餘隨機;助教用副班別優先。
    def _pick_shift(emp: Any) -> Any:
        position = (getattr(emp, "position", None) or "").strip()
        title = (getattr(emp, "title", None) or "").strip()
        if "班導" in position or "班導" in title:
            return next(
                (s for s in shift_types if "班導" in (s.name or "")),
                shift_types[0],
            )
        return rng.choice(shift_types)

    mondays = _week_mondays(months, ctx.config.today)
    sa_added = 0
    for monday in mondays:
        for emp in employees:
            st = _pick_shift(emp)
            session.add(
                ShiftAssignment(
                    employee_id=emp.id,
                    shift_type_id=st.id,
                    week_start_date=monday,
                )
            )
            sa_added += 1

    # 每日調班:每位員工抽樣少量工作日改排其他班別(或排休 shift_type_id=None)。
    ds_added = 0
    seen_daily: set[tuple[int, Any]] = set()
    for emp in employees:
        for year, month in months:
            for d in _workdays_for_month(ctx, year, month):
                if rng.random() >= 0.03:  # 約 3% 工作日有調班
                    continue
                key = (emp.id, d)
                if key in seen_daily:
                    continue
                seen_daily.add(key)
                # 八成換班別、兩成排休(None)。
                if rng.random() < 0.8:
                    st_id: int | None = rng.choice(shift_types).id
                else:
                    st_id = None
                session.add(
                    DailyShift(
                        employee_id=emp.id,
                        shift_type_id=st_id,
                        date=d,
                        notes="調班" if st_id is not None else "排休",
                    )
                )
                ds_added += 1
    return (sa_added, ds_added)


def _student_status(rng: Any) -> str:
    """決定論抽一個學生點名狀態。~88% 出席,其餘缺席/病假/事假/遲到。"""
    r = rng.random()
    if r < 0.88:
        return _STUDENT_STATUS_NORMAL
    if r < 0.92:
        return "病假"
    if r < 0.95:
        return "事假"
    if r < 0.98:
        return "遲到"
    return "缺席"


def _seed_student_attendance(ctx: SeedContext, months: list[tuple[int, int]]) -> int:
    """在籍學生逐月逐工作日建一筆 StudentAttendance。回傳新增筆數。"""
    session = ctx.session
    rng = ctx.rng
    students = ctx.students_active
    recorded_by = _pick_recorder_user_id(ctx)

    mappings: list[dict[str, Any]] = []
    for year, month in months:
        wds = _workdays_for_month(ctx, year, month)
        for stu in students:
            for d in wds:
                mappings.append(
                    {
                        "student_id": stu.id,
                        "date": d,
                        "status": _student_status(rng),
                        "recorded_by": recorded_by,
                    }
                )
    # 分批 bulk insert(沿用既有 seed 慣例,降低 round-trip)。
    for i in range(0, len(mappings), 2000):
        session.bulk_insert_mappings(StudentAttendance, mappings[i : i + 2000])
    return len(mappings)


def _pick_recorder_user_id(ctx: SeedContext) -> int | None:
    """挑一個 User id 當 recorded_by(優先 admin 角色,否則任一)。"""
    if not ctx.users:
        return None
    admin = next(
        (u for u in ctx.users.values() if getattr(u, "role", None) == "admin"),
        None,
    )
    user = admin or next(iter(ctx.users.values()), None)
    return getattr(user, "id", None) if user is not None else None


def seed(ctx: SeedContext) -> None:
    """建立員工考勤/排班與學生每日點名。

    依賴(來自 ctx registry):
        - ctx.employees:m01 已建,含 employee_type/work_start_time/work_end_time/id
        - ctx.students_active:m02 已建在籍學生
        - ctx.users:m01 已建,供 recorded_by
        - m00 已落庫 shift_types(由本模組於 session 查詢)
    """
    months = _months_to_seed(ctx)
    if not months:
        return

    employees = _regular_employees(ctx)

    att_n = _seed_employee_attendance(ctx, employees, months)
    if att_n:
        ctx.log("attendances", att_n)

    sa_n, ds_n = _seed_shifts(ctx, employees, months)
    if sa_n:
        ctx.log("shift_assignments", sa_n)
    if ds_n:
        ctx.log("daily_shifts", ds_n)

    stu_n = _seed_student_attendance(ctx, months)
    if stu_n:
        ctx.log("student_attendances", stu_n)
