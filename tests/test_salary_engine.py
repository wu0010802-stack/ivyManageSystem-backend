"""薪資引擎核心邏輯單元測試"""
import pytest
from datetime import date
from services.salary_engine import SalaryEngine, SalaryBreakdown
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
        """遲到按分鐘比例扣款"""
        att = self._make_attendance(late_count=1, total_late_minutes=30)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[30]
        )
        per_minute = 30000 / 14400
        expected = round(30 * per_minute)
        assert result['late_deduction'] == expected
        assert result['auto_leave_count'] == 0

    def test_late_over_120_min_auto_leave(self, engine):
        """遲到超過 120 分鐘轉事假半天"""
        att = self._make_attendance(late_count=1, total_late_minutes=150)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[150]
        )
        assert result['late_deduction'] == 0  # 不扣分鐘費
        assert result['auto_leave_count'] == 1
        assert result['auto_leave_deduction'] == round(1000 * 0.5)

    def test_mixed_late_details(self, engine):
        """混合：一次正常遲到 + 一次超過 120 分鐘"""
        att = self._make_attendance(late_count=2, total_late_minutes=180)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000, late_details=[30, 150]
        )
        per_minute = 30000 / 14400
        assert result['late_deduction'] == round(30 * per_minute)
        assert result['auto_leave_count'] == 1
        assert result['auto_leave_deduction'] == round(1000 * 0.5)

    def test_early_leave(self, engine):
        """早退扣款"""
        att = self._make_attendance(early_leave_count=1, total_early_minutes=20)
        result = engine.calculate_attendance_deduction(
            att, daily_salary=1000, base_salary=30000
        )
        per_minute = 30000 / 14400
        assert result['early_leave_deduction'] == round(20 * per_minute)

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
