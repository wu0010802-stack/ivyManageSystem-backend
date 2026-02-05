"""
薪資計算引擎 - 整合考勤、獎金、扣款計算
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import date
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
    
    def __init__(self):
        self.insurance_service = InsuranceService()
        self.deduction_rules = {
            'late': {'threshold': 2, 'amount': 100},
            'missing': {'amount': 50},
            'early': {'amount': 50}
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
        """計算獎金"""
        ratio = current / target if target > 0 else 0
        festival_bonus = base_amount * ratio
        overtime_bonus = max(0, current - target) * overtime_per
        return {
            'festival_bonus': round(festival_bonus),
            'overtime_bonus': round(overtime_bonus),
            'ratio': ratio
        }
    
    def calculate_salary(
        self,
        employee: dict,
        year: int,
        month: int,
        attendance: AttendanceResult = None,
        bonus_settings: dict = None,
        leave_deduction: float = 0,
        allowances: List[dict] = None
    ) -> SalaryBreakdown:
        """計算單一員工薪資"""
        
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
            
            # 獎金計算
            if bonus_settings:
                bonus = self.calculate_bonus(
                    bonus_settings.get('target', 0),
                    bonus_settings.get('current', 0),
                    bonus_settings.get('festival_base', 0),
                    bonus_settings.get('overtime_per', 500)
                )
                breakdown.festival_bonus = bonus['festival_bonus']
                breakdown.overtime_bonus = bonus['overtime_bonus']
            
            breakdown.performance_bonus = employee.get('performance_bonus', 0)
            breakdown.special_bonus = employee.get('special_bonus', 0)
            
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
                breakdown.special_bonus
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
