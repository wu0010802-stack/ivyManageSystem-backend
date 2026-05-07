"""
薪資計算引擎 - SalaryEngine 類別
"""

import calendar
import copy
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional
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
    DEFAULT_MEETING_ABSENCE_PENALTY,
    DEFAULT_MEETING_HOURS,
)
from .breakdown import SalaryBreakdown
from .hourly import (
    _compute_hourly_daily_hours,
    _calc_daily_hourly_pay,
    _calc_daily_hourly_pay_with_cap,
)
from utils.constants import MAX_MONTHLY_OVERTIME_HOURS
from .proration import _prorate_for_period, _build_expected_workdays
from .utils import (
    get_bonus_distribution_month,
    get_meeting_deduction_period_start,
    _sum_leave_deduction,
    calc_daily_salary,
)
from . import festival as _festival
from .totals import recompute_record_totals
from services.student_enrollment import count_students_active_on

logger = logging.getLogger(__name__)


def _get_db_session():
    from models.database import get_session

    return get_session()


def _get_ytd_sick_hours_before(
    session, employee_id: int, year: int, month: int
) -> float:
    """查詢 year 年 1/1 起至 year/month/1 前一日為止，指定員工已核准病假時數。

    用於勞基法第 43 條 30 日（240h）半薪上限判斷。跨月假單只要 end_date < 本月 1 日
    就全數納入；若跨入本月，該筆會由當月主查詢一併取到，不重複計算。
    """
    from models.database import LeaveRecord

    year_start = date(year, 1, 1)
    month_start = date(year, month, 1)
    if month_start <= year_start:
        return 0.0

    # 以 end_date 作為落年度判斷：跨年假單（如 2025-12-28 → 2026-01-03）
    # 只要 end_date 落在本年度即納入，避免跨年請假時漏計上限。
    leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.is_approved == True,
            LeaveRecord.leave_type == "sick",
            LeaveRecord.end_date >= year_start,
            LeaveRecord.end_date < month_start,
        )
        .all()
    )
    return float(sum(lv.leave_hours or 0 for lv in leaves))


def _get_ytd_sick_hours_bulk(
    session, employee_ids: list, year: int, month: int
) -> dict:
    """批次版本：一次查詢多員工的年度累計病假時數。回傳 {employee_id: hours}。"""
    from models.database import LeaveRecord
    from sqlalchemy import func

    year_start = date(year, 1, 1)
    month_start = date(year, month, 1)
    result = {emp_id: 0.0 for emp_id in employee_ids}
    if month_start <= year_start or not employee_ids:
        return result

    rows = (
        session.query(
            LeaveRecord.employee_id,
            func.coalesce(func.sum(LeaveRecord.leave_hours), 0.0),
        )
        .filter(
            LeaveRecord.employee_id.in_(employee_ids),
            LeaveRecord.is_approved == True,
            LeaveRecord.leave_type == "sick",
            LeaveRecord.end_date >= year_start,
            LeaveRecord.end_date < month_start,
        )
        .group_by(LeaveRecord.employee_id)
        .all()
    )
    for emp_id, total in rows:
        result[emp_id] = float(total or 0)
    return result


def _fill_salary_record(salary_record, breakdown, engine):
    """將 SalaryBreakdown 的欄位填入 SalaryRecord（供正常路徑與 IntegrityError retry 共用）。

    若 SalaryRecord.manual_overrides 不為空,清單內欄位視為「人工調整鎖定」,
    跳過 breakdown 覆寫,並從 record 重算 gross/total/net 確保總額一致。
    """
    overrides = set(salary_record.manual_overrides or [])

    salary_record.bonus_config_id = engine._bonus_config_id
    salary_record.attendance_policy_id = engine._attendance_policy_id

    def _apply(field, value):
        if field not in overrides:
            setattr(salary_record, field, value)

    # 受 manual_overrides 保護的欄位透過 _apply,其他欄位直接覆寫
    _apply("base_salary", breakdown.base_salary)
    _apply("festival_bonus", breakdown.festival_bonus)
    _apply("overtime_bonus", breakdown.overtime_bonus)
    _apply("performance_bonus", breakdown.performance_bonus)
    _apply("special_bonus", breakdown.special_bonus)
    _apply("supervisor_dividend", breakdown.supervisor_dividend)
    _apply("overtime_pay", breakdown.overtime_work_pay)
    _apply("meeting_overtime_pay", breakdown.meeting_overtime_pay)
    _apply("meeting_absence_deduction", breakdown.meeting_absence_deduction)
    _apply("birthday_bonus", breakdown.birthday_bonus)
    _apply("labor_insurance_employee", breakdown.labor_insurance)
    _apply("health_insurance_employee", breakdown.health_insurance)
    _apply("pension_employee", breakdown.pension_self)
    _apply("late_deduction", breakdown.late_deduction)
    _apply("early_leave_deduction", breakdown.early_leave_deduction)
    _apply("missing_punch_deduction", breakdown.missing_punch_deduction)
    _apply("leave_deduction", breakdown.leave_deduction)
    _apply("absence_deduction", breakdown.absence_deduction)
    _apply("other_deduction", breakdown.other_deduction)

    # 不可由 manual_adjust 調整的欄位:時薪/雇主端/考勤統計恆覆寫
    salary_record.work_hours = breakdown.work_hours
    salary_record.hourly_rate = breakdown.hourly_rate
    salary_record.hourly_total = breakdown.hourly_total
    salary_record.labor_insurance_employer = breakdown.labor_insurance_employer
    salary_record.health_insurance_employer = breakdown.health_insurance_employer
    salary_record.pension_employer = breakdown.pension_employer
    salary_record.late_count = breakdown.late_count
    salary_record.early_leave_count = breakdown.early_leave_count
    salary_record.missing_punch_count = breakdown.missing_punch_count
    salary_record.absent_count = breakdown.absent_count

    if overrides:
        # 有人工調整時,從 record 自身重算總額,避免 breakdown 的 gross/total/net 與
        # 被保留的人工值脫節。
        recompute_record_totals(salary_record)
    else:
        salary_record.gross_salary = breakdown.gross_salary
        salary_record.total_deduction = breakdown.total_deduction
        salary_record.net_salary = breakdown.net_salary
        salary_record.bonus_separate = breakdown.bonus_separate
        salary_record.bonus_amount = (
            breakdown.festival_bonus
            + breakdown.overtime_bonus
            + breakdown.supervisor_dividend
        )

    # 成功重算 → 清除 stale 旗標(若為預載的舊 record 之前可能被標 True)
    salary_record.needs_recalc = False
    # 重算也視為 SalaryRecord 有效異動，推進樂觀鎖版本：
    # - 讓 ETag/If-Match 能偵測「前端拿到的是重算前版本」→ 409，避免覆蓋
    # - 讓 snapshot cache（key=(record_id, version)）自動失效，不再沿用舊 snapshot
    salary_record.version = (salary_record.version or 0) + 1


class SalaryEngine:
    """薪資計算引擎"""

    # 預設扣款規則
    DEFAULT_LATE_PER_MINUTE = DEFAULT_LATE_PER_MINUTE
    DEFAULT_EARLY_PER_MINUTE = DEFAULT_EARLY_PER_MINUTE
    DEFAULT_MISSING_PUNCH = DEFAULT_MISSING_PUNCH
    DEFAULT_MEETING_ABSENCE_PENALTY = DEFAULT_MEETING_ABSENCE_PENALTY
    DEFAULT_MEETING_HOURS = DEFAULT_MEETING_HOURS

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
        # deduction_rules 為歷史相容 stub：實際扣款由 services.salary.deduction
        # 直接以勞基法基準（month_salary / 30 / 8 / 60）計算，AttendancePolicy
        # 的 late_deduction / early_leave_deduction / missing_punch_deduction
        # 欄位已 deprecated，不再進入薪資計算（dev 端點 _build_engine_config 仍引用）
        self.deduction_rules = {
            "late": {"per_minute": 1},
            "missing": {"amount": 0},
            "early": {"per_minute": 1},
        }
        # 重算歷史月份時用來序列化 engine 設定切換,避免兩個並發請求互相
        # 蓋掉對方剛 swap 進來的設定;使用 RLock 讓同一執行緒可重入。
        self._config_swap_lock = threading.RLock()
        # config_for_month snapshot cache：(year, month) → snapshot dict
        # 同一個歷史月份的設定不會變動（除非 admin 寫入 PUT，會 invalidate），
        # 因此把第一次 _apply_configs_for_month 的結果快取，後續 swap 直接 restore。
        # 取代 audit A.P0.1：避免每次 with config_for_month 都跑 5-6 次 DB query。
        self._month_config_cache: dict[tuple[int, int], dict] = {}
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
        # 職稱→節慶獎金等級對應（DB 載入後覆蓋；初始為 hardcode POSITION_GRADE_MAP 副本）
        self._position_grade_map: dict = dict(POSITION_GRADE_MAP)
        # 園務會議設定
        self._meeting_absence_penalty = DEFAULT_MEETING_ABSENCE_PENALTY
        self._meeting_hours = DEFAULT_MEETING_HOURS
        # 考勤政策設定（僅 festival_bonus_months 進入計算；其他欄位已 deprecated，
        # 詳見 deduction_rules 的說明 + services/salary/deduction.py）
        self._attendance_policy = {
            "festival_bonus_months": 3,
        }

        if load_from_db:
            self.load_config_from_db()

    # ─────────────────────────────────────────────────────────────────────
    # 設定版本切換（歷史月份重算用）
    # ─────────────────────────────────────────────────────────────────────
    # 為何需要這層 context：
    #   `load_config_from_db()` 永遠取「目前 active」的 BonusConfig / AttendancePolicy /
    #   InsuranceRate。重算歷史月份時若直接套這份目前設定,會把舊月份的薪資算成新獎金、
    #   新政策,跟使用者實際在「該月份當下」的設定脫鉤。
    #
    # 解法：以該月份 last day 為時間切片,選 `created_at <= 月底` 中最新的版本作為
    # 該月有效設定;若沒有任何 created_at 早於該月,fallback 至最舊版本(代表系統建立
    # 之初的 baseline)。所有 calc 路徑(process_salary_calculation / period accrual)
    # 進入時 swap 進該月份設定,離開時還原。
    # ─────────────────────────────────────────────────────────────────────

    def _snapshot_config_state(self) -> dict:
        """快照所有受 load_config_from_db() 影響的 engine 屬性,供 restore 使用。"""
        return {
            "bonus_config_id": self._bonus_config_id,
            "attendance_policy_id": self._attendance_policy_id,
            "bonus_base": copy.deepcopy(self._bonus_base),
            "supervisor_festival_bonus": dict(self._supervisor_festival_bonus),
            "office_festival_bonus_base": dict(self._office_festival_bonus_base),
            "supervisor_dividend": dict(self._supervisor_dividend),
            "overtime_per_person": copy.deepcopy(self._overtime_per_person),
            "school_wide_target": self._school_wide_target,
            "target_enrollment": copy.deepcopy(self._target_enrollment),
            "overtime_target": copy.deepcopy(self._overtime_target),
            "attendance_policy": dict(self._attendance_policy),
            "meeting_hours": self._meeting_hours,
            "meeting_absence_penalty": self._meeting_absence_penalty,
            "position_grade_map": dict(self._position_grade_map),
            # InsuranceService 的 instance 屬性
            "insurance": {
                "labor_rate": self.insurance_service.labor_rate,
                "labor_employee_ratio": self.insurance_service.labor_employee_ratio,
                "labor_employer_ratio": self.insurance_service.labor_employer_ratio,
                "labor_government_ratio": self.insurance_service.labor_government_ratio,
                "health_rate": self.insurance_service.health_rate,
                "health_employee_ratio": self.insurance_service.health_employee_ratio,
                "health_employer_ratio": self.insurance_service.health_employer_ratio,
                "pension_employer_rate": self.insurance_service.pension_employer_rate,
                "average_dependents": self.insurance_service.average_dependents,
                "labor_max_insured": self.insurance_service.labor_max_insured,
                "health_max_insured": self.insurance_service.health_max_insured,
                "pension_max_insured": self.insurance_service.pension_max_insured,
                "table": copy.deepcopy(self.insurance_service.table),
                "brackets_year": self.insurance_service.brackets_year,
            },
        }

    def _restore_config_state(self, snapshot: dict) -> None:
        """以 snapshot 還原 engine + insurance_service 屬性。"""
        self._bonus_config_id = snapshot["bonus_config_id"]
        self._attendance_policy_id = snapshot["attendance_policy_id"]
        self._bonus_base = snapshot["bonus_base"]
        self._supervisor_festival_bonus = snapshot["supervisor_festival_bonus"]
        self._office_festival_bonus_base = snapshot["office_festival_bonus_base"]
        self._supervisor_dividend = snapshot["supervisor_dividend"]
        self._overtime_per_person = snapshot["overtime_per_person"]
        self._school_wide_target = snapshot["school_wide_target"]
        self._target_enrollment = snapshot["target_enrollment"]
        self._overtime_target = snapshot["overtime_target"]
        self._attendance_policy = snapshot["attendance_policy"]
        # 園規常數（KeyError 防禦：舊 snapshot 沒這兩個 key 時退回現值）
        if "meeting_hours" in snapshot:
            self._meeting_hours = snapshot["meeting_hours"]
        if "meeting_absence_penalty" in snapshot:
            self._meeting_absence_penalty = snapshot["meeting_absence_penalty"]
        # 職稱→等級對應（同步注入給 festival module cache，否則切換歷史月期間
        # festival.py 仍用上層 caller 留下的 map，造成 grade 判定錯亂）
        if "position_grade_map" in snapshot:
            self._position_grade_map = snapshot["position_grade_map"]
            from services.salary import festival as _festival

            _festival.set_active_grade_map(self._position_grade_map)
        for k, v in snapshot["insurance"].items():
            setattr(self.insurance_service, k, v)

    @staticmethod
    def _select_active_at(session, model, year: int, month: int):
        """選出該月最後一日(含)前最新建立的 row;若無則 fallback 最舊 row。

        Why created_at: BonusConfig / InsuranceRate 沒有 effective_date 欄位,
        AttendancePolicy 雖有但歷史資料未必填;以 created_at 推估等同「設定上線當下生效」,
        對純歷史重算來說與直觀預期一致(改設定當天才會影響當月以後計算)。

        Why NOT filter is_active: 歷史 config 改版後舊版本通常會被設為
        is_active=False（見 test_swap_uses_version_active_at_month_end），但
        歷史月份重算仍須能找到「該月當下生效」的版本。一律過濾 is_active=True
        會讓所有歷史月份都拿到目前最新版本，破壞歷史對帳。

        留意攻擊面：admin 持 SALARY_WRITE 可建惡意金額且 is_active=False 的
        BonusConfig/InsuranceRate，等歷史補算被 id desc 撿到。緩解：
        (a) BonusConfig / InsuranceRate / AttendancePolicy 的 INSERT/UPDATE
            必須走 finance_approve + audit（目前 api/config.py / api/insurance.py
            缺此守衛，待補）；
        (b) 上線一筆 needs_recalc 全標守衛（變更設定即時 mark_stale）。
        Refs: 邏輯漏洞 audit 2026-05-07 P0 (#10) — 由衝突回歸測試
        test_swap_uses_version_active_at_month_end 重新評估後決議：原建議
        is_active filter 不採用，改走守衛+稽核路線。
        """
        last_day = calendar.monthrange(year, month)[1]
        cutoff = datetime(year, month, last_day, 23, 59, 59)
        row = (
            session.query(model)
            .filter(model.created_at <= cutoff)
            .order_by(model.id.desc())
            .first()
        )
        if row is None:
            row = session.query(model).order_by(model.id.asc()).first()
        return row

    def _apply_bonus_record_locked(self, bonus) -> None:
        """把 BonusConfig record 套用到 engine state；caller 必須持 _config_swap_lock。

        Why: config_for_month 與 _load_config_from_db_locked 兩處需做同樣 DB→state 對應；
            抽出共用函式後新增 bonus 欄位只改一處，避免「忘了某 key 在另一處被重建抹掉」
            的歷史 bug（art_teacher 基數曾因此被歸零）。
        """
        self._bonus_config_id = bonus.id
        # art_teacher 基數來自 bonus.art_teacher_festival（NULL → 模組預設 2000）；
        # 必須在 _bonus_base 內保留 art_teacher key，否則 festival.py 取不到會回 0
        art_base = bonus.art_teacher_festival
        if art_base is None:
            art_base = FESTIVAL_BONUS_BASE["art_teacher"]["A"]
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
            "art_teacher": {
                "A": art_base,
                "B": art_base,
                "C": art_base,
            },
        }
        # 園規常數（NULL → 模組預設）
        if bonus.meeting_default_hours is not None:
            self._meeting_hours = float(bonus.meeting_default_hours)
        if bonus.meeting_absence_penalty is not None:
            self._meeting_absence_penalty = int(bonus.meeting_absence_penalty)
        self._supervisor_festival_bonus = {
            "園長": bonus.principal_festival,
            "主任": bonus.director_festival,
            "組長": bonus.leader_festival,
        }
        self._office_festival_bonus_base = {
            "司機": bonus.driver_festival,
            "美編": bonus.designer_festival,
            "行政": bonus.admin_festival,
        }
        self._supervisor_dividend = {
            "園長": bonus.principal_dividend,
            "主任": bonus.director_dividend,
            "組長": bonus.leader_dividend,
            "副組長": bonus.vice_leader_dividend,
        }
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
        if bonus.school_wide_target:
            self._school_wide_target = bonus.school_wide_target

    def _apply_configs_for_month(self, session, year: int, month: int) -> None:
        """以 (year, month) 對應的歷史版本覆寫 engine state(in-place)。"""
        from models.database import (
            AttendancePolicy,
            BonusConfig as DBBonusConfig,
            GradeTarget,
            InsuranceRate,
        )

        rate = self._select_active_at(session, InsuranceRate, year, month)
        if rate is not None:
            self.insurance_service.update_rates_from_db(rate)

        # 歷史月份重算：以該月份所屬年度載入級距表（避免用今年級距算去年薪資）
        self.insurance_service.load_brackets_from_db(year)
        # 職稱→等級 grade_map 不分年度，呼叫 load 補上 module-level cache
        self._load_grade_map_from_db(session)

        policy = self._select_active_at(session, AttendancePolicy, year, month)
        if policy is not None:
            self._attendance_policy_id = policy.id
            self._attendance_policy = {
                "festival_bonus_months": policy.festival_bonus_months,
            }

        bonus = self._select_active_at(session, DBBonusConfig, year, month)
        if bonus is not None:
            self._apply_bonus_record_locked(bonus)

            # 年級目標:綁定到 bonus.id 的版本目標 + NULL fallback
            null_targets = {
                t.grade_name: t
                for t in session.query(GradeTarget)
                .filter(GradeTarget.bonus_config_id == None)  # noqa: E711
                .all()
            }
            versioned_targets = {
                t.grade_name: t
                for t in session.query(GradeTarget)
                .filter(GradeTarget.bonus_config_id == bonus.id)
                .all()
            }
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

    @contextmanager
    def config_for_month(self, session, year: int, month: int) -> Iterator[None]:
        """臨時把 engine 設定切換成 (year, month) 對應的歷史版本,離開時還原。

        - 取 _config_swap_lock 序列化,避免兩個並發 calc 把對方的 swap 蓋掉
        - 失敗(任何例外)時也會 restore,保持 engine 狀態一致
        - 同 thread 可重入(RLock),內層 context 一樣會 swap & restore

        效能：同一 (year, month) 第一次 swap 後其 snapshot 進入 _month_config_cache，
        後續 swap 直接 restore（避免 5-6 次 DB query）。設定異動時由 load_config_from_db /
        invalidate_month_config_cache 清空。Audit A.P0.1。
        """
        with self._config_swap_lock:
            outer_snapshot = self._snapshot_config_state()
            try:
                cached = self._month_config_cache.get((year, month))
                if cached is not None:
                    self._restore_config_state(cached)
                else:
                    self._apply_configs_for_month(session, year, month)
                    self._month_config_cache[(year, month)] = (
                        self._snapshot_config_state()
                    )
                yield
            finally:
                self._restore_config_state(outer_snapshot)

    def load_config_from_db(self):
        """從資料庫載入設定。

        必須與 config_for_month 共用 _config_swap_lock,否則:
          T1 進 config_for_month → snapshot OLD,apply 該月歷史設定,yield
          T2 PUT /api/config/* → load_config_from_db 寫入 NEW
          T1 finally → _restore_config_state(OLD) 會把 NEW 整個蓋掉
          → engine 卡在 OLD,直到下次有人觸發 reload 才恢復
        拿同一把 RLock 後,T2 必須等 T1 restore 完才能寫入,reload 永不被覆蓋。
        """
        with self._config_swap_lock:
            self._load_config_from_db_locked()
            # 設定異動：清空所有歷史月份 snapshot cache，下次 config_for_month
            # 會以新版設定重新建立。Audit A.P0.1。
            self._month_config_cache.clear()

    def invalidate_month_config_cache(self) -> None:
        """外部呼叫端可主動清空 (year, month) snapshot cache。

        典型場景：新增 / 修改 / 失效 BonusConfig / InsuranceRate / AttendancePolicy /
        GradeTarget 後（避免 cache 拿到舊版設定）。注意 load_config_from_db 已自動
        清除，此 helper 給「未走 load_config_from_db 但仍想保險」的路徑備用。
        """
        with self._config_swap_lock:
            self._month_config_cache.clear()

    def _load_grade_map_from_db(self, session) -> None:
        """從 job_titles.bonus_grade 載入「職稱→等級」對應，更新 self._position_grade_map
        並注入到 festival.py module-level cache。

        失敗策略：DB 表不存在/欄位未 migrate → 沿用 hardcode POSITION_GRADE_MAP。
        """
        from services.salary import festival as _festival

        try:
            from models.database import JobTitle

            rows = (
                session.query(JobTitle.name, JobTitle.bonus_grade)
                .filter(JobTitle.bonus_grade.isnot(None))
                .all()
            )
            if rows:
                self._position_grade_map = {name: grade for name, grade in rows}
                _festival.set_active_grade_map(self._position_grade_map)
                return
        except Exception:
            logger.warning(
                "_load_grade_map_from_db 失敗，沿用 hardcode POSITION_GRADE_MAP",
                exc_info=True,
            )
        # DB 無資料或讀取失敗：fallback 到 hardcode
        self._position_grade_map = dict(POSITION_GRADE_MAP)
        _festival.set_active_grade_map(self._position_grade_map)

    def _load_config_from_db_locked(self):
        """實際的 DB 讀取 + state 寫入；caller 必須持有 _config_swap_lock。"""
        try:
            session = _get_db_session()
            from models.database import (
                AttendancePolicy,
                BonusConfig as DBBonusConfig,
                GradeTarget,
                InsuranceRate,
            )

            # 載入勞健保費率（部分覆蓋：僅影響 labor_government 與 pension_employee）
            insurance_rate = (
                session.query(InsuranceRate)
                .filter(InsuranceRate.is_active == True)
                .order_by(InsuranceRate.id.desc())
                .first()
            )
            if insurance_rate is not None:
                self.insurance_service.update_rates_from_db(insurance_rate)

            # 載入勞健保級距表（DB 來源優先；若該年無資料 fallback 到最近一年或 hardcode）
            self.insurance_service.load_brackets_from_db()

            # 載入職稱→節慶獎金等級 grade_map（job_titles.bonus_grade）
            self._load_grade_map_from_db(session)

            # 載入考勤政策（依 id desc 取最新 active，與 InsuranceRate 一致；
            # 即使資料庫意外殘留多筆 is_active=true 也能穩定選到最新版本）
            policy = (
                session.query(AttendancePolicy)
                .filter(AttendancePolicy.is_active == True)
                .order_by(AttendancePolicy.id.desc())
                .first()
            )
            if policy:
                self._attendance_policy_id = policy.id  # 記錄版本 ID
                # AttendancePolicy.late_deduction / early_leave_deduction /
                # missing_punch_deduction 欄位已 deprecated，不再進入扣款計算
                # （業主決議維持勞基法基準：每分鐘 = 月薪 / 30 / 8 / 60）。
                # 僅 festival_bonus_months 仍透過 _attendance_policy 影響節慶獎金資格。
                self._attendance_policy = {
                    "festival_bonus_months": policy.festival_bonus_months,
                }

            # 載入獎金設定（依 id desc 取最新 active，與 InsuranceRate 一致）
            bonus = (
                session.query(DBBonusConfig)
                .filter(DBBonusConfig.is_active == True)
                .order_by(DBBonusConfig.id.desc())
                .first()
            )
            if bonus:
                self._apply_bonus_record_locked(bonus)

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
        """設定獎金參數（從前端傳入）。

        與 load_config_from_db 同樣會大批寫入受 _config_swap_lock 保護的屬性,
        必須拿同一把鎖,避免被 config_for_month 的 restore 覆蓋。
        """
        if not bonus_config:
            return

        with self._config_swap_lock:
            self._apply_bonus_config_locked(bonus_config)

    def _apply_bonus_config_locked(self, bonus_config: dict):
        """實際的設定寫入；caller 必須持有 _config_swap_lock。"""
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
        makeup_set=None,
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
            makeup_set,
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
        """節慶獎金資格基準日：薪資月份的月底。

        Why: 舊版用月份首日判斷年資門檻，導致在發放月當月才滿三個月的員工被排除。
        例如 2025-11-15 到職員工在 2026-02-28 已滿 3 個月，但以 2026-02-01 為準
        尚不足門檻 → 應發卻未發。以月底為準可對齊「薪資結算時是否已達資格」的語意。
        """
        _, last_day = calendar.monthrange(year, month)
        return date(year, month, last_day)

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

        # 共用副班導 / 跨班美師：取所有共用班的分數平均（含本班 + shared_other_classes 全部）
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

    @staticmethod
    def _pick_primary_classroom(classrooms, employee_id: int):
        """從候選班級挑「主要班級」：head_teacher > assistant_teacher > art_teacher。

        Why: Employee.classroom_id 是冗餘欄位，可能因班級頁面更新未同步而失準。
        所有薪資路徑改以 Classroom 表的 head/assistant/art_teacher_id 反查為準。
        當教師同時帶多班（如美術老師跨多個班、共用副班導），優先取角色階層較高者。
        """
        head = next((c for c in classrooms if c.head_teacher_id == employee_id), None)
        if head:
            return head
        assistant = next(
            (c for c in classrooms if c.assistant_teacher_id == employee_id), None
        )
        if assistant:
            return assistant
        return next((c for c in classrooms if c.art_teacher_id == employee_id), None)

    def _resolve_classroom_for_employee_in_term(
        self,
        session,
        employee_id: int,
        school_year: int,
        semester: int,
    ):
        """反查指定學期內，員工所屬主要班級。

        若該學期無對應 active 班級，fallback 至跨學期任一 active（並 log warning）。
        Fallback 用於：學校未替每學期建立獨立班級紀錄（單班沿用）的相容情境。
        """
        from sqlalchemy import or_
        from models.database import Classroom

        base_q = (
            session.query(Classroom)
            .options(joinedload(Classroom.grade))
            .filter(
                Classroom.is_active == True,
                or_(
                    Classroom.head_teacher_id == employee_id,
                    Classroom.assistant_teacher_id == employee_id,
                    Classroom.art_teacher_id == employee_id,
                ),
            )
        )

        term_classrooms = (
            base_q.filter(
                Classroom.school_year == school_year,
                Classroom.semester == semester,
            )
            .order_by(Classroom.id.asc())
            .all()
        )
        if term_classrooms:
            return self._pick_primary_classroom(term_classrooms, employee_id)

        any_classrooms = base_q.order_by(Classroom.id.desc()).all()
        picked = self._pick_primary_classroom(any_classrooms, employee_id)
        if picked:
            logger.warning(
                "員工 %s 在 school_year=%s semester=%s 無對應 active 班級；"
                "fallback 使用 classroom_id=%s（學校未建立該學期班級紀錄？）",
                employee_id,
                school_year,
                semester,
                picked.id,
            )
        return picked

    def _resolve_classroom_for_employee_in_month(
        self,
        session,
        employee_id: int,
        year: int,
        month: int,
    ):
        """根據薪資年月解析學期，反查當期主要班級。

        為 _compute_period_accrual_totals 等需要逐月回算的路徑提供「依月份解析」入口，
        避免拿目前的 emp.classroom_id 套用在跨學期的舊月份上（之前的 bug）。
        """
        from utils.academic import resolve_current_academic_term

        school_year, semester = resolve_current_academic_term(date(year, month, 1))
        return self._resolve_classroom_for_employee_in_term(
            session, employee_id, school_year, semester
        )

    def _build_classroom_context_from_db(
        self,
        session,
        classroom,
        employee_id: int,
        reference_date: date,
        classroom_count_map: dict | None = None,
        assistant_to_classes_map: dict | None = None,
        art_to_classes_map: dict | None = None,
    ) -> Optional[dict]:
        """從 DB 班級資料建構帶班獎金計算上下文。

        reference_date:       薪資對應月份的查詢基準日（通常是該月月末）。
                              明細頁與正式計算必須用同一日期，否則在籍人數
                              會以「今天」漂移，明細對不上已計算的薪資記錄。
        classroom_count_map:  可傳入預先批次查詢的 {classroom_id: int}，避免 N+1。
        assistant_to_classes_map / art_to_classes_map: 預先建好的 emp_id → list[Classroom]
                              副班導/美師 shared_classes 反查表（audit B.P0.2）。
                              若任一 supplied 則 shared_classes 從 map 讀取，跳過
                              session.query(DBClassroom)。
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

        # 同一員工跨多班時，將其他班資訊塞進 shared_other_classes，下游
        # _calculate_with_classroom_context_dict 會以在籍人數加權平均節慶/超額獎金。
        # 副班導：依 assistant_teacher_id 反查；美師：依 art_teacher_id 反查
        # （第十二條：美師獎金跨班亦應反映各班負擔，而非只看 _pick_primary_classroom 挑到的單班）。
        # 注意：若員工同時掛多個角色（例：head_teacher 於 A 班 + art_teacher 於 B 班），
        # _pick_primary_classroom 取 head_teacher，B 班暫不會被合併進來；屬於另一個尚未涵蓋的情境。
        shared_filter = None
        if role == "assistant_teacher":
            shared_filter = DBClassroom.assistant_teacher_id == employee_id
        elif role == "art_teacher":
            shared_filter = DBClassroom.art_teacher_id == employee_id

        if shared_filter is not None:
            # 預載 map（audit B.P0.2）優先：避免 session.query(DBClassroom) per-emp。
            preloaded: list | None = None
            if role == "assistant_teacher" and assistant_to_classes_map is not None:
                preloaded = assistant_to_classes_map.get(employee_id, [])
            elif role == "art_teacher" and art_to_classes_map is not None:
                preloaded = art_to_classes_map.get(employee_id, [])

            if preloaded is not None:
                shared_classes = preloaded
            else:
                # is_active 過濾與 process_bulk_salary_calculation 預載一致（line 2093），
                # 否則同一員工在單筆與批次路徑會得到不同的 is_shared_assistant 判斷。
                shared_classes = (
                    session.query(DBClassroom)
                    .options(joinedload(DBClassroom.grade))
                    .filter(
                        shared_filter,
                        DBClassroom.is_active == True,
                    )
                    .all()
                )
            if len(shared_classes) >= 2:
                if role == "assistant_teacher":
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
        art_to_classes: dict,
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

        # 副班導/美師跨多班時：合併其他班為 shared_other_classes，由下游加權平均。
        # 與 _build_classroom_context_from_db 行為一致；mixed-role 邊界同樣留待後續處理。
        shared_classes = []
        if role == "assistant_teacher":
            shared_classes = assistant_to_classes.get(emp.id, [])
        elif role == "art_teacher":
            shared_classes = art_to_classes.get(emp.id, [])

        if len(shared_classes) >= 2:
            if role == "assistant_teacher":
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
        self, breakdown, employee: dict, year: int, month: int
    ) -> float:
        """計算底薪折算，回傳 contracted_base（用於後續保險計算）。"""
        contracted_base = employee.get("base_salary", 0) or 0
        breakdown.base_salary = _prorate_for_period(
            contracted_base,
            employee.get("hire_date"),
            employee.get("resign_date"),
            year,
            month,
        )

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
        *,
        period_festival_override: Optional[float] = None,
        period_overtime_override: Optional[float] = None,
    ) -> None:
        """計算節慶獎金、超額獎金、主管紅利、生日禮金，並寫入 breakdown。

        發放月特殊規則（業主 2026-04-25 確認）：
        若 month 為發放月 (2/6/9/12) 且呼叫端提供 period_*_override，會以期間
        累積值覆蓋當月單月計算（例：6 月 = 2-5 月每月各自比例合計）。會議缺席
        扣款仍由 _calculate_deductions 套用一次（用 absent_period × penalty）。
        """
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
        elif period_festival_override is not None:
            # 發放月：用期間累積總額覆蓋當月單月計算（業主 2026-04-25 確認）。
            # 業務規則：6 月發 = 2-5 月每月各自比例的合計（不含 6 月本身）。
            # 期間每月的計算（含逐月在籍人數、達成率）由呼叫端透過
            # calculate_period_accrual_row 累計後傳入。
            breakdown.festival_bonus = round(period_festival_override)
            breakdown.overtime_bonus = round(period_overtime_override or 0)

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

        # skip_payroll_bonuses：業主指示「不發紅利/節慶/超額/生日禮金」
        # （如總園長指示不薪轉、不作帳的特殊個案）。基本薪 + 勞健保仍正常計算。
        # 放在最後一步：所有 override / 期間累積 / 全勤條件都已套用後再短路歸零，
        # 避免漏蓋某條路徑導致仍發部分獎金。
        if employee.get("skip_payroll_bonuses", False):
            breakdown.festival_bonus = 0
            breakdown.overtime_bonus = 0
            breakdown.supervisor_dividend = 0
            breakdown.birthday_bonus = 0

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
        # 投保薪資來源：insurance_salary_level → base_salary（月薪制）→ hourly_rate × 176（時薪制）
        # 時薪制 fallback 防止 insurance=0 時 silent skip 保費計算（違反勞保條例）
        from .insurance_salary import resolve_insurance_salary_raw

        contracted_base = employee.get("base_salary", 0) or 0
        pension_rate = employee.get("pension_self_rate", 0.0)
        _ins_raw = resolve_insurance_salary_raw(
            employee_type=employee.get("employee_type") or "regular",
            base_salary=contracted_base,
            insurance_salary_level=employee.get("insurance_salary"),
            hourly_rate=employee.get("hourly_rate", 0),
        )
        if _ins_raw > 0:
            _ins_salary = self.insurance_service.get_bracket(_ins_raw)["amount"]
            # 議題 B：三制度分項投保（NULL 沿用 _ins_salary）
            labor_ins = employee.get("labor_insured_salary")
            health_ins = employee.get("health_insured_salary")
            pension_ins = employee.get("pension_insured_salary")
            insurance = self.insurance_service.calculate(
                _ins_salary,
                employee.get("dependents", 0),
                pension_self_rate=pension_rate,
                no_employment_insurance=bool(
                    employee.get("no_employment_insurance", False)
                ),
                health_exempt=bool(employee.get("health_exempt", False)),
                labor_insured=float(labor_ins) if labor_ins is not None else None,
                health_insured=float(health_ins) if health_ins is not None else None,
                pension_insured=float(pension_ins) if pension_ins is not None else None,
            )
            breakdown.labor_insurance = insurance.labor_employee
            breakdown.health_insurance = insurance.health_employee
            breakdown.pension_self = insurance.pension_employee
            # 雇主端：員工薪水不扣，但園方實際支出；落 SalaryRecord 供財報 / 勞保局匯出
            breakdown.labor_insurance_employer = insurance.labor_employer
            breakdown.health_insurance_employer = insurance.health_employer
            breakdown.pension_employer = insurance.pension_employer

            # 季扣眷屬：1/4/7/10 月份額外扣「health_employee × extra_dependents_quarterly × 3」
            # （業主實務：本人+1 月扣 / 第 2+ 名季扣 3 個月一次）
            extra_q = int(employee.get("extra_dependents_quarterly", 0) or 0)
            if (
                extra_q > 0
                and not employee.get("health_exempt", False)
                and month in (1, 4, 7, 10)
            ):
                # 用「未含眷屬」的單口健保金額 × 季扣眷屬數 × 3 個月
                # 議題 B：以 health_insured 為準（NULL 沿用 _ins_salary）
                health_amount_for_quarterly = (
                    float(health_ins) if health_ins is not None else _ins_salary
                )
                health_bracket_emp_base = self.insurance_service.get_bracket(
                    min(
                        health_amount_for_quarterly,
                        self.insurance_service.health_max_insured,
                    )
                )["health_employee"]
                quarterly_extra = round(health_bracket_emp_base * extra_q * 3)
                breakdown.health_insurance += quarterly_extra
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

            breakdown.meeting_overtime_pay = meeting_context.get(
                "overtime_pay_total", 0
            )
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
        """委派至 `services.salary.deduction.calculate_attendance_deduction`。"""
        from .deduction import calculate_attendance_deduction as _impl

        return _impl(attendance, daily_salary, base_salary, late_details)

    def calculate_bonus(
        self, target: int, current: int, base_amount: float, overtime_per: float = 500
    ) -> dict:
        """委派至 `services.salary.deduction.calculate_bonus`（舊版相容）。"""
        from .deduction import calculate_bonus as _impl

        return _impl(target, current, base_amount, overtime_per)

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
        classroom_context: dict = None,
        office_staff_context: dict = None,
        meeting_context: dict = None,
        working_days: int = 22,
        overtime_work_pay: float = 0,
        personal_sick_leave_hours: float = 0,
        period_festival_override: Optional[float] = None,
        period_overtime_override: Optional[float] = None,
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
            classroom_context:  班級上下文 (新版節慶獎金用)
            office_staff_context: 辦公室人員上下文
            meeting_context:    園務會議上下文
            personal_sick_leave_hours: 當月事假+病假累計時數（>40h 時取消所有節慶獎金及紅利）
            period_festival_override: 發放月「期間累積」節慶獎金總額。提供時於發放月覆蓋
                當月單月計算（業主 2026-04-25 確認規則：6 月發 = 2-5 月每月各自比例的合計）。
            period_overtime_override: 發放月「期間累積」超額獎金總額（與上同義）。
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
            self._calculate_base_gross(breakdown, employee, year, month)
            self._calculate_bonuses(
                breakdown,
                employee,
                year,
                month,
                classroom_context,
                office_staff_context,
                bonus_settings,
                personal_sick_leave_hours,
                period_festival_override=period_festival_override,
                period_overtime_override=period_overtime_override,
            )
            breakdown.gross_salary = (
                breakdown.base_salary
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
        breakdown.gross_salary = round(breakdown.gross_salary)
        breakdown.total_deduction = round(breakdown.total_deduction)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

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

            # 優先使用 _ctx 中預先查好的班級；否則依年月反查當期班級
            # （不再讀 emp.classroom_id，因該欄位可能因班級頁面更新未同步而失準）
            if _ctx is not None and "classroom" in _ctx:
                classroom = _ctx["classroom"]
            else:
                classroom = self._resolve_classroom_for_employee_in_month(
                    session, employee_id, year, month
                )

            bonus_base = 0
            target_enrollment = 0
            current_enrollment = 0
            ratio = 0
            festival_bonus = 0
            overtime_bonus_value = 0
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

            elif (
                office_base := self.get_office_festival_bonus_base(position, title_name)
            ) is not None:
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
                    assistant_to_classes_map=(
                        _ctx.get("assistant_to_classes_map") if _ctx else None
                    ),
                    art_to_classes_map=(
                        _ctx.get("art_to_classes_map") if _ctx else None
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
                # overtime 也走相同 _calculate_classroom_bonus_result（含共用副班導加權平均）
                # 並套用相同 eligibility 規則，與 calculate_salary 路徑一致；
                # 由 calculate_period_accrual_row 透過 overtimeBonus 欄位讀取，
                # 避免在累積路徑上自行重算（會漏掉共用班加權）。
                overtime_bonus_value = (
                    int(bonus_result.get("overtime_bonus") or 0) if is_eligible else 0
                )
                if is_eligible:
                    other_count = len(
                        classroom_context.get("shared_other_classes")
                        or (
                            [classroom_context["shared_second_class"]]
                            if classroom_context.get("shared_second_class")
                            else []
                        )
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
                # 僅帶班老師路徑會填入 >0；其他類別維持 0（calculate_salary 約定）
                "overtimeBonus": (
                    overtime_bonus_value if category == "帶班老師" else 0
                ),
                "remark": remark,
            }

        except Exception as e:
            logger.exception("計算節慶獎金明細失敗：employee_id=%s", employee_id)
            return {"name": f"Error: {e}", "festivalBonus": 0}
        finally:
            # 只在自己建立 session 時才關閉（避免關閉外部傳入的 session）
            if _own_session:
                session.close()

    def calculate_period_accrual_row(
        self,
        employee_id: int,
        year: int,
        month: int,
        *,
        _ctx: dict | None = None,
    ) -> dict:
        """單員工×單月的「本期累積」列資料（節慶/超額/會議扣款）。

        不套用「事病假 >40 小時全清零」規則（該規則僅在發放月當月檢查，
        非累積期間規則）。UI 須以 tooltip 聲明。
        """
        import calendar as _cal
        from datetime import date as _date

        from models.database import MeetingRecord

        _own_session = _ctx is None
        if _own_session:
            session = _get_db_session()
        else:
            session = _ctx["session"]

        try:
            # 1) 呼叫 breakdown 同時取得節慶與超額獎金；breakdown 內部已用
            # _calculate_classroom_bonus_result 對共用副班導/跨多班做加權平均，
            # 也統一套 eligibility（未滿 3 個月），與 calculate_salary 路徑一致。
            # 不要在這裡重新呼叫 calculate_overtime_bonus，否則會漏掉共用班加權。
            breakdown = self.calculate_festival_bonus_breakdown(
                employee_id, year, month, _ctx=_ctx
            )
            festival_bonus = int(breakdown.get("festivalBonus") or 0)
            overtime_bonus = int(breakdown.get("overtimeBonus") or 0)
            category = breakdown.get("category", "")

            # 2) 會議缺席扣款；批次路徑（API endpoint）會在 _ctx 預載
            # `meeting_absent_count_map`（employee_id → count），取代逐員工查詢。
            absent_map = (_ctx or {}).get("meeting_absent_count_map") if _ctx else None
            if absent_map is not None:
                absent_count = int(absent_map.get(employee_id, 0))
            else:
                _, last_day = _cal.monthrange(year, month)
                start_date = _date(year, month, 1)
                end_date = _date(year, month, last_day)
                absent_count = (
                    session.query(MeetingRecord)
                    .filter(
                        MeetingRecord.employee_id == employee_id,
                        MeetingRecord.meeting_date >= start_date,
                        MeetingRecord.meeting_date <= end_date,
                        MeetingRecord.attended == False,  # noqa: E712
                    )
                    .count()
                )
            meeting_absence_deduction = int(
                absent_count * (self._meeting_absence_penalty or 0)
            )

            return {
                "festival_bonus": festival_bonus,
                "overtime_bonus": overtime_bonus,
                "meeting_absence_deduction": meeting_absence_deduction,
                "category": category,
            }
        finally:
            if _own_session:
                session.close()

    # ─── process_salary_calculation 私有輔助方法 ─────────────────────────────

    def _resolve_standard_base(self, emp) -> float:
        """依職位標準底薪決定員工底薪。
        有對應標準的職位直接回傳標準薪；無對應（園長、主任等特例）則回傳 emp.base_salary。
        時薪制（base_salary=0）永遠回傳 0。

        2026-05-07 議題 A 選項 3：員工檔可設 `bypass_standard_base=True`，
        強制使用個人 emp.base_salary（給有年資加給、合約底薪 > 職位標準的員工用），
        不走下方分流。
        """
        raw = float(emp.base_salary or 0)
        if raw == 0 or not self._position_salary_standards:
            return raw
        # bypass_standard_base 旗標短路：直接信任員工檔個人底薪
        if getattr(emp, "bypass_standard_base", False):
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
            # 投保薪資 raw 值（不在此處做 bracket 正規化，由 _calculate_deductions 統一處理）
            "insurance_salary": (
                emp.insurance_salary_level
                if emp.insurance_salary_level and emp.insurance_salary_level > 0
                else base_salary
            ),
            "dependents": emp.dependents,
            "pension_self_rate": emp.pension_self_rate or 0,
            # 階段 2-C 特殊狀況欄位（getattr 防舊 schema 還沒套 migration 時 KeyError）
            "no_employment_insurance": getattr(emp, "no_employment_insurance", False),
            "health_exempt": getattr(emp, "health_exempt", False),
            "skip_payroll_bonuses": getattr(emp, "skip_payroll_bonuses", False),
            "extra_dependents_quarterly": getattr(emp, "extra_dependents_quarterly", 0),
            # 議題 B 分項投保（NULL=沿用 insurance_salary_level）
            "labor_insured_salary": getattr(emp, "labor_insured_salary", None),
            "health_insured_salary": getattr(emp, "health_insured_salary", None),
            "pension_insured_salary": getattr(emp, "pension_insured_salary", None),
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

        # admin_waive 標記的考勤異常薪資端視為已豁免（不計入遲到/早退/缺打卡）
        from .utils import is_attendance_waived

        late_count = sum(
            1 for a in attendances if a.is_late and not is_attendance_waived(a)
        )
        early_count = sum(
            1 for a in attendances if a.is_early_leave and not is_attendance_waived(a)
        )
        missing_in = sum(
            1
            for a in attendances
            if a.is_missing_punch_in and not is_attendance_waived(a)
        )
        missing_out = sum(
            1
            for a in attendances
            if a.is_missing_punch_out and not is_attendance_waived(a)
        )
        total_late_minutes = sum(
            a.late_minutes or 0
            for a in attendances
            if a.is_late and not is_attendance_waived(a)
        )
        total_early_minutes = sum(
            a.early_leave_minutes or 0
            for a in attendances
            if a.is_early_leave and not is_attendance_waived(a)
        )

        late_details = [
            a.late_minutes or 0
            for a in attendances
            if a.is_late and not is_attendance_waived(a) and (a.late_minutes or 0) > 0
        ]
        emp_dict["_late_details"] = late_details

        if emp.employee_type == "hourly":
            _work_end_t = datetime.strptime(
                emp.work_end_time or "17:00", "%H:%M"
            ).time()
            total_hours = 0.0
            total_hourly_pay = 0.0
            monthly_ot_used = 0.0
            # 按 punch_in 時間排序，確保先發生的日期先消耗加班 quota
            sorted_attendances = sorted(
                attendances,
                key=lambda a: a.punch_in_time or datetime.max,
            )
            for a in sorted_attendances:
                if not a.punch_in_time:
                    continue
                day_hours = _compute_hourly_daily_hours(
                    a.punch_in_time, a.punch_out_time, _work_end_t
                )
                total_hours += day_hours
                remaining = max(0.0, MAX_MONTHLY_OVERTIME_HOURS - monthly_ot_used)
                day_pay, ot_used = _calc_daily_hourly_pay_with_cap(
                    day_hours, emp.hourly_rate or 0, remaining_ot_quota=remaining
                )
                monthly_ot_used += ot_used
                total_hourly_pay += day_pay
            if monthly_ot_used >= MAX_MONTHLY_OVERTIME_HOURS - 1e-9:
                logger.warning(
                    "時薪員工 emp_id=%d 當月加班時數觸及 %.0fh 上限，後續加班以 1.0 倍率計薪",
                    emp.id,
                    MAX_MONTHLY_OVERTIME_HOURS,
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

    def _build_contexts(self, session, emp, end_date: date) -> tuple:
        """建構 (classroom_context, office_staff_context)。

        以 end_date 對應的薪資月份反查當期班級（不再依賴 emp.classroom_id），
        避免班級頁面更新未同步至 Employee.classroom_id 時整段帶班獎金歸 0。
        """
        classroom_context = None
        classroom = self._resolve_classroom_for_employee_in_month(
            session, emp.id, end_date.year, end_date.month
        )
        if classroom:
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
        ytd_sick_before = _get_ytd_sick_hours_before(session, emp.id, year, month)
        leave_deduction_total = _sum_leave_deduction(
            approved_leaves, daily_salary, ytd_sick_hours_before_month=ytd_sick_before
        )
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
        meeting_overtime_pay_total = sum(
            m.overtime_pay or 0 for m in meeting_records if m.attended
        )

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
                "overtime_pay_total": meeting_overtime_pay_total,
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

        # 累計每日請假時數（支援同日多筆假單，例如上午 4h 事假 + 下午 4h 病假）。
        # Why: 舊版無條件把假單每一天加入 leave_covered，0.5h 事假 + 整日未打卡
        # 也會被判為「已請假 → 非曠職」，導致員工可用短工時假單規避整日曠職扣款。
        # 改為只有「當日累計請假時數 ≥ 整日工時」才視為整日覆蓋；半日假仍留在
        # expected_workdays 中，搭配原有 leave_deduction 扣該段請假工資，未打卡部分
        # 若實際未到班則由請假當日未覆蓋的期望工時保留可曠職判定（整日未打卡才觸發）。
        FULL_DAY_HOURS = 8.0
        per_day_leave_hours: dict[date, float] = {}
        for lv in approved_leaves:
            d = (
                lv.start_date.date()
                if isinstance(lv.start_date, datetime)
                else lv.start_date
            )
            lv_end = (
                lv.end_date.date() if isinstance(lv.end_date, datetime) else lv.end_date
            )
            span_days = (lv_end - d).days + 1
            if span_days <= 0:
                continue
            lv_hours = lv.leave_hours
            # leave_hours 未填或 0：視為整段皆為整日假（舊資料相容）
            if not lv_hours or lv_hours <= 0:
                lv_hours = span_days * FULL_DAY_HOURS
            per_day = lv_hours / span_days
            while d <= lv_end:
                if start_date <= d <= end_date:
                    per_day_leave_hours[d] = per_day_leave_hours.get(d, 0.0) + per_day
                d += timedelta(days=1)

        leave_covered: set = {
            d
            for d, hours in per_day_leave_hours.items()
            if hours >= FULL_DAY_HOURS - 0.01
        }

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

        from models.database import Holiday, DailyShift as _DailyShift, WorkdayOverride

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

        # 補班日（WorkdayOverride）：官方補班的週末須視為應上班
        makeup_in_month = (
            session.query(WorkdayOverride.date)
            .filter(
                WorkdayOverride.date >= start_date,
                WorkdayOverride.date <= end_date,
                WorkdayOverride.is_active.is_(True),
            )
            .all()
        )
        makeup_set = {m.date for m in makeup_in_month}

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
            makeup_set=makeup_set,
        )

        daily_salary_full = calc_daily_salary(self._resolve_standard_base(emp))
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

    def _compute_period_accrual_totals(
        self,
        session,
        emp,
        year: int,
        month: int,
        *,
        monthly_ctx_cache: dict | None = None,
    ) -> tuple[Optional[float], Optional[float]]:
        """發放月時累積期間每月節慶/超額獎金，回傳 (festival_total, overtime_total)。

        非發放月回 (None, None) — 呼叫端應傳給 calculate_salary 做為 override，
        None 代表「不覆蓋，照原本當月單月邏輯」（在非發放月該邏輯會把 festival 歸 0）。

        Args:
            monthly_ctx_cache: 批次計算時用的快取（key=(y,m)），減少重覆查詢；
                cache value 結構與 api/salary.py period-accrual 端點一致。
        """
        from .utils import get_distribution_period_months
        from utils.academic import resolve_current_academic_term

        period_months = get_distribution_period_months(year, month)
        if not period_months:
            return None, None

        # 預先把 period_months 收攏為唯一 term，避免單員工迴圈內每月各跑 1 次
        # _resolve_classroom_for_employee_in_term（同 term 內班級不會變）。
        # Audit A.P0.2：3 個 period 月通常 ≤ 2 個 term，從 6 query 砍到 1-2 query。
        term_classroom_cache: dict[tuple[int, int], object] = {}
        for y, m in period_months:
            term_key = resolve_current_academic_term(date(y, m, 1))
            if term_key not in term_classroom_cache:
                term_classroom_cache[term_key] = (
                    self._resolve_classroom_for_employee_in_term(
                        session, emp.id, term_key[0], term_key[1]
                    )
                )

        festival_total = 0
        overtime_total = 0
        for y, m in period_months:
            try:
                ctx: dict = {"session": session, "employee": emp}
                cache = monthly_ctx_cache.get((y, m)) if monthly_ctx_cache else None
                if cache:
                    # supervisor / office staff 路徑使用 school_active_students；
                    # classroom 路徑使用 classroom_count_map + classroom 物件
                    if "school_active" in cache:
                        ctx["school_active_students"] = cache["school_active"]
                    if "cls_count_map" in cache:
                        ctx["classroom_count_map"] = cache["cls_count_map"]
                    if "meeting_absent_count_map" in cache:
                        ctx["meeting_absent_count_map"] = cache[
                            "meeting_absent_count_map"
                        ]
                # 依「該月份所對應的學期」反查班級，避免拿目前 classroom 套用在跨學期的舊月份。
                # 若 cache 內預先放了該月解析好的 classroom，優先使用以節省查詢。
                cached_classroom = (
                    cache.get("classroom_for_emp", {}).get(emp.id) if cache else None
                )
                if cached_classroom is not None:
                    ctx["classroom"] = cached_classroom
                else:
                    # 用本函式預先建好的 term-keyed cache
                    ctx["classroom"] = term_classroom_cache[
                        resolve_current_academic_term(date(y, m, 1))
                    ]
                # 期間累積每月用該月份對應的 BonusConfig/AttendancePolicy/InsuranceRate,
                # 而非「目前最新」設定;與 _build_breakdown_for_month 同一語意,
                # 但這裡是 per-iteration swap(因為要為每個歷史月份各自挑版本)。
                with self.config_for_month(session, y, m):
                    row = self.calculate_period_accrual_row(emp.id, y, m, _ctx=ctx)
                festival_total += int(row.get("festival_bonus", 0) or 0)
                overtime_total += int(row.get("overtime_bonus", 0) or 0)
            except Exception:
                logger.exception(
                    "計算期間累積失敗 emp=%s year=%s month=%s", emp.id, y, m
                )
        return festival_total, overtime_total

    def _load_manual_salary_fields(
        self, session, employee_id: int, year: int, month: int
    ) -> dict:
        """讀取既有 SalaryRecord 上由 HR 手動調整的欄位,供重算保留。

        Why: 重算流程預設把 performance_bonus / special_bonus 由 employee dict 取出
            後 fill 回 record;若 HR 已在 record 上手動補加績效/特別獎金,沒先載入
            就會被歸 0(計算流程也會誤算 gross)。本 helper 在重算前先撈出舊值,
            塞回 employee dict,計算流程即可把它們算進 gross,_fill_salary_record
            也會原值寫回 record,達成「重算保留人工調整」。
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
        if rec is None:
            return {"performance_bonus": 0, "special_bonus": 0}
        return {
            "performance_bonus": rec.performance_bonus or 0,
            "special_bonus": rec.special_bonus or 0,
        }

    def _build_breakdown_for_month(self, session, emp, year: int, month: int):
        """純計算：依員工、年月產出 SalaryBreakdown，不寫入任何 DB 記錄。

        Why: preview 端點（GET）必須「只算不寫」，避免副作用；同時讓
        process_salary_calculation 與 preview_salary_calculation 共用計算流程。

        歷史月份重算：以 config_for_month 切換成 (year, month) 對應版本後計算,
        避免重算舊月份時套用「目前最新」設定。內層 _compute_period_accrual_totals
        進入時會再為每個被累積月份各自切換版本。
        """
        import calendar

        with self.config_for_month(session, year, month):
            emp_dict = self._load_emp_dict(emp)
            # 把既有 record 的手動調整欄位載入,讓重算路徑保留 HR 人工加的績效/特別獎金。
            emp_dict.update(
                self._load_manual_salary_fields(session, emp.id, year, month)
            )

            _, last_day = calendar.monthrange(year, month)
            start_date = date(year, month, 1)
            end_date = date(year, month, last_day)

            attendance_result, attendances = self._load_attendance_result(
                session, emp, start_date, end_date, emp_dict
            )

            classroom_context, office_staff_context = self._build_contexts(
                session, emp, end_date
            )

            daily_salary = calc_daily_salary(emp_dict["base_salary"])
            period_records = self._load_period_records(
                session, emp, start_date, end_date, year, month, daily_salary
            )

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

            # 發放月：累積期間每月節慶/超額獎金（內部會逐月再 swap 設定）
            period_festival_total, period_overtime_total = (
                self._compute_period_accrual_totals(session, emp, year, month)
            )

            breakdown = self.calculate_salary(
                employee=emp_dict,
                year=year,
                month=month,
                attendance=attendance_result,
                leave_deduction=period_records["leave_deduction"],
                classroom_context=classroom_context,
                office_staff_context=office_staff_context,
                meeting_context=period_records["meeting_context"],
                overtime_work_pay=period_records["overtime_work_pay"],
                personal_sick_leave_hours=period_records["personal_sick_leave_hours"],
                period_festival_override=period_festival_total,
                period_overtime_override=period_overtime_total,
            )

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

            return breakdown

    def preview_salary_calculation(self, employee_id: int, year: int, month: int):
        """只算不存：GET preview 專用，保證不留下任何 SalaryRecord 副作用。

        Why: final-salary-preview 是 GET 端點，過去直接呼叫會寫入 SalaryRecord 的
        process_salary_calculation，一旦後續流程拋例外（例如屬性存取錯誤），
        使用者看到 500 但 DB 已落地。改用此方法避免副作用。
        """
        session = _get_db_session()
        try:
            from models.database import Employee

            emp = session.query(Employee).get(employee_id)
            if not emp:
                raise ValueError(f"Employee {employee_id} not found")

            return self._build_breakdown_for_month(session, emp, year, month)
        finally:
            session.rollback()
            session.close()

    def process_salary_calculation(self, employee_id: int, year: int, month: int):
        """處理單一員工薪資計算並儲存結果"""
        session = _get_db_session()
        try:
            from models.database import Employee, SalaryRecord

            emp = session.query(Employee).get(employee_id)
            if not emp:
                raise ValueError(f"Employee {employee_id} not found")

            breakdown = self._build_breakdown_for_month(session, emp, year, month)

            # 儲存 SalaryRecord（advisory lock 保護：多 worker 不會同時寫同筆）
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
            art_to_classes: dict[int, list] = defaultdict(list)
            for _c in all_classrooms:
                if _c.assistant_teacher_id:
                    assistant_to_classes[_c.assistant_teacher_id].append(_c)
                if _c.art_teacher_id:
                    art_to_classes[_c.art_teacher_id].append(_c)

            # 員工 → 當期主要班級反查表（取代 emp.classroom_id 讀取）
            # 優先用本月份對應學期（school_year, semester）篩選；若該員在當期無班，
            # 個別 fallback 至跨學期任一 active（例如學校沿用同一個 Classroom 紀錄跨學期）。
            from utils.academic import resolve_current_academic_term as _resolve_term

            target_school_year, target_semester = _resolve_term(end_date)

            def _role_priority(c, employee_id: int) -> int:
                if c.head_teacher_id == employee_id:
                    return 1
                if c.assistant_teacher_id == employee_id:
                    return 2
                if c.art_teacher_id == employee_id:
                    return 3
                return 99

            def _accumulate(target: dict, classrooms):
                for _c in classrooms:
                    for tid in (
                        _c.head_teacher_id,
                        _c.assistant_teacher_id,
                        _c.art_teacher_id,
                    ):
                        if not tid:
                            continue
                        existing = target.get(tid)
                        if existing is None or _role_priority(_c, tid) < _role_priority(
                            existing, tid
                        ):
                            target[tid] = _c

            employee_to_classroom: dict[int, Classroom] = {}
            term_classrooms = [
                c
                for c in all_classrooms
                if c.school_year == target_school_year and c.semester == target_semester
            ]
            _accumulate(employee_to_classroom, term_classrooms)
            # 對未在當期班級表中出現的教師，再從跨學期 active 補上 fallback
            missing_classrooms = [c for c in all_classrooms if c not in term_classrooms]
            fallback_target: dict[int, Classroom] = {}
            _accumulate(fallback_target, missing_classrooms)
            for tid, _c in fallback_target.items():
                if tid not in employee_to_classroom:
                    employee_to_classroom[tid] = _c

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

            # 6b. 年度累計病假時數（用於 30 日半薪上限判斷）
            ytd_sick_by_emp = _get_ytd_sick_hours_bulk(
                session, employee_ids, year, month
            )

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

            # 10.5 補班日（WorkdayOverride，全月共用）
            from models.database import WorkdayOverride as _WorkdayOverride

            makeup_raw = (
                session.query(_WorkdayOverride.date)
                .filter(
                    _WorkdayOverride.date >= start_date,
                    _WorkdayOverride.date <= end_date,
                    _WorkdayOverride.is_active.is_(True),
                )
                .all()
            )
            makeup_set = {m.date for m in makeup_raw}

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

            # ── 批次預載結束，開始計算 ──────────────────────────────────────────

            # 發放月：預載期間每月的班級/學生快照，供 _compute_period_accrual_totals
            # 使用，避免 N×期間月 重覆查詢
            monthly_ctx_cache: dict[tuple[int, int], dict] = {}
            from .utils import get_distribution_period_months as _gdpm
            from services.student_enrollment import (
                count_students_active_on as _csa,
                classroom_student_count_map as _csm,
            )

            for _y, _m in _gdpm(year, month):
                _, _last = calendar.monthrange(_y, _m)
                _ref = date(_y, _m, _last)
                monthly_ctx_cache[(_y, _m)] = {
                    "month_end": _ref,
                    "school_active": _csa(session, _ref),
                    "cls_count_map": _csm(session, _ref),
                    "classroom_map": classroom_map,
                }

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

            # 12. 現有薪資記錄（upsert check）
            #
            # 重要：此查詢必須在 advisory lock 全部取得之後才進行，否則會有 TOCTOU：
            # 若在取鎖前載入 salary_record_by_emp，另一個 worker 可能在取鎖前
            # finalize 某筆記錄；待本 worker 取得鎖後還使用陳舊快取，
            # 就會以 is_finalized=False 覆蓋實際已封存的記錄。
            session.expire_all()
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
            # 取鎖後再做一次整月封存檢查：若有人在 API 前置檢查後搶先封存單筆，
            # 這裡能在開始寫入前攔下來，避免覆蓋封存資料。
            finalized_after_lock = [
                r for r in existing_records if getattr(r, "is_finalized", False)
            ]
            if finalized_after_lock:
                from fastapi import HTTPException as _HTTPException

                finalized_names = [
                    emp_map[r.employee_id].name
                    for r in finalized_after_lock
                    if r.employee_id in emp_map
                ]
                # 使用 HTTPException(409) 讓 API 層回傳 conflict，而非被
                # raise_safe_500 吞為 500。前置 API 檢查 + 此二次檢查共同
                # 覆蓋 TOCTOU：介於兩次檢查的封存會在此攔下。
                raise _HTTPException(
                    status_code=409,
                    detail=(
                        f"{year}/{month} 有 {len(finalized_after_lock)} 筆薪資在取得鎖後被封存"
                        f"（{'、'.join(finalized_names)}），無法繼續批次計算；"
                        "請重新整理後再試。"
                    ),
                )

            # 整批共用 config_for_month:本批次都針對同一個 (year, month),
            # 因此進入 loop 前 swap 一次即可;內層 _compute_period_accrual_totals
            # 對每個被累積月份還會各自再 swap(RLock 重入安全)。
            with self.config_for_month(session, year, month):
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
                                logger.debug(
                                    "progress_callback 失敗，忽略", exc_info=True
                                )
                        continue

                    # SAVEPOINT 包覆每位員工:失敗時 sp.rollback() 同時撤回 in-memory
                    # 與 SQL-level(autoflush 觸發的)修改,確保失敗員工保留舊資料,僅
                    # 在外層補上 needs_recalc=True;成功員工繼續同一 transaction,最後
                    # 統一 commit(維持原 batch atomicity)。
                    sp = session.begin_nested()
                    try:
                        emp_dict = self._load_emp_dict(emp)
                        # 載入既有 record 的手動調整欄位(performance/special bonus),
                        # 避免 bulk 重算把 HR 人工加的獎金歸 0。已在 salary_record_by_emp
                        # 預載過 record,直接讀記憶體值省一次 query。
                        _existing_rec = salary_record_by_emp.get(emp.id)
                        emp_dict["performance_bonus"] = (
                            (_existing_rec.performance_bonus or 0)
                            if _existing_rec
                            else 0
                        )
                        emp_dict["special_bonus"] = (
                            (_existing_rec.special_bonus or 0) if _existing_rec else 0
                        )

                        # ── 考勤統計（使用預載）
                        # admin_waive 標記的考勤異常薪資端視為已豁免（不計入遲到/早退/缺打卡）
                        from .utils import is_attendance_waived

                        attendances = att_by_emp[emp.id]
                        late_count = sum(
                            1
                            for a in attendances
                            if a.is_late and not is_attendance_waived(a)
                        )
                        early_count = sum(
                            1
                            for a in attendances
                            if a.is_early_leave and not is_attendance_waived(a)
                        )
                        missing_in = sum(
                            1
                            for a in attendances
                            if a.is_missing_punch_in and not is_attendance_waived(a)
                        )
                        missing_out = sum(
                            1
                            for a in attendances
                            if a.is_missing_punch_out and not is_attendance_waived(a)
                        )
                        total_late_minutes = sum(
                            a.late_minutes or 0
                            for a in attendances
                            if a.is_late and not is_attendance_waived(a)
                        )
                        total_early_minutes = sum(
                            a.early_leave_minutes or 0
                            for a in attendances
                            if a.is_early_leave and not is_attendance_waived(a)
                        )
                        emp_dict["_late_details"] = [
                            a.late_minutes or 0
                            for a in attendances
                            if a.is_late
                            and not is_attendance_waived(a)
                            and (a.late_minutes or 0) > 0
                        ]

                        if emp.employee_type == "hourly":
                            _work_end_t = datetime.strptime(
                                emp.work_end_time or "17:00", "%H:%M"
                            ).time()
                            total_hours = 0.0
                            total_hourly_pay = 0.0
                            monthly_ot_used = 0.0
                            sorted_attendances = sorted(
                                attendances,
                                key=lambda a: a.punch_in_time or datetime.max,
                            )
                            for a in sorted_attendances:
                                if not a.punch_in_time:
                                    continue
                                day_hours = _compute_hourly_daily_hours(
                                    a.punch_in_time, a.punch_out_time, _work_end_t
                                )
                                total_hours += day_hours
                                remaining = max(
                                    0.0, MAX_MONTHLY_OVERTIME_HOURS - monthly_ot_used
                                )
                                day_pay, ot_used = _calc_daily_hourly_pay_with_cap(
                                    day_hours,
                                    emp.hourly_rate or 0,
                                    remaining_ot_quota=remaining,
                                )
                                monthly_ot_used += ot_used
                                total_hourly_pay += day_pay
                            if monthly_ot_used >= MAX_MONTHLY_OVERTIME_HOURS - 1e-9:
                                logger.warning(
                                    "時薪員工 emp_id=%d 當月加班時數觸及 %.0fh 上限，後續加班以 1.0 倍率計薪",
                                    emp.id,
                                    MAX_MONTHLY_OVERTIME_HOURS,
                                )
                            emp_dict["work_hours"] = round(total_hours, 2)
                            emp_dict["hourly_calculated_pay"] = round(
                                total_hourly_pay, 2
                            )

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

                        # ── classroom_context / office_staff_context（使用預載 + 共用方法）
                        # 改用反查 map（不再讀 emp.classroom_id），可同時修：
                        #   - 班級頁面指派老師時未同步 Employee.classroom_id 的 silent zero bug
                        #   - 跨學期老師被誤套用其他學期班級的問題（term filter）
                        classroom_context = None
                        primary_classroom = employee_to_classroom.get(emp.id)
                        if primary_classroom is not None:
                            classroom_context = (
                                self._build_classroom_context_from_batch(
                                    emp,
                                    primary_classroom,
                                    db_count_map,
                                    assistant_to_classes,
                                    art_to_classes,
                                )
                            )
                        office_staff_context = self._build_office_staff_context(
                            emp, total_students, classroom_context
                        )

                        # ── 請假、加班、會議（使用預載）
                        approved_leaves = leaves_by_emp[emp.id]
                        daily_salary = calc_daily_salary(emp_dict["base_salary"])
                        leave_deduction_total = _sum_leave_deduction(
                            approved_leaves,
                            daily_salary,
                            ytd_sick_hours_before_month=ytd_sick_by_emp.get(
                                emp.id, 0.0
                            ),
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
                        meeting_overtime_pay_total = sum(
                            m.overtime_pay or 0 for m in meeting_records if m.attended
                        )
                        absent_period = (
                            meeting_absent_current + prior_absent_by_emp[emp.id]
                        )
                        meeting_context = None
                        if meeting_records or absent_period > 0:
                            meeting_context = {
                                "attended": meeting_attended,
                                "absent": meeting_absent_current,
                                "absent_period": absent_period,
                                "overtime_pay_total": meeting_overtime_pay_total,
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
                                makeup_set=makeup_set,
                            )
                            absent_count, absence_deduction_amount = (
                                self._compute_absence(
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
                            )

                        # 發放月：累積期間每月節慶/超額（共用 monthly_ctx_cache）
                        period_festival_total, period_overtime_total = (
                            self._compute_period_accrual_totals(
                                session,
                                emp,
                                year,
                                month,
                                monthly_ctx_cache=monthly_ctx_cache,
                            )
                        )

                        # ── 計算薪資
                        breakdown = self.calculate_salary(
                            employee=emp_dict,
                            year=year,
                            month=month,
                            attendance=attendance_result,
                            leave_deduction=leave_deduction_total,
                            classroom_context=classroom_context,
                            office_staff_context=office_staff_context,
                            meeting_context=meeting_context,
                            overtime_work_pay=overtime_work_pay_total,
                            personal_sick_leave_hours=personal_sick_leave_hours,
                            period_festival_override=period_festival_total,
                            period_overtime_override=period_overtime_total,
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
                        sp.commit()  # RELEASE SAVEPOINT
                        results.append((emp, breakdown))

                    except Exception as e:
                        sp.rollback()  # ROLLBACK TO SAVEPOINT — 撤回此員工 in-memory + SQL-level 修改
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
                        # 失敗員工:重新 query 該月舊 record,標 needs_recalc=True 讓
                        # 後續 finalize 完整性檢查擋下。已封存的 record 不動(避免與
                        # 「封存即不可變」的不變式衝突;此情況代表 _check_not_finalized
                        # 攔下了重算企圖,本來就不該寫入)。
                        stale = (
                            session.query(SalaryRecord)
                            .filter(
                                SalaryRecord.employee_id == emp.id,
                                SalaryRecord.salary_year == year,
                                SalaryRecord.salary_month == month,
                            )
                            .first()
                        )
                        if stale is not None and not stale.is_finalized:
                            stale.needs_recalc = True
                    finally:
                        done += 1
                        if progress_callback:
                            try:
                                progress_callback(
                                    done, total, emp.name if emp else "(未知)"
                                )
                            except Exception:
                                logger.debug(
                                    "progress_callback 失敗，忽略", exc_info=True
                                )

            # 單次 commit（相較 N 次 commit 大幅減少 I/O 往返）
            session.commit()
            return results, errors

        except Exception as e:
            session.rollback()
            logger.exception("批次薪資計算失敗 year=%d month=%d", year, month)
            raise
        finally:
            session.close()
