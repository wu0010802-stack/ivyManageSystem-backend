"""
薪資明細 dataclass
"""

from dataclasses import dataclass


@dataclass
class SalaryBreakdown:
    """薪資明細"""

    employee_name: str
    employee_id: str
    year: int
    month: int

    # 應領項目
    base_salary: float = 0

    # 獎金
    festival_bonus: float = 0
    overtime_bonus: float = 0
    performance_bonus: float = 0
    special_bonus: float = 0
    supervisor_dividend: float = 0  # 主管紅利
    overtime_work_pay: float = 0  # 加班費
    meeting_overtime_pay: float = 0  # 園務會議加班費
    birthday_bonus: float = 0  # 生日禮金

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
    missing_punch_deduction: float = 0  # 保留欄位但不再扣款
    leave_deduction: float = 0
    absence_deduction: float = 0  # 曠職扣款（全日無打卡且無核准請假）
    meeting_absence_deduction: float = 0  # 園務會議未出席扣節慶獎金
    other_deduction: float = 0

    # 考勤統計
    late_count: int = 0
    early_leave_count: int = 0
    missing_punch_count: int = 0
    absent_count: int = 0  # 曠職天數
    total_late_minutes: int = 0
    total_early_minutes: int = 0
    meeting_attended: int = 0  # 園務會議出席次數
    meeting_absent: int = 0  # 園務會議缺席次數
    personal_sick_leave_hours: float = 0  # 事假+病假累計時數（超過40h取消獎金）

    # 合計
    gross_salary: float = 0
    total_deduction: float = 0
    net_salary: float = 0

    # 獎金獨立轉帳
    bonus_separate: bool = False
    bonus_amount: float = 0
