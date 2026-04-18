"""
薪資計算引擎 - SalaryEngine 類別
"""

import calendar
import logging
from collections import defaultdict
from typing import Dict, List, Optional
from datetime import date, datetime, time, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from ..insurance_service import InsuranceService, InsuranceCalculation
from ..attendance_parser import AttendanceResult
from .constants import (
    MONTHLY_BASE_DAYS,
    MAX_DAILY_WORK_HOURS,
    FESTIVAL_BONUS_BASE,
    TARGET_ENROLLMENT,
    OVERTIME_TARGET,
    OVERTIME_BONUS_PER_PERSON,
    SUPERVISOR_DIVIDEND,
    SUPERVISOR_FESTIVAL_BONUS,
    OFFICE_FESTIVAL_BONUS_BASE,
    POSITION_GRADE_MAP,
    DEFAULT_LATE_PER_MINUTE,
    DEFAULT_EARLY_PER_MINUTE,
    DEFAULT_MISSING_PUNCH,
    DEFAULT_MEETING_PAY,
    DEFAULT_MEETING_PAY_6PM,
    DEFAULT_MEETING_ABSENCE_PENALTY,
)
from .breakdown import SalaryBreakdown
from .hourly import _compute_hourly_daily_hours, _calc_daily_hourly_pay
from .proration import _prorate_for_period, _build_expected_workdays
from .utils import (
    get_bonus_distribution_month,
    get_meeting_deduction_period_start,
    _sum_leave_deduction,
    calc_daily_salary,
)
from . import festival as _festival
from services.student_enrollment import count_students_active_on

logger = logging.getLogger(__name__)


def _get_db_session():
    from models.database import get_session

    return get_session()


def _fill_salary_record(salary_record, breakdown, engine):
    """將 SalaryBreakdown 的欄位填入 SalaryRecord（供正常路徑與 IntegrityError retry 共用）。"""
    salary_record.bonus_config_id = engine._bonus_config_id
    salary_record.attendance_policy_id = engine._attendance_policy_id

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
        breakdown.festival_bonus
        + breakdown.overtime_bonus
        + breakdown.supervisor_dividend
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


class SalaryEngine:
    """薪資計算引擎"""

    # 預設扣款規則
    DEFAULT_LATE_PER_MINUTE = DEFAULT_LATE_PER_MINUTE
    DEFAULT_EARLY_PER_MINUTE = DEFAULT_EARLY_PER_MINUTE
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

    @staticmethod
    def _check_not_finalized(salary_record, emp_name: str, year: int, month: int):
        """若薪資已封存，拋出 ValueError 阻止覆寫。"""
        if salary_record and salary_record.is_finalized:
            raise ValueError(
                f"員工「{emp_name}」{year} 年 {month} 月薪資已封存（is_finalized=True），"
                "禁止覆寫。如需重新計算，請先至薪資管理頁面解除該月封存。"
            )

    def __init__(self, load_from_db: bool = False):
        self.insurance_service = InsuranceService()
        # 記錄目前載入的設定版本 ID，供薪資紀錄稽核用
        self._bonus_config_id: Optional[int] = None
        self._attendance_policy_id: Optional[int] = None
        self.deduction_rules = {
            "late": {"per_minute": 1},
            "missing": {"amount": 0},  # 未打卡不扣款，僅記錄
            "early": {"per_minute": 1},
        }
        # 可被覆蓋的設定 - 節慶獎金（深拷貝，避免測試修改巢狀 dict 時汙染常數）
        self._bonus_base = {k: dict(v) for k, v in FESTIVAL_BONUS_BASE.items()}
        self._target_enrollment = {k: dict(v) for k, v in TARGET_ENROLLMENT.items()}
        # 可被覆蓋的設定 - 超額獎金
        self._overtime_target = {k: dict(v) for k, v in OVERTIME_TARGET.items()}
        self._overtime_per_person = {
            k: dict(v) for k, v in OVERTIME_BONUS_PER_PERSON.items()
        }
        # 可被覆蓋的設定 - 主管紅利
        self._supervisor_dividend = dict(SUPERVISOR_DIVIDEND)
        # 可被覆蓋的設定 - 主管節慶獎金基數
        self._supervisor_festival_bonus = dict(SUPERVISOR_FESTIVAL_BONUS)
        # 可被覆蓋的設定 - 司機/美編節慶獎金基數
        self._office_festival_bonus_base = dict(OFFICE_FESTIVAL_BONUS_BASE)
        # 可被覆蓋的設定 - 全校目標人數
        self._school_wide_target = 160
        # 職位標準底薪（key: 'driver'/'head_teacher_b'/… → float）
        self._position_salary_standards: dict = {}
        # 園務會議設定
        self._meeting_pay = DEFAULT_MEETING_PAY
        self._meeting_pay_6pm = DEFAULT_MEETING_PAY_6PM
        self._meeting_absence_penalty = DEFAULT_MEETING_ABSENCE_PENALTY
        # 考勤政策設定
        self._attendance_policy = {
            "late_per_minute": 1,
            "early_per_minute": 1,
            "missing_punch_deduction": 0,
            "festival_bonus_months": 3,
        }

        if load_from_db:
            self.load_config_from_db()

    def load_config_from_db(self):
        """從資料庫載入設定"""
        try:
            session = _get_db_session()
            from models.database import (
                AttendancePolicy,
                BonusConfig as DBBonusConfig,
                GradeTarget,
                InsuranceRate,
            )

            # 載入考勤政策
            policy = (
                session.query(AttendancePolicy)
                .filter(AttendancePolicy.is_active == True)
                .first()
            )
            if policy:
                self._attendance_policy_id = policy.id  # 記錄版本 ID
                self._attendance_policy = {
                    "late_per_minute": getattr(policy, "late_per_minute", 1) or 1,
                    "early_per_minute": getattr(policy, "early_per_minute", 1) or 1,
                    "missing_punch_deduction": 0,
                    "festival_bonus_months": policy.festival_bonus_months,
                }
                self.deduction_rules = {
                    "late": {
                        "per_minute": self._attendance_policy["late_per_minute"],
                    },
                    "missing": {"amount": 0},
                    "early": {
                        "per_minute": self._attendance_policy["early_per_minute"]
                    },
                }

            # 載入獎金設定
            bonus = (
                session.query(DBBonusConfig)
                .filter(DBBonusConfig.is_active == True)
                .first()
            )
            if bonus:
                self._bonus_config_id = bonus.id  # 記錄版本 ID
                # 更新獎金基數
                self._bonus_base = {
                    "head_teacher": {
                        "A": bonus.head_teacher_ab,
                        "B": bonus.head_teacher_ab,
                        "C": bonus.head_teacher_c,
                    },
                    "assistant_teacher": {
                        "A": bonus.assistant_teacher_ab,
                        "B": bonus.assistant_teacher_ab,
                        "C": bonus.assistant_teacher_c,
                    },
                }
                # 更新主管節慶獎金
                self._supervisor_festival_bonus = {
                    "園長": bonus.principal_festival,
                    "主任": bonus.director_festival,
                    "組長": bonus.leader_festival,
                }
                # 更新司機/美編/行政節慶獎金
                self._office_festival_bonus_base = {
                    "司機": bonus.driver_festival,
                    "美編": bonus.designer_festival,
                    "行政": bonus.admin_festival,
                }
                # 更新主管紅利
                self._supervisor_dividend = {
                    "園長": bonus.principal_dividend,
                    "主任": bonus.director_dividend,
                    "組長": bonus.leader_dividend,
                    "副組長": bonus.vice_leader_dividend,
                }
                # 更新超額獎金每人金額
                self._overtime_per_person = {
                    "head_teacher": {
                        "大班": bonus.overtime_head_normal,
                        "中班": bonus.overtime_head_normal,
                        "小班": bonus.overtime_head_normal,
                        "幼幼班": bonus.overtime_head_baby,
                    },
                    "assistant_teacher": {
                        "大班": bonus.overtime_assistant_normal,
                        "中班": bonus.overtime_assistant_normal,
                        "小班": bonus.overtime_assistant_normal,
                        "幼幼班": bonus.overtime_assistant_baby,
                    },
                }

                # 更新全校目標人數
                if bonus.school_wide_target:
                    self._school_wide_target = bonus.school_wide_target

            # 載入年級目標：合併 NULL（舊資料）與版本特定目標
            null_targets = {
                t.grade_name: t
                for t in session.query(GradeTarget)
                .filter(GradeTarget.bonus_config_id == None)  # noqa: E711
                .all()
            }
            versioned_targets = {}
            if bonus:
                versioned_targets = {
                    t.grade_name: t
                    for t in session.query(GradeTarget)
                    .filter(GradeTarget.bonus_config_id == bonus.id)
                    .all()
                }
            # 合併：版本目標優先覆蓋 NULL 目標
            merged = {**null_targets, **versioned_targets}
            if merged:
                self._target_enrollment = {}
                self._overtime_target = {}
                for grade_name, t in merged.items():
                    self._target_enrollment[grade_name] = {
                        "2_teachers": t.festival_two_teachers,
                        "1_teacher": t.festival_one_teacher,
                        "shared_assistant": t.festival_shared,
                    }
                    self._overtime_target[grade_name] = {
                        "2_teachers": t.overtime_two_teachers,
                        "1_teacher": t.overtime_one_teacher,
                        "shared_assistant": t.overtime_shared,
                    }

            # 載入職位標準底薪
            from models.database import PositionSalaryConfig

            pos_cfg = (
                session.query(PositionSalaryConfig)
                .order_by(PositionSalaryConfig.id.desc())
                .first()
            )
            _pos_defaults = {
                "head_teacher_a": 39240,
                "head_teacher_b": 37160,
                "head_teacher_c": 33000,
                "assistant_teacher_a": 35240,
                "assistant_teacher_b": 33000,
                "assistant_teacher_c": 29500,
                "admin_staff": 37160,
                "english_teacher": 32500,
                "art_teacher": 30000,
                "designer": 30000,
                "nurse": 29800,
                "driver": 30000,
                "kitchen_staff": 29700,
            }
            self._position_salary_standards = {
                k: float(getattr(pos_cfg, k, None) or v) if pos_cfg else float(v)
                for k, v in _pos_defaults.items()
            }
            # director / principal 允許為 None（留空表示不套標準）
            for _role in ("director", "principal"):
                _val = getattr(pos_cfg, _role, None) if pos_cfg else None
                self._position_salary_standards[_role] = float(_val) if _val else None

            session.close()
            logger.info("SalaryEngine: 已從資料庫載入設定")

        except Exception as e:
            logger.warning("SalaryEngine: 從資料庫載入設定失敗，使用預設值: %s", e)

    def set_bonus_config(self, bonus_config: dict):
        """設定獎金參數（從前端傳入）"""
        if not bonus_config:
            return

        # 更新獎金基數
        if "bonusBase" in bonus_config and bonus_config["bonusBase"]:
            bb = bonus_config["bonusBase"]
            self._bonus_base = {
                "head_teacher": {
                    "A": bb.get("headTeacherAB", 2000),
                    "B": bb.get("headTeacherAB", 2000),
                    "C": bb.get("headTeacherC", 1500),
                },
                "assistant_teacher": {
                    "A": bb.get("assistantTeacherAB", 1200),
                    "B": bb.get("assistantTeacherAB", 1200),
                    "C": bb.get("assistantTeacherC", 1200),
                },
            }

        # 更新節慶獎金目標人數
        if "targetEnrollment" in bonus_config and bonus_config["targetEnrollment"]:
            te = bonus_config["targetEnrollment"]
            for grade, targets in te.items():
                self._target_enrollment[grade] = {
                    "2_teachers": targets.get("twoTeachers", 0),
                    "1_teacher": targets.get("oneTeacher", 0),
                    "shared_assistant": targets.get("sharedAssistant", 0),
                }

        # 更新超額獎金目標人數
        if "overtimeTarget" in bonus_config and bonus_config["overtimeTarget"]:
            ot = bonus_config["overtimeTarget"]
            for grade, targets in ot.items():
                self._overtime_target[grade] = {
                    "2_teachers": targets.get("twoTeachers", 0),
                    "1_teacher": targets.get("oneTeacher", 0),
                    "shared_assistant": targets.get("sharedAssistant", 0),
                }

        # 更新超額獎金每人金額
        if "overtimePerPerson" in bonus_config and bonus_config["overtimePerPerson"]:
            op = bonus_config["overtimePerPerson"]
            self._overtime_per_person = {
                "head_teacher": {
                    "大班": op.get("headBig", 400),
                    "中班": op.get("headMid", 400),
                    "小班": op.get("headSmall", 400),
                    "幼幼班": op.get("headBaby", 450),
                },
                "assistant_teacher": {
                    "大班": op.get("assistantBig", 100),
                    "中班": op.get("assistantMid", 100),
                    "小班": op.get("assistantSmall", 100),
                    "幼幼班": op.get("assistantBaby", 150),
                },
            }

        # 更新主管紅利
        if "supervisorDividend" in bonus_config and bonus_config["supervisorDividend"]:
            sd = bonus_config["supervisorDividend"]
            self._supervisor_dividend = {
                "園長": sd.get("principal", 5000),
                "主任": sd.get("director", 4000),
                "組長": sd.get("leader", 3000),
                "副組長": sd.get("viceLeader", 1500),
            }

        # 更新主管節慶獎金基數
        if (
            "supervisorFestivalBonus" in bonus_config
            and bonus_config["supervisorFestivalBonus"]
        ):
            sfb = bonus_config["supervisorFestivalBonus"]
            self._supervisor_festival_bonus = {
                "園長": sfb.get("principal", 6500),
                "主任": sfb.get("director", 3500),
                "組長": sfb.get("leader", 2000),
            }

        # 更新司機/美編/行政節慶獎金基數
        if (
            "officeFestivalBonusBase" in bonus_config
            and bonus_config["officeFestivalBonusBase"]
        ):
            ofb = bonus_config["officeFestivalBonusBase"]
            self._office_festival_bonus_base = {
                "司機": ofb.get("driver", 1000),
                "美編": ofb.get("designer", 1000),
                "行政": ofb.get("admin", 2000),
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

    def get_target_enrollment(
        self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False
    ) -> int:
        """取得目標人數"""
        return _festival.get_target_enrollment(
            grade_name, has_assistant, is_shared_assistant, self._target_enrollment
        )

    def get_supervisor_dividend(
        self, title: str, position: str = "", supervisor_role: str = ""
    ) -> float:
        """取得主管紅利"""
        return _festival.get_supervisor_dividend(
            title, position, self._supervisor_dividend, supervisor_role
        )

    def get_supervisor_festival_bonus(
        self, title: str, position: str = "", supervisor_role: str = ""
    ) -> Optional[float]:
        """取得主管節慶獎金基數"""
        return _festival.get_supervisor_festival_bonus(
            title, position, self._supervisor_festival_bonus, supervisor_role
        )

    def get_office_festival_bonus_base(
        self, position: str, title: str = ""
    ) -> Optional[float]:
        """取得司機/美編節慶獎金基數"""
        return _festival.get_office_festival_bonus_base(
            position, title, self._office_festival_bonus_base
        )

    def get_overtime_target(
        self, grade_name: str, has_assistant: bool, is_shared_assistant: bool = False
    ) -> int:
        """取得超額獎金目標人數"""
        return _festival.get_overtime_target(
            grade_name, has_assistant, is_shared_assistant, self._overtime_target
        )

    def get_overtime_per_person(self, role: str, grade_name: str) -> float:
        """取得超額獎金每人金額"""
        return _festival.get_overtime_per_person(
            role, grade_name, self._overtime_per_person
        )

    def is_eligible_for_festival_bonus(self, hire_date, reference_date=None) -> bool:
        """檢查員工是否符合領取節慶獎金資格（入職滿3個月）"""
        festival_months = self._attendance_policy.get("festival_bonus_months", 3)
        return _festival.is_eligible_for_festival_bonus(
            hire_date, reference_date, festival_months
        )

    # ─── Static method wrappers（委派至 proration.py / utils.py 純函式） ─────

    @staticmethod
    def _prorate_base_salary(
        contracted_base: float, hire_date_raw, year: int, month: int
    ) -> float:
        """月中入職者底薪折算（向後相容靜態方法）"""
        from .proration import _prorate_base_salary

        return _prorate_base_salary(contracted_base, hire_date_raw, year, month)

    @staticmethod
    def _prorate_for_period(
        contracted_base, hire_date_raw, resign_date_raw, year, month
    ):
        """計算當月實際在職天數的底薪折算"""
        return _prorate_for_period(
            contracted_base, hire_date_raw, resign_date_raw, year, month
        )

    @staticmethod
    def _build_expected_workdays(
        year,
        month,
        holiday_set,
        daily_shift_map,
        hire_date_raw=None,
        resign_date_raw=None,
        today=None,
    ):
        """建立指定月份的預期上班日集合"""
        return _build_expected_workdays(
            year,
            month,
            holiday_set,
            daily_shift_map,
            hire_date_raw,
            resign_date_raw,
            today,
        )

    @staticmethod
    def get_bonus_distribution_month(month: int) -> bool:
        """判斷是否為節慶獎金發放月"""
        return get_bonus_distribution_month(month)

    @staticmethod
    def get_meeting_deduction_period_start(year: int, month: int):
        """返回發放月的會議缺席扣款起算日"""
        return get_meeting_deduction_period_start(year, month)

    @staticmethod
    def _get_bonus_reference_date(year: int, month: int) -> date:
        """節慶獎金資格判斷固定以薪資月份首日為準。"""
        return date(year, month, 1)

    @staticmethod
    def _get_effective_bonus_title(
        title: str, bonus_grade_override: Optional[str]
    ) -> str:
        """依 bonus_grade 覆蓋節慶獎金職稱等級。

        bonus_grade 接受大小寫（DB 可能存 "a" 或 "A"），與 _resolve_standard_base
        line 1493 的處理一致，這裡也統一轉大寫再查表。
        """
        grade_to_title = {"A": "幼兒園教師", "B": "教保員", "C": "助理教保員"}
        if not bonus_grade_override:
            return title
        return grade_to_title.get(bonus_grade_override.upper(), title)

    def _calculate_classroom_bonus_result(
        self, bonus_title: str, classroom_context: Optional[dict]
    ) -> dict:
        """統一帶班老師節慶獎金計算，避免主流程與明細頁規則分叉。"""
        if not classroom_context:
            return {
                "festival_bonus": 0,
                "overtime_bonus": 0,
                "target": 0,
                "ratio": 0,
                "base_amount": 0,
                "overtime_target": 0,
                "overtime_count": 0,
                "overtime_per_person": 0,
            }

        bonus_result = self.calculate_festival_bonus_v2(
            position=bonus_title,
            role=classroom_context.get("role", ""),
            grade_name=classroom_context.get("grade_name", ""),
            current_enrollment=classroom_context.get("current_enrollment", 0),
            has_assistant=classroom_context.get("has_assistant", False),
            is_shared_assistant=classroom_context.get("is_shared_assistant", False),
        )

        # 共用副班導：取所有共用班的分數平均（含本班 + shared_other_classes 全部）
        # shared_other_classes 為新介面（list），shared_second_class 為向下相容（單班）
        other_classes = classroom_context.get("shared_other_classes")
        if not other_classes:
            shared_second = classroom_context.get("shared_second_class")
            other_classes = [shared_second] if shared_second else []
        if not other_classes:
            return bonus_result

        other_results = [
            self.calculate_festival_bonus_v2(
                position=bonus_title,
                role=classroom_context.get("role", ""),
                grade_name=oc.get("grade_name", ""),
                current_enrollment=oc.get("current_enrollment", 0),
                has_assistant=True,
                is_shared_assistant=True,
            )
            for oc in other_classes
        ]
        all_results = [bonus_result, *other_results]
        # 按在籍人數加權平均：人數多的班代表授課負擔較重，獎金占比應較大。
        # 若總在籍為 0（全部班級無學生），避免 ZeroDivisionError 並回傳 0。
        main_enrollment = classroom_context.get("current_enrollment", 0) or 0
        enrollments = [
            main_enrollment,
            *[(oc.get("current_enrollment", 0) or 0) for oc in other_classes],
        ]
        total_weight = sum(enrollments)
        if total_weight > 0:
            averaged_festival_bonus = round(
                sum(r["festival_bonus"] * w for r, w in zip(all_results, enrollments))
                / total_weight
            )
            averaged_overtime_bonus = round(
                sum(r["overtime_bonus"] * w for r, w in zip(all_results, enrollments))
                / total_weight
            )
        else:
            averaged_festival_bonus = 0
            averaged_overtime_bonus = 0
        base_amount = bonus_result.get("base_amount", 0) or 0
        averaged_ratio = (averaged_festival_bonus / base_amount) if base_amount else 0

        return {
            **bonus_result,
            "festival_bonus": averaged_festival_bonus,
            "overtime_bonus": averaged_overtime_bonus,
            "ratio": averaged_ratio,
            "shared_second_result": other_results[0],
            "shared_other_results": other_results,
        }

    def _build_classroom_context_from_db(
        self,
        session,
        classroom,
        employee_id: int,
        reference_date: date,
        classroom_count_map: dict | None = None,
    ) -> Optional[dict]:
        """從 DB 班級資料建構帶班獎金計算上下文。

        reference_date:       薪資對應月份的查詢基準日（通常是該月月末）。
                              明細頁與正式計算必須用同一日期，否則在籍人數
                              會以「今天」漂移，明細對不上已計算的薪資記錄。
        classroom_count_map:  可傳入預先批次查詢的 {classroom_id: int}，避免 N+1。
        """
        if not classroom:
            return None

        from models.database import Classroom as DBClassroom, Student

        role = "assistant_teacher"
        if classroom.head_teacher_id == employee_id:
            role = "head_teacher"
        elif classroom.art_teacher_id == employee_id:
            role = "art_teacher"

        has_assistant = (
            classroom.assistant_teacher_id is not None
            and classroom.assistant_teacher_id > 0
        )
        if classroom_count_map is not None:
            student_count = classroom_count_map.get(classroom.id, 0)
        else:
            student_count = count_students_active_on(
                session, reference_date, classroom.id
            )

        if not classroom.grade:
            logger.warning(
                "班級 %s（id=%d）缺少 grade_id，節慶獎金將計算為 0。"
                "請至班級管理設定年級後重新計算薪資。",
                classroom.name,
                classroom.id,
            )
        classroom_context = {
            "role": role,
            "grade_name": classroom.grade.name if classroom.grade else "",
            "current_enrollment": student_count,
            "has_assistant": has_assistant,
            "is_shared_assistant": False,
        }

        if role == "assistant_teacher":
            # is_active 過濾與 process_bulk_salary_calculation 預載一致（line 2093），
            # 否則同一員工在單筆與批次路徑會得到不同的 is_shared_assistant 判斷。
            shared_classes = (
                session.query(DBClassroom)
                .options(joinedload(DBClassroom.grade))
                .filter(
                    DBClassroom.assistant_teacher_id == employee_id,
                    DBClassroom.is_active == True,
                )
                .all()
            )
            if len(shared_classes) >= 2:
                classroom_context["is_shared_assistant"] = True
                other_classes = [c for c in shared_classes if c.id != classroom.id]
                other_payload = []
                for other in other_classes:
                    if classroom_count_map is not None:
                        other_count = classroom_count_map.get(other.id, 0)
                    else:
                        other_count = count_students_active_on(
                            session, reference_date, other.id
                        )
                    other_payload.append(
                        {
                            "grade_name": other.grade.name if other.grade else "",
                            "current_enrollment": other_count,
                        }
                    )
                if other_payload:
                    classroom_context["shared_other_classes"] = other_payload
                    # 向下相容：保留 shared_second_class（首個其他班）
                    classroom_context["shared_second_class"] = other_payload[0]

        return classroom_context

    def _build_classroom_context_from_batch(
        self,
        emp,
        classroom,
        db_count_map: dict,
        assistant_to_classes: dict,
    ) -> Optional[dict]:
        """使用批次預載資料建構帶班獎金計算上下文（與 _build_classroom_context_from_db 邏輯一致）。"""
        if not classroom:
            return None

        role = "assistant_teacher"
        if classroom.head_teacher_id == emp.id:
            role = "head_teacher"
        elif classroom.art_teacher_id == emp.id:
            role = "art_teacher"

        has_assistant = (
            classroom.assistant_teacher_id is not None
            and classroom.assistant_teacher_id > 0
        )
        student_count = db_count_map.get(classroom.id, 0)
        is_shared_assistant = False
        shared_other_classes: list[dict] = []

        if role == "assistant_teacher":
            shared_classes = assistant_to_classes.get(emp.id, [])
            if len(shared_classes) >= 2:
                is_shared_assistant = True
                for other in shared_classes:
                    if other.id == classroom.id:
                        continue
                    shared_other_classes.append(
                        {
                            "grade_name": other.grade.name if other.grade else "",
                            "current_enrollment": db_count_map.get(other.id, 0),
                        }
                    )

        if not classroom.grade:
            logger.warning(
                "班級 %s（id=%d）缺少 grade_id，節慶獎金將計算為 0。",
                classroom.name,
                classroom.id,
            )
        classroom_context = {
            "role": role,
            "grade_name": (classroom.grade.name if classroom.grade else ""),
            "current_enrollment": student_count,
            "has_assistant": has_assistant,
            "is_shared_assistant": is_shared_assistant,
        }
        if shared_other_classes:
            classroom_context["shared_other_classes"] = shared_other_classes
            # 向下相容：保留 shared_second_class（首個其他班）
            classroom_context["shared_second_class"] = shared_other_classes[0]

        return classroom_context

    def _build_office_staff_context(
        self,
        emp,
        total_students: int,
        classroom_context: Optional[dict],
    ) -> Optional[dict]:
        """判斷員工是否為主管或辦公室人員，回傳 office_staff_context。"""
        title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
        supervisor_role = emp.supervisor_role or ""
        is_supervisor = (
            self.get_supervisor_festival_bonus(
                title_name, emp.position or "", supervisor_role
            )
            is not None
        )
        office_bonus_base = self.get_office_festival_bonus_base(
            emp.position or "", title_name
        )
        if is_supervisor or (office_bonus_base is not None and not classroom_context):
            return {"school_enrollment": total_students}
        return None

    # ─── 主要計算方法 ────────────────────────────────────────────────────────

    def _calculate_base_gross(
        self, breakdown, employee: dict, year: int, month: int, allowances
    ) -> float:
        """計算底薪折算與各類津貼，回傳 contracted_base（用於後續保險計算）。"""
        contracted_base = employee.get("base_salary", 0) or 0
        breakdown.base_salary = _prorate_for_period(
            contracted_base,
            employee.get("hire_date"),
            employee.get("resign_date"),
            year,
            month,
        )

        # 處理津貼：以 AllowanceType.code 為準（穩定識別碼），不再依賴中文 name substring。
        # code 對應：見 scripts/migrate_allowances.py 預設 5 種；未知 code 一律進 other。
        _CODE_TO_FIELD = {
            "supervisor": "supervisor_allowance",
            "teacher": "teacher_allowance",
            "meal": "meal_allowance",
            "transportation": "transportation_allowance",
        }
        if allowances:
            for allowance in allowances:
                amount = allowance.get("amount", 0) or 0
                code = allowance.get("code", "")
                field = _CODE_TO_FIELD.get(code, "other_allowance")
                setattr(breakdown, field, getattr(breakdown, field) + amount)
        else:
            # 向下相容：尚未跑 scripts/migrate_allowances.py 的舊資料，
            # 仍從 Employee 直接欄位讀取。已遷移者 list 必非空，不會雙倍計。
            breakdown.supervisor_allowance += employee.get("supervisor_allowance", 0) or 0
            breakdown.teacher_allowance += employee.get("teacher_allowance", 0) or 0
            breakdown.meal_allowance += employee.get("meal_allowance", 0) or 0
            breakdown.transportation_allowance += employee.get("transportation_allowance", 0) or 0
            breakdown.other_allowance += employee.get("other_allowance", 0) or 0

        breakdown.performance_bonus = employee.get("performance_bonus", 0)
        breakdown.special_bonus = employee.get("special_bonus", 0)

        return contracted_base

    def _calculate_bonuses(
        self,
        breakdown,
        employee: dict,
        year: int,
        month: int,
        classroom_context,
        office_staff_context,
        bonus_settings,
        personal_sick_leave_hours: float,
    ) -> None:
        """計算節慶獎金、超額獎金、主管紅利、生日禮金，並寫入 breakdown。"""
        hire_date = employee.get("hire_date")
        bonus_reference_date = self._get_bonus_reference_date(year, month)
        is_eligible = self.is_eligible_for_festival_bonus(
            hire_date,
            reference_date=bonus_reference_date,
        )

        emp_title = employee.get("title", "")
        emp_position = employee.get("position", "")
        emp_supervisor_role = employee.get("supervisor_role", "")

        bonus_grade_override = employee.get("bonus_grade")
        _effective_title = self._get_effective_bonus_title(
            emp_title, bonus_grade_override
        )

        supervisor_festival_base = self.get_supervisor_festival_bonus(
            emp_title, emp_position, emp_supervisor_role
        )

        if supervisor_festival_base is not None:
            if is_eligible:
                school_enrollment = (
                    office_staff_context.get("school_enrollment", 0)
                    if office_staff_context
                    else 0
                )
                school_target = self._school_wide_target or 160
                ratio = school_enrollment / school_target if school_target > 0 else 0
                breakdown.festival_bonus = round(supervisor_festival_base * ratio)
            else:
                breakdown.festival_bonus = 0
            breakdown.overtime_bonus = 0
        elif office_staff_context and emp_position:
            office_base = self.get_office_festival_bonus_base(emp_position, emp_title)
            # 與主管路徑一致：office_base is None 表示「未設定」(跳過)，
            # office_base == 0 表示「設為 0」(計算結果為 0)
            if office_base is not None and is_eligible:
                school_enrollment = office_staff_context.get("school_enrollment", 0)
                school_target = self._school_wide_target or 160
                ratio = school_enrollment / school_target if school_target > 0 else 0
                breakdown.festival_bonus = round(office_base * ratio)
            else:
                breakdown.festival_bonus = 0
            breakdown.overtime_bonus = 0
        elif classroom_context and emp_position:
            bonus_result = self._calculate_classroom_bonus_result(
                _effective_title,
                classroom_context,
            )
            if is_eligible:
                breakdown.festival_bonus = bonus_result["festival_bonus"]
                breakdown.overtime_bonus = bonus_result["overtime_bonus"]
            else:
                breakdown.festival_bonus = 0
                breakdown.overtime_bonus = 0
        elif bonus_settings:
            base_amount = bonus_settings.get("festival_base", 0)
            position_bonus_base = bonus_settings.get("position_bonus_base", {})

            if position_bonus_base and emp_title and emp_title in position_bonus_base:
                base_amount = position_bonus_base[emp_title]

            bonus = self.calculate_bonus(
                bonus_settings.get("target", 0),
                bonus_settings.get("current", 0),
                base_amount,
                bonus_settings.get("overtime_per", 500),
            )
            breakdown.festival_bonus = bonus["festival_bonus"]
            breakdown.overtime_bonus = bonus["overtime_bonus"]

        # 計算主管紅利
        breakdown.supervisor_dividend = self.get_supervisor_dividend(
            emp_title, emp_position, emp_supervisor_role
        )

        # 非發放月份不計節慶獎金與超額獎金
        if not get_bonus_distribution_month(month):
            breakdown.festival_bonus = 0
            breakdown.overtime_bonus = 0

        # 事假+病假累計超過40小時：取消節慶獎金與超額獎金（兩者具「全勤」性質）。
        # 主管月紅利（supervisor_dividend）與職務掛鉤、不與出勤掛鉤，不受此條件影響。
        breakdown.personal_sick_leave_hours = personal_sick_leave_hours
        if personal_sick_leave_hours > 40:
            breakdown.festival_bonus = 0
            breakdown.overtime_bonus = 0

        # 生日禮金：當月壽星 $500
        birthday_val = employee.get("birthday")
        if birthday_val:
            if isinstance(birthday_val, str):
                try:
                    birthday_val = datetime.strptime(birthday_val, "%Y-%m-%d").date()
                except ValueError:
                    birthday_val = None
            if birthday_val and birthday_val.month == month:
                breakdown.birthday_bonus = 500

    def _calculate_deductions(
        self,
        breakdown,
        employee: dict,
        attendance,
        leave_deduction: float,
        meeting_context,
        overtime_work_pay: float,
        month: int,
    ) -> None:
        """計算保險、考勤扣款、請假扣款、園務會議費用，彙總 total_deduction。"""
        # 勞健保：時薪制與正職皆計算（依勞基法時薪工亦須投保）
        # 投保薪資來源優先序：employee['insurance_salary'] → contracted_base
        # 兩者皆為 raw 值，由本函式統一以 get_bracket 正規化至官方級距
        contracted_base = employee.get("base_salary", 0) or 0
        pension_rate = employee.get("pension_self_rate", 0.0)
        _ins_raw = employee.get("insurance_salary") or contracted_base or 0
        if _ins_raw > 0:
            _ins_salary = self.insurance_service.get_bracket(_ins_raw)["amount"]
            insurance = self.insurance_service.calculate(
                _ins_salary,
                employee.get("dependents", 0),
                pension_self_rate=pension_rate,
            )
            breakdown.labor_insurance = insurance.labor_employee
            breakdown.health_insurance = insurance.health_employee
            breakdown.pension_self = insurance.pension_employee
        elif employee.get("employee_type") == "hourly":
            logger.warning(
                "時薪制員工 %s 未設定 insurance_salary_level，本月不計勞健保扣繳，"
                "請至員工資料補設投保級距以符合勞基法",
                employee.get("name") or employee.get("employee_id"),
            )

        # 考勤扣款
        base_sal = employee.get("base_salary", 0) or 0
        daily_salary = calc_daily_salary(base_sal)
        late_details = employee.get("_late_details", None)
        if attendance:
            att_ded = self.calculate_attendance_deduction(
                attendance,
                daily_salary=daily_salary,
                base_salary=base_sal,
                late_details=late_details,
            )
            breakdown.late_deduction = att_ded["late_deduction"]
            breakdown.early_leave_deduction = att_ded["early_leave_deduction"]
            breakdown.missing_punch_deduction = 0  # 不扣款
            breakdown.late_count = att_ded["late_count"]
            breakdown.early_leave_count = att_ded["early_leave_count"]
            breakdown.missing_punch_count = att_ded["missing_punch_count"]
            breakdown.total_late_minutes = att_ded["total_late_minutes"]
            breakdown.total_early_minutes = att_ded["total_early_minutes"]

        breakdown.leave_deduction = leave_deduction

        # 園務會議加班費與缺席扣款
        if meeting_context:
            attended = meeting_context.get("attended", 0)
            absent = meeting_context.get("absent", 0)
            work_end = meeting_context.get("work_end_time", "17:00")

            if work_end == "18:00":
                per_meeting_pay = self._meeting_pay_6pm
            else:
                per_meeting_pay = self._meeting_pay

            breakdown.meeting_overtime_pay = attended * per_meeting_pay
            breakdown.meeting_attended = attended
            breakdown.meeting_absent = absent

            if get_bonus_distribution_month(month):
                absent_for_deduction = meeting_context.get("absent_period", absent)
                breakdown.meeting_absence_deduction = (
                    absent_for_deduction * self._meeting_absence_penalty
                )
                breakdown.festival_bonus = max(
                    0, breakdown.festival_bonus - breakdown.meeting_absence_deduction
                )

        # 將園務會議加班費與核准加班費加入應發總額
        breakdown.overtime_work_pay = overtime_work_pay
        breakdown.gross_salary += breakdown.meeting_overtime_pay + overtime_work_pay

        # 計算扣款總額
        breakdown.total_deduction = (
            breakdown.labor_insurance
            + breakdown.health_insurance
            + breakdown.pension_self
            + breakdown.late_deduction
            + breakdown.early_leave_deduction
            + breakdown.leave_deduction
            + breakdown.absence_deduction
            + breakdown.other_deduction
        )

    def calculate_attendance_deduction(
        self,
        attendance: AttendanceResult,
        daily_salary: float = 0,
        base_salary: float = 0,
        late_details: list = None,
    ) -> dict:
        """
        計算考勤扣款

        規則：
        - 遲到/早退：一律按實際分鐘比例扣款（每分鐘 = 月薪 ÷ 30 ÷ 8 ÷ 60，依勞基法固定基準）
          並設「單筆遲到/早退不超過當日日薪」上限，避免打卡資料異常造成超額扣款
        - 未打卡：不扣款，僅記錄次數（供考核用）
        """
        # 每分鐘薪資 = 月薪 ÷ 30 ÷ 8 ÷ 60（依勞基法時薪基準，固定 30 天）
        # base_salary 為 0（時薪制或未設定）時：扣款為 0。時薪制按工時付費，
        # 不應再以「每分鐘 1 元」的硬寫死值扣款（原 fallback 會造成 60 元/小時誤扣）。
        per_minute_rate = (
            base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 0
        )

        # 遲到扣款 — 逐筆套用「不超過當日日薪」上限
        if late_details:
            late_minutes_per_day = late_details
        else:
            late_minutes_per_day = (
                [attendance.total_late_minutes] if attendance.total_late_minutes else []
            )
        late_deduction = sum(
            min(m * per_minute_rate, daily_salary) if daily_salary > 0
            else m * per_minute_rate
            for m in late_minutes_per_day
        )

        # 早退扣款 — 無逐筆 details，整月加總時一律以「日數 × 日薪」為上限
        total_early_minutes = attendance.total_early_minutes
        early_count = attendance.early_leave_count or 0
        raw_early_deduction = total_early_minutes * per_minute_rate
        if daily_salary > 0 and early_count > 0:
            early_deduction = min(raw_early_deduction, early_count * daily_salary)
        else:
            early_deduction = raw_early_deduction

        # 未打卡：不扣款，僅記錄
        missing_count = (
            attendance.missing_punch_in_count + attendance.missing_punch_out_count
        )

        return {
            "late_deduction": late_deduction,
            "missing_punch_deduction": 0,  # 不扣款
            "early_leave_deduction": early_deduction,
            "late_count": attendance.late_count,
            "early_leave_count": attendance.early_leave_count,
            "missing_punch_count": missing_count,
            "total_late_minutes": attendance.total_late_minutes,
            "total_early_minutes": total_early_minutes,
        }

    def calculate_bonus(
        self, target: int, current: int, base_amount: float, overtime_per: float = 500
    ) -> dict:
        """計算獎金 (舊版，保留相容性)"""
        ratio = current / target if target > 0 else 0
        festival_bonus = base_amount * ratio
        overtime_bonus = max(0, current - target) * overtime_per
        return {
            "festival_bonus": round(festival_bonus),
            "overtime_bonus": round(overtime_bonus),
            "ratio": ratio,
        }

    def calculate_overtime_bonus(
        self,
        role: str,
        grade_name: str,
        current_enrollment: int,
        has_assistant: bool,
        is_shared_assistant: bool = False,
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
        is_shared_assistant: bool = False,
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
        personal_sick_leave_hours: float = 0,
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
            personal_sick_leave_hours: 當月事假+病假累計時數（>40h 時取消所有節慶獎金及紅利）
        """

        is_hourly = employee.get("employee_type") == "hourly"

        breakdown = SalaryBreakdown(
            employee_name=employee.get("name", ""),
            employee_id=employee.get("employee_id", ""),
            year=year,
            month=month,
        )

        if is_hourly:
            # 時薪制計算
            breakdown.hourly_rate = employee.get("hourly_rate", 0)
            breakdown.work_hours = employee.get("work_hours", 0)
            # 優先使用已依勞基法分段計費的結果（process_salary_calculation 提供）；
            # 未提供時 fallback 至等比計算（向後相容直接傳入 employee dict 的測試情境）
            breakdown.hourly_total = (
                employee.get("hourly_calculated_pay")
                or breakdown.hourly_rate * breakdown.work_hours
            )
            breakdown.gross_salary = breakdown.hourly_total
        else:
            # 正職員工
            self._calculate_base_gross(breakdown, employee, year, month, allowances)
            self._calculate_bonuses(
                breakdown,
                employee,
                year,
                month,
                classroom_context,
                office_staff_context,
                bonus_settings,
                personal_sick_leave_hours,
            )
            breakdown.gross_salary = (
                breakdown.base_salary
                + breakdown.supervisor_allowance
                + breakdown.teacher_allowance
                + breakdown.meal_allowance
                + breakdown.transportation_allowance
                + breakdown.other_allowance
                + breakdown.performance_bonus
                + breakdown.special_bonus
                + breakdown.supervisor_dividend
                + breakdown.birthday_bonus
            )

        self._calculate_deductions(
            breakdown,
            employee,
            attendance,
            leave_deduction,
            meeting_context,
            overtime_work_pay,
            month,
        )

        # 獎金獨立轉帳旗標
        breakdown.bonus_separate = (
            breakdown.festival_bonus
            + breakdown.overtime_bonus
            + breakdown.supervisor_dividend
        ) > 0
        breakdown.bonus_amount = (
            breakdown.festival_bonus
            + breakdown.overtime_bonus
            + breakdown.supervisor_dividend
        )

        # 最終一次舍入
        _net_raw = breakdown.gross_salary - breakdown.total_deduction
        breakdown.gross_salary = round(breakdown.gross_salary)
        breakdown.total_deduction = round(breakdown.total_deduction)
        breakdown.net_salary = round(_net_raw)

        if breakdown.gross_salary < 0:
            raise ValueError(f"gross_salary 異常負值: {breakdown.gross_salary}")
        if breakdown.total_deduction < 0:
            raise ValueError(f"total_deduction 異常負值: {breakdown.total_deduction}")
        if breakdown.net_salary < 0:
            raise ValueError(f"net_salary 異常負值: {breakdown.net_salary}")

        return breakdown

    def calculate_festival_bonus_breakdown(
        self,
        employee_id: int,
        year: int,
        month: int,
        *,
        _ctx: dict | None = None,
    ) -> dict:
        """計算單一員工節慶獎金明細 (for UI display)。

        _ctx 可傳入預先批次查詢的資料，避免 N+1 查詢：
          - session: 複用外部 session
          - employee: 預先查好的 Employee ORM 物件
          - classroom: 預先查好的 Classroom ORM 物件（或 None）
          - school_active_students: 全校在籍人數（int）
          - classroom_count_map: {classroom_id: int} 各班在籍人數
        """
        _own_session = _ctx is None
        if _own_session:
            session = _get_db_session()
        else:
            session = _ctx["session"]

        try:
            from models.database import (
                Employee,
                Classroom,
                ClassGrade,
                JobTitle,
                Student,
            )

            _, month_last_day = calendar.monthrange(year, month)
            month_end = date(year, month, month_last_day)

            # 優先使用 _ctx 中預先查好的員工，避免 N 次 Employee 查詢
            if _ctx is not None and "employee" in _ctx:
                emp = _ctx["employee"]
            else:
                emp = session.query(Employee).get(employee_id)
            if not emp:
                return {}

            # 優先使用 _ctx 中預先查好的班級
            if _ctx is not None and "classroom" in _ctx:
                classroom = _ctx["classroom"]
            elif emp.classroom_id:
                classroom = session.query(Classroom).get(emp.classroom_id)
            else:
                classroom = None

            bonus_base = 0
            target_enrollment = 0
            current_enrollment = 0
            ratio = 0
            festival_bonus = 0
            remark = ""
            category = ""

            position = emp.position or ""
            title_name = (
                emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
            )
            supervisor_role = emp.supervisor_role or ""

            bonus_reference_date = self._get_bonus_reference_date(year, month)
            is_eligible = self.is_eligible_for_festival_bonus(
                emp.hire_date,
                reference_date=bonus_reference_date,
            )
            effective_title = self._get_effective_bonus_title(
                title_name,
                getattr(emp, "bonus_grade", None),
            )

            if not position:
                is_eligible = False
                remark = "無職位資料(不發放)"
            elif not is_eligible:
                remark = "未滿3個月"

            # 全校在籍人數：優先使用 _ctx 預先批次查詢的結果，避免 N 次相同查詢
            if _ctx is not None and "school_active_students" in _ctx:
                school_active_students = _ctx["school_active_students"]
            else:
                school_active_students = count_students_active_on(session, month_end)

            supervisor_base = self.get_supervisor_festival_bonus(
                title_name, position, supervisor_role
            )
            # 與 _calculate_bonuses 一致：is not None 才視為「設為主管」
            if supervisor_base is not None:
                category = "主管"
                bonus_base = supervisor_base
                current_enrollment = school_active_students
                target_enrollment = self._school_wide_target or 160
                ratio = (
                    current_enrollment / target_enrollment
                    if target_enrollment > 0
                    else 0
                )
                festival_bonus = round(supervisor_base * ratio) if is_eligible else 0
                if is_eligible:
                    remark = "全校比例(主管)"

            elif (office_base := self.get_office_festival_bonus_base(
                position, title_name
            )) is not None:
                category = "辦公室"
                current_enrollment = school_active_students
                bonus_base = office_base
                school_target = self._school_wide_target or 160
                target_enrollment = school_target if school_target > 0 else 100
                ratio = (
                    current_enrollment / target_enrollment
                    if target_enrollment > 0
                    else 0
                )
                festival_bonus = round(bonus_base * ratio) if is_eligible else 0
                if is_eligible:
                    remark = "全校比例"

            elif classroom:
                category = "帶班老師"
                classroom_context = self._build_classroom_context_from_db(
                    session,
                    classroom,
                    emp.id,
                    reference_date=month_end,
                    classroom_count_map=(
                        _ctx.get("classroom_count_map") if _ctx else None
                    ),
                )
                current_enrollment = classroom_context.get("current_enrollment", 0)
                bonus_result = self._calculate_classroom_bonus_result(
                    effective_title,
                    classroom_context,
                )

                bonus_base = bonus_result.get("base_amount", 0)
                target_enrollment = bonus_result.get("target", 0)
                ratio = bonus_result.get("ratio", 0)
                festival_bonus = bonus_result["festival_bonus"] if is_eligible else 0
                if is_eligible:
                    other_count = len(
                        classroom_context.get("shared_other_classes")
                        or ([classroom_context["shared_second_class"]]
                            if classroom_context.get("shared_second_class") else [])
                    )
                    if other_count == 1:
                        remark = "兩班平均"
                    elif other_count >= 2:
                        remark = f"{other_count + 1}班平均"

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
                "remark": remark,
            }

        except Exception as e:
            logger.exception("計算節慶獎金明細失敗：employee_id=%s", employee_id)
            return {"name": f"Error: {e}", "festivalBonus": 0}
        finally:
            # 只在自己建立 session 時才關閉（避免關閉外部傳入的 session）
            if _own_session:
                session.close()

    # ─── process_salary_calculation 私有輔助方法 ─────────────────────────────

    def _resolve_standard_base(self, emp) -> float:
        """依職位標準底薪決定員工底薪。
        有對應標準的職位直接回傳標準薪；無對應（園長、主任等特例）則回傳 emp.base_salary。
        時薪制（base_salary=0）永遠回傳 0。
        """
        raw = float(emp.base_salary or 0)
        if raw == 0 or not self._position_salary_standards:
            return raw

        title = emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
        position = emp.position or ""
        bonus_grade = getattr(emp, "bonus_grade", None)

        # 領導職：有設定標準才套用，否則用個人設定
        if position == "主任" or title == "主任":
            std = self._position_salary_standards.get("director")
            return float(std) if std else raw
        if position == "園長" or title == "園長":
            std = self._position_salary_standards.get("principal")
            return float(std) if std else raw

        # 決定等級
        if bonus_grade and bonus_grade.lower() in ("a", "b", "c"):
            grade = bonus_grade.lower()
        elif title == "幼兒園教師":
            grade = "a"
        elif title in ("教保員", "助理教保員"):
            grade = "b"
        else:
            grade = "c"

        # 對應標準鍵
        if "司機" in title:
            key = "driver"
        elif "廚" in title:
            key = "kitchen_staff"
        elif "美師" in title or "藝術" in title:
            key = "art_teacher"
        elif position == "行政":
            key = "admin_staff"
        elif position in ("班導", "班導師") or (title == "組長" and position == "班導"):
            key = f"head_teacher_{grade}"
        elif position in ("副班導", "副班導師"):
            key = f"assistant_teacher_{grade}"
        else:
            return raw

        return self._position_salary_standards.get(key, raw)

    def _load_emp_dict(self, emp) -> dict:
        """從 Employee ORM 物件建構計算用字典。底薪取自職位標準（若有對應）。"""
        title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
        base_salary = self._resolve_standard_base(emp)
        return {
            "employee_id": emp.employee_id,
            "name": emp.name,
            "title": title_name,
            "position": emp.position,
            "supervisor_role": emp.supervisor_role,
            "bonus_grade": getattr(emp, "bonus_grade", None) or None,
            "employee_type": emp.employee_type,
            "base_salary": base_salary,
            "hourly_rate": emp.hourly_rate,
            "work_hours": 0,
            "supervisor_allowance": emp.supervisor_allowance,
            "teacher_allowance": emp.teacher_allowance,
            "meal_allowance": emp.meal_allowance,
            "transportation_allowance": emp.transportation_allowance,
            "other_allowance": emp.other_allowance,
            # 投保薪資 raw 值（不在此處做 bracket 正規化，由 _calculate_deductions 統一處理）
            "insurance_salary": (
                emp.insurance_salary_level
                if emp.insurance_salary_level and emp.insurance_salary_level > 0
                else base_salary
            ),
            "dependents": emp.dependents,
            "pension_self_rate": emp.pension_self_rate or 0,
            "hire_date": emp.hire_date,
            "resign_date": getattr(emp, "resign_date", None),
            "birthday": emp.birthday,
        }

    def _load_attendance_result(
        self, session, emp, start_date: date, end_date: date, emp_dict: dict
    ) -> tuple:
        """查詢考勤、彙總統計，時薪制計算 hourly_calculated_pay（mutates emp_dict）。回傳 AttendanceResult。"""
        from models.database import Attendance

        attendances = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .all()
        )

        late_count = sum(1 for a in attendances if a.is_late)
        early_count = sum(1 for a in attendances if a.is_early_leave)
        missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
        missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
        total_late_minutes = sum(a.late_minutes or 0 for a in attendances if a.is_late)
        total_early_minutes = sum(
            a.early_leave_minutes or 0 for a in attendances if a.is_early_leave
        )

        late_details = [
            a.late_minutes or 0
            for a in attendances
            if a.is_late and (a.late_minutes or 0) > 0
        ]
        emp_dict["_late_details"] = late_details

        if emp.employee_type == "hourly":
            _work_end_t = datetime.strptime(
                emp.work_end_time or "17:00", "%H:%M"
            ).time()
            total_hours = 0.0
            total_hourly_pay = 0.0
            for a in attendances:
                if not a.punch_in_time:
                    continue
                day_hours = _compute_hourly_daily_hours(
                    a.punch_in_time, a.punch_out_time, _work_end_t
                )
                total_hours += day_hours
                total_hourly_pay += _calc_daily_hourly_pay(
                    day_hours, emp.hourly_rate or 0
                )
            emp_dict["work_hours"] = round(total_hours, 2)
            emp_dict["hourly_calculated_pay"] = round(total_hourly_pay, 2)

        return (
            AttendanceResult(
                employee_name=emp.name,
                total_days=len(attendances),
                normal_days=len(attendances) - late_count - early_count,
                late_count=late_count,
                early_leave_count=early_count,
                missing_punch_in_count=missing_in,
                missing_punch_out_count=missing_out,
                total_late_minutes=total_late_minutes,
                total_early_minutes=total_early_minutes,
                details=[],
            ),
            attendances,
        )

    def _load_allowances_list(self, session, emp) -> List[dict]:
        """查詢員工有效津貼，回傳 [{'name': ..., 'amount': ...}, ...]。"""
        from models.database import EmployeeAllowance, AllowanceType

        rows = (
            session.query(EmployeeAllowance, AllowanceType)
            .join(
                AllowanceType, EmployeeAllowance.allowance_type_id == AllowanceType.id
            )
            .filter(
                EmployeeAllowance.employee_id == emp.id,
                EmployeeAllowance.is_active == True,
            )
            .all()
        )
        return [
            {"code": at.code, "name": at.name, "amount": ea.amount}
            for ea, at in rows
        ]

    def _build_contexts(self, session, emp, end_date: date) -> tuple:
        """建構 (classroom_context, office_staff_context)。"""
        from models.database import Classroom

        classroom_context = None
        if emp.classroom_id:
            classroom = session.query(Classroom).get(emp.classroom_id)
            classroom_context = self._build_classroom_context_from_db(
                session, classroom, emp.id, reference_date=end_date
            )

        total_students = count_students_active_on(session, end_date)
        office_staff_context = self._build_office_staff_context(
            emp, total_students, classroom_context
        )

        return classroom_context, office_staff_context

    def _load_period_records(
        self,
        session,
        emp,
        start_date: date,
        end_date: date,
        year: int,
        month: int,
        daily_salary: float,
    ) -> dict:
        """查詢請假、加班、園務會議記錄，回傳計算所需彙總資料。"""
        from models.database import (
            LeaveRecord,
            OvertimeRecord as DBOvertimeRecord,
            MeetingRecord,
        )

        # 已核准請假
        approved_leaves = (
            session.query(LeaveRecord)
            .filter(
                LeaveRecord.employee_id == emp.id,
                LeaveRecord.is_approved == True,
                LeaveRecord.start_date <= end_date,
                LeaveRecord.end_date >= start_date,
            )
            .all()
        )
        leave_deduction_total = _sum_leave_deduction(approved_leaves, daily_salary)
        personal_sick_leave_hours = sum(
            lv.leave_hours or 0
            for lv in approved_leaves
            if lv.leave_type in ("personal", "sick")
        )

        # 已核准加班
        approved_overtimes = (
            session.query(DBOvertimeRecord)
            .filter(
                DBOvertimeRecord.employee_id == emp.id,
                DBOvertimeRecord.is_approved == True,
                DBOvertimeRecord.overtime_date >= start_date,
                DBOvertimeRecord.overtime_date <= end_date,
            )
            .all()
        )
        overtime_work_pay_total = sum(o.overtime_pay or 0 for o in approved_overtimes)

        # 園務會議
        meeting_records = (
            session.query(MeetingRecord)
            .filter(
                MeetingRecord.employee_id == emp.id,
                MeetingRecord.meeting_date >= start_date,
                MeetingRecord.meeting_date <= end_date,
            )
            .all()
        )

        meeting_attended = sum(1 for m in meeting_records if m.attended)
        meeting_absent_current = sum(1 for m in meeting_records if not m.attended)

        absent_period = meeting_absent_current
        period_start = get_meeting_deduction_period_start(year, month)
        if period_start is not None and period_start < start_date:
            prior_records = (
                session.query(MeetingRecord)
                .filter(
                    MeetingRecord.employee_id == emp.id,
                    MeetingRecord.meeting_date >= period_start,
                    MeetingRecord.meeting_date < start_date,
                )
                .all()
            )
            absent_period += sum(1 for m in prior_records if not m.attended)

        meeting_context = None
        if meeting_records or absent_period > 0:
            meeting_context = {
                "attended": meeting_attended,
                "absent": meeting_absent_current,
                "absent_period": absent_period,
                "work_end_time": emp.work_end_time or "17:00",
            }

        return {
            "leave_deduction": leave_deduction_total,
            "personal_sick_leave_hours": personal_sick_leave_hours,
            "overtime_work_pay": overtime_work_pay_total,
            "meeting_context": meeting_context,
            "approved_leaves": approved_leaves,
        }

    @staticmethod
    def _compute_absence(
        emp_id: int,
        attendances,
        approved_leaves,
        expected_workdays: set,
        daily_salary: float,
        start_date: date,
        end_date: date,
        year: int,
        month: int,
    ) -> tuple:
        """曠職核心計算（純邏輯，不查 DB），回傳 (absent_count, absence_deduction_amount)。"""
        attendance_dates = {
            (
                a.attendance_date.date()
                if isinstance(a.attendance_date, datetime)
                else a.attendance_date
            )
            for a in attendances
        }

        leave_covered: set = set()
        for lv in approved_leaves:
            d = (
                lv.start_date.date()
                if isinstance(lv.start_date, datetime)
                else lv.start_date
            )
            lv_end = (
                lv.end_date.date() if isinstance(lv.end_date, datetime) else lv.end_date
            )
            while d <= lv_end:
                if start_date <= d <= end_date:
                    leave_covered.add(d)
                d += timedelta(days=1)

        absent_days = expected_workdays - attendance_dates - leave_covered
        absent_count = len(absent_days)
        absence_deduction_amount = absent_count * daily_salary
        if absent_count > 0:
            logger.info(
                "曠職偵測：emp_id=%d %d/%d 曠職 %d 天，扣款 %d 元（%s）",
                emp_id,
                year,
                month,
                absent_count,
                absence_deduction_amount,
                sorted(absent_days),
            )
        return absent_count, absence_deduction_amount

    def _detect_absences(
        self,
        session,
        emp,
        attendances,
        approved_leaves,
        start_date: date,
        end_date: date,
        year: int,
        month: int,
    ) -> tuple:
        """曠職偵測（查 DB 取得假日與班別後委託 _compute_absence）。"""
        if emp.employee_type == "hourly":
            return 0, 0

        from models.database import Holiday, DailyShift as _DailyShift

        holidays_in_month = (
            session.query(Holiday.date)
            .filter(
                Holiday.date >= start_date,
                Holiday.date <= end_date,
                Holiday.is_active == True,
            )
            .all()
        )
        holiday_set = {h.date for h in holidays_in_month}

        daily_shifts_in_month = (
            session.query(_DailyShift)
            .filter(
                _DailyShift.employee_id == emp.id,
                _DailyShift.date >= start_date,
                _DailyShift.date <= end_date,
            )
            .all()
        )
        daily_shift_map = {ds.date: ds.shift_type_id for ds in daily_shifts_in_month}

        expected_workdays = _build_expected_workdays(
            year=year,
            month=month,
            holiday_set=holiday_set,
            daily_shift_map=daily_shift_map,
            hire_date_raw=emp.hire_date,
            resign_date_raw=getattr(emp, "resign_date", None),
        )

        daily_salary_full = calc_daily_salary(emp.base_salary)
        return self._compute_absence(
            emp.id,
            attendances,
            approved_leaves,
            expected_workdays,
            daily_salary_full,
            start_date,
            end_date,
            year,
            month,
        )

    def process_salary_calculation(self, employee_id: int, year: int, month: int):
        """處理單一員工薪資計算並儲存結果"""
        session = _get_db_session()
        try:
            from models.database import Employee, SalaryRecord

            # 1. 取得員工資料
            emp = session.query(Employee).get(employee_id)
            if not emp:
                raise ValueError(f"Employee {employee_id} not found")

            # 2. 轉換為 dict
            emp_dict = self._load_emp_dict(emp)

            # 3. 計算日期範圍
            import calendar

            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)

            # 4. 取得考勤並計算統計
            attendance_result, attendances = self._load_attendance_result(
                session, emp, start_date, end_date, emp_dict
            )

            # 5. 取得津貼
            allowances = self._load_allowances_list(session, emp)

            # 6. 建構 Classroom Context 與 Office Context
            classroom_context, office_staff_context = self._build_contexts(
                session, emp, end_date
            )

            # 7. 查詢請假、加班、園務會議記錄
            daily_salary = calc_daily_salary(emp.base_salary)
            period_records = self._load_period_records(
                session, emp, start_date, end_date, year, month, daily_salary
            )

            # 8. 曠職偵測
            absent_count, absence_deduction_amount = self._detect_absences(
                session,
                emp,
                attendances,
                period_records["approved_leaves"],
                start_date,
                end_date,
                year,
                month,
            )

            # 9. 計算薪資
            breakdown = self.calculate_salary(
                employee=emp_dict,
                year=year,
                month=month,
                attendance=attendance_result,
                leave_deduction=period_records["leave_deduction"],
                allowances=allowances,
                classroom_context=classroom_context,
                office_staff_context=office_staff_context,
                meeting_context=period_records["meeting_context"],
                overtime_work_pay=period_records["overtime_work_pay"],
                personal_sick_leave_hours=period_records["personal_sick_leave_hours"],
            )

            # 加入曠職扣款
            breakdown.absent_count = absent_count
            breakdown.absence_deduction = round(absence_deduction_amount)
            breakdown.total_deduction = round(
                breakdown.total_deduction + absence_deduction_amount
            )
            breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

            if breakdown.total_deduction < 0:
                raise ValueError(
                    f"total_deduction 異常負值（含曠職）: {breakdown.total_deduction}"
                )
            if breakdown.net_salary < 0:
                raise ValueError(
                    f"net_salary 異常負值（含曠職）: {breakdown.net_salary}"
                )

            # 10. 儲存 SalaryRecord（advisory lock 保護：多 worker 不會同時寫同筆）
            from utils.advisory_lock import acquire_salary_lock

            acquire_salary_lock(session, employee_id=emp.id, year=year, month=month)

            salary_record = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.employee_id == emp.id,
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                )
                .first()
            )

            self._check_not_finalized(salary_record, emp.name, year, month)

            if not salary_record:
                salary_record = SalaryRecord(
                    employee_id=emp.id, salary_year=year, salary_month=month
                )
                session.add(salary_record)

            _fill_salary_record(salary_record, breakdown, self)

            try:
                session.commit()
            except IntegrityError:
                # 有 advisory lock 後理論上不會觸發，保留原本重試作為防禦。
                session.rollback()
                salary_record = (
                    session.query(SalaryRecord)
                    .filter(
                        SalaryRecord.employee_id == emp.id,
                        SalaryRecord.salary_year == year,
                        SalaryRecord.salary_month == month,
                    )
                    .first()
                )
                self._check_not_finalized(salary_record, emp.name, year, month)
                _fill_salary_record(salary_record, breakdown, self)
                session.commit()

            return breakdown

        except Exception as e:
            session.rollback()
            logger.exception("薪資計算失敗：employee_id=%s", employee_id)
            raise e
        finally:
            session.close()

    def process_bulk_salary_calculation(
        self, employee_ids: list, year: int, month: int, progress_callback=None
    ):
        """批次計算所有員工薪資，使用預先批次載入避免 N+1 查詢。

        相較 process_salary_calculation 每人獨立開 session 並執行 ~13 次 DB 查詢，
        此方法以 ~13 次批次查詢完成所有員工，並在一次 commit 寫入全部 SalaryRecord。

        Args:
            progress_callback: 選用，型別為 callable(done: int, total: int, emp_name: str)。
                每完成一位員工呼叫一次（錯誤也算），用於 async job 進度回報。

        Returns:
            (results: list[dict], errors: list[dict])
        """
        from models.database import (
            Employee,
            SalaryRecord,
            Attendance,
            EmployeeAllowance,
            AllowanceType,
            LeaveRecord,
            OvertimeRecord as DBOvertimeRecord,
            MeetingRecord,
            Holiday,
            Classroom,
            ClassGrade,
        )

        try:
            from models.database import DailyShift as _DailyShift

            _has_daily_shift = True
        except ImportError:
            _DailyShift = None
            _has_daily_shift = False

        from services.student_enrollment import classroom_student_count_map

        session = _get_db_session()
        session.expire_on_commit = (
            False  # 防止 commit 後屬性過期，避免 DetachedInstanceError
        )
        try:
            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)

            # ── 批次預載所有資料（~13 次 DB 查詢取代 N×13）─────────────────────

            # 1. 員工（含 job_title_rel，避免 N 次 lazy load）
            employees = (
                session.query(Employee)
                .options(joinedload(Employee.job_title_rel))
                .filter(Employee.id.in_(employee_ids))
                .all()
            )
            emp_map = {e.id: e for e in employees}

            # 2. 考勤
            all_attendances = (
                session.query(Attendance)
                .filter(
                    Attendance.employee_id.in_(employee_ids),
                    Attendance.attendance_date >= start_date,
                    Attendance.attendance_date <= end_date,
                )
                .all()
            )
            att_by_emp = defaultdict(list)
            for a in all_attendances:
                att_by_emp[a.employee_id].append(a)

            # 3. 津貼（JOIN AllowanceType）
            all_ea = (
                session.query(EmployeeAllowance, AllowanceType)
                .join(
                    AllowanceType,
                    EmployeeAllowance.allowance_type_id == AllowanceType.id,
                )
                .filter(
                    EmployeeAllowance.employee_id.in_(employee_ids),
                    EmployeeAllowance.is_active == True,
                )
                .all()
            )
            allowance_by_emp = defaultdict(list)
            for ea, at in all_ea:
                allowance_by_emp[ea.employee_id].append(
                    {"code": at.code, "name": at.name, "amount": ea.amount}
                )

            # 4. 班級與年級（預載 grade 避免 lazy load）
            all_classrooms = (
                session.query(Classroom)
                .options(joinedload(Classroom.grade))
                .filter(Classroom.is_active == True)
                .all()
            )
            classroom_map = {c.id: c for c in all_classrooms}
            # 助理教師 → 班級清單（避免迴圈內 O(classrooms) 線性掃描，改為 O(1) 查表）
            assistant_to_classes: dict[int, list] = defaultdict(list)
            for _c in all_classrooms:
                if _c.assistant_teacher_id:
                    assistant_to_classes[_c.assistant_teacher_id].append(_c)

            # 5. 學生數（1 次批次查詢，取代每人 1 次）
            db_count_map = classroom_student_count_map(session, end_date)
            total_students = sum(db_count_map.values())

            # 6. 請假
            all_leaves = (
                session.query(LeaveRecord)
                .filter(
                    LeaveRecord.employee_id.in_(employee_ids),
                    LeaveRecord.is_approved == True,
                    LeaveRecord.start_date <= end_date,
                    LeaveRecord.end_date >= start_date,
                )
                .all()
            )
            leaves_by_emp = defaultdict(list)
            for lv in all_leaves:
                leaves_by_emp[lv.employee_id].append(lv)

            # 7. 加班
            all_ot = (
                session.query(DBOvertimeRecord)
                .filter(
                    DBOvertimeRecord.employee_id.in_(employee_ids),
                    DBOvertimeRecord.is_approved == True,
                    DBOvertimeRecord.overtime_date >= start_date,
                    DBOvertimeRecord.overtime_date <= end_date,
                )
                .all()
            )
            ot_by_emp = defaultdict(list)
            for ot in all_ot:
                ot_by_emp[ot.employee_id].append(ot)

            # 8. 園務會議（當月）
            all_meetings = (
                session.query(MeetingRecord)
                .filter(
                    MeetingRecord.employee_id.in_(employee_ids),
                    MeetingRecord.meeting_date >= start_date,
                    MeetingRecord.meeting_date <= end_date,
                )
                .all()
            )
            meetings_by_emp = defaultdict(list)
            for m in all_meetings:
                meetings_by_emp[m.employee_id].append(m)

            # 9. 發放月前幾月會議缺席（bonus months: 2/6/9/12）
            prior_absent_by_emp = defaultdict(int)
            period_start = get_meeting_deduction_period_start(year, month)
            if period_start is not None and period_start < start_date:
                prior_meetings = (
                    session.query(MeetingRecord)
                    .filter(
                        MeetingRecord.employee_id.in_(employee_ids),
                        MeetingRecord.meeting_date >= period_start,
                        MeetingRecord.meeting_date < start_date,
                    )
                    .all()
                )
                for m in prior_meetings:
                    if not m.attended:
                        prior_absent_by_emp[m.employee_id] += 1

            # 10. 假日（全月共用，1 次）
            holidays_raw = (
                session.query(Holiday.date)
                .filter(
                    Holiday.date >= start_date,
                    Holiday.date <= end_date,
                    Holiday.is_active == True,
                )
                .all()
            )
            holiday_set = {h.date for h in holidays_raw}

            # 11. 班別（DailyShift）
            shifts_by_emp = defaultdict(dict)
            if _has_daily_shift:
                all_shifts = (
                    session.query(_DailyShift)
                    .filter(
                        _DailyShift.employee_id.in_(employee_ids),
                        _DailyShift.date >= start_date,
                        _DailyShift.date <= end_date,
                    )
                    .all()
                )
                for ds in all_shifts:
                    shifts_by_emp[ds.employee_id][ds.date] = ds.shift_type_id

            # 12. 現有薪資記錄（upsert check）
            existing_records = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.employee_id.in_(employee_ids),
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                )
                .all()
            )
            salary_record_by_emp = {r.employee_id: r for r in existing_records}

            # ── 批次預載結束，開始計算 ──────────────────────────────────────────

            results = []
            errors = []
            total = len(employee_ids)
            done = 0

            # ── advisory lock：對每位員工依 id 排序取鎖，避免多 worker 交錯死鎖 ──
            # 鎖綁定在本 transaction，commit/rollback 時才釋放。
            from utils.advisory_lock import acquire_salary_lock

            for locked_emp_id in sorted(employee_ids):
                acquire_salary_lock(
                    session, employee_id=locked_emp_id, year=year, month=month
                )

            for emp_id in employee_ids:
                emp = emp_map.get(emp_id)
                if not emp:
                    errors.append(
                        {
                            "employee_id": emp_id,
                            "employee_name": "(未知)",
                            "error": "Employee not found",
                        }
                    )
                    done += 1
                    if progress_callback:
                        try:
                            progress_callback(done, total, "(未知)")
                        except Exception:
                            logger.debug("progress_callback 失敗，忽略", exc_info=True)
                    continue

                try:
                    emp_dict = self._load_emp_dict(emp)

                    # ── 考勤統計（使用預載）
                    attendances = att_by_emp[emp.id]
                    late_count = sum(1 for a in attendances if a.is_late)
                    early_count = sum(1 for a in attendances if a.is_early_leave)
                    missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
                    missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
                    total_late_minutes = sum(
                        a.late_minutes or 0 for a in attendances if a.is_late
                    )
                    total_early_minutes = sum(
                        a.early_leave_minutes or 0
                        for a in attendances
                        if a.is_early_leave
                    )
                    emp_dict["_late_details"] = [
                        a.late_minutes or 0
                        for a in attendances
                        if a.is_late and (a.late_minutes or 0) > 0
                    ]

                    if emp.employee_type == "hourly":
                        _work_end_t = datetime.strptime(
                            emp.work_end_time or "17:00", "%H:%M"
                        ).time()
                        total_hours = 0.0
                        total_hourly_pay = 0.0
                        for a in attendances:
                            if not a.punch_in_time:
                                continue
                            day_hours = _compute_hourly_daily_hours(
                                a.punch_in_time, a.punch_out_time, _work_end_t
                            )
                            total_hours += day_hours
                            total_hourly_pay += _calc_daily_hourly_pay(
                                day_hours, emp.hourly_rate or 0
                            )
                        emp_dict["work_hours"] = round(total_hours, 2)
                        emp_dict["hourly_calculated_pay"] = round(total_hourly_pay, 2)

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
                        details=[],
                    )

                    # ── 津貼（使用預載）
                    allowances = allowance_by_emp[emp.id]

                    # ── classroom_context / office_staff_context（使用預載 + 共用方法）
                    classroom_context = None
                    if emp.classroom_id and emp.classroom_id in classroom_map:
                        classroom_context = self._build_classroom_context_from_batch(
                            emp,
                            classroom_map[emp.classroom_id],
                            db_count_map,
                            assistant_to_classes,
                        )
                    office_staff_context = self._build_office_staff_context(
                        emp, total_students, classroom_context
                    )

                    # ── 請假、加班、會議（使用預載）
                    approved_leaves = leaves_by_emp[emp.id]
                    daily_salary = calc_daily_salary(emp.base_salary)
                    leave_deduction_total = _sum_leave_deduction(
                        approved_leaves, daily_salary
                    )
                    personal_sick_leave_hours = sum(
                        lv.leave_hours or 0
                        for lv in approved_leaves
                        if lv.leave_type in ("personal", "sick")
                    )
                    overtime_work_pay_total = sum(
                        o.overtime_pay or 0 for o in ot_by_emp[emp.id]
                    )

                    meeting_records = meetings_by_emp[emp.id]
                    meeting_attended = sum(1 for m in meeting_records if m.attended)
                    meeting_absent_current = sum(
                        1 for m in meeting_records if not m.attended
                    )
                    absent_period = meeting_absent_current + prior_absent_by_emp[emp.id]
                    meeting_context = None
                    if meeting_records or absent_period > 0:
                        meeting_context = {
                            "attended": meeting_attended,
                            "absent": meeting_absent_current,
                            "absent_period": absent_period,
                            "work_end_time": emp.work_end_time or "17:00",
                        }

                    # ── 曠職偵測（使用預載 + 共用方法）
                    absent_count = 0
                    absence_deduction_amount = 0.0
                    if emp.employee_type != "hourly":
                        expected_workdays = _build_expected_workdays(
                            year=year,
                            month=month,
                            holiday_set=holiday_set,
                            daily_shift_map=shifts_by_emp[emp.id],
                            hire_date_raw=emp.hire_date,
                            resign_date_raw=getattr(emp, "resign_date", None),
                        )
                        absent_count, absence_deduction_amount = self._compute_absence(
                            emp.id,
                            attendances,
                            approved_leaves,
                            expected_workdays,
                            daily_salary,
                            start_date,
                            end_date,
                            year,
                            month,
                        )

                    # ── 計算薪資
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
                        personal_sick_leave_hours=personal_sick_leave_hours,
                    )

                    breakdown.absent_count = absent_count
                    breakdown.absence_deduction = round(absence_deduction_amount)
                    breakdown.total_deduction = round(
                        breakdown.total_deduction + absence_deduction_amount
                    )
                    breakdown.net_salary = (
                        breakdown.gross_salary - breakdown.total_deduction
                    )

                    if breakdown.total_deduction < 0:
                        raise ValueError(
                            f"total_deduction 異常負值（含曠職）: {breakdown.total_deduction}"
                        )
                    if breakdown.net_salary < 0:
                        raise ValueError(
                            f"net_salary 異常負值（含曠職）: {breakdown.net_salary}"
                        )

                    # ── SalaryRecord upsert（延後至迴圈結束統一 commit）
                    salary_record = salary_record_by_emp.get(emp.id)
                    self._check_not_finalized(salary_record, emp.name, year, month)
                    if not salary_record:
                        salary_record = SalaryRecord(
                            employee_id=emp.id,
                            salary_year=year,
                            salary_month=month,
                        )
                        session.add(salary_record)

                    _fill_salary_record(salary_record, breakdown, self)
                    results.append((emp, breakdown))

                except Exception as e:
                    logger.error(
                        "薪資計算失敗 員工=%s(id=%d): %s",
                        emp.name,
                        emp.id,
                        e,
                        exc_info=True,
                    )
                    errors.append(
                        {
                            "employee_id": emp.id,
                            "employee_name": emp.name,
                            "error": str(e),
                        }
                    )
                finally:
                    done += 1
                    if progress_callback:
                        try:
                            progress_callback(
                                done, total, emp.name if emp else "(未知)"
                            )
                        except Exception:
                            logger.debug("progress_callback 失敗，忽略", exc_info=True)

            # 單次 commit（相較 N 次 commit 大幅減少 I/O 往返）
            session.commit()
            return results, errors

        except Exception as e:
            session.rollback()
            logger.exception("批次薪資計算失敗 year=%d month=%d", year, month)
            raise
        finally:
            session.close()
