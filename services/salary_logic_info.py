"""薪資邏輯顯示資料組裝 service

把「薪資邏輯」分頁要顯示的所有靜態 / DB / Engine runtime 資料集中一處組裝，供
api/salary.py 的正式端點 `/api/salaries/logic` 與 api/dev.py 的 `/api/dev/salary-logic`
共用，避免兩邊各自維護同一份 helper。

Why: 過去這段邏輯只放在 api/dev.py，導致前端 SalaryLogicPanel.vue 必須打 dev
router；當 ENV 不在白名單時 (staging / production / 未設) /api/dev/* 不掛載，
正式頁面就 404。抽 service + 在 salary 模組重新提供端點是治本作法。
"""

from __future__ import annotations

from typing import Any

from models.database import (
    AttendancePolicy,
    BonusConfig,
    GradeTarget,
    InsuranceRate,
    ShiftType,
)
from services.insurance_service import (
    AVERAGE_DEPENDENTS,
    HEALTH_EMPLOYEE_RATIO,
    HEALTH_EMPLOYER_RATIO,
    HEALTH_INSURANCE_RATE,
    INSURANCE_TABLE_2026,
    LABOR_EMPLOYEE_RATIO,
    LABOR_EMPLOYER_RATIO,
    LABOR_GOVERNMENT_RATIO,
    LABOR_INSURANCE_RATE,
    PENSION_EMPLOYER_RATE,
)
from services.salary_engine import MONTHLY_BASE_DAYS


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _build_attendance_formulas() -> list[dict]:
    return [
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
    ]


def _build_insurance_formulas() -> list[dict]:
    return [
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
    ]


def _build_official_checks(db_checks: list[dict]) -> list[dict]:
    return [
        {
            "item": "Runtime 勞保+就保合計費率",
            "system_value": _pct(LABOR_INSURANCE_RATE),
            "official_value": "12.50%",
            "match": True,
        },
        {
            "item": "Runtime 勞保負擔比例",
            "system_value": (
                f"員工 {_pct(LABOR_EMPLOYEE_RATIO)} / 雇主 {_pct(LABOR_EMPLOYER_RATIO)} "
                f"/ 政府 {_pct(LABOR_GOVERNMENT_RATIO)}"
            ),
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
    ] + db_checks


def _build_sample_bracket_checks() -> list[dict]:
    runtime_samples = {entry["amount"]: entry for entry in INSURANCE_TABLE_2026}
    sample_29500 = runtime_samples[29500]
    sample_30300 = runtime_samples[30300]
    return [
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
    ]


def _build_official_sources() -> list[dict]:
    return [
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
    ]


def _build_db_insurance_checks(insurance_rate_db: dict | None) -> list[dict]:
    if not insurance_rate_db:
        return []
    return [
        {
            "item": "DB 勞保+就保合計費率",
            "system_value": _pct(insurance_rate_db["labor_rate"]),
            "official_value": "12.50%",
            "match": abs((insurance_rate_db["labor_rate"] or 0) - LABOR_INSURANCE_RATE)
            < 1e-9,
        },
        {
            "item": "DB 平均眷口數",
            "system_value": f'{insurance_rate_db["average_dependents"]:.2f}',
            "official_value": f"{AVERAGE_DEPENDENTS:.2f}",
            "match": abs(
                (insurance_rate_db["average_dependents"] or 0) - AVERAGE_DEPENDENTS
            )
            < 1e-9,
        },
    ]


def _build_formula_verification(insurance_rate_db: dict | None) -> dict:
    db_checks = _build_db_insurance_checks(insurance_rate_db)
    return {
        "attendance_formulas": _build_attendance_formulas(),
        "insurance_formulas": _build_insurance_formulas(),
        "official_checks": _build_official_checks(db_checks),
        "sample_bracket_checks": _build_sample_bracket_checks(),
        "official_sources": _build_official_sources(),
        "runtime_note": (
            "薪資實算使用 InsuranceService 常數與 INSURANCE_TABLE_2026；"
            "DB 的 insurance_rates 目前只作顯示/版本紀錄。"
        ),
    }


def _query_attendance_policy(session) -> dict | None:
    policy = (
        session.query(AttendancePolicy)
        .filter(AttendancePolicy.is_active == True)  # noqa: E712
        .order_by(AttendancePolicy.id.desc())
        .first()
    )
    if not policy:
        return None
    return {
        "default_work_start": policy.default_work_start,
        "default_work_end": policy.default_work_end,
        "late_deduction": policy.late_deduction,
        "early_leave_deduction": policy.early_leave_deduction,
        "missing_punch_deduction": policy.missing_punch_deduction,
        "festival_bonus_months": policy.festival_bonus_months,
    }


def _query_bonus_config(session) -> dict | None:
    bonus = (
        session.query(BonusConfig)
        .filter(BonusConfig.is_active == True)  # noqa: E712
        .order_by(BonusConfig.id.desc())
        .first()
    )
    if not bonus:
        return None
    return {
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


def _query_grade_targets(session) -> list[dict]:
    targets = session.query(GradeTarget).order_by(GradeTarget.grade_name).all()
    return [
        {
            "grade_name": t.grade_name,
            "festival_two_teachers": t.festival_two_teachers,
            "festival_one_teacher": t.festival_one_teacher,
            "festival_shared": t.festival_shared,
            "overtime_two_teachers": t.overtime_two_teachers,
            "overtime_one_teacher": t.overtime_one_teacher,
            "overtime_shared": t.overtime_shared,
        }
        for t in targets
    ]


def _query_insurance_rate(session) -> dict | None:
    rate = (
        session.query(InsuranceRate)
        .filter(InsuranceRate.is_active == True)  # noqa: E712
        .order_by(InsuranceRate.id.desc())
        .first()
    )
    if not rate:
        return None
    return {
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


def _build_insurance_runtime() -> dict:
    return {
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


def _build_engine_config(engine: Any) -> dict:
    if not engine:
        return {}
    return {
        "deduction_rules": engine.deduction_rules,
        "attendance_policy": engine._attendance_policy,
        "school_wide_target": engine._school_wide_target,
        "meeting_hours": engine._meeting_hours,
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


def _query_shift_types(session) -> list[dict]:
    shift_types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "work_start": s.work_start,
            "work_end": s.work_end,
            "is_active": s.is_active,
        }
        for s in shift_types
    ]


def _build_leave_deduction_rules() -> dict:
    return {
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
        "paternity_new": {
            "label": "陪產檢及陪產假",
            "ratio": 0.0,
            "note": "不扣薪，共7日",
        },
        "miscarriage": {
            "label": "流產假",
            "ratio": 0.0,
            "note": "不扣薪，依週數5日/1週/4週",
        },
        "family_care": {
            "label": "家庭照顧假",
            "ratio": 1.0,
            "note": "不給薪，併入事假計算，年7日",
        },
        "parental_unpaid": {
            "label": "育嬰留職停薪",
            "ratio": 0.0,
            "note": "留停無薪，最長2年",
        },
        "compensatory": {"label": "補休", "ratio": 0.0, "note": "加班換休，不扣薪"},
    }


def _build_salary_formula() -> dict:
    return {
        "應發總額": (
            "底薪 + 津貼(主管/導師/伙食/交通/其他) + 績效獎金 + 特別獎金 + 主管紅利 + "
            "加班費(核准加班記錄) + 園務會議加班費 + 生日禮金(當月壽星 $500)"
        ),
        "應發總額備註": "節慶獎金 / 超額獎金 獨立轉帳，不計入應發總額",
        "節慶獎金": "節慶獎金（2/6/9/12月發放）— 獨立匯款，不進應發總額",
        "超額獎金": "超額獎金 — 與節慶獎金同月獨立匯款，不進應發總額",
        "獎金另行匯款": "節慶獎金 + 超額獎金 + 主管紅利 > 0 時為「是」，表示當月有另行匯款",
        "扣款總額": "勞保(員工) + 健保(員工) + 勞退自提 + 遲到扣款 + 早退扣款 + 請假扣款 + 曠職扣款 + 其他扣款（不含園務會議缺席扣款）",
        "實領薪資": "應發總額 − 扣款總額",
        "遲到扣款公式": "遲到分鐘 × (月薪 ÷ 30 ÷ 8 ÷ 60)  ← 依勞基法固定30天",
        "早退扣款公式": "早退分鐘 × (月薪 ÷ 30 ÷ 8 ÷ 60)  ← 依勞基法固定30天",
        "遲到扣款規則": "遲到一律按實際分鐘數比例扣款（月薪 ÷ 30 ÷ 8 ÷ 60），依勞基法第26條工資核實發給原則",
        "日薪": "月薪 ÷ 30  ← 依勞基法固定30天（遲到轉事假、請假扣款均適用）",
        "每分鐘費率": "月薪 ÷ 30 ÷ 8 ÷ 60  ← 依勞基法固定30天",
        "請假扣款": "請假天數 × 日薪 × 扣薪比例 (事假1.0 / 病假0.5 / 特休0.0)",
        "未打卡": "不扣款，僅記錄次數",
        "節慶獎金（導師／教師）": "獎金基數 × (班級在籍人數 ÷ 目標人數)，入職滿3個月才計算",
        "節慶獎金（主管）": "主管基數 × (全校在籍 ÷ 全校目標)，入職滿3個月才計算",
        "節慶獎金（辦公室）": "辦公室基數 × (全校在籍 ÷ 全校目標)，入職滿3個月才計算",
        "節慶獎金資格": "入職滿 3 個月",
        "節慶獎金發放月份": "發放月份：2、6、9、12 月（依考勤政策的節慶獎金發放月份設定）",
        "超額獎金公式": "(在籍人數 − 超額目標) × 每人金額，超額才有；與節慶獎金同月發放",
        "園務會議加班費": "出席次數 × 每次金額（下班時間 17:00 → $200；18:00 → $100）",
        "園務會議缺席扣款": "缺席次數 × $100，從節慶獎金直接扣減，不進入扣款總額；僅在節慶獎金發放月才計算",
        "勞健保查表": "依投保薪資級距表查表，非按比例計算",
        "健保眷屬計算": "健保員工自付 × (1 + min(眷屬人數, 3))",
    }


def build_salary_logic_info(session, salary_engine) -> dict:
    """組裝完整薪資邏輯資訊（給「薪資邏輯」分頁顯示）。

    Why: 同一份資料原本只有 dev router 在組，正式環境的 SalaryLogicPanel
    被迫打 /api/dev/* 而 404。抽到 service 後，正式 router 與 dev router 都用
    同一條 path：DB 查詢 + runtime 常數 + engine 設定 + 公式顯示文字。

    How to apply: 端點處保持薄殼（只負責權限與 session 生命週期）；任何顯示文字、
    新比對欄位、新查詢 (e.g. 新 DB 設定) 都加在這裡，前端不必區分入口。
    """
    attendance_policy = _query_attendance_policy(session)
    bonus_config = _query_bonus_config(session)
    grade_targets = _query_grade_targets(session)
    insurance_rate = _query_insurance_rate(session)
    insurance_runtime = _build_insurance_runtime()
    engine_config = _build_engine_config(salary_engine)
    shifts = _query_shift_types(session)
    leave_deduction_rules = _build_leave_deduction_rules()
    salary_formula = _build_salary_formula()
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
