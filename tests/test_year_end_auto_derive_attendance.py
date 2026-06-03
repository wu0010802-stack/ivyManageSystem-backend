"""B5：⑤a 考勤扣款 attendance_deductions 純計算測試。

年終 Excel「遲到一覽表」的扣款是**獨立定額罰則**（業主 2026-06-02 確認），
與 payroll（services/salary/deduction.py）的「比例/病假半薪/未打卡不扣」scheme 不同：
  - 遲到 = -50/次（config late_deduction_per_time，Excel 已從 100 改 50）
  - 未打卡 = -50/次（config missing_punch_deduction_per_time，業主確認納入）
  - 事假 = -500/天（config personal_leave_deduction_per_day）
  - 病假 = -500/天（全扣，非半薪；config sick_leave_deduction_per_day）
  - 會議缺席 = COUNT(MeetingRecord attended=false) × meeting_absence_penalty
  - 白名單：請假只扣 leave_type IN ('personal','sick')；其餘一律不扣
    （生理假/特休/產假/陪產/補休… 全不扣 → 繞過 leave_type enum gap）

無乾淨 gold 總額（Excel 扣款欄為手動快照、前期常空），故測試 = 用 derived 費率
+ seeded data 驗證公式，**不是**對某 Excel 總數。per-unit 費率對齊 Excel 案例
（蔡宜倩遲到 5 次=-250、張庭滋未打卡 1 次=-50、楊思瑜事假 2 次=-1000、
張庭滋病假 1 天=-500），在註解引用佐證，但斷言用本檔 seed 的數。

期間（業主確認）= 民國曆年 Jan–Dec（對齊 B3/proration），即
date(cycle.academic_year+1911,1,1) ~ date(...,12,31)；ay114 → 2025/1/1~2025/12/31。
**不是** Excel 表頭的 Feb–Jan。期間邊界測試把這個選擇釘死。
"""

from datetime import date
from decimal import Decimal

import pytest

from models.attendance import Attendance
from models.config import BonusConfig
from models.employee import Employee
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from services.year_end.auto_derive import attendance_deductions as ad


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _mk_employee(db, code, name):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        hire_date=date(2023, 8, 1),
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_cycle(db, academic_year=114):
    cycle = YearEndCycle(
        academic_year=academic_year,
        start_date=date(academic_year + 1911, 8, 1),
        end_date=date(academic_year + 1912, 7, 31),
        bonus_calc_date=date(academic_year + 1912, 1, 15),
    )
    db.add(cycle)
    db.flush()
    return cycle


def _mk_attendance(
    db,
    emp,
    d,
    *,
    is_late=False,
    miss_in=False,
    miss_out=False,
):
    a = Attendance(
        employee_id=emp.id,
        attendance_date=d,
        is_late=is_late,
        is_missing_punch_in=miss_in,
        is_missing_punch_out=miss_out,
    )
    db.add(a)
    db.flush()
    return a


def _mk_leave(db, emp, leave_type, start, end, hours, *, status="approved"):
    lr = LeaveRecord(
        employee_id=emp.id,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        leave_hours=hours,
        status=status,
    )
    db.add(lr)
    db.flush()
    return lr


def _mk_meeting(db, emp, d, *, attended):
    m = MeetingRecord(employee_id=emp.id, meeting_date=d, attended=attended)
    db.add(m)
    db.flush()
    return m


def _mk_config(
    db,
    *,
    late=50,
    missing_punch=50,
    personal=500,
    sick=500,
    meeting_penalty=100,
):
    cfg = BonusConfig(
        config_year=114,
        is_active=True,
        late_deduction_per_time=late,
        missing_punch_deduction_per_time=missing_punch,
        personal_leave_deduction_per_day=personal,
        sick_leave_deduction_per_day=sick,
        meeting_absence_penalty=meeting_penalty,
    )
    db.add(cfg)
    db.flush()
    return cfg


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def base(test_db_session):
    db = test_db_session
    cycle = _mk_cycle(db, 114)  # 期間 = 2025/1/1 ~ 2025/12/31
    cfg = _mk_config(db)  # 全部用 Excel 定額費率
    emp = _mk_employee(db, "E_TEST_01", "測試員工")
    db.commit()
    return {"db": db, "cycle": cycle, "cfg": cfg, "emp": emp}


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #
def test_late_only(base):
    """遲到 5 次 × 50 = -250（Excel 蔡宜倩 5 次 -250 佐證費率）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    for i in range(5):
        _mk_attendance(db, emp, date(2025, 3, i + 1), is_late=True)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.late == Decimal("-250.00")
    assert res.calc_meta["late_count"] == 5
    assert res.calc_meta["missing_punch_count"] == 0


def test_missing_punch_merged_into_late(base):
    """未打卡併入 late 欄（settlement 只有 deduction_late 一欄）。

    1 筆 miss_in + 1 筆 miss_out（兩列各算 1 次） = 2 次 × 50 = -100；
    再加 1 列同時缺上下班 = 2 次 × 50 = -100；合計未打卡 4 次 -200。
    Excel 張庭滋「未打卡一次-50」佐證單價。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_attendance(db, emp, date(2025, 4, 1), miss_in=True)
    _mk_attendance(db, emp, date(2025, 4, 2), miss_out=True)
    _mk_attendance(db, emp, date(2025, 4, 3), miss_in=True, miss_out=True)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 未打卡 4 次 × 50 = -200，全進 late 欄
    assert res.late == Decimal("-200.00")
    assert res.calc_meta["late_count"] == 0
    assert res.calc_meta["missing_punch_count"] == 4


def test_late_and_missing_punch_combined(base):
    """遲到 + 未打卡 合併進 late 欄，calc_meta 分開記。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_attendance(db, emp, date(2025, 5, 1), is_late=True)
    _mk_attendance(db, emp, date(2025, 5, 2), is_late=True, miss_out=True)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 遲到 2 次 + 未打卡 1 次 = 3 × 50 = -150
    assert res.late == Decimal("-150.00")
    assert res.calc_meta["late_count"] == 2
    assert res.calc_meta["missing_punch_count"] == 1


def test_personal_leave(base):
    """事假 2 天（16h）× 500 = -1000（Excel 楊思瑜事假二次 -1000 佐證）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "personal", date(2025, 6, 2), date(2025, 6, 3), 16)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.personal_leave == Decimal("-1000.00")


def test_sick_leave_full_deduction(base):
    """病假 1 天（8h）× 500 = -500（全扣，非半薪；Excel 張庭滋病假一天 -500）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "sick", date(2025, 7, 1), date(2025, 7, 1), 8)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.sick_leave == Decimal("-500.00")


def test_meeting_absence(base):
    """會議缺席 3 次 × penalty(100) = -300；attended=True 不計。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_meeting(db, emp, date(2025, 3, 10), attended=False)
    _mk_meeting(db, emp, date(2025, 4, 10), attended=False)
    _mk_meeting(db, emp, date(2025, 5, 10), attended=False)
    _mk_meeting(db, emp, date(2025, 6, 10), attended=True)  # 出席不計
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.meeting == Decimal("-300.00")


def test_whitelist_menstrual_and_maternity_not_deducted(base):
    """白名單最重要：生理假 + 產假 一律不扣（Excel 楊思瑜「生理假一天不扣」佐證）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    # 生理假 1 天 / 產假 5 天 / 特休 2 天 / 補休 1 天 — 全不扣
    _mk_leave(db, emp, "menstrual", date(2025, 3, 1), date(2025, 3, 1), 8)
    _mk_leave(db, emp, "maternity", date(2025, 4, 1), date(2025, 4, 5), 40)
    _mk_leave(db, emp, "annual", date(2025, 5, 1), date(2025, 5, 2), 16)
    _mk_leave(db, emp, "compensatory", date(2025, 6, 1), date(2025, 6, 1), 8)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 白名單外假別 → 事假/病假皆為 0
    assert res.personal_leave == Decimal("0.00")
    assert res.sick_leave == Decimal("0.00")
    assert res.late == Decimal("0.00")
    assert res.meeting == Decimal("0.00")


def test_pending_leave_not_counted(base):
    """非 approved 的假單不計（status='pending'）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(
        db, emp, "personal", date(2025, 6, 2), date(2025, 6, 3), 16, status="pending"
    )
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.personal_leave == Decimal("0.00")


def test_period_boundary_jan_2025_included_jan_2026_excluded(base):
    """期間邊界釘死「民國曆年 Jan–Dec 2025」這個選擇。

    - 2025/1/15 的記錄 → 必須計入（排除 Excel Feb-start 讀法）
    - 2026/1/15 的記錄 → 必須排除（排除學年 Aug–Jul 讀法）
    兩者一起斷言「正好 = 2025/1/1~2025/12/31，其餘皆不計」。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    # 期間內邊界（含）：2025/1/15
    _mk_attendance(db, emp, date(2025, 1, 15), is_late=True)
    _mk_leave(db, emp, "personal", date(2025, 1, 20), date(2025, 1, 20), 8)
    _mk_meeting(db, emp, date(2025, 1, 25), attended=False)
    # 期間外（學年讀法會誤含）：2026/1/15
    _mk_attendance(db, emp, date(2026, 1, 15), is_late=True)
    _mk_leave(db, emp, "personal", date(2026, 1, 20), date(2026, 1, 20), 8)
    _mk_meeting(db, emp, date(2026, 1, 25), attended=False)
    # 期間外（Excel Feb-start 讀法會漏含 2025/1，但會誤含 2024/12）：2024/12/31
    _mk_attendance(db, emp, date(2024, 12, 31), is_late=True)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 只計 2025 年內：遲到 1 次 -50、事假 1 天 -500、會議缺席 1 次 -100
    assert res.late == Decimal("-50.00")
    assert res.personal_leave == Decimal("-500.00")
    assert res.meeting == Decimal("-100.00")


def test_other_employee_not_counted(base):
    """跨員工隔離：別人的考勤/假單不算進本員工。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    other = _mk_employee(db, "E_OTHER_01", "別人")
    _mk_attendance(db, other, date(2025, 3, 1), is_late=True)
    _mk_leave(db, other, "sick", date(2025, 3, 1), date(2025, 3, 1), 8)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.late == Decimal("0.00")
    assert res.sick_leave == Decimal("0.00")


def test_meeting_penalty_falls_back_when_config_none(test_db_session):
    """meeting_absence_penalty 為 None → 用 DEFAULT_MEETING_ABSENCE_PENALTY(100)。"""
    from services.salary.constants import DEFAULT_MEETING_ABSENCE_PENALTY

    db = test_db_session
    cycle = _mk_cycle(db, 114)
    cfg = _mk_config(db, meeting_penalty=None)
    emp = _mk_employee(db, "E_NONE_PEN", "缺penalty")
    _mk_meeting(db, emp, date(2025, 5, 10), attended=False)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.meeting == Decimal(f"-{DEFAULT_MEETING_ABSENCE_PENALTY}.00")


def test_no_config_zero(test_db_session):
    """無 active BonusConfig → 費率用 None-safe 預設；無資料時全 0。"""
    db = test_db_session
    cycle = _mk_cycle(db, 114)
    emp = _mk_employee(db, "E_NOCFG_01", "無設定")
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.late == Decimal("0.00")
    assert res.personal_leave == Decimal("0.00")
    assert res.sick_leave == Decimal("0.00")
    assert res.meeting == Decimal("0.00")


def test_all_values_non_positive(base):
    """所有扣款欄皆為負值或 0（罰則，永不為正）。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_attendance(db, emp, date(2025, 3, 1), is_late=True)
    _mk_leave(db, emp, "personal", date(2025, 6, 1), date(2025, 6, 1), 8)
    _mk_leave(db, emp, "sick", date(2025, 7, 1), date(2025, 7, 1), 8)
    _mk_meeting(db, emp, date(2025, 5, 10), attended=False)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    for v in (res.late, res.personal_leave, res.sick_leave, res.meeting):
        assert v <= Decimal("0")


# --------------------------------------------------------------------------- #
# sabotage 自驗（這些斷言驗證測試本身能抓錯；非永久 assert）                  #
# --------------------------------------------------------------------------- #
def test_sabotage_whitelist_removed_would_fail(base):
    """sabotage：若白名單失效（生理假被當扣款），本員工會多扣 → 證明白名單有效。

    這裡正向確認：生理假 8h 即使換算 1 天，也**不**進 personal/sick 任一欄。
    若實作誤把 menstrual 當 personal，personal_leave 會是 -500（≠ 0）。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "menstrual", date(2025, 3, 1), date(2025, 3, 1), 8)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 若白名單被拿掉，下面任一會變 -500 → FAIL（即 sabotage 能被抓）
    assert res.personal_leave == Decimal("0.00")
    assert res.sick_leave == Decimal("0.00")


def test_sabotage_wrong_rate_would_fail(base):
    """sabotage：費率若誤用 100（payroll 舊值）而非 config 的 50，遲到 3 次會是 -300。

    本檔 config 設 late=50；遲到 3 次正解 -150。若實作 hardcode 100 → -300 FAIL。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    for i in range(3):
        _mk_attendance(db, emp, date(2025, 3, i + 1), is_late=True)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.late == Decimal("-150.00")  # 50/次，非 100/次


# --------------------------------------------------------------------------- #
# item 2：半天/小時假 fixture（鎖定比例扣法 + calc_meta 原始時數可重建驗證）   #
# --------------------------------------------------------------------------- #
def test_personal_leave_half_day(base):
    """事假 4h（半天）→ 4/8 × 500 = -250；calc_meta 記 personal_hours=4.0（原始）。

    業主規則「事假 -500/天」採按比例：半天 = 0.5 天 = -250。
    同時驗證 calc_meta 可重建：personal_hours / 8 × personal_rate（_q2） == 250.00。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "personal", date(2025, 6, 2), date(2025, 6, 2), 4)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.personal_leave == Decimal("-250.00")

    meta = res.calc_meta
    # calc_meta 原始時數
    assert meta["personal_hours"] == 4.0
    # 可重建驗證：hours / 8 × rate（全精度後 _q2）== abs(personal_leave)
    reconstructed = (
        Decimal(str(meta["personal_hours"]))
        / Decimal("8")
        * Decimal(str(meta["personal_rate"]))
    ).quantize(Decimal("0.01"), rounding=__import__("decimal").ROUND_HALF_UP)
    assert reconstructed == abs(res.personal_leave)


def test_sick_leave_partial_hours(base):
    """病假 1.5h → 1.5/8 × 500 = 93.75 → -93.75；calc_meta 記 sick_hours=1.5（原始）。

    若 meta 改存四捨五入後 sick_days=0.19 → 重建 0.19×500=95≠93.75，稽核反推對不上。
    存原始時數後：1.5/8×500=93.75 → 精確重建。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "sick", date(2025, 7, 2), date(2025, 7, 2), 1.5)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.sick_leave == Decimal("-93.75")

    meta = res.calc_meta
    # calc_meta 存原始時數（非四捨後天數）
    assert meta["sick_hours"] == 1.5
    # 可重建驗證
    reconstructed = (
        Decimal(str(meta["sick_hours"]))
        / Decimal("8")
        * Decimal(str(meta["sick_rate"]))
    ).quantize(Decimal("0.01"), rounding=__import__("decimal").ROUND_HALF_UP)
    assert reconstructed == abs(res.sick_leave)


def test_sick_leave_half_day(base):
    """病假 4h（半天）→ 4/8 × 500 = -250；同時確認 sick_hours=4.0 可重建。"""
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    _mk_leave(db, emp, "sick", date(2025, 7, 3), date(2025, 7, 3), 4)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    assert res.sick_leave == Decimal("-250.00")

    meta = res.calc_meta
    assert meta["sick_hours"] == 4.0
    reconstructed = (
        Decimal(str(meta["sick_hours"]))
        / Decimal("8")
        * Decimal(str(meta["sick_rate"]))
    ).quantize(Decimal("0.01"), rounding=__import__("decimal").ROUND_HALF_UP)
    assert reconstructed == abs(res.sick_leave)


# --------------------------------------------------------------------------- #
# item 3：跨期假單行為鎖定（以 start_date 落期間整單計入）                    #
# --------------------------------------------------------------------------- #
def test_cross_period_leave_counted_by_start_date(base):
    """跨期假單：start=2025-12-30、end=2026-01-02（end 在期間外）。

    現行以 start_date 落期間為準 → 整單計入（授權近似，非拆分）。
    seed 整單 4 天（32h），期間內應全計入 → personal_leave = -2000.00。

    注意：end_date 與 LeaveRecord.leave_hours 計算無關（_leave_hours 只 SUM hours，
    不依日期切割）。本測試釘死「整單計入」行為——若改為按比例拆分，
    期間內僅 2 天（16h）→ 金額 -1000.00，此 assert 將 FAIL 並提醒。
    """
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    # start_date=2025-12-30（在 2025 期間內）；end_date=2026-01-02（在期間外）
    # 整單 4 天 = 32h
    _mk_leave(db, emp, "personal", date(2025, 12, 30), date(2026, 1, 2), 32)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # 整單 32h 計入 → 32/8 × 500 = 2000.00 → -2000.00
    assert res.personal_leave == Decimal("-2000.00")

    meta = res.calc_meta
    # calc_meta 也應記錄完整 32h（原始時數）
    assert meta["personal_hours"] == 32.0


# --------------------------------------------------------------------------- #
# item 4：費率 fallback（無 active BonusConfig 但有資料 → 用預設值扣款）      #
# --------------------------------------------------------------------------- #
def test_no_config_with_data_uses_default_rates(test_db_session):
    """無 active BonusConfig 但有遲到/事假/病假資料 → 使用 default 費率扣款。

    補強 test_no_config_zero 的盲點（那個測試因無資料，fallback 與 0 無法區分）。
    預設費率：遲到/未打卡=50，事假=500，病假=500。
    """
    db = test_db_session
    # 確認無 active BonusConfig（不加 cfg）
    cycle = _mk_cycle(db, 114)
    emp = _mk_employee(db, "E_NOFG_DATA", "無設定有資料")
    # 遲到 2 次 → default 50 × 2 = -100
    _mk_attendance(db, emp, date(2025, 3, 1), is_late=True)
    _mk_attendance(db, emp, date(2025, 3, 2), is_late=True)
    # 事假 1 天（8h）→ default 500 × 1 = -500
    _mk_leave(db, emp, "personal", date(2025, 6, 1), date(2025, 6, 1), 8)
    # 病假 0.5 天（4h）→ default 500 × 0.5 = -250
    _mk_leave(db, emp, "sick", date(2025, 7, 1), date(2025, 7, 1), 4)
    db.commit()

    res = ad.derive_attendance_deductions(db, cycle, emp)
    # fallback 費率：遲到 50、事假 500、病假 500
    assert res.late == Decimal("-100.00"), "遲到應用 default 50/次"
    assert res.personal_leave == Decimal("-500.00"), "事假應用 default 500/天"
    assert res.sick_leave == Decimal("-250.00"), "病假應用 default 500/天（按比例）"

    meta = res.calc_meta
    assert meta["late_rate"] == 50.0
    assert meta["personal_rate"] == 500.0
    assert meta["sick_rate"] == 500.0
