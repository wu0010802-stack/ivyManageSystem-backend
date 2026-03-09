"""
薪資計算引擎 - SalaryEngine 類別
"""

import logging
from typing import Dict, List, Optional
from datetime import date, datetime, time, timedelta

from ..insurance_service import InsuranceService, InsuranceCalculation
from ..attendance_parser import AttendanceResult
from .constants import (
    MONTHLY_BASE_DAYS, MAX_DAILY_WORK_HOURS,
    FESTIVAL_BONUS_BASE, TARGET_ENROLLMENT, OVERTIME_TARGET, OVERTIME_BONUS_PER_PERSON,
    SUPERVISOR_DIVIDEND, SUPERVISOR_FESTIVAL_BONUS, OFFICE_FESTIVAL_BONUS_BASE,
    POSITION_GRADE_MAP,
    DEFAULT_LATE_PER_MINUTE, DEFAULT_EARLY_PER_MINUTE, DEFAULT_AUTO_LEAVE_THRESHOLD,
    DEFAULT_MISSING_PUNCH, DEFAULT_MEETING_PAY, DEFAULT_MEETING_PAY_6PM,
    DEFAULT_MEETING_ABSENCE_PENALTY,
)
from .breakdown import SalaryBreakdown
from .hourly import _compute_hourly_daily_hours, _calc_daily_hourly_pay
from .proration import _prorate_for_period, _build_expected_workdays
from .utils import get_bonus_distribution_month, get_meeting_deduction_period_start, _sum_leave_deduction
from . import festival as _festival

logger = logging.getLogger(__name__)


def _get_db_session():
    from models.database import get_session
    return get_session()


class SalaryEngine:
    """薪資計算引擎"""

    # 預設扣款規則
    DEFAULT_LATE_PER_MINUTE = DEFAULT_LATE_PER_MINUTE
    DEFAULT_EARLY_PER_MINUTE = DEFAULT_EARLY_PER_MINUTE
    DEFAULT_AUTO_LEAVE_THRESHOLD = DEFAULT_AUTO_LEAVE_THRESHOLD
    DEFAULT_MISSING_PUNCH = DEFAULT_MISSING_PUNCH
    DEFAULT_MEETING_PAY = DEFAULT_MEETING_PAY
    DEFAULT_MEETING_PAY_6PM = DEFAULT_MEETING_PAY_6PM
    DEFAULT_MEETING_ABSENCE_PENALTY = DEFAULT_MEETING_ABSENCE_PENALTY

    # 節慶獎金職位等級對應
    POSITION_GRADE_MAP = POSITION_GRADE_MAP

    # 節慶獎金基數
    FESTIVAL_BONUS_BASE = FESTIVAL_BONUS_BASE

    # 節慶獎金目標人數
    TARGET_ENROLLMENT = TARGET_ENROLLMENT

    # 超額獎金目標人數
    OVERTIME_TARGET = OVERTIME_TARGET

    # 超額獎金每人金額
    OVERTIME_BONUS_PER_PERSON = OVERTIME_BONUS_PER_PERSON

    # 主管紅利
    SUPERVISOR_DIVIDEND = SUPERVISOR_DIVIDEND

    # 主管節慶獎金基數
    SUPERVISOR_FESTIVAL_BONUS = SUPERVISOR_FESTIVAL_BONUS

    # 司機/美編/行政節慶獎金基數
    OFFICE_FESTIVAL_BONUS_BASE = OFFICE_FESTIVAL_BONUS_BASE

    def __init__(self, load_from_db: bool = False):
        self.insurance_service = InsuranceService()
        # 記錄目前載入的設定版本 ID，供薪資紀錄稽核用
        self._bonus_config_id: Optional[int] = None
        self._attendance_policy_id: Optional[int] = None
        self.deduction_rules = {
            'late': {'per_minute': 1},
            'missing': {'amount': 0},   # 未打卡不扣款，僅記錄
            'early': {'per_minute': 1}
        }
        # 可被覆蓋的設定 - 節慶獎金
        self._bonus_base = dict(FESTIVAL_BONUS_BASE)
        self._target_enrollment = {k: dict(v) for k, v in TARGET_ENROLLMENT.items()}
        # 可被覆蓋的設定 - 超額獎金
        self._overtime_target = {k: dict(v) for k, v in OVERTIME_TARGET.items()}
        self._overtime_per_person = {k: dict(v) for k, v in OVERTIME_BONUS_PER_PERSON.items()}
        # 可被覆蓋的設定 - 主管紅利
        self._supervisor_dividend = dict(SUPERVISOR_DIVIDEND)
        # 可被覆蓋的設定 - 主管節慶獎金基數
        self._supervisor_festival_bonus = dict(SUPERVISOR_FESTIVAL_BONUS)
        # 可被覆蓋的設定 - 司機/美編節慶獎金基數
        self._office_festival_bonus_base = dict(OFFICE_FESTIVAL_BONUS_BASE)
        # 可被覆蓋的設定 - 全校目標人數
        self._school_wide_target = 160
        # 園務會議設定
        self._meeting_pay = DEFAULT_MEETING_PAY
        self._meeting_pay_6pm = DEFAULT_MEETING_PAY_6PM
        self._meeting_absence_penalty = DEFAULT_MEETING_ABSENCE_PENALTY
        # 考勤政策設定（無寬限期）
        self._attendance_policy = {
            'grace_minutes': 0,
            'late_per_minute': 1,
            'early_per_minute': 1,
            'missing_punch_deduction': 0,
            'festival_bonus_months': 3
        }

        if load_from_db:
            self.load_config_from_db()

    def load_config_from_db(self):
        """從資料庫載入設定"""
        try:
            session = _get_db_session()
            from models.database import AttendancePolicy, BonusConfig as DBBonusConfig, GradeTarget, InsuranceRate

            # 載入考勤政策
            policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
            if policy:
                self._attendance_policy_id = policy.id  # 記錄版本 ID
                self._attendance_policy = {
                    'grace_minutes': policy.grace_minutes,
                    'late_per_minute': getattr(policy, 'late_per_minute', 1) or 1,
                    'early_per_minute': getattr(policy, 'early_per_minute', 1) or 1,
                    'missing_punch_deduction': 0,
                    'festival_bonus_months': policy.festival_bonus_months
                }
                self.deduction_rules = {
                    'late': {
                        'per_minute': self._attendance_policy['late_per_minute'],
                    },
                    'missing': {'amount': 0},
                    'early': {'per_minute': self._attendance_policy['early_per_minute']}
                }

            # 載入獎金設定
            bonus = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).first()
            if bonus:
                self._bonus_config_id = bonus.id  # 記錄版本 ID
                # 更新獎金基數
                self._bonus_base = {
                    'head_teacher': {
                        'A': bonus.head_teacher_ab,
                        'B': bonus.head_teacher_ab,
                        'C': bonus.head_teacher_c,
                    },
                    'assistant_teacher': {
                        'A': bonus.assistant_teacher_ab,
                        'B': bonus.assistant_teacher_ab,
                        'C': bonus.assistant_teacher_c,
                    }
                }
                # 更新主管節慶獎金
                self._supervisor_festival_bonus = {
                    '園長': bonus.principal_festival,
                    '主任': bonus.director_festival,
                    '組長': bonus.leader_festival
                }
                # 更新司機/美編/行政節慶獎金
                self._office_festival_bonus_base = {
                    '司機': bonus.driver_festival,
                    '美編': bonus.designer_festival,
                    '行政': bonus.admin_festival
                }
                # 更新主管紅利
                self._supervisor_dividend = {
                    '園長': bonus.principal_dividend,
                    '主任': bonus.director_dividend,
                    '組長': bonus.leader_dividend,
                    '副組長': bonus.vice_leader_dividend
                }
                # 更新超額獎金每人金額
                self._overtime_per_person = {
                    'head_teacher': {
                        '大班': bonus.overtime_head_normal,
                        '中班': bonus.overtime_head_normal,
                        '小班': bonus.overtime_head_normal,
                        '幼幼班': bonus.overtime_head_baby
                    },
                    'assistant_teacher': {
                        '大班': bonus.overtime_assistant_normal,
                        '中班': bonus.overtime_assistant_normal,
                        '小班': bonus.overtime_assistant_normal,
                        '幼幼班': bonus.overtime_assistant_baby
                    }
                }

                # 更新全校目標人數
                if bonus.school_wide_target:
                    self._school_wide_target = bonus.school_wide_target

            # 載入年級目標：合併 NULL（舊資料）與版本特定目標
            null_targets = {
                t.grade_name: t
                for t in session.query(GradeTarget).filter(
                    GradeTarget.bonus_config_id == None  # noqa: E711
                ).all()
            }
            versioned_targets = {}
            if bonus:
                versioned_targets = {
                    t.grade_name: t
                    for t in session.query(GradeTarget).filter(
                        GradeTarget.bonus_config_id == bonus.id
                    ).all()
                }
            # 合併：版本目標優先覆蓋 NULL 目標
            merged = {**null_targets, **versioned_targets}
            if merged:
                self._target_enrollment = {}
                self._overtime_target = {}
                for grade_name, t in merged.items():
                    self._target_enrollment[grade_name] = {
                        '2_teachers': t.festival_two_teachers,
                        '1_teacher': t.festival_one_teacher,
                        'shared_assistant': t.festival_shared
                    }
                    self._overtime_target[grade_name] = {
                        '2_teachers': t.overtime_two_teachers,
                        '1_teacher': t.overtime_one_teacher,
                        'shared_assistant': t.overtime_shared
                    }

            session.close()
            logger.info("SalaryEngine: 已從資料庫載入設定")

        except Exception as e:
            logger.warning("SalaryEngine: 從資料庫載入設定失敗，使用預設值: %s", e)

    def set_bonus_config(self, bonus_config: dict):
        """設定獎金參數（從前端傳入）"""
        if not bonus_config:
            return

        # 更新獎金基數
        if 'bonusBase' in bonus_config and bonus_config['bonusBase']:
            bb = bonus_config['bonusBase']
            self._bonus_base = {
                'head_teacher': {
                    'A': bb.get('headTeacherAB', 2000),
                    'B': bb.get('headTeacherAB', 2000),
                    'C': bb.get('headTeacherC', 1500),
                },
                'assistant_teacher': {
                    'A': bb.get('assistantTeacherAB', 1200),
                    'B': bb.get('assistantTeacherAB', 1200),
                    'C': bb.get('assistantTeacherC', 1200),
                }
            }

        # 更新節慶獎金目標人數
        if 'targetEnrollment' in bonus_config and bonus_config['targetEnrollment']:
            te = bonus_config['targetEnrollment']
            for grade, targets in te.items():
                self._target_enrollment[grade] = {
                    '2_teachers': targets.get('twoTeachers', 0),
                    '1_teacher': targets.get('oneTeacher', 0),
                    'shared_assistant': targets.get('sharedAssistant', 0)
                }

        # 更新超額獎金目標人數
        if 'overtimeTarget' in bonus_config and bonus_config['overtimeTarget']:
            ot = bonus_config['overtimeTarget']
            for grade, targets in ot.items():
                self._overtime_target[grade] = {
                    '2_teachers': targets.get('twoTeachers', 0),
                    '1_teacher': targets.get('oneTeacher', 0),
                    'shared_assistant': targets.get('sharedAssistant', 0)
                }

        # 更新超額獎金每人金額
        if 'overtimePerPerson' in bonus_config and bonus_config['overtimePerPerson']:
            op = bonus_config['overtimePerPerson']
            self._overtime_per_person = {
                'head_teacher': {
                    '大班': op.get('headBig', 400),
                    '中班': op.get('headMid', 400),
                    '小班': op.get('headSmall', 400),
                    '幼幼班': op.get('headBaby', 450)
                },
                'assistant_teacher': {
                    '大班': op.get('assistantBig', 100),
                    '中班': op.get('assistantMid', 100),
                    '小班': op.get('assistantSmall', 100),
                    '幼幼班': op.get('assistantBaby', 150)
                }
            }

        # 更新主管紅利
        if 'supervisorDividend' in bonus_config and bonus_config['supervisorDividend']:
            sd = bonus_config['supervisorDividend']
            self._supervisor_dividend = {
                '園長': sd.get('principal', 5000),
                '主任': sd.get('director', 4000),
                '組長': sd.get('leader', 3000),
                '副組長': sd.get('viceLeader', 1500)
            }

        # 更新主管節慶獎金基數
        if 'supervisorFestivalBonus' in bonus_config and bonus_config['supervisorFestivalBonus']:
            sfb = bonus_config['supervisorFestivalBonus']
            self._supervisor_festival_bonus = {
                '園長': sfb.get('principal', 6500),
                '主任': sfb.get('director', 3500),
                '組長': sfb.get('leader', 2000)
            }

        # 更新司機/美編/行政節慶獎金基數
        if 'officeFestivalBonusBase' in bonus_config and bonus_config['officeFestivalBonusBase']:
            ofb = bonus_config['officeFestivalBonusBase']
            self._office_festival_bonus_base = {
                '司機': ofb.get('driver', 1000),
                '美編': ofb.get('designer', 1000),
                '行政': ofb.get('admin', 2000)
            }

    def set_deduction_rules(self, rules: dict):
        """設定扣款規則"""
        self.deduction_rules.update(rules)

    # ─── Thin wrappers（委派至 festival.py 純函式） ───────────────────────────

    def get_position_grade(self, position: str) -> Optional[str]:
        """取得職位等級 (A/B/C)"""
        return _festival.get_position_grade(position, self.POSITION_GRADE_MAP)

    def get_festival_bonus_base(self, position: str, role: str) -> float:
        """取得節慶獎金基數"""
        return _festival.get_festival_bonus_base(position, role, self._bonus_base)

    def get_target_enrollment(self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int:
        """取得目標人數"""
        return _festival.get_target_enrollment(grade_name, has_assistant, is_shared_assistant, self._target_enrollment)

    def get_supervisor_dividend(self, title: str, position: str = '') -> float:
        """取得主管紅利"""
        return _festival.get_supervisor_dividend(title, position, self._supervisor_dividend)

    def get_supervisor_festival_bonus(self, title: str, position: str = '') -> Optional[float]:
        """取得主管節慶獎金基數"""
        return _festival.get_supervisor_festival_bonus(title, position, self._supervisor_festival_bonus)

    def get_office_festival_bonus_base(self, position: str, title: str = '') -> Optional[float]:
        """取得司機/美編節慶獎金基數"""
        return _festival.get_office_festival_bonus_base(position, title, self._office_festival_bonus_base)

    def get_overtime_target(self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int:
        """取得超額獎金目標人數"""
        return _festival.get_overtime_target(grade_name, has_assistant, is_shared_assistant, self._overtime_target)

    def get_overtime_per_person(self, role: str, grade_name: str) -> float:
        """取得超額獎金每人金額"""
        return _festival.get_overtime_per_person(role, grade_name, self._overtime_per_person)

    def is_eligible_for_festival_bonus(self, hire_date, reference_date=None) -> bool:
        """檢查員工是否符合領取節慶獎金資格（入職滿3個月）"""
        festival_months = self._attendance_policy.get('festival_bonus_months', 3)
        return _festival.is_eligible_for_festival_bonus(hire_date, reference_date, festival_months)

    # ─── Static method wrappers（委派至 proration.py / utils.py 純函式） ─────

    @staticmethod
    def _prorate_base_salary(contracted_base: float, hire_date_raw, year: int, month: int) -> float:
        """月中入職者底薪折算（向後相容靜態方法）"""
        from .proration import _prorate_base_salary
        return _prorate_base_salary(contracted_base, hire_date_raw, year, month)

    @staticmethod
    def _prorate_for_period(contracted_base, hire_date_raw, resign_date_raw, year, month):
        """計算當月實際在職天數的底薪折算"""
        return _prorate_for_period(contracted_base, hire_date_raw, resign_date_raw, year, month)

    @staticmethod
    def _build_expected_workdays(year, month, holiday_set, daily_shift_map, hire_date_raw=None, resign_date_raw=None, today=None):
        """建立指定月份的預期上班日集合"""
        return _build_expected_workdays(year, month, holiday_set, daily_shift_map, hire_date_raw, resign_date_raw, today)

    @staticmethod
    def get_bonus_distribution_month(month: int) -> bool:
        """判斷是否為節慶獎金發放月"""
        return get_bonus_distribution_month(month)

    @staticmethod
    def get_meeting_deduction_period_start(year: int, month: int):
        """返回發放月的會議缺席扣款起算日"""
        return get_meeting_deduction_period_start(year, month)

    # ─── 主要計算方法 ────────────────────────────────────────────────────────

    def calculate_attendance_deduction(self, attendance: AttendanceResult, daily_salary: float = 0, base_salary: float = 0, late_details: list = None) -> dict:
        """
        計算考勤扣款

        規則：
        - 遲到/早退：一律按實際分鐘比例扣款（每分鐘 = 月薪 ÷ 30 ÷ 8 ÷ 60，依勞基法固定基準）
        - 未打卡：不扣款，僅記錄次數（供考核用）
        """
        early_rule = self.deduction_rules.get('early', {})

        # 每分鐘薪資 = 月薪 ÷ 30 ÷ 8 ÷ 60（依勞基法時薪基準，固定 30 天）
        per_minute_rate = base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 1

        # 遲到扣款
        if late_details:
            total_late_min = sum(late_details)
        else:
            total_late_min = attendance.total_late_minutes
        late_deduction = total_late_min * per_minute_rate

        # 早退扣款
        total_early_minutes = attendance.total_early_minutes
        early_deduction = total_early_minutes * per_minute_rate

        # 未打卡：不扣款，僅記錄
        missing_count = attendance.missing_punch_in_count + attendance.missing_punch_out_count

        return {
            'late_deduction': late_deduction,
            'missing_punch_deduction': 0,  # 不扣款
            'early_leave_deduction': early_deduction,
            'late_count': attendance.late_count,
            'early_leave_count': attendance.early_leave_count,
            'missing_punch_count': missing_count,
            'total_late_minutes': attendance.total_late_minutes,
            'total_early_minutes': total_early_minutes,
        }

    def calculate_bonus(self, target: int, current: int, base_amount: float, overtime_per: float = 500) -> dict:
        """計算獎金 (舊版，保留相容性)"""
        ratio = current / target if target > 0 else 0
        festival_bonus = base_amount * ratio
        overtime_bonus = max(0, current - target) * overtime_per
        return {
            'festival_bonus': round(festival_bonus),
            'overtime_bonus': round(overtime_bonus),
            'ratio': ratio
        }

    def calculate_overtime_bonus(
        self,
        role: str,
        grade_name: str,
        current_enrollment: int,
        has_assistant: bool,
        is_shared_assistant: bool = False
    ) -> dict:
        """計算超額獎金"""
        return _festival.calculate_overtime_bonus(
            role=role,
            grade_name=grade_name,
            current_enrollment=current_enrollment,
            has_assistant=has_assistant,
            is_shared_assistant=is_shared_assistant,
            overtime_target_map=self._overtime_target,
            overtime_per_person_map=self._overtime_per_person,
        )

    def calculate_festival_bonus_v2(
        self,
        position: str,
        role: str,
        grade_name: str,
        current_enrollment: int,
        has_assistant: bool,
        is_shared_assistant: bool = False
    ) -> dict:
        """計算節慶獎金 (新版 - 依職位等級和角色計算)"""
        return _festival.calculate_festival_bonus_v2(
            position=position,
            role=role,
            grade_name=grade_name,
            current_enrollment=current_enrollment,
            has_assistant=has_assistant,
            is_shared_assistant=is_shared_assistant,
            bonus_base=self._bonus_base,
            target_enrollment_map=self._target_enrollment,
            overtime_target_map=self._overtime_target,
            overtime_per_person_map=self._overtime_per_person,
        )

    def calculate_salary(
        self,
        employee: dict,
        year: int,
        month: int,
        attendance: AttendanceResult = None,
        bonus_settings: dict = None,
        leave_deduction: float = 0,
        allowances: List[dict] = None,
        classroom_context: dict = None,
        office_staff_context: dict = None,
        meeting_context: dict = None,
        working_days: int = 22,
        overtime_work_pay: float = 0,
    ) -> SalaryBreakdown:
        """
        計算單一員工薪資

        Args:
            employee:           員工資料字典
            year:               年
            month:              月
            attendance:         考勤資料
            bonus_settings:     舊版獎金設定 (target, current, festival_base...)
            leave_deduction:    請假扣款
            allowances:         津貼列表
            classroom_context:  班級上下文 (新版節慶獎金用)
            office_staff_context: 辦公室人員上下文
            meeting_context:    園務會議上下文
        """

        is_hourly = employee.get('employee_type') == 'hourly'

        breakdown = SalaryBreakdown(
            employee_name=employee.get('name', ''),
            employee_id=employee.get('employee_id', ''),
            year=year,
            month=month
        )

        if is_hourly:
            # 時薪制計算
            breakdown.hourly_rate = employee.get('hourly_rate', 0)
            breakdown.work_hours = employee.get('work_hours', 0)
            # 優先使用已依勞基法分段計費的結果（process_salary_calculation 提供）；
            # 未提供時 fallback 至等比計算（向後相容直接傳入 employee dict 的測試情境）
            breakdown.hourly_total = (
                employee.get('hourly_calculated_pay')
                or breakdown.hourly_rate * breakdown.work_hours
            )
            breakdown.gross_salary = breakdown.hourly_total
        else:
            # 正職員工
            contracted_base = employee.get('base_salary', 0) or 0
            breakdown.base_salary = _prorate_for_period(
                contracted_base,
                employee.get('hire_date'),
                employee.get('resign_date'),
                year,
                month,
            )

            # 處理津貼
            if allowances:
                for allowance in allowances:
                    amount = allowance.get('amount', 0)
                    name = allowance.get('name', '')

                    if '主管' in name:
                        breakdown.supervisor_allowance += amount
                    elif '導師' in name:
                        breakdown.teacher_allowance += amount
                    elif '伙食' in name:
                        breakdown.meal_allowance += amount
                    elif '交通' in name:
                        breakdown.transportation_allowance += amount
                    else:
                        breakdown.other_allowance += amount

            # 相容舊版欄位
            breakdown.supervisor_allowance += employee.get('supervisor_allowance', 0)
            breakdown.teacher_allowance += employee.get('teacher_allowance', 0)
            breakdown.meal_allowance += employee.get('meal_allowance', 0)
            breakdown.transportation_allowance += employee.get('transportation_allowance', 0)
            breakdown.other_allowance += employee.get('other_allowance', 0)

            # 檢查是否符合領取節慶獎金資格（入職滿3個月）
            hire_date = employee.get('hire_date')
            is_eligible = self.is_eligible_for_festival_bonus(hire_date)

            # 獎金計算
            emp_title = employee.get('title', '')
            emp_position = employee.get('position', '')

            if not emp_position:
                supervisor_festival_base = None
            else:
                supervisor_festival_base = self.get_supervisor_festival_bonus(emp_title, emp_position)

            if supervisor_festival_base is not None:
                # 主管節慶獎金 = 固定基數 × 全校比例
                if is_eligible and emp_position:
                    school_enrollment = office_staff_context.get('school_enrollment', 0) if office_staff_context else 0
                    school_target = self._school_wide_target or 160
                    ratio = school_enrollment / school_target if school_target > 0 else 0
                    breakdown.festival_bonus = round(supervisor_festival_base * ratio)
                else:
                    breakdown.festival_bonus = 0
                breakdown.overtime_bonus = 0
            elif office_staff_context and emp_position:
                office_base = self.get_office_festival_bonus_base(emp_position, emp_title)
                if office_base and is_eligible:
                    school_enrollment = office_staff_context.get('school_enrollment', 0)
                    school_target = self._school_wide_target or 160
                    ratio = school_enrollment / school_target if school_target > 0 else 0
                    breakdown.festival_bonus = round(office_base * ratio)
                else:
                    breakdown.festival_bonus = 0
                breakdown.overtime_bonus = 0
            elif classroom_context and emp_position:
                bonus_result = self.calculate_festival_bonus_v2(
                    position=employee.get('position', ''),
                    role=classroom_context.get('role', ''),
                    grade_name=classroom_context.get('grade_name', ''),
                    current_enrollment=classroom_context.get('current_enrollment', 0),
                    has_assistant=classroom_context.get('has_assistant', False),
                    is_shared_assistant=classroom_context.get('is_shared_assistant', False)
                )
                if is_eligible:
                    breakdown.festival_bonus = bonus_result['festival_bonus']
                    breakdown.overtime_bonus = bonus_result['overtime_bonus']
                else:
                    breakdown.festival_bonus = 0
                    breakdown.overtime_bonus = 0
            elif bonus_settings:
                # 舊版計算方式（相容性保留）
                base_amount = bonus_settings.get('festival_base', 0)
                position_bonus_base = bonus_settings.get('position_bonus_base', {})

                if position_bonus_base and emp_title and emp_title in position_bonus_base:
                    base_amount = position_bonus_base[emp_title]

                bonus = self.calculate_bonus(
                    bonus_settings.get('target', 0),
                    bonus_settings.get('current', 0),
                    base_amount,
                    bonus_settings.get('overtime_per', 500)
                )
                breakdown.festival_bonus = bonus['festival_bonus']
                breakdown.overtime_bonus = bonus['overtime_bonus']

            breakdown.performance_bonus = employee.get('performance_bonus', 0)
            breakdown.special_bonus = employee.get('special_bonus', 0)

            # 計算主管紅利
            if emp_position:
                breakdown.supervisor_dividend = self.get_supervisor_dividend(emp_title, emp_position)
            else:
                breakdown.supervisor_dividend = 0

            # 非發放月份不計節慶獎金與超額獎金
            if not get_bonus_distribution_month(month):
                breakdown.festival_bonus = 0
                breakdown.overtime_bonus = 0

            # 生日禮金：當月壽星 $500
            birthday_val = employee.get('birthday')
            if birthday_val:
                if isinstance(birthday_val, str):
                    try:
                        birthday_val = datetime.strptime(birthday_val, '%Y-%m-%d').date()
                    except ValueError:
                        birthday_val = None
                if birthday_val and birthday_val.month == month:
                    breakdown.birthday_bonus = 500

            # 計算應發總額（festival_bonus / overtime_bonus 獨立轉帳，不計入月薪）
            breakdown.gross_salary = (
                breakdown.base_salary +
                breakdown.supervisor_allowance +
                breakdown.teacher_allowance +
                breakdown.meal_allowance +
                breakdown.transportation_allowance +
                breakdown.other_allowance +
                breakdown.performance_bonus +
                breakdown.special_bonus +
                breakdown.supervisor_dividend +
                breakdown.birthday_bonus
            )

            # 勞健保計算
            pension_rate = employee.get("pension_self_rate", 0.0)
            _ins_raw = employee.get('insurance_salary') or contracted_base
            _ins_salary = (self.insurance_service.get_bracket(_ins_raw)["amount"] if _ins_raw else 0)
            insurance = self.insurance_service.calculate(
                _ins_salary,
                employee.get('dependents', 0),
                pension_self_rate=pension_rate
            )
            breakdown.labor_insurance = insurance.labor_employee
            breakdown.health_insurance = insurance.health_employee
            breakdown.pension_self = insurance.pension_employee

        # 考勤扣款
        base_sal = employee.get('base_salary', 0) or 0
        daily_salary = base_sal / MONTHLY_BASE_DAYS if base_sal else 0
        late_details = employee.get('_late_details', None)
        if attendance:
            att_ded = self.calculate_attendance_deduction(
                attendance, daily_salary=daily_salary, base_salary=base_sal, late_details=late_details
            )
            breakdown.late_deduction = att_ded['late_deduction']
            breakdown.early_leave_deduction = att_ded['early_leave_deduction']
            breakdown.missing_punch_deduction = 0  # 不扣款
            breakdown.late_count = att_ded['late_count']
            breakdown.early_leave_count = att_ded['early_leave_count']
            breakdown.missing_punch_count = att_ded['missing_punch_count']
            breakdown.total_late_minutes = att_ded['total_late_minutes']
            breakdown.total_early_minutes = att_ded['total_early_minutes']

        breakdown.leave_deduction = leave_deduction

        # 園務會議加班費與缺席扣款
        if meeting_context:
            attended = meeting_context.get('attended', 0)
            absent = meeting_context.get('absent', 0)
            work_end = meeting_context.get('work_end_time', '17:00')

            if work_end == '18:00':
                per_meeting_pay = self._meeting_pay_6pm
            else:
                per_meeting_pay = self._meeting_pay

            breakdown.meeting_overtime_pay = attended * per_meeting_pay
            breakdown.meeting_attended = attended
            breakdown.meeting_absent = absent

            if get_bonus_distribution_month(month):
                absent_for_deduction = meeting_context.get('absent_period', absent)
                breakdown.meeting_absence_deduction = absent_for_deduction * self._meeting_absence_penalty
                breakdown.festival_bonus = max(0, breakdown.festival_bonus - breakdown.meeting_absence_deduction)

        # 將園務會議加班費與核准加班費加入應發總額
        breakdown.overtime_work_pay = overtime_work_pay
        breakdown.gross_salary += breakdown.meeting_overtime_pay + overtime_work_pay

        # 計算扣款總額
        breakdown.total_deduction = (
            breakdown.labor_insurance +
            breakdown.health_insurance +
            breakdown.pension_self +
            breakdown.late_deduction +
            breakdown.early_leave_deduction +
            breakdown.leave_deduction +
            breakdown.absence_deduction +
            breakdown.other_deduction
        )

        # 獎金獨立轉帳旗標
        breakdown.bonus_separate = (
            breakdown.festival_bonus + breakdown.overtime_bonus + breakdown.supervisor_dividend
        ) > 0
        breakdown.bonus_amount = (
            breakdown.festival_bonus + breakdown.overtime_bonus + breakdown.supervisor_dividend
        )

        # 最終一次舍入
        _net_raw = breakdown.gross_salary - breakdown.total_deduction
        breakdown.gross_salary = round(breakdown.gross_salary)
        breakdown.total_deduction = round(breakdown.total_deduction)
        breakdown.net_salary = round(_net_raw)

        assert breakdown.gross_salary >= 0, f"gross_salary 異常負值: {breakdown.gross_salary}"
        assert breakdown.total_deduction >= 0, f"total_deduction 異常負值: {breakdown.total_deduction}"
        assert breakdown.net_salary >= 0, f"net_salary 異常負值: {breakdown.net_salary}"

        return breakdown

    def calculate_festival_bonus_breakdown(self, employee_id: int, year: int, month: int) -> dict:
        """計算單一員工節慶獎金明細 (for UI display)"""
        session = _get_db_session()
        try:
            from models.database import Employee, Classroom, ClassGrade, JobTitle, Student

            emp = session.query(Employee).get(employee_id)
            if not emp:
                return {}

            classroom = None
            if emp.classroom_id:
                classroom = session.query(Classroom).get(emp.classroom_id)

            bonus_base = 0
            target_enrollment = 0
            current_enrollment = 0
            ratio = 0
            festival_bonus = 0
            remark = ""
            category = ""

            position = emp.position or ''
            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')

            is_eligible = self.is_eligible_for_festival_bonus(emp.hire_date)

            if not position:
                is_eligible = False
                remark = "無職位資料(不發放)"
            elif not is_eligible:
                remark = "未滿3個月"

            # 提前查一次全校在籍人數，主管與辦公室分支共用
            school_active_students = session.query(Student).filter(Student.is_active == True).count()

            supervisor_base = self.get_supervisor_festival_bonus(title_name, position)
            if supervisor_base:
                category = "主管"
                bonus_base = supervisor_base
                current_enrollment = school_active_students
                target_enrollment = self._school_wide_target or 160
                ratio = current_enrollment / target_enrollment if target_enrollment > 0 else 0
                festival_bonus = round(supervisor_base * ratio) if is_eligible else 0
                if is_eligible:
                    remark = "全校比例(主管)"

            elif emp.is_office_staff:
                office_base = self.get_office_festival_bonus_base(position, title_name)
                category = "辦公室"
                current_enrollment = school_active_students

                if office_base:
                    bonus_base = office_base
                    school_target = self._school_wide_target or 160
                    target_enrollment = school_target if school_target > 0 else 100
                    ratio = current_enrollment / target_enrollment if target_enrollment > 0 else 0
                    festival_bonus = round(bonus_base * ratio) if is_eligible else 0
                    if is_eligible:
                        remark = "全校比例"

            elif classroom:
                category = "帶班老師"
                grade_name = classroom.grade.name if classroom.grade else ''
                current_enrollment = session.query(Student).filter(
                    Student.classroom_id == classroom.id,
                    Student.is_active == True
                ).count()

                role = 'assistant_teacher'
                if classroom.head_teacher_id == emp.id:
                    role = 'head_teacher'
                elif classroom.assistant_teacher_id == emp.id:
                    role = 'assistant_teacher'
                elif classroom.art_teacher_id == emp.id:
                    role = 'art_teacher'

                has_assistant = (classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0)
                is_shared = False

                role_for_base = role
                if role == 'art_teacher':
                    role_for_base = 'assistant_teacher'

                bonus_base = self.get_festival_bonus_base(position, role_for_base)
                target_enrollment = self.get_target_enrollment(grade_name, has_assistant, is_shared)

                ratio = current_enrollment / target_enrollment if target_enrollment > 0 else 0
                festival_bonus = round(bonus_base * ratio) if is_eligible else 0

            else:
                category = "其他"
                remark = "無帶班/無設定"

            return {
                "name": emp.name,
                "category": category,
                "bonusBase": bonus_base,
                "targetEnrollment": target_enrollment,
                "currentEnrollment": current_enrollment,
                "ratio": ratio,
                "festivalBonus": festival_bonus,
                "remark": remark
            }

        except Exception as e:
            logger.exception("計算節慶獎金明細失敗：employee_id=%s", employee_id)
            return {
                "name": f"Error: {e}",
                "festivalBonus": 0
            }
        finally:
            session.close()

    def process_salary_calculation(self, employee_id: int, year: int, month: int):
        """處理單一員工薪資計算並儲存結果"""
        session = _get_db_session()
        try:
            from models.database import Employee, Attendance, SalaryRecord, EmployeeAllowance, AllowanceType, Classroom, ClassGrade, JobTitle, SalaryItem, Student

            # 1. 取得員工資料
            emp = session.query(Employee).get(employee_id)
            if not emp:
                raise ValueError(f"Employee {employee_id} not found")

            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')

            # 2. 轉換為 dict
            emp_dict = {
                'employee_id': emp.employee_id,
                'name': emp.name,
                'title': title_name,
                'position': emp.position,
                'employee_type': emp.employee_type,
                'base_salary': emp.base_salary,
                'hourly_rate': emp.hourly_rate,
                'work_hours': 0,
                'supervisor_allowance': emp.supervisor_allowance,
                'teacher_allowance': emp.teacher_allowance,
                'meal_allowance': emp.meal_allowance,
                'transportation_allowance': emp.transportation_allowance,
                'other_allowance': emp.other_allowance,
                'insurance_salary': (
                    self.insurance_service.get_bracket(
                        emp.insurance_salary_level if emp.insurance_salary_level and emp.insurance_salary_level > 0
                        else emp.base_salary
                    )["amount"]
                    if (emp.insurance_salary_level or emp.base_salary) else 0
                ),
                'dependents': emp.dependents,
                'hire_date': emp.hire_date,
                'resign_date': getattr(emp, 'resign_date', None),
                'birthday': emp.birthday,
            }

            # 3. 取得考勤並計算統計
            import calendar
            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)

            attendances = session.query(Attendance).filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date
            ).all()

            late_count = sum(1 for a in attendances if a.is_late)
            early_count = sum(1 for a in attendances if a.is_early_leave)
            missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
            missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
            total_late_minutes = sum(a.late_minutes or 0 for a in attendances if a.is_late)
            total_early_minutes = sum(a.early_leave_minutes or 0 for a in attendances if a.is_early_leave)

            late_details = [a.late_minutes or 0 for a in attendances if a.is_late and (a.late_minutes or 0) > 0]
            emp_dict['_late_details'] = late_details

            total_hours = 0.0
            total_hourly_pay = 0.0
            if emp.employee_type == 'hourly':
                _work_end_t = datetime.strptime(emp.work_end_time or "17:00", "%H:%M").time()
                for a in attendances:
                    if not a.punch_in_time:
                        continue
                    day_hours = _compute_hourly_daily_hours(
                        a.punch_in_time, a.punch_out_time, _work_end_t
                    )
                    total_hours += day_hours
                    total_hourly_pay += _calc_daily_hourly_pay(day_hours, emp.hourly_rate or 0)
                emp_dict['work_hours'] = round(total_hours, 2)
                emp_dict['hourly_calculated_pay'] = round(total_hourly_pay, 2)

            attendance_result = AttendanceResult(
                employee_name=emp.name,
                total_days=len(attendances),
                normal_days=len(attendances) - late_count - early_count,
                late_count=late_count,
                early_leave_count=early_count,
                missing_punch_in_count=missing_in,
                missing_punch_out_count=missing_out,
                total_late_minutes=total_late_minutes,
                total_early_minutes=total_early_minutes,
                details=[]
            )

            # 4. 取得津貼
            allowances = []
            emp_allowances = session.query(EmployeeAllowance).filter(
                EmployeeAllowance.employee_id == emp.id,
                EmployeeAllowance.is_active == True,
            ).all()

            allowance_type_ids = [ea.allowance_type_id for ea in emp_allowances]
            if allowance_type_ids:
                allowance_type_map = {
                    at.id: at
                    for at in session.query(AllowanceType).filter(
                        AllowanceType.id.in_(allowance_type_ids)
                    ).all()
                }
            else:
                allowance_type_map = {}

            for ea in emp_allowances:
                a_type = allowance_type_map.get(ea.allowance_type_id)
                if a_type:
                    allowances.append({
                        'name': a_type.name,
                        'amount': ea.amount
                    })

            # 5. 建構 Classroom Context
            classroom_context = None
            office_staff_context = None

            if emp.classroom_id:
                classroom = session.query(Classroom).get(emp.classroom_id)
                if classroom:
                    role = 'assistant_teacher'
                    if classroom.head_teacher_id == emp.id:
                        role = 'head_teacher'
                    elif classroom.art_teacher_id == emp.id:
                        role = 'art_teacher'

                    has_assistant = (classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0)

                    student_count = session.query(Student).filter(
                        Student.classroom_id == classroom.id,
                        Student.is_active == True
                    ).count()

                    classroom_context = {
                        'role': role,
                        'grade_name': classroom.grade.name if classroom.grade else '',
                        'current_enrollment': student_count,
                        'has_assistant': has_assistant,
                        'is_shared_assistant': False
                    }

            # 5b. 辦公室人員 / 主管需要全校比例
            is_supervisor = False
            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')
            if self.get_supervisor_festival_bonus(title_name, emp.position):
                is_supervisor = True

            if is_supervisor or (emp.is_office_staff and not classroom_context):
                total_students = session.query(Student).filter(Student.is_active == True).count()
                office_staff_context = {
                    'school_enrollment': total_students
                }

            # 5c. 查詢已核准請假記錄
            from models.database import LeaveRecord, OvertimeRecord as DBOvertimeRecord
            approved_leaves = session.query(LeaveRecord).filter(
                LeaveRecord.employee_id == emp.id,
                LeaveRecord.is_approved == True,
                LeaveRecord.start_date <= end_date,
                LeaveRecord.end_date >= start_date
            ).all()
            daily_salary = emp.base_salary / MONTHLY_BASE_DAYS if emp.base_salary else 0
            leave_deduction_total = _sum_leave_deduction(approved_leaves, daily_salary)

            # 5d. 查詢已核准加班記錄
            approved_overtimes = session.query(DBOvertimeRecord).filter(
                DBOvertimeRecord.employee_id == emp.id,
                DBOvertimeRecord.is_approved == True,
                DBOvertimeRecord.overtime_date >= start_date,
                DBOvertimeRecord.overtime_date <= end_date
            ).all()
            overtime_work_pay_total = sum(o.overtime_pay or 0 for o in approved_overtimes)

            # 5e. 查詢園務會議記錄
            from models.database import MeetingRecord, Holiday
            meeting_records = session.query(MeetingRecord).filter(
                MeetingRecord.employee_id == emp.id,
                MeetingRecord.meeting_date >= start_date,
                MeetingRecord.meeting_date <= end_date
            ).all()

            meeting_attended = sum(1 for m in meeting_records if m.attended)
            meeting_absent_current = sum(1 for m in meeting_records if not m.attended)

            absent_period = meeting_absent_current
            period_start = get_meeting_deduction_period_start(year, month)
            if period_start is not None and period_start < start_date:
                prior_records = session.query(MeetingRecord).filter(
                    MeetingRecord.employee_id == emp.id,
                    MeetingRecord.meeting_date >= period_start,
                    MeetingRecord.meeting_date < start_date,
                ).all()
                absent_period += sum(1 for m in prior_records if not m.attended)

            meeting_context = None
            if meeting_records or absent_period > 0:
                meeting_context = {
                    'attended': meeting_attended,
                    'absent': meeting_absent_current,
                    'absent_period': absent_period,
                    'work_end_time': emp.work_end_time or '17:00',
                }

            # 5f. 曠職偵測
            absent_count = 0
            absence_deduction_amount = 0
            if emp.employee_type != 'hourly':
                holidays_in_month = session.query(Holiday.date).filter(
                    Holiday.date >= start_date,
                    Holiday.date <= end_date,
                    Holiday.is_active == True
                ).all()
                holiday_set = {h.date for h in holidays_in_month}

                from models.database import DailyShift as _DailyShift
                daily_shifts_in_month = session.query(_DailyShift).filter(
                    _DailyShift.employee_id == emp.id,
                    _DailyShift.date >= start_date,
                    _DailyShift.date <= end_date,
                ).all()
                daily_shift_map = {ds.date: ds.shift_type_id for ds in daily_shifts_in_month}

                expected_workdays = _build_expected_workdays(
                    year=year,
                    month=month,
                    holiday_set=holiday_set,
                    daily_shift_map=daily_shift_map,
                    hire_date_raw=emp.hire_date,
                    resign_date_raw=getattr(emp, 'resign_date', None),
                )

                attendance_dates = {
                    (a.attendance_date.date() if isinstance(a.attendance_date, datetime) else a.attendance_date)
                    for a in attendances
                }

                from datetime import timedelta as _td
                leave_covered: set = set()
                for lv in approved_leaves:
                    d = lv.start_date.date() if isinstance(lv.start_date, datetime) else lv.start_date
                    lv_end = lv.end_date.date() if isinstance(lv.end_date, datetime) else lv.end_date
                    while d <= lv_end:
                        if start_date <= d <= end_date:
                            leave_covered.add(d)
                        d += _td(days=1)

                absent_days = expected_workdays - attendance_dates - leave_covered
                absent_count = len(absent_days)
                daily_salary_full = emp.base_salary / MONTHLY_BASE_DAYS if emp.base_salary else 0
                absence_deduction_amount = absent_count * daily_salary_full
                if absent_count > 0:
                    logger.info(
                        "曠職偵測：emp_id=%d %d/%d 曠職 %d 天，扣款 %d 元（%s）",
                        emp.id, year, month, absent_count, absence_deduction_amount,
                        sorted(absent_days)
                    )

            # 6. 計算薪資
            breakdown = self.calculate_salary(
                employee=emp_dict,
                year=year,
                month=month,
                attendance=attendance_result,
                leave_deduction=leave_deduction_total,
                allowances=allowances,
                classroom_context=classroom_context,
                office_staff_context=office_staff_context,
                meeting_context=meeting_context,
                overtime_work_pay=overtime_work_pay_total,
            )

            # 加入曠職扣款
            breakdown.absent_count = absent_count
            breakdown.absence_deduction = round(absence_deduction_amount)
            breakdown.total_deduction = round(breakdown.total_deduction + absence_deduction_amount)
            breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

            assert breakdown.total_deduction >= 0, f"total_deduction 異常負值（含曠職）: {breakdown.total_deduction}"
            assert breakdown.net_salary >= 0, f"net_salary 異常負值（含曠職）: {breakdown.net_salary}"

            # 7. 儲存 SalaryRecord
            salary_record = session.query(SalaryRecord).filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month
            ).first()

            if salary_record and salary_record.is_finalized:
                raise ValueError(
                    f"員工「{emp.name}」{year} 年 {month} 月薪資已封存（is_finalized=True），"
                    "禁止覆寫。如需重新計算，請先至薪資管理頁面解除該月封存。"
                )

            if not salary_record:
                salary_record = SalaryRecord(
                    employee_id=emp.id,
                    salary_year=year,
                    salary_month=month
                )
                session.add(salary_record)

            # 記錄計算時使用的設定版本
            salary_record.bonus_config_id = self._bonus_config_id
            salary_record.attendance_policy_id = self._attendance_policy_id

            salary_record.base_salary = breakdown.base_salary
            salary_record.supervisor_allowance = breakdown.supervisor_allowance
            salary_record.teacher_allowance = breakdown.teacher_allowance
            salary_record.meal_allowance = breakdown.meal_allowance
            salary_record.transportation_allowance = breakdown.transportation_allowance
            salary_record.other_allowance = breakdown.other_allowance
            salary_record.festival_bonus = breakdown.festival_bonus
            salary_record.overtime_bonus = breakdown.overtime_bonus
            salary_record.bonus_separate = breakdown.bonus_separate
            salary_record.performance_bonus = breakdown.performance_bonus
            salary_record.special_bonus = breakdown.special_bonus
            salary_record.bonus_amount = (
                breakdown.festival_bonus + breakdown.overtime_bonus + breakdown.supervisor_dividend
            )
            salary_record.supervisor_dividend = breakdown.supervisor_dividend
            salary_record.overtime_pay = breakdown.overtime_work_pay
            salary_record.meeting_overtime_pay = breakdown.meeting_overtime_pay
            salary_record.meeting_absence_deduction = breakdown.meeting_absence_deduction
            salary_record.birthday_bonus = breakdown.birthday_bonus
            salary_record.work_hours = breakdown.work_hours
            salary_record.hourly_rate = breakdown.hourly_rate
            salary_record.hourly_total = breakdown.hourly_total
            salary_record.labor_insurance_employee = breakdown.labor_insurance
            salary_record.health_insurance_employee = breakdown.health_insurance
            salary_record.pension_employee = breakdown.pension_self
            salary_record.late_deduction = breakdown.late_deduction
            salary_record.early_leave_deduction = breakdown.early_leave_deduction
            salary_record.missing_punch_deduction = breakdown.missing_punch_deduction
            salary_record.leave_deduction = breakdown.leave_deduction
            salary_record.absence_deduction = breakdown.absence_deduction
            salary_record.other_deduction = breakdown.other_deduction
            salary_record.gross_salary = breakdown.gross_salary
            salary_record.total_deduction = breakdown.total_deduction
            salary_record.net_salary = breakdown.net_salary
            salary_record.late_count = breakdown.late_count
            salary_record.early_leave_count = breakdown.early_leave_count
            salary_record.missing_punch_count = breakdown.missing_punch_count
            salary_record.absent_count = breakdown.absent_count

            session.commit()

            return breakdown

        except Exception as e:
            session.rollback()
            logger.exception("薪資計算失敗：employee_id=%s", employee_id)
            raise e
        finally:
            session.close()
