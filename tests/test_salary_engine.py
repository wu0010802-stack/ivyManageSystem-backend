"""薪資引擎核心邏輯單元測試"""
import pytest
from datetime import date, datetime, time
from unittest.mock import MagicMock
from services.salary_engine import SalaryEngine, SalaryBreakdown, _compute_hourly_daily_hours, get_working_days
from services.attendance_parser import AttendanceResult


# ──────────────────────────────────────────────
# 查找函數
# ──────────────────────────────────────────────
class TestPositionGrade:

    def test_teacher_grade_a(self, engine):
        assert engine.get_position_grade('幼兒園教師') == 'A'

    def test_childcare_grade_b(self, engine):
        assert engine.get_position_grade('教保員') == 'B'

    def test_assistant_grade_c(self, engine):
        assert engine.get_position_grade('助理教保員') == 'C'

    def test_unknown_position(self, engine):
        assert engine.get_position_grade('司機') is None


class TestFestivalBonusBase:

    def test_head_teacher_a(self, engine):
        assert engine.get_festival_bonus_base('幼兒園教師', 'head_teacher') == 2000

    def test_head_teacher_c(self, engine):
        assert engine.get_festival_bonus_base('助理教保員', 'head_teacher') == 1500

    def test_assistant_teacher(self, engine):
        assert engine.get_festival_bonus_base('幼兒園教師', 'assistant_teacher') == 1200

    def test_unknown_position_defaults_to_c(self, engine):
        """未知職位預設使用 C 級"""
        result = engine.get_festival_bonus_base('其他', 'head_teacher')
        assert result == 1500  # C 級

    def test_unknown_role(self, engine):
        assert engine.get_festival_bonus_base('幼兒園教師', 'unknown_role') == 0


class TestTargetEnrollment:

    def test_big_class_two_teachers(self, engine):
        assert engine.get_target_enrollment('大班', has_assistant=True) == 27

    def test_big_class_one_teacher(self, engine):
        assert engine.get_target_enrollment('大班', has_assistant=False) == 14

    def test_baby_class_shared(self, engine):
        assert engine.get_target_enrollment('幼幼班', has_assistant=True, is_shared_assistant=True) == 12

    def test_unknown_grade(self, engine):
        assert engine.get_target_enrollment('未知班', has_assistant=True) == 0


class TestSupervisorDividend:

    def test_principal(self, engine):
        assert engine.get_supervisor_dividend('園長') == 5000

    def test_director(self, engine):
        assert engine.get_supervisor_dividend('主任') == 4000

    def test_vice_leader(self, engine):
        assert engine.get_supervisor_dividend('副組長') == 1500

    def test_non_supervisor(self, engine):
        assert engine.get_supervisor_dividend('幼兒園教師') == 0

    def test_position_priority(self, engine):
        """position 參數優先於 title"""
        assert engine.get_supervisor_dividend('幼兒園教師', position='園長') == 5000


# ──────────────────────────────────────────────
# 節慶獎金資格
# ──────────────────────────────────────────────
class TestFestivalBonusEligibility:

    def test_eligible_after_3_months(self, engine):
        assert engine.is_eligible_for_festival_bonus(
            hire_date='2025-01-01',
            reference_date='2025-04-01'
        ) is True

    def test_not_eligible_before_3_months(self, engine):
        assert engine.is_eligible_for_festival_bonus(
            hire_date='2025-01-15',
            reference_date='2025-04-01'
        ) is False

    def test_none_hire_date_defaults_eligible(self, engine):
        assert engine.is_eligible_for_festival_bonus(None) is True

    def test_invalid_date_string_defaults_eligible(self, engine):
        assert engine.is_eligible_for_festival_bonus('not-a-date') is True

    def test_date_object(self, engine):
        assert engine.is_eligible_for_festival_bonus(
            hire_date=date(2025, 1, 1),
            reference_date=date(2025, 6, 1)
        ) is True


# ──────────────────────────────────────────────
# 考勤扣款
# ──────────────────────────────────────────────
class TestAttendanceDeduction:

    def _make_attendance(self, **kwargs):
        defaults = dict(
            employee_name='測試',
            total_days=22, normal_days=20,
            late_count=0, early_leave_count=0,
            missing_punch_in_count=0, missing_punch_out_count=0,
            total_late_minutes=0, total_early_minutes=0,
            details=[]
        )
        defaults.update(kwargs)
        return AttendanceResult(**defaults)

    def test_no_deduction_perfect_attendance(self, engine):
        """全勤無扣款"""
        att = self._make_attendance()
        result = engine.calculate_attendance_deduction(att, daily_salary=1000, base_salary=30000)
        assert result['late_deduction'] == 0
        assert result['early_leave_deduction'] == 0
        assert result['missing_punch_deduction'] == 0

    def test_late_per_minute(self, engine):
        """遲到按分鐘比例扣款（中間值保留浮點，不提前 round）"""
        att = self._make_attendance(late_count=1, total_late_minutes=30)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[30]
        )
        per_minute = 30000 / 14400
        assert result['late_deduction'] == 30 * per_minute  # 62.5，不提前舍入

    def test_late_over_120_min_per_minute(self, engine):
        """遲到超過 120 分鐘仍按實際分鐘比例扣款（依勞基法，不得溢扣）"""
        att = self._make_attendance(late_count=1, total_late_minutes=150)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[150]
        )
        per_minute = 30000 / 14400
        assert result['late_deduction'] == 150 * per_minute  # 312.5，不提前舍入
        assert 'auto_leave_count' not in result
        assert 'auto_leave_deduction' not in result

    def test_mixed_late_details(self, engine):
        """混合：一次正常遲到 + 一次超過 120 分鐘，全部按分鐘比例合計"""
        att = self._make_attendance(late_count=2, total_late_minutes=180)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[30, 150]
        )
        per_minute = 30000 / 14400
        assert result['late_deduction'] == round(180 * per_minute)
        assert 'auto_leave_count' not in result
        assert 'auto_leave_deduction' not in result

    def test_early_leave(self, engine):
        """早退扣款"""
        att = self._make_attendance(early_leave_count=1, total_early_minutes=20)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000
        )
        per_minute = 30000 / 14400
        assert result['early_leave_deduction'] == 20 * per_minute  # 41.666...，不提前舍入

    def test_missing_punch_no_deduction(self, engine):
        """缺卡不扣款，僅記錄"""
        att = self._make_attendance(missing_punch_in_count=2, missing_punch_out_count=1)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000
        )
        assert result['missing_punch_deduction'] == 0
        assert result['missing_punch_count'] == 3

    def test_zero_base_salary(self, engine):
        """底薪為 0（時薪制）不會除以零"""
        att = self._make_attendance(late_count=1, total_late_minutes=10)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=0, base_salary=0, late_details=[10]
        )
        # per_minute_rate = 1 (fallback)
        assert result['late_deduction'] == round(10 * 1)


# ──────────────────────────────────────────────
# 節慶獎金 V2
# ──────────────────────────────────────────────
class TestFestivalBonusV2:

    def test_head_teacher_exact_target(self, engine):
        """班導剛好達到節慶目標人數 → ratio=1.0，超額獎金另計"""
        result = engine.calculate_festival_bonus_v2(
            position='幼兒園教師', role='head_teacher',
            grade_name='大班', current_enrollment=27,
            has_assistant=True
        )
        assert result['festival_bonus'] == 2000
        assert result['ratio'] == 1.0
        assert result['target'] == 27  # 節慶目標
        assert result['overtime_target'] == 25  # 超額目標較低
        # 27 > 25，已超額 2 人 × 400
        assert result['overtime_bonus'] == 2 * 400

    def test_head_teacher_over_target(self, engine):
        """班導超過目標人數 → 有超額獎金"""
        result = engine.calculate_festival_bonus_v2(
            position='幼兒園教師', role='head_teacher',
            grade_name='大班', current_enrollment=30,
            has_assistant=True
        )
        assert result['festival_bonus'] == round(2000 * (30 / 27))
        # 超額目標: 25, 超額人數: 30-25=5, 每人 400
        assert result['overtime_bonus'] == 5 * 400

    def test_assistant_teacher(self, engine):
        """副班導計算"""
        result = engine.calculate_festival_bonus_v2(
            position='教保員', role='assistant_teacher',
            grade_name='中班', current_enrollment=25,
            has_assistant=True
        )
        assert result['base_amount'] == 1200
        assert result['target'] == 25

    def test_art_teacher_uses_shared(self, engine):
        """美師使用 shared_assistant 目標"""
        result = engine.calculate_festival_bonus_v2(
            position='教保員', role='art_teacher',
            grade_name='大班', current_enrollment=20,
            has_assistant=True
        )
        assert result['target'] == 20  # shared_assistant 目標

    def test_zero_target(self, engine):
        """目標為 0 不會除以零"""
        result = engine.calculate_festival_bonus_v2(
            position='幼兒園教師', role='head_teacher',
            grade_name='未知班', current_enrollment=10,
            has_assistant=True
        )
        assert result['festival_bonus'] == 0
        assert result['ratio'] == 0

    def test_below_target(self, engine):
        """未達標，節慶獎金按比例減少"""
        result = engine.calculate_festival_bonus_v2(
            position='幼兒園教師', role='head_teacher',
            grade_name='小班', current_enrollment=10,
            has_assistant=True
        )
        # target=23, ratio=10/23
        expected = round(2000 * (10 / 23))
        assert result['festival_bonus'] == expected
        assert result['overtime_bonus'] == 0  # 未達超額目標


# ──────────────────────────────────────────────
# 超額獎金
# ──────────────────────────────────────────────
class TestOvertimeBonus:

    def test_no_excess(self, engine):
        """未超額，獎金為 0"""
        result = engine.calculate_overtime_bonus(
            role='head_teacher', grade_name='大班',
            current_enrollment=20, has_assistant=True
        )
        assert result['overtime_bonus'] == 0
        assert result['overtime_count'] == 0

    def test_with_excess(self, engine):
        """超額計算"""
        result = engine.calculate_overtime_bonus(
            role='head_teacher', grade_name='大班',
            current_enrollment=30, has_assistant=True
        )
        # overtime_target=25, excess=5, per_person=400
        assert result['overtime_count'] == 5
        assert result['overtime_bonus'] == 2000

    def test_baby_class_higher_rate(self, engine):
        """幼幼班超額金額較高"""
        result = engine.calculate_overtime_bonus(
            role='head_teacher', grade_name='幼幼班',
            current_enrollment=20, has_assistant=True
        )
        # overtime_target=14, excess=6, per_person=450
        assert result['per_person'] == 450
        assert result['overtime_bonus'] == 6 * 450


# ──────────────────────────────────────────────
# calculate_salary 整合測試
# ──────────────────────────────────────────────
class TestCalculateSalary:

    def test_regular_employee_basic(self, engine, sample_employee):
        """正職員工基本薪資計算"""
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1
        )
        assert breakdown.base_salary == 30000
        assert breakdown.teacher_allowance == 2000
        assert breakdown.meal_allowance == 2400
        # gross = base + teacher + meal = 34400 (無獎金無加班費)
        assert breakdown.gross_salary == 34400
        # 有保險扣款
        assert breakdown.labor_insurance > 0
        assert breakdown.health_insurance > 0
        assert breakdown.net_salary == breakdown.gross_salary - breakdown.total_deduction

    def test_hourly_employee(self, engine):
        """時薪制員工"""
        emp = {
            'employee_id': 'H001', 'name': '時薪員工',
            'employee_type': 'hourly',
            'hourly_rate': 200, 'work_hours': 80,
            'base_salary': 0,
            'supervisor_allowance': 0, 'teacher_allowance': 0,
            'meal_allowance': 0, 'transportation_allowance': 0,
            'other_allowance': 0,
        }
        breakdown = engine.calculate_salary(emp, 2026, 1)
        assert breakdown.hourly_total == 200 * 80
        assert breakdown.gross_salary == 16000

    def test_with_attendance_deduction(self, engine, sample_employee, sample_attendance):
        """含考勤扣款"""
        sample_employee['_late_details'] = [20, 25]
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            attendance=sample_attendance
        )
        assert breakdown.late_deduction > 0
        assert breakdown.early_leave_deduction > 0
        assert breakdown.late_count == 2
        assert breakdown.early_leave_count == 1
        assert breakdown.total_deduction > 0

    def test_with_classroom_context(self, engine, sample_employee, sample_classroom_context):
        """含節慶獎金（班級上下文）"""
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,  # 6月為發放月
            classroom_context=sample_classroom_context
        )
        assert breakdown.festival_bonus > 0

    def test_with_leave_deduction(self, engine, sample_employee):
        """含請假扣款"""
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            leave_deduction=1000
        )
        assert breakdown.leave_deduction == 1000
        assert breakdown.total_deduction >= 1000

    def test_meeting_overtime_pay(self, engine, sample_employee):
        """園務會議加班費"""
        meeting = {'attended': 2, 'absent': 1, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            meeting_context=meeting
        )
        assert breakdown.meeting_overtime_pay == 2 * 200  # 17:00 下班 → 200/次
        assert breakdown.meeting_attended == 2
        assert breakdown.meeting_absent == 1

    def test_meeting_6pm_rate(self, engine, sample_employee):
        """6 點下班者園務會議加班費較低"""
        meeting = {'attended': 1, 'absent': 0, 'work_end_time': '18:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            meeting_context=meeting
        )
        assert breakdown.meeting_overtime_pay == 100  # 18:00 下班 → 100/次

    def test_net_salary_formula(self, engine, sample_employee, sample_attendance):
        """net_salary = gross_salary - total_deduction"""
        sample_employee['_late_details'] = [10]
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            attendance=sample_attendance,
            leave_deduction=500
        )
        assert breakdown.net_salary == breakdown.gross_salary - breakdown.total_deduction

    def test_no_position_no_festival_bonus(self, engine, sample_employee, sample_classroom_context):
        """沒有 position 的員工不發節慶獎金"""
        sample_employee['position'] = ''
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            classroom_context=sample_classroom_context
        )
        assert breakdown.festival_bonus == 0


# ──────────────────────────────────────────────
# 空職稱 / DB bonus 為 NULL — TypeError 回歸測試
# ──────────────────────────────────────────────
class TestEmptyPositionFestivalBonus:
    """
    回歸測試：職稱為空值或 DB 節慶獎金設定為 NULL 時，
    不應拋出 TypeError 導致全校薪資卡住。

    根本原因：
      _bonus_base 從 DB 載入後，若欄位為 NULL 則值為 None。
      dict.get(grade, 0) 在 key 存在但值為 None 時回傳 None（不用預設 0），
      後續 None * ratio 觸發 TypeError，process_salary_calculation 的
      raise e 導致整批薪資中斷。
    """

    def test_null_c_grade_bonus_does_not_raise_type_error(self, engine):
        """
        DB 節慶獎金 C 級基數為 NULL 時，空職稱（grade 預設 C）
        不應拋出 TypeError。

        修復前：calculate_festival_bonus_v2 → get_festival_bonus_base
                回傳 None → festival_bonus = None * ratio → TypeError
        """
        # 模擬從 DB 載入了 NULL 的 C 級基數
        engine._bonus_base['head_teacher']['C'] = None

        result = engine.calculate_festival_bonus_v2(
            position='',          # 空職稱 → grade 預設 C
            role='head_teacher',
            grade_name='大班',
            current_enrollment=20,
            has_assistant=True,
        )
        assert result['festival_bonus'] == 0

    def test_null_bonus_base_with_c_grade_position(self, engine):
        """
        DB C 級基數為 NULL + C 級職稱（助理教保員）
        → festival_bonus 應為 0，不拋 TypeError。
        """
        engine._bonus_base['head_teacher']['C'] = None
        engine._bonus_base['assistant_teacher']['C'] = None

        result = engine.calculate_festival_bonus_v2(
            position='助理教保員',   # grade C
            role='head_teacher',
            grade_name='大班',
            current_enrollment=20,
            has_assistant=True,
        )
        assert result['festival_bonus'] == 0

    def test_empty_position_in_distribution_month_no_crash(self, engine, sample_employee, sample_classroom_context):
        """
        節慶發放月（6月）空職稱員工薪資計算不應 crash，festival_bonus 應為 0。
        與 test_no_position_no_festival_bonus 的差異：使用發放月，
        確保保護不是靠「非發放月歸零」掩蓋。
        """
        sample_employee['position'] = ''
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,   # 發放月
            classroom_context=sample_classroom_context,
        )
        assert breakdown.festival_bonus == 0


# ──────────────────────────────────────────────
# 節慶獎金季度發放
# ──────────────────────────────────────────────
class TestBonusDistributionMonth:

    def test_distribution_months(self, engine):
        """發放月份（2, 6, 9, 12）回傳 True"""
        for m in [2, 6, 9, 12]:
            assert engine.get_bonus_distribution_month(m) is True

    def test_non_distribution_months(self, engine):
        """非發放月份回傳 False"""
        for m in [1, 3, 4, 5, 7, 8, 10, 11]:
            assert engine.get_bonus_distribution_month(m) is False

    def test_salary_no_bonus_in_non_distribution_month(self, engine, sample_employee, sample_classroom_context):
        """非發放月份，節慶獎金應為 0"""
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=3,
            classroom_context=sample_classroom_context
        )
        assert breakdown.festival_bonus == 0

    def test_salary_has_bonus_in_distribution_month(self, engine, sample_employee, sample_classroom_context):
        """發放月份，節慶獎金應 > 0"""
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=6,
            classroom_context=sample_classroom_context
        )
        assert breakdown.festival_bonus > 0


# ──────────────────────────────────────────────
# 園務會議缺席扣款 — 跨月補查回歸測試
# ──────────────────────────────────────────────
class TestMeetingAbsencePeriod:
    """
    回歸測試：確保非發放月的缺席罰金不會被「吃掉」，
    而是在下一個獎金發放月一次扣除（accumulated absent_period）。

    正確行為：
    - 非發放月（1,3,4,5,7,8,10,11）：缺席罰金 = 0（當月不扣，留待發放月補算）
    - 發放月（2,6,9,12）：使用 absent_period（含前幾個非發放月的累計缺席）計算
    """

    def test_period_start_february(self, engine):
        """2月：起算日為1月1日（補查1月缺席）"""
        assert engine.get_meeting_deduction_period_start(2026, 2) == date(2026, 1, 1)

    def test_period_start_june(self, engine):
        """6月：起算日為3月1日（補查3–5月缺席）"""
        assert engine.get_meeting_deduction_period_start(2026, 6) == date(2026, 3, 1)

    def test_period_start_september(self, engine):
        """9月：起算日為7月1日（補查7–8月缺席）"""
        assert engine.get_meeting_deduction_period_start(2026, 9) == date(2026, 7, 1)

    def test_period_start_december(self, engine):
        """12月：起算日為10月1日（補查10–11月缺席）"""
        assert engine.get_meeting_deduction_period_start(2026, 12) == date(2026, 10, 1)

    def test_non_bonus_month_returns_none(self, engine):
        """非發放月回傳 None（不補查）"""
        for month in [1, 3, 4, 5, 7, 8, 10, 11]:
            assert engine.get_meeting_deduction_period_start(2026, month) is None

    def test_no_deduction_in_non_bonus_month(self, engine, sample_employee):
        """非發放月（3月）：即使缺席，meeting_absence_deduction 為 0"""
        meeting = {'attended': 0, 'absent': 2, 'absent_period': 2, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=3,
            meeting_context=meeting
        )
        assert breakdown.meeting_absence_deduction == 0

    def test_prior_months_accumulated_in_bonus_month(self, engine, sample_employee):
        """
        發放月（6月）：3月缺席1次 + 4月缺席1次，
        6月本月未缺席 → absent_period=2 → 扣 200 元
        """
        meeting = {'attended': 1, 'absent': 0, 'absent_period': 2, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=6,
            meeting_context=meeting
        )
        assert breakdown.meeting_absence_deduction == 2 * 100

    def test_combined_current_and_prior_absences(self, engine, sample_employee):
        """
        發放月（6月）：當月（6月）缺席1次 + 前幾月累計缺席2次 = 3次 → 扣 300 元
        """
        meeting = {'attended': 0, 'absent': 1, 'absent_period': 3, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=6,
            meeting_context=meeting
        )
        assert breakdown.meeting_absence_deduction == 3 * 100

    def test_fallback_to_current_absent_when_no_period(self, engine, sample_employee):
        """
        absent_period 未提供時退回使用當月 absent（向下相容）
        """
        meeting = {'attended': 0, 'absent': 3, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=6,
            meeting_context=meeting
        )
        assert breakdown.meeting_absence_deduction == 3 * 100

    def test_full_year_8_non_bonus_months_all_covered(self, engine, sample_employee):
        """
        整年 8 個非發放月的缺席應被各發放月完整覆蓋，不遺漏：
        - Jan(1次) → 由 Feb 補扣
        - Mar(1次) + Apr(1次) + May(1次) → 由 Jun 補扣（共3次）
        - Jul(1次) + Aug(1次) → 由 Sep 補扣（共2次）
        - Oct(1次) + Nov(1次) → 由 Dec 補扣（共2次）
        """
        # 驗證 Feb 補扣 Jan（1次，本月 Feb 自己出席）
        meeting_feb = {'attended': 1, 'absent': 0, 'absent_period': 1}
        bd_feb = engine.calculate_salary(sample_employee, 2026, 2, meeting_context=meeting_feb)
        assert bd_feb.meeting_absence_deduction == 1 * 100

        # 驗證 Jun 補扣 Mar+Apr+May（共3次，Jun 本月自己也缺1次）
        meeting_jun = {'attended': 0, 'absent': 1, 'absent_period': 4}
        bd_jun = engine.calculate_salary(sample_employee, 2026, 6, meeting_context=meeting_jun)
        assert bd_jun.meeting_absence_deduction == 4 * 100

        # 驗證 Sep 補扣 Jul+Aug（共2次，Sep 本月全勤）
        meeting_sep = {'attended': 1, 'absent': 0, 'absent_period': 2}
        bd_sep = engine.calculate_salary(sample_employee, 2026, 9, meeting_context=meeting_sep)
        assert bd_sep.meeting_absence_deduction == 2 * 100

        # 驗證 Dec 補扣 Oct+Nov（共2次，Dec 本月也缺1次）
        meeting_dec = {'attended': 0, 'absent': 1, 'absent_period': 3}
        bd_dec = engine.calculate_salary(sample_employee, 2026, 12, meeting_context=meeting_dec)
        assert bd_dec.meeting_absence_deduction == 3 * 100


# ──────────────────────────────────────────────
# 月中入職底薪折算 + 加班費時薪保護
# ──────────────────────────────────────────────
class TestMidMonthHireSalaryProration:
    """
    月中入職（hire_date 在計算月份的 2 日以後）應按在職天數比例折算底薪。

    Bug 場景「雙重縮水」：
      新人 1 月 16 日入職，契約月薪 30,000。
      Step1（正確）：本月底薪折算 → 30,000 × 16/31 ≈ 15,484
      Step2（Bug）：若以本月折算後底薪計算加班時薪
                    → 15,484 / 30 / 8 = 64.5 NTD/hr（遠低於勞基法最低工資）
      Step2（Fix）：加班時薪應以「完整契約月薪」計算
                    → 30,000 / 30 / 8 = 125 NTD/hr

    修復設計：
    - _prorate_base_salary()  僅折算 breakdown.base_salary（顯示用）
    - calculate_salary()       考勤扣款 base_sal 仍使用 employee['base_salary']（契約月薪）
    - process_salary_calculation() 加班費取自 DB 已儲存的 o.overtime_pay
                                    （建立時已以 emp.base_salary 計算，不受折算影響）
    """

    # ── _prorate_base_salary 單元測試 ────────────────

    def test_mid_month_hire_31_day_month(self, engine):
        """1月(31天)16日入職 → 在職16天 → 30000×16/31 ≈ 15483.87（保留浮點，不提前舍入）"""
        result = engine._prorate_base_salary(30000, '2026-01-16', 2026, 1)
        assert result == 30000 * 16 / 31

    def test_mid_month_hire_30_day_month(self, engine):
        """6月(30天)16日入職 → 在職15天 → 30000×15/30 = 15000"""
        result = engine._prorate_base_salary(30000, '2026-06-16', 2026, 6)
        assert result == 15000

    def test_first_day_hire_no_proration(self, engine):
        """月初（1日）入職 → 全額，不折算"""
        result = engine._prorate_base_salary(30000, '2026-01-01', 2026, 1)
        assert result == 30000

    def test_prior_month_hire_no_proration(self, engine):
        """上月入職 → 本月全月在職，不折算"""
        result = engine._prorate_base_salary(30000, '2025-12-15', 2026, 1)
        assert result == 30000

    def test_last_day_hire_one_day(self, engine):
        """最後一天（31日）入職 → 在職1天 → 30000×1/31 ≈ 967.74（保留浮點，不提前舍入）"""
        result = engine._prorate_base_salary(30000, '2026-01-31', 2026, 1)
        assert result == 30000 * 1 / 31

    def test_no_hire_date_no_proration(self, engine):
        """無到職日 → 不折算，回傳完整月薪"""
        result = engine._prorate_base_salary(30000, None, 2026, 1)
        assert result == 30000

    def test_date_object_input(self, engine):
        """支援 date 物件輸入（非字串）"""
        result = engine._prorate_base_salary(30000, date(2026, 1, 16), 2026, 1)
        assert result == 30000 * 16 / 31

    # ── calculate_salary 整合測試 ────────────────────

    def test_calculate_salary_prorates_base_for_mid_month_hire(self, engine):
        """calculate_salary 應對月中入職者折算 breakdown.base_salary"""
        employee = {
            'employee_id': 'E999', 'name': '月中新人',
            'title': '', 'position': '', 'employee_type': 'regular',
            'base_salary': 30000, 'hourly_rate': 0,
            'supervisor_allowance': 0, 'teacher_allowance': 0,
            'meal_allowance': 0, 'transportation_allowance': 0,
            'other_allowance': 0,
            'insurance_salary': 30000, 'dependents': 0,
            'hire_date': '2026-01-16',   # 1月16日入職，當月31天
        }
        breakdown = engine.calculate_salary(employee=employee, year=2026, month=1)
        expected_base = 30000 * 16 / 31   # 在職16天/共31天，保留浮點
        assert breakdown.base_salary == expected_base

    def test_full_month_employee_no_proration(self, engine, sample_employee):
        """上月已入職的員工，本月 breakdown.base_salary 不折算（全額）"""
        # sample_employee hire_date = '2025-01-01'（早於計算月份 2026/1）
        breakdown = engine.calculate_salary(employee=sample_employee, year=2026, month=1)
        assert breakdown.base_salary == 30000

    # ── 雙重縮水防護（加班費時薪保護）─────────────────

    def test_overtime_rate_must_use_contracted_not_prorated(self):
        """
        Bug 復現：加班費時薪應以「契約月薪」計算，而非「本月折算後底薪」。

        雙重縮水場景：
          契約月薪 30,000；月中入職 → 本月實領 15,000（首次縮水，正確）
          誤用折算後底薪計算時薪：15,000/30/8 = 62.5 NTD/hr（二次縮水，違法）
          正確做法應用契約月薪：30,000/30/8 = 125 NTD/hr

        修復驗證：以契約月薪計算的加班費，必須為折算後底薪的 2 倍。
        """
        from api.overtimes import calculate_overtime_pay

        contracted = 30000
        prorated = 15000   # 月中入職後本月折算後底薪（15天/30天月份）

        correct_pay = calculate_overtime_pay(contracted, 2, 'weekday')   # 契約月薪（正確）
        wrong_pay = calculate_overtime_pay(prorated, 2, 'weekday')       # 折算底薪（雙重縮水）

        # 契約月薪是折算底薪的 2 倍，加班費應大幅高於錯誤值
        assert correct_pay > wrong_pay
        # 具體驗證：30000/30/8 * 2hr * 1.34倍率 = 335
        assert correct_pay == round(30000 / 30 / 8 * 2 * 1.34)
        # 錯誤值：15000/30/8 * 2hr * 1.34倍率 = 168（遠低於法定最低時薪）
        assert wrong_pay == round(15000 / 30 / 8 * 2 * 1.34)


class TestComputeHourlyDailyHours:
    """回歸測試：時薪制單日工時時空穿越防護

    Bug 情境：
    - 員工在排班下班時間（17:00）之後才到班（如 18:00），且忘記打下班卡
    - 系統補填 17:00 為下班時間 → effective_out(17:00) ≤ punch_in(18:00)
    - 若 guard 只在 else 分支，diff 為負數 → 負薪資或靜默歸零
    - 若 punch_out 被明確設定為早於 punch_in（管理員誤植），同樣缺少防護
    """

    WORK_END = time(17, 0)

    def test_late_arrival_after_work_end_no_punch_out_returns_zero(self):
        """18:00 才上班，缺下班打卡，補填 17:00 → 時空穿越 → 0.0"""
        punch_in = datetime(2026, 1, 15, 18, 0)
        assert _compute_hourly_daily_hours(punch_in, None, self.WORK_END) == 0.0

    def test_exact_work_end_arrival_no_punch_out_returns_zero(self):
        """剛好 17:00 上班，缺下班打卡，補填 17:00 → 上下班相同 → 0.0"""
        punch_in = datetime(2026, 1, 15, 17, 0)
        assert _compute_hourly_daily_hours(punch_in, None, self.WORK_END) == 0.0

    def test_inverted_explicit_punch_out_returns_zero(self):
        """下班打卡 16:00 早於上班打卡 17:30（管理員誤植）→ 0.0，不得為負"""
        punch_in = datetime(2026, 1, 15, 17, 30)
        punch_out = datetime(2026, 1, 15, 16, 0)
        assert _compute_hourly_daily_hours(punch_in, punch_out, self.WORK_END) == 0.0

    def test_normal_day_no_punch_out_fills_default(self):
        """正常：08:00 上班，缺下班打卡，補填 17:00 → 8h（扣午休 1h）"""
        punch_in = datetime(2026, 1, 15, 8, 0)
        assert _compute_hourly_daily_hours(punch_in, None, self.WORK_END) == 8.0

    def test_normal_with_both_punches(self):
        """09:00–18:00，雙打卡 → 8h（扣午休 1h）"""
        punch_in = datetime(2026, 1, 15, 9, 0)
        punch_out = datetime(2026, 1, 15, 18, 0)
        assert _compute_hourly_daily_hours(punch_in, punch_out, self.WORK_END) == 8.0

    def test_afternoon_only_no_lunch_overlap(self):
        """13:00–17:00，不跨午休 → 4h"""
        punch_in = datetime(2026, 1, 15, 13, 0)
        punch_out = datetime(2026, 1, 15, 17, 0)
        assert _compute_hourly_daily_hours(punch_in, punch_out, self.WORK_END) == 4.0


class TestComputeHourlyDailyHoursOvernight:
    """回歸測試：時薪制跨夜班工時計算

    Bug 情境：
    - 跨夜班員工（如 18:00 上班，隔日 02:00 下班），排班下班時間 work_end_t = time(2, 0)
    - 缺下班打卡時，datetime.combine(punch_in.date(), time(2,0)) = 當日 02:00 < 18:00
    - 舊邏輯：effective_out <= punch_in → return 0.0（工時空白）
    - 預期：應補填「隔日 02:00」，計算得 8h
    """

    OVERNIGHT_END = time(2, 0)  # 跨夜班排班下班 02:00

    def test_overnight_both_punches(self):
        """18:00 上班，隔日 02:00 下班，雙打卡 → 8h（不跨午休）"""
        punch_in = datetime(2026, 1, 14, 18, 0)
        punch_out = datetime(2026, 1, 15, 2, 0)
        assert _compute_hourly_daily_hours(punch_in, punch_out, self.OVERNIGHT_END) == 8.0

    def test_overnight_missing_punch_out_fills_next_day(self):
        """18:00 上班，缺下班打卡，work_end_t=02:00（隔日）→ 補填隔日 02:00 → 8h"""
        punch_in = datetime(2026, 1, 14, 18, 0)
        assert _compute_hourly_daily_hours(punch_in, None, self.OVERNIGHT_END) == 8.0

    def test_late_after_normal_end_still_zero(self):
        """18:00 才上班，work_end_t=17:00（正常日班），缺下班打卡 → 補填當日 17:00 → 0h（不視為跨夜）"""
        punch_in = datetime(2026, 1, 14, 18, 0)
        assert _compute_hourly_daily_hours(punch_in, None, time(17, 0)) == 0.0

    def test_overnight_partial_punch_out_early(self):
        """18:00 上班，隔日 01:00 提早下班（排班 02:00）→ 7h"""
        punch_in = datetime(2026, 1, 14, 18, 0)
        punch_out = datetime(2026, 1, 15, 1, 0)
        assert _compute_hourly_daily_hours(punch_in, punch_out, self.OVERNIGHT_END) == 7.0


# ──────────────────────────────────────────────
# 會議缺席扣款應從 festival_bonus 扣，不進 total_deduction
# ──────────────────────────────────────────────
class TestMeetingAbsenceDeductFromFestivalBonus:
    """
    回歸測試：會議缺席扣款應從 festival_bonus 直接扣減，不進入 total_deduction。

    Bug 描述：
      meeting_absence_deduction 被錯誤加入 total_deduction，
      導致罰款從月薪（net_salary）扣除，
      但節慶獎金仍全額另行轉帳，罰款實際上形同虛設。

    正確行為（依 CLAUDE.md 規範）：
      festival_bonus = max(0, original_festival_bonus - meeting_absence_deduction)
      total_deduction 不含 meeting_absence_deduction
    """

    def test_absence_deducted_from_festival_bonus_not_total_deduction(
        self, engine, sample_employee, sample_classroom_context
    ):
        """發放月缺席 2 次：festival_bonus 應減少 200，total_deduction 不受影響"""
        baseline = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,
            classroom_context=sample_classroom_context,
        )

        meeting = {'attended': 1, 'absent': 2, 'absent_period': 2, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,
            classroom_context=sample_classroom_context,
            meeting_context=meeting,
        )

        assert breakdown.meeting_absence_deduction == 200
        # festival_bonus 應被扣減 200
        assert breakdown.festival_bonus == baseline.festival_bonus - 200
        # total_deduction 不含 meeting_absence_deduction
        assert breakdown.total_deduction == baseline.total_deduction

    def test_net_salary_unaffected_by_meeting_absence(
        self, engine, sample_employee, sample_classroom_context
    ):
        """會議缺席扣款不應影響月薪 net_salary，只影響節慶獎金轉帳金額"""
        baseline = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,
            classroom_context=sample_classroom_context,
        )

        meeting = {'attended': 0, 'absent': 3, 'absent_period': 3, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,
            classroom_context=sample_classroom_context,
            meeting_context=meeting,
        )

        assert breakdown.net_salary == baseline.net_salary

    def test_festival_bonus_floor_at_zero(self, engine, sample_employee):
        """缺席扣款超過 festival_bonus 時，festival_bonus 應為 0，不為負值"""
        low_context = {
            'role': 'head_teacher',
            'grade_name': '大班',
            'current_enrollment': 1,  # 在籍極低 → festival_bonus 極小
            'has_assistant': True,
            'is_shared_assistant': False,
        }
        meeting = {'attended': 0, 'absent': 50, 'absent_period': 50, 'work_end_time': '17:00'}
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=6,
            classroom_context=low_context,
            meeting_context=meeting,
        )

        assert breakdown.festival_bonus >= 0


# ──────────────────────────────────────────────
# 加班費路徑一致性（overtime_work_pay 應在 calculate_salary 內計入）
# ──────────────────────────────────────────────
class TestOvertimeWorkPayInCalculateSalary:

    def test_overtime_work_pay_included_in_gross_salary(self, engine, sample_employee):
        """overtime_work_pay 傳入 calculate_salary 後應計入 gross_salary"""
        breakdown = engine.calculate_salary(
            employee=sample_employee,
            year=2026, month=1,
            overtime_work_pay=1200,
        )
        assert breakdown.overtime_work_pay == 1200
        assert breakdown.gross_salary == (
            sample_employee['base_salary'] +
            sample_employee['teacher_allowance'] +
            sample_employee['meal_allowance'] +
            1200
        )

    def test_overtime_work_pay_included_in_net_salary(self, engine, sample_employee):
        """overtime_work_pay 加入後 net_salary 應比不加時多出同等金額"""
        base = engine.calculate_salary(employee=sample_employee, year=2026, month=1)
        with_ot = engine.calculate_salary(
            employee=sample_employee, year=2026, month=1, overtime_work_pay=1200
        )
        assert with_ot.net_salary == base.net_salary + 1200

    def test_zero_overtime_work_pay_is_backward_compatible(self, engine, sample_employee):
        """overtime_work_pay=0（預設）不影響原有計算結果"""
        base = engine.calculate_salary(employee=sample_employee, year=2026, month=1)
        explicit_zero = engine.calculate_salary(
            employee=sample_employee, year=2026, month=1, overtime_work_pay=0
        )
        assert base.gross_salary == explicit_zero.gross_salary
        assert base.net_salary == explicit_zero.net_salary


# ──────────────────────────────────────────────
# 曠職偵測：預期上班日計算（_build_expected_workdays）
# ──────────────────────────────────────────────
class TestBuildExpectedWorkdays:
    """
    2026 年 1 月有 22 個平日（週一～週五）：
    1(Thu), 2(Fri), 5-9, 12-16, 19-23, 26-30
    """

    def _workdays(self, **kwargs):
        """呼叫 _build_expected_workdays，固定 today=2026-01-31 排除未來過濾干擾"""
        return SalaryEngine._build_expected_workdays(
            year=2026, month=1,
            holiday_set=set(),
            daily_shift_map={},
            today=date(2026, 1, 31),
            **kwargs,
        )

    def test_no_filters_returns_all_weekdays(self, engine):
        """無入離職限制應回傳當月所有平日（22 天）"""
        result = self._workdays()
        assert len(result) == 22

    def test_hire_date_excludes_days_before_hire(self, engine):
        """2026-01-15 入職：1/1–1/14 不算曠職，共 8 個預期工作日（1/15–1/30 平日）"""
        result = self._workdays(hire_date_raw=date(2026, 1, 15))
        # 1/15(Thu), 16(Fri), 19-23, 26-30 = 12 天
        assert date(2026, 1, 14) not in result
        assert date(2026, 1, 15) in result
        assert len(result) == 12

    def test_resign_date_excludes_days_after_resignation(self, engine):
        """2026-01-15 離職：1/16 起不算曠職，只留 1/1–1/15 的平日"""
        result = self._workdays(resign_date_raw=date(2026, 1, 15))
        # 1/1(Thu), 2(Fri), 5-9, 12-16 不對——15 是 Thu，所以到 15
        # 1(Thu),2(Fri),5,6,7,8,9,12,13,14,15 = 11 天
        assert date(2026, 1, 16) not in result
        assert date(2026, 1, 15) in result  # 離職當天仍算
        assert len(result) == 11

    def test_resign_day_is_included(self, engine):
        """離職當天（2026-01-02 Fri）本身應包含在預期工作日內"""
        result = self._workdays(resign_date_raw=date(2026, 1, 2))
        assert date(2026, 1, 2) in result
        assert date(2026, 1, 5) not in result
        assert len(result) == 2  # 1/1, 1/2

    def test_hire_and_resign_both_applied(self, engine):
        """同時有入職（1/5）與離職（1/16 Fri）：只留 1/5–1/16 的平日 = 10 天"""
        result = self._workdays(
            hire_date_raw=date(2026, 1, 5),
            resign_date_raw=date(2026, 1, 16),
        )
        assert date(2026, 1, 2) not in result   # 入職前
        assert date(2026, 1, 19) not in result  # 離職後
        assert date(2026, 1, 5) in result
        assert date(2026, 1, 16) in result
        assert len(result) == 10  # 5-9, 12-16

    def test_resign_string_date_accepted(self, engine):
        """resign_date_raw 為字串格式時應能正確解析"""
        result = self._workdays(resign_date_raw='2026-01-02')
        assert len(result) == 2

    def test_invalid_month_raises_value_error(self, engine):
        """month=13 應拋出含明確中文說明的 ValueError（由我們的 guard 產生，非 calendar 內部訊息）"""
        with pytest.raises(ValueError, match="month 必須介於"):
            SalaryEngine._build_expected_workdays(
                year=2026, month=13,
                holiday_set=set(), daily_shift_map={},
            )

    def test_month_zero_raises_value_error(self, engine):
        """month=0 同樣應拋出明確 ValueError"""
        with pytest.raises(ValueError, match="month 必須介於"):
            SalaryEngine._build_expected_workdays(
                year=2026, month=0,
                holiday_set=set(), daily_shift_map={},
            )

    def test_valid_boundary_months_do_not_raise(self, engine):
        """month=1 與 month=12 為合法邊界，不應拋出例外"""
        SalaryEngine._build_expected_workdays(
            year=2026, month=1,
            holiday_set=set(), daily_shift_map={},
            today=date(2026, 1, 31),
        )
        SalaryEngine._build_expected_workdays(
            year=2026, month=12,
            holiday_set=set(), daily_shift_map={},
            today=date(2026, 12, 31),
        )


# ──────────────────────────────────────────────
# get_working_days 月份驗證
# ──────────────────────────────────────────────
class TestGetWorkingDaysValidation:

    def _mock_session(self):
        """回傳一個空假日列表的 mock session，避免真實 DB 連線"""
        sess = MagicMock()
        sess.query.return_value.filter.return_value.all.return_value = []
        return sess

    def test_invalid_month_13_raises_value_error(self):
        """month=13 應拋出含明確說明的 ValueError（非 calendar 內部訊息）"""
        with pytest.raises(ValueError, match="month 必須介於"):
            get_working_days(2026, 13)

    def test_month_zero_raises_value_error(self):
        """month=0 應拋出 ValueError"""
        with pytest.raises(ValueError, match="month 必須介於"):
            get_working_days(2026, 0)

    def test_valid_boundary_month_1_returns_int(self):
        """month=1 為合法邊界，應回傳整數工作日數"""
        result = get_working_days(2026, 1, session=self._mock_session())
        assert isinstance(result, int)
        assert result > 0

    def test_valid_boundary_month_12_returns_int(self):
        """month=12 為合法邊界，應回傳整數工作日數"""
        result = get_working_days(2026, 12, session=self._mock_session())
        assert isinstance(result, int)
        assert result > 0


# ──────────────────────────────────────────────
# 浮點舍入：中間值保留精度，最終一次舍入
# ──────────────────────────────────────────────
class TestDeferredRounding:
    """
    場景：2026年1月，員工 1/3 入職（29/31 天），遲到 3 分鐘
      月中折算 raw : 30000 × 29/31 = 28064.516...  → 個別 round → 28065（誤差 +0.484）
      遲到扣款 raw : 3 × (30000/30/8/60) = 6.25    → 個別 round → 6   （誤差 −0.25）
      個別舍入 net  = (28065 + 2000 + 2400) − (758 + 470 + 6) = 31231
      延遲舍入 net  = round(28064.516... + 2000 + 2400 − 758 − 470 − 6.25)
                    = round(31230.266...) = 31230
    """

    def _make_att(self, late_minutes=3):
        return AttendanceResult(
            employee_name='舍入測試',
            total_days=22, normal_days=21,
            late_count=1, early_leave_count=0,
            missing_punch_in_count=0, missing_punch_out_count=0,
            total_late_minutes=late_minutes,
            total_early_minutes=0,
            details=[],
        )

    def test_net_salary_deferred_rounding(self, engine, sample_employee):
        """延遲舍入應得 31230，個別舍入誤計 31231"""
        emp = {**sample_employee, 'hire_date': '2026-01-03'}
        breakdown = engine.calculate_salary(
            employee=emp, year=2026, month=1,
            attendance=self._make_att(late_minutes=3),
        )
        # round(28064.516... + 2000 + 2400 − 758 − 470 − 6.25) = round(31230.266) = 31230
        assert breakdown.net_salary == 31230

    def test_gross_and_total_deduction_are_integers(self, engine, sample_employee):
        """gross_salary 與 total_deduction 最終應為整數（前端顯示不出現小數）"""
        emp = {**sample_employee, 'hire_date': '2026-01-03'}
        breakdown = engine.calculate_salary(
            employee=emp, year=2026, month=1,
            attendance=self._make_att(late_minutes=3),
        )
        assert breakdown.gross_salary == int(breakdown.gross_salary)
        assert breakdown.total_deduction == int(breakdown.total_deduction)

    def test_no_rounding_sources_net_exact(self, engine, sample_employee):
        """無月中入職、無遲到時，net_salary 應為精確整數（無舍入差異）"""
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=3,
        )
        assert breakdown.net_salary == int(breakdown.net_salary)


# ──────────────────────────────────────────────
# bonus_amount 正確賦值
# ──────────────────────────────────────────────
class TestBonusAmount:

    def test_bonus_amount_is_festival_plus_overtime_plus_dividend(self, engine, sample_employee):
        """bonus_amount 應為三項之和：festival_bonus + overtime_bonus + supervisor_dividend"""
        emp = {**sample_employee, 'title': '園長', 'position': '園長'}
        classroom_ctx = {
            'role': 'head_teacher',
            'grade_name': '大班',
            'current_enrollment': 27,
            'has_assistant': True,
            'is_shared_assistant': False,
        }
        breakdown = engine.calculate_salary(
            employee=emp, year=2026, month=2, classroom_context=classroom_ctx
        )
        assert breakdown.bonus_amount == (
            breakdown.festival_bonus + breakdown.overtime_bonus + breakdown.supervisor_dividend
        )

    def test_bonus_separate_includes_supervisor_dividend(self, engine, sample_employee):
        """主管紅利 > 0 時，即使無節慶獎金 bonus_separate 也應為 True"""
        emp = {**sample_employee, 'title': '園長', 'position': '園長'}
        breakdown = engine.calculate_salary(
            employee=emp, year=2026, month=3,  # 3月非節慶發放月
        )
        assert breakdown.supervisor_dividend > 0
        assert breakdown.bonus_separate is True

    def test_bonus_amount_zero_for_no_separate_items(self, engine, sample_employee):
        """一般員工在非發放月：三項皆 0，bonus_amount = 0，bonus_separate = False"""
        breakdown = engine.calculate_salary(
            employee=sample_employee, year=2026, month=3,
        )
        assert breakdown.bonus_amount == 0
        assert breakdown.bonus_separate is False
        assert breakdown.net_salary == breakdown.gross_salary - breakdown.total_deduction
