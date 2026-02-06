"""
薪資計算引擎 - 整合考勤、獎金、扣款計算
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from .insurance_service import InsuranceService, InsuranceCalculation
from .attendance_parser import AttendanceResult


@dataclass
class SalaryBreakdown:
    """薪資明細"""
    employee_name: str
    employee_id: str
    year: int
    month: int
    
    # 應領項目
    base_salary: float = 0
    supervisor_allowance: float = 0
    teacher_allowance: float = 0
    meal_allowance: float = 0
    transportation_allowance: float = 0
    other_allowance: float = 0
    
    # 獎金
    festival_bonus: float = 0
    overtime_bonus: float = 0
    performance_bonus: float = 0
    special_bonus: float = 0
    supervisor_dividend: float = 0  # 主管紅利
    
    # 時薪制
    work_hours: float = 0
    hourly_rate: float = 0
    hourly_total: float = 0
    
    # 法定代扣
    labor_insurance: float = 0
    health_insurance: float = 0
    pension_self: float = 0
    
    # 考勤扣款
    late_deduction: float = 0
    early_leave_deduction: float = 0
    missing_punch_deduction: float = 0
    leave_deduction: float = 0
    other_deduction: float = 0
    
    # 考勤統計
    late_count: int = 0
    early_leave_count: int = 0
    missing_punch_count: int = 0
    
    # 合計
    gross_salary: float = 0
    total_deduction: float = 0
    net_salary: float = 0
    
    # 獎金獨立轉帳
    bonus_separate: bool = False
    bonus_amount: float = 0


class SalaryEngine:
    """薪資計算引擎"""

    # 預設扣款規則
    DEFAULT_LATE_DEDUCTION = 50       # 遲到每次扣款
    DEFAULT_LATE_THRESHOLD = 2        # 遲到幾次開始扣
    DEFAULT_MISSING_PUNCH = 50        # 未打卡扣款
    DEFAULT_EARLY_LEAVE = 50          # 早退扣款

    # 節慶獎金職位等級對應
    # A級 = 幼兒園教師, B級 = 教保員, C級 = 助理教保員
    POSITION_GRADE_MAP = {
        '幼兒園教師': 'A',
        '教保員': 'B',
        '助理教保員': 'C',
    }

    # 節慶獎金基數 (依職位等級和角色)
    # 角色: head_teacher=班導, assistant_teacher=副班導
    FESTIVAL_BONUS_BASE = {
        'head_teacher': {
            'A': 2000,
            'B': 2000,
            'C': 1500,
        },
        'assistant_teacher': {
            'A': 1200,
            'B': 1200,
            'C': 1200,  # 假設 C級副班導也是 1200
        }
    }

    # 節慶獎金目標人數 (依年級和教師配置)
    # 格式: grade_name -> { teacher_count -> target }
    # 2_teachers = 班導+副班導 (1班1副班導)
    # 1_teacher = 只有班導 (無副班導)
    # shared_assistant = 2班共用同一個副班導
    TARGET_ENROLLMENT = {
        '大班': {'2_teachers': 27, '1_teacher': 14, 'shared_assistant': 20},
        '中班': {'2_teachers': 25, '1_teacher': 13, 'shared_assistant': 18},
        '小班': {'2_teachers': 23, '1_teacher': 12, 'shared_assistant': 16},
        '幼幼班': {'2_teachers': 15, '1_teacher': 7, 'shared_assistant': 12},
    }

    # 超額獎金目標人數（與節慶獎金不同）
    OVERTIME_TARGET = {
        '大班': {'2_teachers': 25, '1_teacher': 13, 'shared_assistant': 20},
        '中班': {'2_teachers': 23, '1_teacher': 12, 'shared_assistant': 18},
        '小班': {'2_teachers': 21, '1_teacher': 11, 'shared_assistant': 16},
        '幼幼班': {'2_teachers': 14, '1_teacher': 7, 'shared_assistant': 12},
    }

    # 超額獎金每人金額（依角色和年級）
    OVERTIME_BONUS_PER_PERSON = {
        'head_teacher': {
            '大班': 400, '中班': 400, '小班': 400, '幼幼班': 450
        },
        'assistant_teacher': {
            '大班': 100, '中班': 100, '小班': 100, '幼幼班': 150
        }
    }

    # 主管紅利（依職稱）
    SUPERVISOR_DIVIDEND = {
        '園長': 5000,
        '主任': 4000,
        '組長': 3000,
        '副組長': 1500
    }

    # 主管節慶獎金基數（依職稱）
    SUPERVISOR_FESTIVAL_BONUS = {
        '園長': 6500,
        '主任': 3500,
        '組長': 2000
    }

    # 司機/美編節慶獎金基數（全校比例計算，無超額獎金）
    OFFICE_FESTIVAL_BONUS_BASE = {
        '司機': 1000,
        '美編': 1000
    }

    def __init__(self):
        self.insurance_service = InsuranceService()
        self.deduction_rules = {
            'late': {'threshold': 2, 'amount': 100},
            'missing': {'amount': 50},
            'early': {'amount': 50}
        }
        # 可被覆蓋的設定 - 節慶獎金
        self._bonus_base = self.FESTIVAL_BONUS_BASE.copy()
        self._target_enrollment = self.TARGET_ENROLLMENT.copy()
        # 可被覆蓋的設定 - 超額獎金
        self._overtime_target = self.OVERTIME_TARGET.copy()
        self._overtime_per_person = self.OVERTIME_BONUS_PER_PERSON.copy()
        # 可被覆蓋的設定 - 主管紅利
        self._supervisor_dividend = self.SUPERVISOR_DIVIDEND.copy()
        # 可被覆蓋的設定 - 主管節慶獎金基數
        self._supervisor_festival_bonus = self.SUPERVISOR_FESTIVAL_BONUS.copy()
        # 可被覆蓋的設定 - 司機/美編節慶獎金基數
        self._office_festival_bonus_base = self.OFFICE_FESTIVAL_BONUS_BASE.copy()

    def set_bonus_config(self, bonus_config: dict):
        """
        設定獎金參數（從前端傳入）

        Args:
            bonus_config: {
                'bonusBase': {
                    'headTeacherAB': 2000,
                    'headTeacherC': 1500,
                    'assistantTeacherAB': 1200,
                    'assistantTeacherC': 1200
                },
                'targetEnrollment': {
                    '大班': {'twoTeachers': 27, 'oneTeacher': 14, 'sharedAssistant': 20},
                    ...
                }
            }
        """
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
            self._target_enrollment = {}
            for grade, targets in te.items():
                self._target_enrollment[grade] = {
                    '2_teachers': targets.get('twoTeachers', 0),
                    '1_teacher': targets.get('oneTeacher', 0),
                    'shared_assistant': targets.get('sharedAssistant', 0)
                }

        # 更新超額獎金目標人數
        if 'overtimeTarget' in bonus_config and bonus_config['overtimeTarget']:
            ot = bonus_config['overtimeTarget']
            self._overtime_target = {}
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

        # 更新司機/美編節慶獎金基數
        if 'officeFestivalBonusBase' in bonus_config and bonus_config['officeFestivalBonusBase']:
            ofb = bonus_config['officeFestivalBonusBase']
            self._office_festival_bonus_base = {
                '司機': ofb.get('driver', 1000),
                '美編': ofb.get('designer', 1000)
            }

    def set_deduction_rules(self, rules: dict):
        """設定扣款規則"""
        self.deduction_rules.update(rules)
    
    def calculate_attendance_deduction(self, attendance: AttendanceResult) -> dict:
        """計算考勤扣款"""
        late_rule = self.deduction_rules.get('late', {})
        missing_rule = self.deduction_rules.get('missing', {})
        early_rule = self.deduction_rules.get('early', {})
        
        # 遲到扣款（累計制）
        late_threshold = late_rule.get('threshold', 2)
        late_amount = late_rule.get('amount', 100)
        late_deduction = 0
        if attendance.late_count >= late_threshold:
            late_deduction = (attendance.late_count - late_threshold + 1) * late_amount
        
        # 未打卡扣款
        missing_count = attendance.missing_punch_in_count + attendance.missing_punch_out_count
        missing_deduction = missing_count * missing_rule.get('amount', 50)
        
        # 早退扣款
        early_deduction = attendance.early_leave_count * early_rule.get('amount', 50)
        
        return {
            'late_deduction': late_deduction,
            'missing_punch_deduction': missing_deduction,
            'early_leave_deduction': early_deduction,
            'late_count': attendance.late_count,
            'early_leave_count': attendance.early_leave_count,
            'missing_punch_count': missing_count
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

    def get_position_grade(self, position: str) -> Optional[str]:
        """取得職位等級 (A/B/C)"""
        return self.POSITION_GRADE_MAP.get(position)

    def get_festival_bonus_base(self, position: str, role: str) -> float:
        """
        取得節慶獎金基數

        Args:
            position: 職位 (幼兒園教師/教保員/助理教保員)
            role: 角色 (head_teacher/assistant_teacher)

        Returns:
            獎金基數，若職位不適用則返回 0
        """
        grade = self.get_position_grade(position)
        if not grade or role not in self._bonus_base:
            return 0
        return self._bonus_base[role].get(grade, 0)

    def get_target_enrollment(self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int:
        """
        取得目標人數

        Args:
            grade_name: 年級名稱 (大班/中班/小班/幼幼班)
            has_assistant: 班級是否有副班導
            is_shared_assistant: 是否為共用美師

        Returns:
            目標人數
        """
        if grade_name not in self._target_enrollment:
            return 0

        targets = self._target_enrollment[grade_name]

        if is_shared_assistant:
            return targets.get('shared_assistant', 0)
        elif has_assistant:
            return targets.get('2_teachers', 0)
        else:
            return targets.get('1_teacher', 0)

    def get_supervisor_dividend(self, title: str) -> float:
        """
        取得主管紅利

        Args:
            title: 職稱 (園長/主任/組長/副組長)

        Returns:
            紅利金額，若非主管職則返回 0
        """
        return self._supervisor_dividend.get(title, 0)

    def get_supervisor_festival_bonus(self, title: str) -> Optional[float]:
        """
        取得主管節慶獎金基數

        Args:
            title: 職稱 (園長/主任/組長)

        Returns:
            節慶獎金基數，若非主管職則返回 None
        """
        if title in self._supervisor_festival_bonus:
            return self._supervisor_festival_bonus[title]
        return None

    def is_eligible_for_festival_bonus(self, hire_date, reference_date=None) -> bool:
        """
        檢查員工是否符合領取節慶獎金資格（入職滿3個月）

        Args:
            hire_date: 到職日期 (date 或 str 格式 'YYYY-MM-DD')
            reference_date: 參考日期，預設為今天

        Returns:
            True 如果入職滿3個月，否則 False
        """
        if hire_date is None:
            return True  # 如果沒有到職日期資料，預設可以領

        if isinstance(hire_date, str):
            try:
                hire_date = datetime.strptime(hire_date, '%Y-%m-%d').date()
            except ValueError:
                return True  # 日期格式錯誤，預設可以領

        if reference_date is None:
            reference_date = date.today()
        elif isinstance(reference_date, str):
            reference_date = datetime.strptime(reference_date, '%Y-%m-%d').date()

        # 計算入職滿3個月的日期
        eligible_date = hire_date + relativedelta(months=3)

        return reference_date >= eligible_date

    def get_overtime_target(self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int:
        """取得超額獎金目標人數"""
        if grade_name not in self._overtime_target:
            return 0

        targets = self._overtime_target[grade_name]

        if is_shared_assistant:
            return targets.get('shared_assistant', 0)
        elif has_assistant:
            return targets.get('2_teachers', 0)
        else:
            return targets.get('1_teacher', 0)

    def get_overtime_per_person(self, role: str, grade_name: str) -> float:
        """取得超額獎金每人金額"""
        if role not in self._overtime_per_person:
            return 0
        return self._overtime_per_person[role].get(grade_name, 0)

    def get_office_festival_bonus_base(self, position: str) -> Optional[float]:
        """
        取得司機/美編節慶獎金基數

        Args:
            position: 職位 (司機/美編)

        Returns:
            節慶獎金基數，若非司機/美編則返回 None
        """
        if position in self._office_festival_bonus_base:
            return self._office_festival_bonus_base[position]
        return None

    def calculate_overtime_bonus(
        self,
        role: str,
        grade_name: str,
        current_enrollment: int,
        has_assistant: bool,
        is_shared_assistant: bool = False
    ) -> dict:
        """
        計算超額獎金

        Args:
            role: 角色 (head_teacher/assistant_teacher/art_teacher)
            grade_name: 年級名稱
            current_enrollment: 在籍人數
            has_assistant: 班級是否有副班導
            is_shared_assistant: 是否為共用美師

        Returns:
            包含 overtime_bonus, overtime_target, overtime_count, per_person 的字典
        """
        # 美師特別處理
        if role == 'art_teacher':
            is_shared_assistant = True
            role_for_bonus = 'assistant_teacher'
        else:
            role_for_bonus = role

        # 取得超額目標人數
        overtime_target = self.get_overtime_target(grade_name, has_assistant, is_shared_assistant)

        # 計算超額人數
        overtime_count = max(0, current_enrollment - overtime_target)

        # 取得每人金額
        per_person = self.get_overtime_per_person(role_for_bonus, grade_name)

        # 計算超額獎金
        overtime_bonus = overtime_count * per_person

        return {
            'overtime_bonus': round(overtime_bonus),
            'overtime_target': overtime_target,
            'overtime_count': overtime_count,
            'per_person': per_person
        }

    def calculate_festival_bonus_v2(
        self,
        position: str,
        role: str,
        grade_name: str,
        current_enrollment: int,
        has_assistant: bool,
        is_shared_assistant: bool = False
    ) -> dict:
        """
        計算節慶獎金 (新版 - 依職位等級和角色計算)

        Args:
            position: 職位 (幼兒園教師/教保員/助理教保員)
            role: 角色 (head_teacher/assistant_teacher/art_teacher)
            grade_name: 年級名稱
            current_enrollment: 在籍人數
            has_assistant: 班級是否有副班導
            is_shared_assistant: 是否為共用美師 (美師)

        Returns:
            包含 festival_bonus, overtime_bonus, target, ratio 等的字典
        """
        # 美師特別處理：用 shared_assistant 的目標人數
        if role == 'art_teacher':
            is_shared_assistant = True
            # 美師視為副班導級別
            role_for_bonus = 'assistant_teacher'
        else:
            role_for_bonus = role

        # 取得獎金基數
        base_amount = self.get_festival_bonus_base(position, role_for_bonus)

        # 取得節慶獎金目標人數
        target = self.get_target_enrollment(grade_name, has_assistant, is_shared_assistant)

        # 計算比例和節慶獎金
        if target > 0:
            ratio = current_enrollment / target
            festival_bonus = base_amount * ratio
        else:
            ratio = 0
            festival_bonus = 0

        # 計算超額獎金
        overtime_result = self.calculate_overtime_bonus(
            role=role,
            grade_name=grade_name,
            current_enrollment=current_enrollment,
            has_assistant=has_assistant,
            is_shared_assistant=is_shared_assistant
        )

        return {
            'festival_bonus': round(festival_bonus),
            'overtime_bonus': overtime_result['overtime_bonus'],
            'target': target,
            'ratio': ratio,
            'base_amount': base_amount,
            'overtime_target': overtime_result['overtime_target'],
            'overtime_count': overtime_result['overtime_count'],
            'overtime_per_person': overtime_result['per_person']
        }
    
    def calculate_salary(
        self,
        employee: dict,
        year: int,
        month: int,
        attendance: AttendanceResult = None,
        bonus_settings: dict = None,
        leave_deduction: float = 0,
        allowances: List[dict] = None,
        classroom_context: dict = None
    ) -> SalaryBreakdown:
        """
        計算單一員工薪資

        Args:
            employee: 員工資料字典
            year: 年
            month: 月
            attendance: 考勤資料
            bonus_settings: 舊版獎金設定 (target, current, festival_base...)
            leave_deduction: 請假扣款
            allowances: 津貼列表
            classroom_context: 班級上下文 (新版節慶獎金用)
                - role: 角色 (head_teacher/assistant_teacher/art_teacher)
                - grade_name: 年級名稱
                - current_enrollment: 在籍人數
                - has_assistant: 是否有副班導
                - is_shared_assistant: 是否為共用美師
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
            breakdown.hourly_total = breakdown.hourly_rate * breakdown.work_hours
            breakdown.gross_salary = breakdown.hourly_total
        else:
            # 正職員工
            breakdown.base_salary = employee.get('base_salary', 0)

            # 處理津貼 (從 normalized 列表)
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

            # 相容舊版欄位 (如果有值則累加)
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

            # 檢查是否為主管（園長/主任/組長）- 有特別的節慶獎金基數
            supervisor_festival_base = self.get_supervisor_festival_bonus(emp_title)

            if supervisor_festival_base is not None:
                # 主管使用固定的節慶獎金基數
                if is_eligible:
                    breakdown.festival_bonus = supervisor_festival_base
                else:
                    breakdown.festival_bonus = 0
                # 主管無超額獎金
                breakdown.overtime_bonus = 0
            # 優先使用新版計算 (classroom_context)
            elif classroom_context and employee.get('position'):
                bonus_result = self.calculate_festival_bonus_v2(
                    position=employee.get('position'),
                    role=classroom_context.get('role', ''),
                    grade_name=classroom_context.get('grade_name', ''),
                    current_enrollment=classroom_context.get('current_enrollment', 0),
                    has_assistant=classroom_context.get('has_assistant', False),
                    is_shared_assistant=classroom_context.get('is_shared_assistant', False)
                )
                # 入職未滿3個月，節慶獎金和超額獎金都不發
                if is_eligible:
                    breakdown.festival_bonus = bonus_result['festival_bonus']
                    breakdown.overtime_bonus = bonus_result['overtime_bonus']
                else:
                    breakdown.festival_bonus = 0
                    breakdown.overtime_bonus = 0
            elif bonus_settings:
                # 舊版計算方式 (相容性保留)
                base_amount = bonus_settings.get('festival_base', 0)
                position_bonus_base = bonus_settings.get('position_bonus_base', {})

                # 如果有設定該職位的基數，則優先使用
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
            breakdown.supervisor_dividend = self.get_supervisor_dividend(emp_title)

            # 計算應發總額
            breakdown.gross_salary = (
                breakdown.base_salary +
                breakdown.supervisor_allowance +
                breakdown.teacher_allowance +
                breakdown.meal_allowance +
                breakdown.transportation_allowance +
                breakdown.other_allowance +
                breakdown.festival_bonus +
                breakdown.overtime_bonus +
                breakdown.performance_bonus +
                breakdown.special_bonus +
                breakdown.supervisor_dividend
            )

            # 勞健保計算
            insurance = self.insurance_service.calculate(
                employee.get('insurance_salary', breakdown.base_salary),
                employee.get('dependents', 0)
            )
            breakdown.labor_insurance = insurance.labor_employee
            breakdown.health_insurance = insurance.health_employee
            breakdown.pension_self = insurance.pension_employee
        
        # 考勤扣款
        if attendance:
            att_ded = self.calculate_attendance_deduction(attendance)
            breakdown.late_deduction = att_ded['late_deduction']
            breakdown.early_leave_deduction = att_ded['early_leave_deduction']
            breakdown.missing_punch_deduction = att_ded['missing_punch_deduction']
            breakdown.late_count = att_ded['late_count']
            breakdown.early_leave_count = att_ded['early_leave_count']
            breakdown.missing_punch_count = att_ded['missing_punch_count']
        
        breakdown.leave_deduction = leave_deduction
        
        # 計算扣款總額
        breakdown.total_deduction = (
            breakdown.labor_insurance +
            breakdown.health_insurance +
            breakdown.pension_self +
            breakdown.late_deduction +
            breakdown.early_leave_deduction +
            breakdown.missing_punch_deduction +
            breakdown.leave_deduction +
            breakdown.other_deduction
        )
        
        # 實發金額
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction
        
        return breakdown
