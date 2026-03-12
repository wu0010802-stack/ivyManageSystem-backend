"""
開發用 API - 檢視薪資計算邏輯、出缺勤規則、系統設定
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from utils.auth import require_permission
from utils.permissions import Permission
from services.insurance_service import (
    LABOR_INSURANCE_RATE, LABOR_EMPLOYEE_RATIO, LABOR_EMPLOYER_RATIO,
    LABOR_GOVERNMENT_RATIO, HEALTH_INSURANCE_RATE, HEALTH_EMPLOYEE_RATIO,
    HEALTH_EMPLOYER_RATIO, PENSION_EMPLOYER_RATE, AVERAGE_DEPENDENTS,
    INSURANCE_TABLE_2026,
)
from services.salary_engine import MONTHLY_BASE_DAYS

from models.database import (
    get_session, Employee, Attendance, LeaveRecord, OvertimeRecord,
    Classroom, ClassGrade, Student, ShiftType, ShiftAssignment, DailyShift,
    AttendancePolicy, BonusConfig, GradeTarget, InsuranceRate,
    MeetingRecord, EmployeeAllowance, AllowanceType, JobTitle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dev", tags=["dev"])

_salary_engine = None


def _pct(value):
    return f"{value * 100:.2f}%"


def _build_formula_verification(insurance_rate_db: dict | None):
    runtime_samples = {entry["amount"]: entry for entry in INSURANCE_TABLE_2026}
    sample_29500 = runtime_samples[29500]
    sample_30300 = runtime_samples[30300]

    db_checks = []
    if insurance_rate_db:
        db_checks = [
            {
                "item": "DB 勞保+就保合計費率",
                "system_value": _pct(insurance_rate_db["labor_rate"]),
                "official_value": "12.50%",
                "match": abs((insurance_rate_db["labor_rate"] or 0) - LABOR_INSURANCE_RATE) < 1e-9,
            },
            {
                "item": "DB 平均眷口數",
                "system_value": f'{insurance_rate_db["average_dependents"]:.2f}',
                "official_value": f"{AVERAGE_DEPENDENTS:.2f}",
                "match": abs((insurance_rate_db["average_dependents"] or 0) - AVERAGE_DEPENDENTS) < 1e-9,
            },
        ]

    return {
        "attendance_formulas": [
            {
                "item": "日薪",
                "formula": f"月薪 ÷ {MONTHLY_BASE_DAYS}",
                "note": "系統以固定 30 天換算日薪，供請假扣薪與曠職扣薪使用。",
            },
            {
                "item": "每分鐘費率",
                "formula": f"月薪 ÷ {MONTHLY_BASE_DAYS} ÷ 8 ÷ 60",
                "note": "遲到 / 早退按分鐘比例扣款。",
            },
            {
                "item": "請假扣薪",
                "formula": "請假時數 ÷ 8 × 日薪 × 扣薪比例",
                "note": "事假 1.0、病假 0.5，其餘依假別規則。",
            },
        ],
        "insurance_formulas": [
            {
                "item": "勞保+就保合計費率",
                "formula": "投保薪資 × 12.5%",
                "note": "普通事故保險 11.5% + 就業保險 1%。",
            },
            {
                "item": "勞保負擔比例",
                "formula": "員工 20% / 雇主 70% / 政府 10%",
                "note": "公民營受僱勞工適用。",
            },
            {
                "item": "健保費率",
                "formula": "投保金額 × 5.17%",
                "note": "第 1 類被保險人由員工 30%、雇主 60%、政府 10% 分擔。",
            },
            {
                "item": "健保眷屬計算",
                "formula": "員工自付額 × (1 + min(眷屬人數, 3))",
                "note": "最多計入 3 名眷屬；平均眷口數 0.56 僅供雇主估算參考。",
            },
            {
                "item": "勞退雇主提繳",
                "formula": "月提繳工資 × 6%",
                "note": "雇主不得低於 6%。",
            },
        ],
        "official_checks": [
            {
                "item": "Runtime 勞保+就保合計費率",
                "system_value": _pct(LABOR_INSURANCE_RATE),
                "official_value": "12.50%",
                "match": True,
            },
            {
                "item": "Runtime 勞保負擔比例",
                "system_value": f"員工 {_pct(LABOR_EMPLOYEE_RATIO)} / 雇主 {_pct(LABOR_EMPLOYER_RATIO)} / 政府 {_pct(LABOR_GOVERNMENT_RATIO)}",
                "official_value": "員工 20.00% / 雇主 70.00% / 政府 10.00%",
                "match": True,
            },
            {
                "item": "Runtime 健保費率",
                "system_value": _pct(HEALTH_INSURANCE_RATE),
                "official_value": "5.17%",
                "match": True,
            },
            {
                "item": "Runtime 健保負擔比例",
                "system_value": f"員工 {_pct(HEALTH_EMPLOYEE_RATIO)} / 雇主 {_pct(HEALTH_EMPLOYER_RATIO)}",
                "official_value": "員工 30.00% / 雇主 60.00%",
                "match": True,
            },
            {
                "item": "Runtime 平均眷口數",
                "system_value": f"{AVERAGE_DEPENDENTS:.2f}",
                "official_value": "0.56",
                "match": True,
            },
            {
                "item": "Runtime 勞退雇主提繳率",
                "system_value": _pct(PENSION_EMPLOYER_RATE),
                "official_value": "6.00%",
                "match": True,
            },
        ] + db_checks,
        "sample_bracket_checks": [
            {
                "insured_amount": 29500,
                "labor_employee_system": sample_29500["labor_employee"],
                "labor_employee_official": 738,
                "health_employee_system": sample_29500["health_employee"],
                "health_employee_official": 458,
                "health_employer_system": sample_29500["health_employer"],
                "health_employer_official": 1428,
                "match": (
                    sample_29500["labor_employee"] == 738
                    and sample_29500["health_employee"] == 458
                    and sample_29500["health_employer"] == 1428
                ),
            },
            {
                "insured_amount": 30300,
                "labor_employee_system": sample_30300["labor_employee"],
                "labor_employee_official": 758,
                "labor_employer_system": sample_30300["labor_employer"],
                "labor_employer_official": 2651,
                "health_employee_system": sample_30300["health_employee"],
                "health_employee_official": 470,
                "health_employer_system": sample_30300["health_employer"],
                "health_employer_official": 1466,
                "match": (
                    sample_30300["labor_employee"] == 758
                    and sample_30300["labor_employer"] == 2651
                    and sample_30300["health_employee"] == 470
                    and sample_30300["health_employer"] == 1466
                ),
            },
        ],
        "official_sources": [
            {
                "label": "勞保局 115 年勞保 / 就保費率與負擔比例",
                "url": "https://www.bli.gov.tw/0014161.html",
            },
            {
                "label": "健保署 115 年健保費率與平均眷口數",
                "url": "https://www.nhi.gov.tw/ch/cp-3273-d65d7-2582-1.html",
            },
            {
                "label": "健保署 115 年保險費負擔金額表",
                "url": "https://www.nhi.gov.tw/ch/cp-3273-d65d7-2582-1.html",
            },
            {
                "label": "勞動部 / 法務部 勞工退休金條例第 14 條",
                "url": "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=N0030020&flno=14",
            },
        ],
        "runtime_note": "薪資實算使用 InsuranceService 常數與 INSURANCE_TABLE_2026；DB 的 insurance_rates 目前只作顯示/版本紀錄。",
    }


def init_dev_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


@router.get("/salary-logic")
def get_salary_logic(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    """傾印目前的薪資計算邏輯與所有參數設定"""
    session = get_session()
    try:
        engine = _salary_engine

        # 1. 考勤政策
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        attendance_policy = None
        if policy:
            attendance_policy = {
                "default_work_start": policy.default_work_start,
                "default_work_end": policy.default_work_end,
                "late_deduction": policy.late_deduction,
                "early_leave_deduction": policy.early_leave_deduction,
                "missing_punch_deduction": policy.missing_punch_deduction,
                "festival_bonus_months": policy.festival_bonus_months,
            }

        # 2. 獎金設定
        bonus = session.query(BonusConfig).filter(BonusConfig.is_active == True).first()
        bonus_config = None
        if bonus:
            bonus_config = {
                "config_year": bonus.config_year,
                "head_teacher_ab": bonus.head_teacher_ab,
                "head_teacher_c": bonus.head_teacher_c,
                "assistant_teacher_ab": bonus.assistant_teacher_ab,
                "assistant_teacher_c": bonus.assistant_teacher_c,
                "principal_festival": bonus.principal_festival,
                "director_festival": bonus.director_festival,
                "leader_festival": bonus.leader_festival,
                "driver_festival": bonus.driver_festival,
                "designer_festival": bonus.designer_festival,
                "admin_festival": bonus.admin_festival,
                "principal_dividend": bonus.principal_dividend,
                "director_dividend": bonus.director_dividend,
                "leader_dividend": bonus.leader_dividend,
                "vice_leader_dividend": bonus.vice_leader_dividend,
                "overtime_head_normal": bonus.overtime_head_normal,
                "overtime_head_baby": bonus.overtime_head_baby,
                "overtime_assistant_normal": bonus.overtime_assistant_normal,
                "overtime_assistant_baby": bonus.overtime_assistant_baby,
                "school_wide_target": bonus.school_wide_target,
            }

        # 3. 年級目標
        targets = session.query(GradeTarget).order_by(GradeTarget.grade_name).all()
        grade_targets = [{
            "grade_name": t.grade_name,
            "festival_two_teachers": t.festival_two_teachers,
            "festival_one_teacher": t.festival_one_teacher,
            "festival_shared": t.festival_shared,
            "overtime_two_teachers": t.overtime_two_teachers,
            "overtime_one_teacher": t.overtime_one_teacher,
            "overtime_shared": t.overtime_shared,
        } for t in targets]

        # 4. 勞健保費率
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).first()
        insurance_rate = None
        if rate:
            insurance_rate = {
                "rate_year": rate.rate_year,
                "labor_rate": rate.labor_rate,
                "labor_employee_ratio": rate.labor_employee_ratio,
                "labor_employer_ratio": rate.labor_employer_ratio,
                "labor_government_ratio": rate.labor_government_ratio,
                "health_rate": rate.health_rate,
                "health_employee_ratio": rate.health_employee_ratio,
                "health_employer_ratio": rate.health_employer_ratio,
                "pension_employer_rate": rate.pension_employer_rate,
                "average_dependents": rate.average_dependents,
            }

        insurance_runtime = {
            "labor_rate": LABOR_INSURANCE_RATE,
            "labor_employee_ratio": LABOR_EMPLOYEE_RATIO,
            "labor_employer_ratio": LABOR_EMPLOYER_RATIO,
            "labor_government_ratio": LABOR_GOVERNMENT_RATIO,
            "health_rate": HEALTH_INSURANCE_RATE,
            "health_employee_ratio": HEALTH_EMPLOYEE_RATIO,
            "health_employer_ratio": HEALTH_EMPLOYER_RATIO,
            "pension_employer_rate": PENSION_EMPLOYER_RATE,
            "average_dependents": AVERAGE_DEPENDENTS,
        }

        # 5. Engine 內部運算參數
        engine_config = {}
        if engine:
            engine_config = {
                "deduction_rules": engine.deduction_rules,
                "attendance_policy": engine._attendance_policy,
                "school_wide_target": engine._school_wide_target,
                "meeting_pay": engine._meeting_pay,
                "meeting_pay_6pm": engine._meeting_pay_6pm,
                "meeting_absence_penalty": engine._meeting_absence_penalty,
                "bonus_base": engine._bonus_base,
                "target_enrollment": engine._target_enrollment,
                "overtime_target": engine._overtime_target,
                "overtime_per_person": engine._overtime_per_person,
                "supervisor_dividend": engine._supervisor_dividend,
                "supervisor_festival_bonus": engine._supervisor_festival_bonus,
                "office_festival_bonus_base": engine._office_festival_bonus_base,
                "position_grade_map": engine.POSITION_GRADE_MAP,
            }

        # 6. 班別設定
        shift_types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
        shifts = [{
            "id": s.id,
            "name": s.name,
            "work_start": s.work_start,
            "work_end": s.work_end,
            "is_active": s.is_active,
        } for s in shift_types]

        # 7. 請假扣薪規則（硬編碼在 process_salary_calculation 中）
        leave_deduction_rules = {
            "personal": {"label": "事假", "ratio": 1.0, "note": "全額扣薪"},
            "sick": {"label": "病假", "ratio": 0.5, "note": "扣半薪"},
            "menstrual": {"label": "生理假", "ratio": 0.5, "note": "扣半薪"},
            "annual": {"label": "特休", "ratio": 0.0, "note": "不扣薪"},
            "maternity": {"label": "產假", "ratio": 0.0, "note": "不扣薪"},
            "paternity": {"label": "陪產假", "ratio": 0.0, "note": "不扣薪"},
            "official": {"label": "公假", "ratio": 0.0, "note": "不扣薪（教召、研習等）"},
            "marriage": {"label": "婚假", "ratio": 0.0, "note": "不扣薪，共8日"},
            "bereavement": {"label": "喪假", "ratio": 0.0, "note": "不扣薪，依親疏3/6/8日"},
            "prenatal": {"label": "產檢假", "ratio": 0.0, "note": "不扣薪，共7日"},
            "paternity_new": {"label": "陪產檢及陪產假", "ratio": 0.0, "note": "不扣薪，共7日"},
            "miscarriage": {"label": "流產假", "ratio": 0.0, "note": "不扣薪，依週數5日/1週/4週"},
            "family_care": {"label": "家庭照顧假", "ratio": 1.0, "note": "不給薪，併入事假計算，年7日"},
            "parental_unpaid": {"label": "育嬰留職停薪", "ratio": 0.0, "note": "留停無薪，最長2年"},
            "compensatory": {"label": "補休", "ratio": 0.0, "note": "加班換休，不扣薪"},
        }

        # 8. 薪資公式說明
        salary_formula = {
            "gross_salary": "底薪 + 津貼(主管/導師/伙食/交通/其他) + 績效獎金 + 特別獎金 + 主管紅利 + 加班費(核准加班記錄) + 園務會議加班費 + 生日禮金(當月壽星 $500)",
            "gross_salary_note": "節慶獎金 / 超額獎金 獨立轉帳，不計入 gross_salary",
            "festival_bonus": "節慶獎金（2/6/9/12月發放）— 獨立匯款，不進 gross_salary",
            "overtime_bonus": "超額獎金 — 與節慶獎金同月獨立匯款，不進 gross_salary",
            "bonus_separate": "festival_bonus + overtime_bonus + supervisor_dividend > 0 時為 True，表示當月有另行匯款",
            "total_deduction": "勞保(員工) + 健保(員工) + 勞退自提 + 遲到扣款 + 早退扣款 + 請假扣款 + 曠職扣款 + 其他扣款（不含 meeting_absence_deduction）",
            "net_salary": "gross_salary - total_deduction",
            "late_deduction_formula": "遲到分鐘 × (月薪 ÷ 30 ÷ 8 ÷ 60)  ← 依勞基法固定30天",
            "early_leave_deduction_formula": "早退分鐘 × (月薪 ÷ 30 ÷ 8 ÷ 60)  ← 依勞基法固定30天",
            "late_deduction_rule": "遲到一律按實際分鐘數比例扣款（月薪 ÷ 30 ÷ 8 ÷ 60），依勞基法第26條工資核實發給原則",
            "daily_salary": "月薪 ÷ 30  ← 依勞基法固定30天（遲到轉事假、請假扣款均適用）",
            "per_minute_rate": "月薪 ÷ 30 ÷ 8 ÷ 60  ← 依勞基法固定30天",
            "leave_deduction": "請假天數 × 日薪 × 扣薪比例 (事假1.0 / 病假0.5 / 特休0.0)",
            "missing_punch": "不扣款，僅記錄次數",
            "festival_bonus_teacher": "獎金基數 × (班級在籍人數 ÷ 目標人數)，入職滿3個月才計算",
            "festival_bonus_supervisor": "主管基數 × (全校在籍 ÷ 全校目標)，入職滿3個月才計算",
            "festival_bonus_office": "辦公室基數 × (全校在籍 ÷ 全校目標)，入職滿3個月才計算",
            "festival_bonus_eligibility": "入職滿 3 個月",
            "festival_bonus_months": "發放月份：2、6、9、12 月（依 AttendancePolicy.festival_bonus_months 設定）",
            "overtime_bonus_formula": "(在籍人數 - 超額目標) × 每人金額，超額才有；與節慶獎金同月發放",
            "meeting_overtime_pay": "出席次數 × 每次金額（下班時間 17:00 → $200；18:00 → $100）",
            "meeting_absence_deduction": "缺席次數 × $100，從節慶獎金直接扣減，不進入 total_deduction；僅在節慶獎金發放月才計算",
            "insurance_lookup": "依投保薪資級距表查表，非按比例計算",
            "health_insurance_dependents": "健保員工自付 × (1 + min(眷屬人數, 3))",
        }

        formula_verification = _build_formula_verification(insurance_rate)

        return {
            "attendance_policy_db": attendance_policy,
            "bonus_config_db": bonus_config,
            "grade_targets_db": grade_targets,
            "insurance_rate_db": insurance_rate,
            "insurance_runtime_config": insurance_runtime,
            "engine_runtime_config": engine_config,
            "shift_types": shifts,
            "leave_deduction_rules": leave_deduction_rules,
            "salary_formula": salary_formula,
            "formula_verification": formula_verification,
        }
    finally:
        session.close()


@router.get("/employee-salary-debug")
def debug_employee_salary(
    current_user: dict = Depends(require_permission(Permission.SETTINGS_READ)),
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(...),
):
    """模擬計算單一員工薪資並回傳完整明細（不存檔）"""
    import calendar as cal_module
    from services.salary_engine import MONTHLY_BASE_DAYS
    from services.salary.constants import LEAVE_DEDUCTION_RULES
    from services.salary.utils import get_bonus_distribution_month, get_meeting_deduction_period_start
    from services.salary.proration import _prorate_for_period

    session = get_session()
    try:
        engine = _salary_engine
        if not engine:
            return {"error": "SalaryEngine not initialized"}

        emp = session.query(Employee).get(employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found"}

        if emp.employee_type == 'hourly':
            return {"error": "時薪制員工請使用正式薪資計算流程，debug 端點僅支援月薪正職員工"}

        title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')
        # 計算節慶獎金用的有效職稱（bonus_grade 可覆蓋職稱等級）
        _GRADE_TO_TITLE = {'A': '幼兒園教師', 'B': '教保員', 'C': '助理教保員'}
        bonus_grade_override = getattr(emp, 'bonus_grade', None)
        _effective_title = (
            _GRADE_TO_TITLE.get(bonus_grade_override, title_name)
            if bonus_grade_override else title_name
        )
        _, last_day = cal_module.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)
        is_bonus_month = get_bonus_distribution_month(month)

        # Attendance
        attendances = session.query(Attendance).filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date,
        ).all()

        late_count = sum(1 for a in attendances if a.is_late)
        early_count = sum(1 for a in attendances if a.is_early_leave)
        missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
        missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
        total_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
        total_early_min = sum(a.early_leave_minutes or 0 for a in attendances if a.is_early_leave)
        late_details = [a.late_minutes or 0 for a in attendances if a.is_late and (a.late_minutes or 0) > 0]

        # Leaves（daily_salary 使用全月底薪，與引擎 line 1037 一致）
        approved_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.is_approved == True,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
        ).all()
        daily_salary = (emp.base_salary or 0) / MONTHLY_BASE_DAYS if emp.base_salary else 0
        leave_deduction_total = 0
        leave_breakdown = []
        for lv in approved_leaves:
            # 與引擎一致：優先使用 LeaveRecord.deduction_ratio，None 時才 fallback 至預設規則
            ratio = lv.deduction_ratio if lv.deduction_ratio is not None \
                else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
            deduction = round((lv.leave_hours / 8) * daily_salary * ratio)
            leave_deduction_total += deduction
            leave_breakdown.append({
                "type": lv.leave_type,
                "start": lv.start_date.isoformat(),
                "end": lv.end_date.isoformat(),
                "hours": lv.leave_hours,
                "ratio": ratio,
                "deduction": deduction,
            })

        personal_sick_leave_hours = sum(
            lv.leave_hours or 0
            for lv in approved_leaves
            if lv.leave_type in ('personal', 'sick')
        )
        bonus_forfeited_by_leave = personal_sick_leave_hours > 40

        # Overtime
        approved_ot = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.is_approved == True,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
        ).all()
        ot_pay = sum(o.overtime_pay or 0 for o in approved_ot)

        # Meeting（與引擎一致：發放月累積跨期缺席，計算 meeting_absence_deduction）
        meetings = session.query(MeetingRecord).filter(
            MeetingRecord.employee_id == emp.id,
            MeetingRecord.meeting_date >= start_date,
            MeetingRecord.meeting_date <= end_date,
        ).all()
        meeting_attended = sum(1 for m in meetings if m.attended)
        meeting_absent_current = sum(1 for m in meetings if not m.attended)
        absent_period = meeting_absent_current
        if is_bonus_month:
            period_start = get_meeting_deduction_period_start(year, month)
            if period_start is not None and period_start < start_date:
                prior_records = session.query(MeetingRecord).filter(
                    MeetingRecord.employee_id == emp.id,
                    MeetingRecord.meeting_date >= period_start,
                    MeetingRecord.meeting_date < start_date,
                ).all()
                absent_period += sum(1 for m in prior_records if not m.attended)
        meeting_absence_deduction = (absent_period * engine._meeting_absence_penalty) if is_bonus_month else 0

        # Allowances
        emp_allowances = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.employee_id == emp.id,
            EmployeeAllowance.is_active == True,
        ).all()
        allowances = []
        for ea in emp_allowances:
            a_type = session.query(AllowanceType).get(ea.allowance_type_id)
            if a_type:
                allowances.append({"name": a_type.name, "amount": ea.amount})

        # Classroom context
        classroom_context = None
        classroom = None
        if emp.classroom_id:
            classroom = session.query(Classroom).get(emp.classroom_id)
        if classroom:
            role = 'assistant_teacher'
            if classroom.head_teacher_id == emp.id:
                role = 'head_teacher'
            elif classroom.art_teacher_id == emp.id:
                role = 'art_teacher'
            has_assistant = classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0
            student_count = session.query(Student).filter(
                Student.classroom_id == classroom.id, Student.is_active == True
            ).count()
            grade_name = classroom.grade.name if classroom.grade else ''
            classroom_context = {
                "role": role,
                "grade_name": grade_name,
                "current_enrollment": student_count,
                "has_assistant": has_assistant,
                "is_shared_assistant": False,
            }

            # 第九條：偵測共用副班導
            if role == 'assistant_teacher':
                shared_classes = session.query(Classroom).filter(
                    Classroom.assistant_teacher_id == emp.id
                ).all()
                if len(shared_classes) >= 2:
                    classroom_context['is_shared_assistant'] = True
                    second_class = next(
                        (c for c in shared_classes if c.id != classroom.id), None
                    )
                    if second_class:
                        second_count = session.query(Student).filter(
                            Student.classroom_id == second_class.id, Student.is_active == True
                        ).count()
                        classroom_context['shared_second_class'] = {
                            'grade_name': second_class.grade.name if second_class.grade else '',
                            'current_enrollment': second_count,
                        }

        # Office / Supervisor context
        office_staff_context = None
        is_supervisor = engine.get_supervisor_festival_bonus(title_name, emp.position) is not None
        if is_supervisor or (emp.is_office_staff and not classroom_context):
            total_students = session.query(Student).filter(Student.is_active == True).count()
            office_staff_context = {"school_enrollment": total_students}

        # 底薪相關（與引擎一致：per_minute_rate 用全月底薪；gross_salary 用折算後底薪）
        base_sal = emp.base_salary or 0
        resign_date = getattr(emp, 'resign_date', None)
        prorated_base = _prorate_for_period(base_sal, emp.hire_date, resign_date, year, month)
        birthday_bonus = 500 if (emp.birthday and emp.birthday.month == month) else 0
        per_minute_rate = base_sal / (MONTHLY_BASE_DAYS * 8 * 60) if base_sal > 0 else 0

        # Attendance deduction detail（全程按實際分鐘比例，依勞基法第26條）
        att_deduction_detail = []
        normal_late_deduction = 0
        for minutes in late_details:
            d = round(minutes * per_minute_rate)
            att_deduction_detail.append({"minutes": minutes, "type": "per_minute", "deduction": d})
            normal_late_deduction += d

        early_deduction = round(total_early_min * per_minute_rate)

        # Festival bonus detail（與引擎一致：非發放月清零；發放月扣除會議缺席罰款）
        festival_detail = {}
        supervisor_festival_base = engine.get_supervisor_festival_bonus(title_name, emp.position or '')
        if supervisor_festival_base is not None:
            school_enrollment = office_staff_context['school_enrollment'] if office_staff_context else 0
            school_target = engine._school_wide_target or 160
            ratio = school_enrollment / school_target if school_target > 0 else 0
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            raw_result = round(supervisor_festival_base * ratio) if is_eligible else 0
            effective_result = max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0
            festival_detail = {
                "category": "主管",
                "base": supervisor_festival_base,
                "enrollment": school_enrollment,
                "target": school_target,
                "ratio": round(ratio, 4),
                "eligible": is_eligible,
                "is_bonus_month": is_bonus_month,
                "result": raw_result,
                "result_after_penalty": effective_result,
            }
        elif office_staff_context and emp.position:
            office_base = engine.get_office_festival_bonus_base(emp.position or '', title_name)
            if office_base:
                school_enrollment = office_staff_context['school_enrollment']
                school_target = engine._school_wide_target or 160
                ratio = school_enrollment / school_target if school_target > 0 else 0
                is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
                raw_result = round(office_base * ratio) if is_eligible else 0
                effective_result = max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0
                festival_detail = {
                    "category": "辦公室",
                    "base": office_base,
                    "enrollment": school_enrollment,
                    "target": school_target,
                    "ratio": round(ratio, 4),
                    "eligible": is_eligible,
                    "is_bonus_month": is_bonus_month,
                    "result": raw_result,
                    "result_after_penalty": effective_result,
                }
        elif classroom_context:
            cc = classroom_context
            base_amount = engine.get_festival_bonus_base(_effective_title, cc['role'])
            target = engine.get_target_enrollment(cc['grade_name'], cc['has_assistant'], cc['is_shared_assistant'])
            ratio = cc['current_enrollment'] / target if target > 0 else 0
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            ot_target = engine.get_overtime_target(cc['grade_name'], cc['has_assistant'], cc['is_shared_assistant'])
            ot_count = max(0, cc['current_enrollment'] - ot_target)
            ot_per = engine.get_overtime_per_person(cc['role'] if cc['role'] != 'art_teacher' else 'assistant_teacher', cc['grade_name'])
            raw_festival = round(base_amount * ratio) if is_eligible else 0
            raw_overtime = round(ot_count * ot_per) if is_eligible else 0

            # 第九條：兩班共用副班導，取兩班分數平均
            shared_second = cc.get('shared_second_class')
            if shared_second and is_eligible:
                target2 = engine.get_target_enrollment(shared_second['grade_name'], True, True)
                ratio2 = shared_second['current_enrollment'] / target2 if target2 > 0 else 0
                raw_festival2 = round(base_amount * ratio2)
                ot_target2 = engine.get_overtime_target(shared_second['grade_name'], True, True)
                ot_count2 = max(0, shared_second['current_enrollment'] - ot_target2)
                raw_overtime2 = round(ot_count2 * ot_per)
                raw_festival = round((raw_festival + raw_festival2) / 2)
                raw_overtime = round((raw_overtime + raw_overtime2) / 2)

            effective_festival = max(0, raw_festival - meeting_absence_deduction) if is_bonus_month else 0
            effective_overtime = raw_overtime if is_bonus_month else 0
            festival_detail = {
                "category": "帶班老師",
                "role": cc['role'],
                "grade": cc['grade_name'],
                "base": base_amount,
                "enrollment": cc['current_enrollment'],
                "target": target,
                "ratio": round(ratio, 4),
                "eligible": is_eligible,
                "is_bonus_month": is_bonus_month,
                "festival_result": raw_festival,
                "festival_result_after_penalty": effective_festival,
                "overtime_target": ot_target,
                "overtime_count": ot_count,
                "overtime_per_person": ot_per,
                "overtime_result": effective_overtime,
            }
            if shared_second:
                festival_detail["shared_second_class"] = {
                    "grade": shared_second['grade_name'],
                    "enrollment": shared_second['current_enrollment'],
                    "target": target2,
                    "ratio": round(ratio2, 4),
                }

        # 事假+病假累計超過40小時：節慶獎金及紅利全數取消
        if bonus_forfeited_by_leave and festival_detail:
            festival_detail['forfeited_by_leave'] = True
            for key in ('result_after_penalty', 'festival_result_after_penalty', 'overtime_result'):
                if key in festival_detail:
                    festival_detail[key] = 0

        # Supervisor dividend
        supervisor_dividend = engine.get_supervisor_dividend(title_name, emp.position or '') if emp.position else 0
        if bonus_forfeited_by_leave:
            supervisor_dividend = 0

        # 曠職偵測（與引擎 engine.py line 1079–1131 一致）
        from models.database import Holiday as _Holiday
        from services.salary.proration import _build_expected_workdays
        from datetime import timedelta as _td
        absent_count = 0
        absence_deduction_amount = 0
        holidays_in_month = session.query(_Holiday.date).filter(
            _Holiday.date >= start_date,
            _Holiday.date <= end_date,
            _Holiday.is_active == True,
        ).all()
        holiday_set = {h.date for h in holidays_in_month}
        daily_shifts_in_month = session.query(DailyShift).filter(
            DailyShift.employee_id == emp.id,
            DailyShift.date >= start_date,
            DailyShift.date <= end_date,
        ).all()
        daily_shift_map = {ds.date: ds.shift_type_id for ds in daily_shifts_in_month}
        expected_workdays = _build_expected_workdays(
            year=year, month=month,
            holiday_set=holiday_set, daily_shift_map=daily_shift_map,
            hire_date_raw=emp.hire_date, resign_date_raw=resign_date,
        )
        attendance_dates = {
            (a.attendance_date.date() if hasattr(a.attendance_date, 'date') else a.attendance_date)
            for a in attendances
        }
        leave_covered: set = set()
        for lv in approved_leaves:
            d = lv.start_date.date() if hasattr(lv.start_date, 'date') else lv.start_date
            lv_end = lv.end_date.date() if hasattr(lv.end_date, 'date') else lv.end_date
            while d <= lv_end:
                if start_date <= d <= end_date:
                    leave_covered.add(d)
                d += _td(days=1)
        absent_days = expected_workdays - attendance_dates - leave_covered
        absent_count = len(absent_days)
        absence_deduction_amount = round(absent_count * daily_salary)

        # Insurance（與引擎一致：先經 get_bracket() 正規化至級距金額，再帶入計算）
        ins_service = engine.insurance_service
        ins_salary_raw = (
            emp.insurance_salary_level
            if emp.insurance_salary_level and emp.insurance_salary_level > 0
            else emp.base_salary
        ) or 0
        ins_salary = ins_service.get_bracket(ins_salary_raw)["amount"] if ins_salary_raw else 0
        ins = ins_service.calculate(ins_salary, emp.dependents or 0, pension_self_rate=emp.pension_self_rate or 0)

        # 計算薪資匯總（與引擎 calculate_salary 邏輯對齊）
        per_meeting_pay = engine._meeting_pay_6pm if (emp.work_end_time or '17:00') == '18:00' else engine._meeting_pay
        meeting_overtime_pay = meeting_attended * per_meeting_pay
        fixed_allowances_total = sum([
            emp.supervisor_allowance or 0,
            emp.teacher_allowance or 0,
            emp.meal_allowance or 0,
            emp.transportation_allowance or 0,
            emp.other_allowance or 0,
        ])
        extra_allowances_total = sum(a['amount'] for a in allowances)
        gross_salary = round(
            prorated_base
            + fixed_allowances_total
            + extra_allowances_total
            + supervisor_dividend
            + birthday_bonus
            + meeting_overtime_pay
            + ot_pay
        )
        total_deduction = round(
            ins.labor_employee
            + ins.health_employee
            + ins.pension_employee
            + normal_late_deduction
            + early_deduction
            + leave_deduction_total
            + absence_deduction_amount
        )
        net_salary = gross_salary - total_deduction

        return {
            "employee": {
                "id": emp.id,
                "employee_id": emp.employee_id,
                "name": emp.name,
                "title": title_name,
                "position": emp.position,
                "employee_type": emp.employee_type,
                "base_salary": base_sal,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "is_office_staff": emp.is_office_staff,
                "classroom_id": emp.classroom_id,
                "insurance_salary_level": emp.insurance_salary_level,
                "dependents": emp.dependents,
                "work_start_time": emp.work_start_time,
                "work_end_time": emp.work_end_time,
            },
            "period": {"year": year, "month": month, "is_bonus_month": is_bonus_month},
            "attendance_summary": {
                "total_records": len(attendances),
                "late_count": late_count,
                "early_leave_count": early_count,
                "missing_punch_in": missing_in,
                "missing_punch_out": missing_out,
                "total_late_minutes": total_late_min,
                "total_early_minutes": total_early_min,
                "late_details": late_details,
            },
            "deduction_calc": {
                "daily_salary": round(daily_salary),
                "per_minute_rate": round(per_minute_rate, 4),
                "late_deduction_detail": att_deduction_detail,
                "late_deduction": normal_late_deduction,
                "early_leave_deduction": early_deduction,
                "missing_punch_deduction": 0,
            },
            "leave_breakdown": leave_breakdown,
            "leave_deduction_total": leave_deduction_total,
            "overtime_pay": ot_pay,
            "meeting": {
                "attended": meeting_attended,
                "absent_this_month": meeting_absent_current,
                "absent_period": absent_period,
                "meeting_absence_deduction": meeting_absence_deduction,
                "overtime_pay_per_session": per_meeting_pay,
                "absence_penalty_per_session": engine._meeting_absence_penalty,
            },
            "allowances": allowances,
            "classroom_context": classroom_context,
            "festival_bonus_detail": festival_detail,
            "supervisor_dividend": supervisor_dividend,
            "insurance": {
                "insured_amount_raw": ins_salary_raw,
                "insured_amount": ins.insured_amount,
                "labor_employee": ins.labor_employee,
                "labor_employer": ins.labor_employer,
                "health_employee": ins.health_employee,
                "health_employer": ins.health_employer,
                "pension_employee": ins.pension_employee,
                "pension_employer": ins.pension_employer,
                "total_employee_deduction": ins.total_employee,
            },
            "salary_summary": {
                "prorated_base_salary": round(prorated_base),
                "proration_applied": round(prorated_base) != base_sal,
                "birthday_bonus": birthday_bonus,
                "meeting_overtime_pay": meeting_overtime_pay,
                "fixed_allowances": round(fixed_allowances_total),
                "extra_allowances": round(extra_allowances_total),
                "absent_count": absent_count,
                "absence_deduction": absence_deduction_amount,
                "personal_sick_leave_hours": personal_sick_leave_hours,
                "bonus_forfeited_by_leave": bonus_forfeited_by_leave,
                "gross_salary": gross_salary,
                "total_deduction": total_deduction,
                "net_salary": net_salary,
            },
        }
    finally:
        session.close()
