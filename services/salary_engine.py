"""
薪資計算引擎 - 整合考勤、獎金、扣款計算
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from .insurance_service import InsuranceService, InsuranceCalculation
from .attendance_parser import AttendanceResult

MONTHLY_BASE_DAYS = 30  # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）


# 資料庫相關匯入（延遲匯入避免循環依賴）
def _get_db_session():
    from models.database import get_session
    return get_session()


def get_working_days(year: int, month: int, session=None) -> int:
    """計算指定月份的法定工作日數（週一至週五，排除國定假日）"""
    import calendar
    from models.database import Holiday, get_session

    cal = calendar.Calendar()
    # 取得當月所有工作日（週一=0 到 週五=4）
    workdays = [d for d in cal.itermonthdays2(year, month)
                if d[0] != 0 and d[1] < 5]

    # 查詢當月國定假日
    _session = session or get_session()
    try:
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        holidays = _session.query(Holiday.date).filter(
            Holiday.date >= month_start,
            Holiday.date <= month_end,
            Holiday.is_active == True
        ).all()
        holiday_dates = {h.date for h in holidays}
    finally:
        if not session:
            _session.close()

    # 排除落在工作日的國定假日
    working_days = len([d for d in workdays if date(year, month, d[0]) not in holiday_dates])
    return working_days


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
    overtime_work_pay: float = 0   # 加班費
    meeting_overtime_pay: float = 0 # 園務會議加班費

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
    missing_punch_deduction: float = 0   # 保留欄位但不再扣款
    leave_deduction: float = 0
    auto_leave_deduction: float = 0      # 遲到2小時以上自動轉事假扣款
    meeting_absence_deduction: float = 0 # 園務會議未出席扣節慶獎金
    other_deduction: float = 0
    
    # 考勤統計
    late_count: int = 0
    early_leave_count: int = 0
    missing_punch_count: int = 0
    total_late_minutes: int = 0
    total_early_minutes: int = 0
    auto_leave_count: int = 0            # 遲到2小時以上自動轉事假次數
    meeting_attended: int = 0            # 園務會議出席次數
    meeting_absent: int = 0              # 園務會議缺席次數
    
    # 合計
    gross_salary: float = 0
    total_deduction: float = 0
    net_salary: float = 0
    
    # 獎金獨立轉帳
    bonus_separate: bool = False
    bonus_amount: float = 0
    
    @property
    def total_allowances(self) -> float:
        return (self.supervisor_allowance + self.teacher_allowance + 
                self.meal_allowance + self.transportation_allowance + 
                self.other_allowance)


class SalaryEngine:
    """薪資計算引擎"""

    # 預設扣款規則
    DEFAULT_LATE_PER_MINUTE = 1       # 遲到每分鐘扣款（會被按比例覆蓋）
    DEFAULT_EARLY_PER_MINUTE = 1      # 早退每分鐘扣款（會被按比例覆蓋）
    DEFAULT_AUTO_LEAVE_THRESHOLD = 120  # 遲到超過幾分鐘轉事假半天
    DEFAULT_MISSING_PUNCH = 0         # 未打卡不扣款（僅記錄）
    DEFAULT_MEETING_PAY = 200         # 園務會議加班費
    DEFAULT_MEETING_PAY_6PM = 100     # 6點下班者園務會議加班費
    DEFAULT_MEETING_ABSENCE_PENALTY = 100  # 園務會議缺席扣節慶獎金

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

    # 司機/美編/行政節慶獎金基數（全校比例計算，無超額獎金）
    OFFICE_FESTIVAL_BONUS_BASE = {
        '司機': 1000,
        '美編': 1000,
        '行政': 2000
    }

    def __init__(self, load_from_db: bool = False):
        self.insurance_service = InsuranceService()
        self.deduction_rules = {
            'late': {'per_minute': 1, 'auto_leave_threshold': 120},
            'missing': {'amount': 0},   # 未打卡不扣款，僅記錄
            'early': {'per_minute': 1}
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
        # 可被覆蓋的設定 - 全校目標人數
        self._school_wide_target = 160
        # 勞退自提固定 6%
        self._pension_self_rate = 0.06
        # 園務會議設定
        self._meeting_pay = self.DEFAULT_MEETING_PAY
        self._meeting_pay_6pm = self.DEFAULT_MEETING_PAY_6PM
        self._meeting_absence_penalty = self.DEFAULT_MEETING_ABSENCE_PENALTY
        # 考勤政策設定（無寬限期）
        self._attendance_policy = {
            'grace_minutes': 0,
            'late_per_minute': 1,
            'early_per_minute': 1,
            'auto_leave_threshold': 120,
            'missing_punch_deduction': 0,
            'festival_bonus_months': 3
        }

        if load_from_db:
            self.load_config_from_db()

    def load_config_from_db(self):
        """
        從資料庫載入設定
        """
        try:
            session = _get_db_session()
            from models.database import AttendancePolicy, BonusConfig as DBBonusConfig, GradeTarget, InsuranceRate

            # 載入考勤政策
            policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
            if policy:
                self._attendance_policy = {
                    'grace_minutes': policy.grace_minutes,
                    'late_per_minute': getattr(policy, 'late_per_minute', 1) or 1,
                    'early_per_minute': getattr(policy, 'early_per_minute', 1) or 1,
                    'auto_leave_threshold': getattr(policy, 'auto_leave_threshold', 120) or 120,
                    'missing_punch_deduction': 0,
                    'festival_bonus_months': policy.festival_bonus_months
                }
                self.deduction_rules = {
                    'late': {
                        'per_minute': self._attendance_policy['late_per_minute'],
                        'auto_leave_threshold': self._attendance_policy['auto_leave_threshold']
                    },
                    'missing': {'amount': 0},
                    'early': {'per_minute': self._attendance_policy['early_per_minute']}
                }

            # 載入獎金設定
            bonus = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).first()
            if bonus:
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

            # 載入年級目標
            targets = session.query(GradeTarget).all()
            if targets:
                self._target_enrollment = {}
                self._overtime_target = {}
                for t in targets:
                    self._target_enrollment[t.grade_name] = {
                        '2_teachers': t.festival_two_teachers,
                        '1_teacher': t.festival_one_teacher,
                        'shared_assistant': t.festival_shared
                    }
                    self._overtime_target[t.grade_name] = {
                        '2_teachers': t.overtime_two_teachers,
                        '1_teacher': t.overtime_one_teacher,
                        'shared_assistant': t.overtime_shared
                    }

            session.close()
            print("SalaryEngine: 已從資料庫載入設定")

        except Exception as e:
            print(f"SalaryEngine: 從資料庫載入設定失敗，使用預設值: {e}")

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
    
    def calculate_attendance_deduction(self, attendance: AttendanceResult, daily_salary: float = 0, base_salary: float = 0, late_details: list = None) -> dict:
        """
        計算考勤扣款

        新規則：
        - 遲到/早退：按分鐘比例扣款（每分鐘 = 月薪 ÷ 30 ÷ 8 ÷ 60，依勞基法固定基準）
        - 無寬限期
        - 遲到超過 2 小時（120 分鐘）：該次不扣分鐘費，改為請事假半天扣薪
        - 未打卡：不扣款，僅記錄次數（供考核用）
        """
        late_rule = self.deduction_rules.get('late', {})
        early_rule = self.deduction_rules.get('early', {})

        # 每分鐘薪資 = 月薪 ÷ 30 ÷ 8 ÷ 60（依勞基法時薪基準，固定 30 天）
        per_minute_rate = base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 1
        
        auto_leave_threshold = late_rule.get('auto_leave_threshold', 120)
        
        late_deduction = 0
        auto_leave_deduction = 0
        auto_leave_count = 0
        normal_late_minutes = 0
        
        if late_details:
            # 有逐筆遲到明細，逐筆判斷是否超過 2 小時
            for minutes in late_details:
                if minutes >= auto_leave_threshold:
                    # 遲到超過 2 小時 → 請事假半天（扣 0.5 天日薪）
                    auto_leave_count += 1
                    auto_leave_deduction += daily_salary * 0.5
                else:
                    # 正常遲到 → 按薪資比例分鐘扣款
                    normal_late_minutes += minutes
                    late_deduction += minutes * per_minute_rate
        else:
            # 沒有逐筆明細，使用總分鐘數按比例計算
            total_late_minutes = attendance.total_late_minutes
            late_deduction = total_late_minutes * per_minute_rate
            normal_late_minutes = total_late_minutes
        
        # 早退扣款（按薪資比例分鐘計算）
        total_early_minutes = attendance.total_early_minutes
        early_deduction = total_early_minutes * per_minute_rate
        
        # 未打卡：不扣款，僅記錄
        missing_count = attendance.missing_punch_in_count + attendance.missing_punch_out_count
        
        return {
            'late_deduction': round(late_deduction),
            'missing_punch_deduction': 0,  # 不扣款
            'early_leave_deduction': round(early_deduction),
            'auto_leave_deduction': round(auto_leave_deduction),
            'auto_leave_count': auto_leave_count,
            'late_count': attendance.late_count,
            'early_leave_count': attendance.early_leave_count,
            'missing_punch_count': missing_count,
            'total_late_minutes': attendance.total_late_minutes,
            'total_early_minutes': total_early_minutes,
            'normal_late_minutes': normal_late_minutes
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
            position: 職位 (幼兒園教師/教保員/助理教保員/職員)
            role: 角色 (head_teacher/assistant_teacher)

        Returns:
            獎金基數
        """
        grade = self.get_position_grade(position)
        if role not in self._bonus_base:
            return 0
            
        # 如果沒有對應的職位等級，預設使用 C 級
        if not grade:
            grade = 'C'
            
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

    def get_supervisor_dividend(self, title: str, position: str = '') -> float:
        """
        取得主管紅利

        Args:
            title: 職務 (園長/主任/組長/副組長)
            position: 職稱，也會檢查

        Returns:
            紅利金額，若非主管職則返回 0
        """
        # 同時檢查 position 和 title，優先使用 position
        if position in self._supervisor_dividend:
            return self._supervisor_dividend[position]
        if title in self._supervisor_dividend:
            return self._supervisor_dividend[title]
        return 0

    def get_supervisor_festival_bonus(self, title: str, position: str = '') -> Optional[float]:
        """
        取得主管節慶獎金基數

        Args:
            title: 職務 (園長/主任/組長)
            position: 職稱，也會檢查

        Returns:
            節慶獎金基數，若非主管職則返回 None
        """
        # 同時檢查 position 和 title，優先使用 position
        if position in self._supervisor_festival_bonus:
            return self._supervisor_festival_bonus[position]
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

    @staticmethod
    def get_bonus_distribution_month(month: int) -> bool:
        """
        判斷是否為節慶獎金發放月
        2月 → 發放 12+1月
        6月 → 發放 2-5月
        9月 → 發放 6-8月
        12月 → 發放 9-11月
        """
        return month in (2, 6, 9, 12)

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

    def get_office_festival_bonus_base(self, position: str, title: str = '') -> Optional[float]:
        """
        取得司機/美編節慶獎金基數

        Args:
            position: 職稱 (司機/美編)
            title: 職務，也會檢查

        Returns:
            節慶獎金基數，若非司機/美編則返回 None
        """
        # 同時檢查 position 和 title，優先使用 position
        if position in self._office_festival_bonus_base:
            return self._office_festival_bonus_base[position]
        if title in self._office_festival_bonus_base:
            return self._office_festival_bonus_base[title]
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
        classroom_context: dict = None,
        office_staff_context: dict = None,
        meeting_context: dict = None,
        working_days: int = 22
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
            office_staff_context: 辦公室人員上下文
            meeting_context: 園務會議上下文
                - attended: 出席次數
                - absent: 缺席次數
                - work_end_time: 員工下班時間 (HH:MM)
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
            emp_position = employee.get('position', '')

            # 檢查是否為主管（園長/主任/組長）- 有特別的節慶獎金基數
            # 檢查是否為主管（園長/主任/組長）- 有特別的節慶獎金基數
            # 同時檢查 title 和 position (已在 helper 中優先檢查 position)
            # 如果 position 為空，則視為不符合資格
            if not emp_position:
                 supervisor_festival_base = None
                 # 也無法領取其他節慶獎金
            else:
                supervisor_festival_base = self.get_supervisor_festival_bonus(emp_title, emp_position)

            if supervisor_festival_base is not None:
                # 主管節慶獎金 = 固定基數 × 全校比例
                # office_staff_context 由 process_salary_calculation 負責準備（主管無論有無班級皆會提供）
                if is_eligible and emp_position:
                    school_enrollment = office_staff_context.get('school_enrollment', 0) if office_staff_context else 0
                    school_target = self._school_wide_target or 160
                    ratio = school_enrollment / school_target if school_target > 0 else 0
                    breakdown.festival_bonus = round(supervisor_festival_base * ratio)
                else:
                    breakdown.festival_bonus = 0
                # 主管無超額獎金
                breakdown.overtime_bonus = 0
            # 辦公室人員（司機/美編/行政）使用全校比例計算
            elif office_staff_context and emp_position:
                office_base = self.get_office_festival_bonus_base(emp_position, emp_title)
                if office_base and is_eligible:
                    school_enrollment = office_staff_context.get('school_enrollment', 0)
                    school_target = self._school_wide_target or 160
                    ratio = school_enrollment / school_target if school_target > 0 else 0
                    breakdown.festival_bonus = round(office_base * ratio)
                else:
                    breakdown.festival_bonus = 0
                # 辦公室人員無超額獎金
                breakdown.overtime_bonus = 0
            # 優先使用新版計算 (classroom_context)
            elif classroom_context and emp_position:
                bonus_result = self.calculate_festival_bonus_v2(
                    position=employee.get('position', ''),
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

            # 計算主管紅利（同時檢查 title 和 position）
            if emp_position:
                 breakdown.supervisor_dividend = self.get_supervisor_dividend(emp_title, emp_position)
            else:
                 breakdown.supervisor_dividend = 0

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

            # 勞健保計算（勞退自提固定 6%）
            insurance = self.insurance_service.calculate(
                employee.get('insurance_salary', breakdown.base_salary),
                employee.get('dependents', 0),
                pension_self_rate=self._pension_self_rate
            )
            breakdown.labor_insurance = insurance.labor_employee
            breakdown.health_insurance = insurance.health_employee
            breakdown.pension_self = insurance.pension_employee
        
        # 考勤扣款（遲到/早退時薪基準：月薪 ÷ 30 ÷ 8，依勞基法固定 30 天）
        base_sal = employee.get('base_salary', 0) or 0
        daily_salary = base_sal / MONTHLY_BASE_DAYS if base_sal else 0
        late_details = employee.get('_late_details', None)  # 逐筆遲到分鐘數列表
        if attendance:
            att_ded = self.calculate_attendance_deduction(
                attendance, daily_salary=daily_salary, base_salary=base_sal, late_details=late_details
            )
            breakdown.late_deduction = att_ded['late_deduction']
            breakdown.early_leave_deduction = att_ded['early_leave_deduction']
            breakdown.missing_punch_deduction = 0  # 不扣款
            breakdown.auto_leave_deduction = att_ded['auto_leave_deduction']
            breakdown.auto_leave_count = att_ded['auto_leave_count']
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
            
            # 加班費：6點下班的員工每次 $100，其他 $200
            if work_end == '18:00':
                per_meeting_pay = self._meeting_pay_6pm
            else:
                per_meeting_pay = self._meeting_pay
            
            breakdown.meeting_overtime_pay = attended * per_meeting_pay
            breakdown.meeting_attended = attended
            breakdown.meeting_absent = absent
            
            # 缺席扣節慶獎金（每次 $100）
            breakdown.meeting_absence_deduction = absent * self._meeting_absence_penalty
        
        # 將園務會議加班費加入應發總額
        breakdown.gross_salary += breakdown.meeting_overtime_pay
        # 將園務會議缺席從節慶獎金扣款
        breakdown.festival_bonus = max(0, breakdown.festival_bonus - breakdown.meeting_absence_deduction)

        # 非發放月份不計節慶獎金（季度合併發放：2月、6月、9月、12月）
        if not self.get_bonus_distribution_month(month):
            breakdown.festival_bonus = 0

        # 計算扣款總額（未打卡不扣款）
        breakdown.total_deduction = (
            breakdown.labor_insurance +
            breakdown.health_insurance +
            breakdown.pension_self +
            breakdown.late_deduction +
            breakdown.early_leave_deduction +
            breakdown.auto_leave_deduction +
            breakdown.leave_deduction +
            breakdown.meeting_absence_deduction +
            breakdown.other_deduction
        )

        # 計算實領薪資
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

        return breakdown

    def calculate_festival_bonus_breakdown(self, employee_id: int, year: int, month: int) -> dict:
        """
        計算單一員工節慶獎金明細 (for UI display)
        """
        session = _get_db_session()
        try:
            from models.database import Employee, Classroom, ClassGrade, JobTitle, Student

            emp = session.query(Employee).get(employee_id)
            if not emp:
                return {}

            # Prepare breakdown data
            # Logic similar to what frontend did: determine category, bonusBase, ratio, remark
            
            # Fetch Classroom info if assigned
            classroom = None
            if emp.classroom_id:
                classroom = session.query(Classroom).get(emp.classroom_id)
            
            # Default values
            bonus_base = 0
            target_enrollment = 0
            current_enrollment = 0
            ratio = 0
            festival_bonus = 0
            remark = ""
            category = ""
            
            # Get Position & Title
            position = emp.position or ''
            # Update title handling using relation if available
            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')
            
            # Check Eligibility
            is_eligible = self.is_eligible_for_festival_bonus(emp.hire_date)
            
            # Position check: If empty, not eligible for festival bonus
            if not position:
                is_eligible = False
                remark = "無職位資料(不發放)"
            elif not is_eligible:
                remark = "未滿3個月"

            # 1. Supervisor (Principal/Director/Leader)
            # Use position as primary key if available, fallback to title_name check only if position matches
            supervisor_base = self.get_supervisor_festival_bonus(title_name, position)
            if supervisor_base:
                category = "主管"
                bonus_base = supervisor_base
                
                # Calculate School Ratio for Supervisor
                total_students = session.query(Student).filter(Student.is_active == True).count()
                current_enrollment = total_students
                
                # Use configured school target if available, otherwise calculate or default
                if hasattr(self, '_school_wide_target') and self._school_wide_target > 0:
                    school_target = self._school_wide_target
                else:
                    school_target = 160 # Fallback default
                
                target_enrollment = school_target
                ratio = current_enrollment / target_enrollment if target_enrollment > 0 else 0
                
                festival_bonus = round(supervisor_base * ratio) if is_eligible else 0
                if is_eligible: remark = "全校比例(主管)"
            
            # 2. Office Staff (Driver/Admin/Designer)
            elif emp.is_office_staff:
                 office_base = self.get_office_festival_bonus_base(position, title_name)

                 category = "辦公室"

                 # Calculate total school enrollment dynamically from students table
                 total_students = session.query(Student).filter(Student.is_active == True).count()
                 current_enrollment = total_students
                 
                 if office_base:
                     bonus_base = office_base
                     
                     # Use configured school target if available, otherwise calculate or default
                     if hasattr(self, '_school_wide_target') and self._school_wide_target > 0:
                         school_target = self._school_wide_target
                     else:
                         school_target = 0
                         for c in all_classrooms:
                             # Use relationship to get grade name
                             grade = c.grade.name if c.grade else None
                             if grade:
                                 school_target += self._target_enrollment.get(grade, {}).get('2_teachers', 0)
                     
                     target_enrollment = school_target if school_target > 0 else 100
                     
                     ratio = current_enrollment / target_enrollment if target_enrollment > 0 else 0
                     festival_bonus = round(bonus_base * ratio) if is_eligible else 0
                     if is_eligible: remark = "全校比例"

            # 3. Classroom Teachers
            elif classroom:
                category = "帶班老師"
                grade_name = classroom.grade.name if classroom.grade else ''
                current_enrollment = session.query(Student).filter(
                    Student.classroom_id == classroom.id,
                    Student.is_active == True
                ).count()
                
                # Determine Role
                role = 'assistant_teacher' # default
                if classroom.head_teacher_id == emp.id:
                    role = 'head_teacher'
                elif classroom.assistant_teacher_id == emp.id:
                    role = 'assistant_teacher'
                elif classroom.art_teacher_id == emp.id:
                    role = 'art_teacher'
                
                has_assistant = (classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0)
                is_shared = False 
                
                role_for_base = role
                if role == 'art_teacher': role_for_base = 'assistant_teacher'

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
            print(f"Error calculating bonus breakdown for {employee_id}: {e}")
            return {
                "name": f"Error: {e}", 
                "festivalBonus": 0
            }
        finally:
            session.close()

    def process_salary_calculation(self, employee_id: int, year: int, month: int):
        """
        處理單一員工薪資計算並儲存結果
        """
        session = _get_db_session()
        try:
            from models.database import Employee, Attendance, SalaryRecord, EmployeeAllowance, AllowanceType, Classroom, ClassGrade, JobTitle, SalaryItem, Student

            # 1. 取得員工資料
            emp = session.query(Employee).get(employee_id)
            if not emp:
                raise ValueError(f"Employee {employee_id} not found")

            # Update title handling using relation if available
            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')

            # 2. 轉換為 dict (供 calculate_salary 使用)
            emp_dict = {
                'employee_id': emp.employee_id,
                'name': emp.name,
                'title': title_name, # Use resolved title
                'position': emp.position,
                'employee_type': emp.employee_type,
                'base_salary': emp.base_salary,
                'hourly_rate': emp.hourly_rate,
                'work_hours': 0, # Will be calculated from attendance or set manually? For now 0 or default
                'supervisor_allowance': emp.supervisor_allowance,
                'teacher_allowance': emp.teacher_allowance,
                'meal_allowance': emp.meal_allowance,
                'transportation_allowance': emp.transportation_allowance,
                'other_allowance': emp.other_allowance,
                # Fallback to base_salary if insurance_salary_level is 0
                'insurance_salary': emp.insurance_salary_level if emp.insurance_salary_level and emp.insurance_salary_level > 0 else emp.base_salary,
                'dependents': emp.dependents,
                'hire_date': emp.hire_date
            }

            # 3. 取得考勤並計算統計
            # Fetch raw attendance records
            # start_date, end_date for the month
            import calendar
            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)

            attendances = session.query(Attendance).filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date
            ).all()

            # Parse attendance
            # We need AttendanceResult object from parser
            # But Parser usually parses excel file.
            # Here we need to aggregate from DB records.
            # Let's create a helper to aggregate or manually calculate here.
            
            # Aggregation with minute-level detail
            late_count = sum(1 for a in attendances if a.is_late)
            early_count = sum(1 for a in attendances if a.is_early_leave)
            missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
            missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
            total_late_minutes = sum(a.late_minutes or 0 for a in attendances if a.is_late)
            total_early_minutes = sum(a.early_leave_minutes or 0 for a in attendances if a.is_early_leave)
            
            # 逐筆遲到分鐘數列表（用於判斷是否超過 2 小時要轉事假）
            late_details = [a.late_minutes or 0 for a in attendances if a.is_late and (a.late_minutes or 0) > 0]
            emp_dict['_late_details'] = late_details
            
            # Work hours for hourly employees (sum difference between punch in/out)
            total_hours = 0
            if emp.employee_type == 'hourly':
                for a in attendances:
                    if a.punch_in_time and a.punch_out_time:
                         diff = (a.punch_out_time - a.punch_in_time).total_seconds() / 3600
                         total_hours += diff
                emp_dict['work_hours'] = round(total_hours, 2)

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
                # EmployeeAllowance.effective_date <= end_date, # simplified
                # (EmployeeAllowance.end_date == None) | (EmployeeAllowance.end_date >= start_date)
            ).all()
            
            for ea in emp_allowances:
                # Need allowance name
                a_type = session.query(AllowanceType).get(ea.allowance_type_id)
                if a_type:
                    allowances.append({
                        'name': a_type.name,
                        'amount': ea.amount
                    })

            # 5. 建構 Classroom Context (Festival Bonus V2)
            classroom_context = None
            office_staff_context = None

            if emp.classroom_id:
                classroom = session.query(Classroom).get(emp.classroom_id)
                if classroom:
                    # Determine role
                    role = 'assistant_teacher'
                    if classroom.head_teacher_id == emp.id:
                        role = 'head_teacher'
                    elif classroom.art_teacher_id == emp.id:
                        role = 'art_teacher'

                    has_assistant = (classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0)

                    # 動態計算班級學生人數
                    student_count = session.query(Student).filter(
                        Student.classroom_id == classroom.id,
                        Student.is_active == True
                    ).count()

                    classroom_context = {
                        'role': role,
                        'grade_name': classroom.grade.name if classroom.grade else '',
                        'current_enrollment': student_count,
                        'has_assistant': has_assistant,
                        'is_shared_assistant': False # Default
                    }

            # 5b. 辦公室人員（司機/美編/行政）使用全校比例
            # 主管現在也需要全校比例，所以只要是主管或辦公室人員都需要 office_staff_context
            # Check if supervisor
            is_supervisor = False
            title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')
            if self.get_supervisor_festival_bonus(title_name, emp.position):
                is_supervisor = True

            # 主管一律需要全校比例（不受 classroom_context 影響，因為主管節慶獎金走全校路徑）
            # 辦公室人員若有 classroom_context 則走班級路徑，不需要 office_staff_context
            if is_supervisor or (emp.is_office_staff and not classroom_context):
                total_students = session.query(Student).filter(Student.is_active == True).count()
                office_staff_context = {
                    'school_enrollment': total_students
                }

            # 5c. 查詢已核准請假記錄，計算請假扣款
            from models.database import LeaveRecord, OvertimeRecord as DBOvertimeRecord
            LEAVE_DEDUCTION_RULES = {
                "personal": 1.0, "sick": 0.5, "menstrual": 0.5,
                "annual": 0.0, "maternity": 0.0, "paternity": 0.0,
            }
            approved_leaves = session.query(LeaveRecord).filter(
                LeaveRecord.employee_id == emp.id,
                LeaveRecord.is_approved == True,
                LeaveRecord.start_date <= end_date,
                LeaveRecord.end_date >= start_date
            ).all()
            leave_deduction_total = 0
            daily_salary = emp.base_salary / MONTHLY_BASE_DAYS if emp.base_salary else 0
            for lv in approved_leaves:
                ratio = LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
                leave_deduction_total += (lv.leave_hours / 8) * daily_salary * ratio
            leave_deduction_total = round(leave_deduction_total)

            # 5d. 查詢已核准加班記錄，計算加班費
            approved_overtimes = session.query(DBOvertimeRecord).filter(
                DBOvertimeRecord.employee_id == emp.id,
                DBOvertimeRecord.is_approved == True,
                DBOvertimeRecord.overtime_date >= start_date,
                DBOvertimeRecord.overtime_date <= end_date
            ).all()
            overtime_work_pay_total = sum(o.overtime_pay or 0 for o in approved_overtimes)

            # 5e. 查詢園務會議記錄
            from models.database import MeetingRecord
            meeting_records = session.query(MeetingRecord).filter(
                MeetingRecord.employee_id == emp.id,
                MeetingRecord.meeting_date >= start_date,
                MeetingRecord.meeting_date <= end_date
            ).all()
            
            meeting_context = None
            if meeting_records:
                meeting_attended = sum(1 for m in meeting_records if m.attended)
                meeting_absent = sum(1 for m in meeting_records if not m.attended)
                meeting_context = {
                    'attended': meeting_attended,
                    'absent': meeting_absent,
                    'work_end_time': emp.work_end_time or '17:00'
                }

            # 6. 計算薪資（working_days 僅保留給請假扣款日薪，考勤扣款已改用 MONTHLY_BASE_DAYS）
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
            )
            # 加入加班費
            breakdown.overtime_work_pay = overtime_work_pay_total
            breakdown.gross_salary += overtime_work_pay_total
            breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

            # 7. 儲存 SalaryRecord
            # check if exists
            salary_record = session.query(SalaryRecord).filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month
            ).first()

            if not salary_record:
                salary_record = SalaryRecord(
                    employee_id=emp.id,
                    salary_year=year,
                    salary_month=month
                )
                session.add(salary_record)
            
            # Update fields
            salary_record.base_salary = breakdown.base_salary
            salary_record.supervisor_allowance = breakdown.supervisor_allowance
            salary_record.teacher_allowance = breakdown.teacher_allowance
            salary_record.meal_allowance = breakdown.meal_allowance
            salary_record.transportation_allowance = breakdown.transportation_allowance
            salary_record.other_allowance = breakdown.other_allowance
            
            salary_record.festival_bonus = breakdown.festival_bonus
            salary_record.overtime_bonus = breakdown.overtime_bonus
            salary_record.performance_bonus = breakdown.performance_bonus
            salary_record.special_bonus = breakdown.special_bonus
            salary_record.bonus_amount = breakdown.supervisor_dividend
            salary_record.overtime_pay = breakdown.overtime_work_pay
            salary_record.meeting_overtime_pay = breakdown.meeting_overtime_pay
            salary_record.meeting_absence_deduction = breakdown.meeting_absence_deduction

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
            salary_record.other_deduction = breakdown.other_deduction
            
            salary_record.gross_salary = breakdown.gross_salary
            salary_record.total_deduction = breakdown.total_deduction
            salary_record.net_salary = breakdown.net_salary
            
            salary_record.late_count = breakdown.late_count
            salary_record.early_leave_count = breakdown.early_leave_count
            salary_record.missing_punch_count = breakdown.missing_punch_count
            
            # Save items (clear old?)
            # Simplified: just update record for now.
            
            session.commit()
            
            # Return breakdown or record
            return breakdown

        except Exception as e:
            session.rollback()
            print(f"Error processing salary for {employee_id}: {e}")
            raise e
        finally:
            session.close()
