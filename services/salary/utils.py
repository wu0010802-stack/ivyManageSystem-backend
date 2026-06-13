"""
薪資計算工具函式 - 請假扣款、工作天數、發放月判斷
"""

from datetime import date
from typing import Optional

from .constants import (
    LEAVE_DEDUCTION_RULES,
    MONTHLY_BASE_DAYS,
    SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS,
)
from services.workday_rules import classify_day, load_day_rule_maps
from utils.rounding import round_down


def is_attendance_waived(att) -> bool:
    """考勤異常是否已由管理員豁免（薪資端應視為不扣）。

    管理員在「考勤異常確認」頁面以 admin_waive 標記後，UI 顯示「管理員豁免」，
    薪資計算遲到/早退/缺打卡扣款時亦應排除該日，否則前台處理狀態與薪資結果分叉
    （員工以為被豁免、薪資仍照扣）。

    與 admin_accept 區別：admin_accept 是管理員代為承認異常（仍扣），
    admin_waive 才是真正豁免（不扣）。
    """
    return getattr(att, "confirmed_action", None) == "admin_waive"


def _sum_leave_deduction_legacy(
    leaves,
    daily_salary: float,
    ytd_sick_hours_before_month: float = 0.0,
) -> float:
    """計算請假扣款總額（舊版，以 LeaveRecord 列表為 SoT）。

    保留供 Task 26 parity 測試對照用，勿刪。

    優先使用 LeaveRecord.deduction_ratio 欄位；
    若為 None，fallback 至 LEAVE_DEDUCTION_RULES[leave_type]（向後相容舊資料）。

    勞基法第 43 條 + 勞工請假規則第 4 條：
    普通傷病假一年內累計未逾 30 日（240h）部分折半薪，超過之部分不給薪。
    `ytd_sick_hours_before_month` 提供本月之前、同一年度內已核准的病假時數，
    讓本月超過年度上限的病假改以 ratio=1.0（全扣）計算。人工覆寫的
    `deduction_ratio` 仍優先採用（尊重 HR 判斷），但該筆時數仍計入年度累計。

    Args:
        leaves:                      LeaveRecord 列表（需有 leave_type, leave_hours,
                                      deduction_ratio, start_date 屬性）
        daily_salary:                日薪（base_salary / MONTHLY_BASE_DAYS）
        ytd_sick_hours_before_month: 本月之前、同年度已核准病假時數（預設 0）
    Returns:
        扣款金額（浮點數，由呼叫端決定是否 round）
    """
    total = 0.0
    sick_used = float(ytd_sick_hours_before_month or 0.0)

    # 病假按 start_date 由早到晚處理（先請的先享半薪額度）
    sick_leaves = sorted(
        [lv for lv in leaves if lv.leave_type == "sick"],
        key=lambda lv: getattr(lv, "start_date", None) or date.min,
    )
    other_leaves = [lv for lv in leaves if lv.leave_type != "sick"]
    standard_sick_ratio = LEAVE_DEDUCTION_RULES.get("sick", 0.5)

    for lv in sick_leaves:
        hours = lv.leave_hours or 0
        # 僅「明確偏離標準 0.5」的 ratio 視為 HR 人工覆寫；
        # 核准流程會把 ratio 寫成標準值 0.5，此時應照常套用年度上限。
        is_genuine_override = (
            lv.deduction_ratio is not None and lv.deduction_ratio != standard_sick_ratio
        )
        if is_genuine_override:
            # 無條件捨去（對齊園所實務：扣款捨小數、對員工有利）
            total += round_down((hours / 8) * daily_salary * lv.deduction_ratio)
        else:
            half_paid = max(
                0.0, min(SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS - sick_used, hours)
            )
            unpaid = hours - half_paid
            # 同一筆病假的半薪段 + 全薪段合併後再捨去
            total += round_down(
                (half_paid / 8) * daily_salary * 0.5 + (unpaid / 8) * daily_salary * 1.0
            )
        sick_used += hours

    for lv in other_leaves:
        ratio = (
            lv.deduction_ratio
            if lv.deduction_ratio is not None
            else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
        )
        total += round_down((lv.leave_hours / 8) * daily_salary * ratio)
    return total


def _sum_leave_deduction(
    att_leave_pairs,
    daily_salary: float,
    ytd_sick_hours_before_month: float = 0.0,
) -> float:
    """請假扣款（Attendance 為 SoT 的新版，以 (Attendance, LeaveRecord) tuples 為輸入）。

    hours 來源以 Attendance 記錄為準：
      - status == LEAVE（全日假）→ 8.0 小時
      - partial_leave_hours > 0   → 部分時數
      - 其餘                       → 0.0（不計扣）

    保留業務規則與 _sum_leave_deduction_legacy 完全對齊：
      - 病假 240hr 年度半薪上限（勞基法第 43 條）
      - 病假依 start_date 由早到晚處理（先請的先享半薪額度）
      - HR deduction_ratio 覆寫偵測（偏離標準 0.5 才視為人工覆寫）
      - 非病假 fallback 至 LEAVE_DEDUCTION_RULES[leave_type]

    Args:
        att_leave_pairs:             (Attendance, LeaveRecord) tuple 列表
        daily_salary:                日薪（base_salary / MONTHLY_BASE_DAYS）
        ytd_sick_hours_before_month: 本月之前、同年度已核准病假時數（預設 0）
    Returns:
        扣款金額（浮點數，由呼叫端決定是否 round）
    """
    from models.attendance import AttendanceStatus

    def _hours(att, lv) -> float:  # noqa: ARG001  lv 保留供未來擴充
        if att.status == AttendanceStatus.LEAVE.value:
            return 8.0
        if att.partial_leave_hours is not None and float(att.partial_leave_hours) > 0:
            return float(att.partial_leave_hours)
        return 0.0

    sick_pairs = sorted(
        [(att, lv) for att, lv in att_leave_pairs if lv.leave_type == "sick"],
        key=lambda x: getattr(x[1], "start_date", None) or date.min,
    )
    other_pairs = [(att, lv) for att, lv in att_leave_pairs if lv.leave_type != "sick"]
    standard_sick_ratio = LEAVE_DEDUCTION_RULES.get("sick", 0.5)

    total = 0.0
    sick_used = float(ytd_sick_hours_before_month or 0.0)

    for att, lv in sick_pairs:
        hours = _hours(att, lv)
        if hours <= 0:
            continue
        is_genuine_override = (
            lv.deduction_ratio is not None and lv.deduction_ratio != standard_sick_ratio
        )
        if is_genuine_override:
            # 無條件捨去（對齊園所實務：扣款捨小數、對員工有利）
            total += round_down((hours / 8) * daily_salary * lv.deduction_ratio)
        else:
            half_paid = max(
                0.0, min(SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS - sick_used, hours)
            )
            unpaid = hours - half_paid
            # 同一筆病假的半薪段 + 全薪段合併後再捨去
            total += round_down(
                (half_paid / 8) * daily_salary * 0.5 + (unpaid / 8) * daily_salary * 1.0
            )
        sick_used += hours

    for att, lv in other_pairs:
        hours = _hours(att, lv)
        if hours <= 0:
            continue
        ratio = (
            lv.deduction_ratio
            if lv.deduction_ratio is not None
            else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
        )
        total += round_down((hours / 8) * daily_salary * ratio)

    return total


def get_working_days(year: int, month: int, session=None) -> int:
    """計算指定月份的法定工作日數（含補班日，排除國定假日）"""
    if not 1 <= month <= 12:
        raise ValueError(f"month 必須介於 1–12，收到 {month!r}")
    import calendar
    from models.database import get_session

    # 查詢當月國定假日
    _session = session or get_session()
    try:
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        holiday_map, makeup_map = load_day_rule_maps(_session, month_start, month_end)
    finally:
        if not session:
            _session.close()

    working_days = 0
    for day in range(1, month_end.day + 1):
        current = date(year, month, day)
        if classify_day(current, holiday_map, makeup_map)["kind"] == "workday":
            working_days += 1
    return working_days


def get_bonus_distribution_month(month: int) -> bool:
    """
    判斷是否為節慶獎金發放月
    2月 → 發放 12+1月
    6月 → 發放 2-5月
    9月 → 發放 6-8月
    12月 → 發放 9-11月
    """
    return month in (2, 6, 9, 12)


def get_current_period_passed_months(year: int, month: int) -> list[tuple[int, int]]:
    """
    回傳該月所屬發放期起點至查詢月（含）之 (year, month) 清單。
    發放月（2/6/9/12）輸入時回空 list。

    期間定義（對齊 get_meeting_deduction_period_start）：
      - 2 月發放 12(去年)、1
      - 6 月發放 2、3、4、5
      - 9 月發放 6、7、8
      - 12 月發放 9、10、11
    """
    if get_bonus_distribution_month(month):
        return []

    if month == 1:
        return [(year - 1, 12), (year, 1)]
    if 3 <= month <= 5:
        return [(year, m) for m in range(2, month + 1)]
    if 7 <= month <= 8:
        return [(year, m) for m in range(6, month + 1)]
    if 10 <= month <= 11:
        return [(year, m) for m in range(9, month + 1)]
    return []


def get_distribution_period_months(year: int, month: int) -> list[tuple[int, int]]:
    """發放月所結算的月份清單（不含發放月本身）。

    Why: 節慶獎金規則為「發放月時加總期間每月各自比例」（業主 2026-04-25 確認）；
    此 helper 提供 calculate_salary 在發放月時要 iterate 的目標月份清單。
    非發放月輸入回 []。

    對應關係：
      2 月  → [(year-1, 12), (year, 1)]   （與 get_current_period_passed_months(year, 1) 一致）
      6 月  → [(year, 2), (year, 3), (year, 4), (year, 5)]
      9 月  → [(year, 6), (year, 7), (year, 8)]
      12 月 → [(year, 9), (year, 10), (year, 11)]
    """
    if not get_bonus_distribution_month(month):
        return []
    if month == 2:
        return get_current_period_passed_months(year, 1)
    return get_current_period_passed_months(year, month - 1)


def calc_daily_salary(base_salary) -> float:
    """日薪計算：base_salary / 30（勞基法基準 MONTHLY_BASE_DAYS）"""
    return (base_salary or 0) / MONTHLY_BASE_DAYS


def next_distribution_month(year: int, month: int) -> tuple[int, int]:
    """事件月的人數會進哪個發放月（get_distribution_period_months 的反向映射）。

    12 月 → 次年 2 月、1 月 → 同年 2 月、2-5 月 → 6 月、6-8 月 → 9 月、
    9-11 月 → 12 月。
    """
    if month == 12:
        return year + 1, 2
    if month == 1:
        return year, 2
    if 2 <= month <= 5:
        return year, 6
    if 6 <= month <= 8:
        return year, 9
    return year, 12


def mark_salary_stale_for_enrollment_event(session, event_date) -> int:
    """學生在籍/班籍異動後，標記受影響發放月的未封存薪資 needs_recalc（L1c）。

    班級/全校在籍人數只進「發放月」（2/6/9/12）的節慶/超額累計；事件日所屬
    發放期的發放月（含）之後所有發放月都可能用到事件日之後的人數，全部標記。
    不分員工：人數影響該班導師/副班導/美師與走全校比例的主管/辦公室職。

    已封存（is_finalized）不動，理由同 mark_salary_stale。

    Returns:
        被標記的 record 數（caller 自行 commit）。
    """
    from models.database import SalaryRecord

    dist_year, dist_month = next_distribution_month(event_date.year, event_date.month)
    key = dist_year * 100 + dist_month
    return (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.salary_month.in_((2, 6, 9, 12)),
            (SalaryRecord.salary_year * 100 + SalaryRecord.salary_month) >= key,
            SalaryRecord.is_finalized.is_(False),
        )
        .update({"needs_recalc": True}, synchronize_session=False)
    )


def mark_salary_stale(session, employee_id: int, year: int, month: int) -> bool:
    """將指定員工該月 SalaryRecord 標記為 needs_recalc=True。

    用於上游事件(假單/加班審核)後薪資重算失敗、批次重算 except 路徑等場景,
    確保 finalize 完整性檢查能擋下未成功重算的記錄。

    Why 排除 finalized:
        已封存(is_finalized=True)的薪資代表結帳已鎖定,不可再被重算,
        若仍標 stale 等同把已封存資料標成「待修改」,會與封存語意衝突,
        並讓上游 admin_waive 等異動誤動到已封存月份的計算來源。
        上游若需異動已封存月份,呼叫端應先用 finalize 守衛攔下並要求解封,
        此 helper 不負責豁免封存。

    Returns:
        True  — 找到未封存 record 並標記成功(caller 仍需自行 commit)
        False — 該月無 record 或 record 已封存(屬「不該重算」場景)
    """
    from models.database import SalaryRecord

    rec = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .first()
    )
    # 用 getattr 防禦:單元測試常以 SimpleNamespace 模擬 ORM 物件,有時缺欄位。
    # 真實 SalaryRecord 必有 is_finalized 欄位,fallback 不會影響 production。
    if rec is None or getattr(rec, "is_finalized", False):
        return False
    rec.needs_recalc = True
    return True


def mark_salary_stale_from_month(
    session, employee_id: int, year: int, from_month: int
) -> int:
    """把該員工同年、salary_month >= from_month、未封存的 SalaryRecord 標 needs_recalc=True。

    用於「改某月 YTD 累計獎金欄位」場景：二代健保補充保費採 per-payment 增額制
    （supplementary_premium.query_ytd_bonus_before 以 salary_month < month 累計），改
    N 月獎金會使 N 月自身與同年「之後」月份的補充保費基底（ytd_before / threshold）失準。
    本 helper 一次把「當月及之後」未封存月份標 stale，強制 finalize 前重算
    （重算尊重 manual_overrides，手動調整的獎金值得以保留）。

    排除已封存月份理由同 mark_salary_stale：封存代表結帳鎖定，不可被重算覆寫。
    對照範本：api/insurance.py:_bulk_mark_salary_stale_for_year（級距異動標整年 stale）。

    Returns:
        被標記的 record 數（caller 自行 commit）。
    """
    from models.database import SalaryRecord

    return (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month >= from_month,
            SalaryRecord.is_finalized.is_(False),
        )
        .update({"needs_recalc": True}, synchronize_session=False)
    )


def snapshot_ytd_bonus(record) -> dict:
    """快照 record 的 BONUS_FIELDS_FOR_YTD 欄位值，供 engine 重算後 delta 比較。

    在 _fill_salary_record 寫入新值「之前」呼叫。record 為 None 或新建（屬性尚未設）
    時各欄回 0。`or 0` 把 falsy（0/0.0/None）統一成 0，使 before/after 比較不受
    int/float/None 表示差異干擾。
    """
    from services.salary.supplementary_premium import BONUS_FIELDS_FOR_YTD

    if record is None:
        return {f: 0 for f in BONUS_FIELDS_FOR_YTD}
    return {f: (getattr(record, f, 0) or 0) for f in BONUS_FIELDS_FOR_YTD}


def mark_stale_if_ytd_bonus_changed(
    session, employee_id: int, year: int, month: int, before: dict, record
) -> int:
    """engine 重算路徑：本月 YTD 累計獎金 vs 重算前有變動 → 標同年「後月」needs_recalc。

    `before` 為 _fill_salary_record 之前 snapshot_ytd_bonus(record) 的值；本函式在
    _fill 之後呼叫，比較 record 當前 BONUS_FIELDS_FOR_YTD。

    **from_month = month + 1**：engine 重算當月已連同當月補充保費一起算對
    （ytd_before = sum(1..month-1) + 當月新獎金），當月自身正確；要傳播的是「之後」
    月份（其 ytd_before 含當月、尚未重算）。manual_adjust 用 month（不重算當月補充
    保費）語意不同，勿混淆。

    Returns:
        被標記的後月 record 數（0 表無變動或無後月；caller 隨主路徑事務 commit）。
    """
    from services.salary.supplementary_premium import BONUS_FIELDS_FOR_YTD

    after = {f: (getattr(record, f, 0) or 0) for f in BONUS_FIELDS_FOR_YTD}
    if before == after:
        return 0
    return mark_salary_stale_from_month(session, employee_id, year, month + 1)


def lock_and_premark_stale(
    session, employee_id: int, months: set[tuple[int, int]]
) -> None:
    """為「上游異動 → 重算」流程同時取鎖並把對應月份預標 stale（同 transaction）。

    Why（鎖延伸 + pre-mark-stale）:
        上游路徑（請假/加班/考勤/會議/排班核准）原本流程是
            check_finalized → 異動來源 → commit → process_salary_calculation
        中間有兩個 race window:
        1. check 與 commit 之間 → 由 acquire_salary_lock 在同 session 取鎖封住
        2. commit（lock 釋放）與 engine 取鎖 之間 → finalize 可在此搶先封存
           當下 needs_recalc 還是 False 的舊薪資

        本 helper 同時做兩件事:
        - acquire_salary_lock 取得 per-emp 鎖（caller 必須維持同一 session 直到 commit）
        - mark_salary_stale 把每個受影響月份的 SalaryRecord 標 needs_recalc=True

        commit 後即使 finalize 搶到鎖,也會看到 needs_recalc=True 而被擋下;
        engine 後續開新 session 取鎖再做 process_salary_calculation 時會把 stale 重算清掉。

    呼叫端責任：
        - 必須在 caller 自己的 session 上呼叫,且後續同一 session commit
        - lock 為 pg_advisory_xact_lock,在 commit/rollback 時自動釋放
        - 已封存(is_finalized=True)的 record mark_salary_stale 會自然跳過

    Args:
        session: 與 leave/overtime/attendance 異動同一 session
        employee_id: 員工 id
        months: 受影響的 (year, month) 集合
    """
    from utils.advisory_lock import acquire_salary_lock

    for year, month in sorted(months):
        acquire_salary_lock(session, employee_id=employee_id, year=year, month=month)
        mark_salary_stale(session, employee_id, year, month)


def get_meeting_deduction_period_start(year: int, month: int) -> Optional[date]:
    """
    返回發放月的會議缺席扣款起算日。
    計算範圍 = 上次發放月（不含，因其當月已計算）至當發放月（含）。

    2月  → 1月1日  （上次發放為12月，1月為未扣款的非發放月）
    6月  → 3月1日  （上次發放為2月，3–5月為未扣款的非發放月）
    9月  → 7月1日  （上次發放為6月，7–8月為未扣款的非發放月）
    12月 → 10月1日 （上次發放為9月，10–11月為未扣款的非發放月）

    非發放月返回 None（不需要補查歷史記錄）。
    """
    if month == 2:
        return date(year, 1, 1)
    elif month == 6:
        return date(year, 3, 1)
    elif month == 9:
        return date(year, 7, 1)
    elif month == 12:
        return date(year, 10, 1)
    return None
