"""m00_config:設定型資料(年級/職稱/保險級距費率/底薪/獎金/扣款/假別額度/

費目模板/班別/才藝課程/國定假日/政策版本/簽核政策等),含 2025+2026 兩組
config_year。所有 domain 模組依賴本模組先行落庫。

執行序最前(orchestrator m00)。本模組只「建立」設定型 + 法定參考表,
不依賴任何前序模組;產出寫入 ctx registry:
    ctx.class_grades -> list[ClassGrade]
    ctx.job_titles   -> dict[role_key, JobTitle]

值域對齊(已逐欄 introspect models/ 確認):
- InsuranceBracket / InsuranceRate / PositionSalaryConfig 走 reference_data
  的 canonical 常數,每年度(2025/2026)各一套(period-aware resolver 需跨年度)。
- AttendancePolicy / BonusConfig / GradeTarget 亦每 config_year 各一套。
- FeeTemplate 依 class_grades × 學期(1/2)× 費目產生(fee_type 在 CHECK 白名單內)。
- 考核計分目錄 AppraisalScoreItemCatalog 走 reference_data.appraisal_catalog()。
"""

from __future__ import annotations

from datetime import date, time
from typing import TYPE_CHECKING

from .. import reference_data as ref
from ..context import SeedContext

if TYPE_CHECKING:  # pragma: no cover - 僅型別檢查
    pass


# ---------------------------------------------------------------------------
# 年級定義:依規模建幼幼/小班/中班/大班(name 唯一)。
# tuple:(name, age_range, sort_order, is_graduation_grade)
# ---------------------------------------------------------------------------
_GRADES: list[tuple[str, str, int, bool]] = [
    ("幼幼班", "2-3歲", 1, False),
    ("小班", "3-4歲", 2, False),
    ("中班", "4-5歲", 3, False),
    ("大班", "5-6歲", 4, True),  # 畢業班年級
]

# ---------------------------------------------------------------------------
# 7 職稱(role key → (顯示名, bonus_grade, sort_order))。
# role key 對齊「共享契約」employees_by_role:
#   supervisor/admin/accountant/homeroom/assistant/art/support
# bonus_grade:帶班職稱對應節慶獎金等級(A/B/C);非帶班 NULL。
# ---------------------------------------------------------------------------
_JOB_TITLES: list[tuple[str, str, str | None, int]] = [
    ("supervisor", "主任", None, 1),
    ("admin", "行政", None, 2),
    ("accountant", "會計", None, 3),
    ("homeroom", "班導師", "B", 4),
    ("assistant", "助教", "B", 5),
    ("art", "才藝老師", None, 6),
    ("support", "支援人員", None, 7),
]


def _academic_year_to_roc(ctx: SeedContext) -> int:
    """民國學年(如 114)。"""
    return ctx.config.academic_year


def _config_years() -> tuple[int, ...]:
    """跨 config_year 兩套(西元 2025 / 2026),供 period-aware resolver。"""
    return ref.INSURANCE_EFFECTIVE_YEARS


# ---------------------------------------------------------------------------
# 各子建置函式;皆回傳「建了幾筆」,由 seed() 統一 ctx.log。
# ---------------------------------------------------------------------------
def _seed_class_grades(ctx: SeedContext) -> int:
    from models.classroom import ClassGrade

    grades: list = []
    for name, age_range, sort_order, is_grad in _GRADES:
        g = ClassGrade(
            name=name,
            age_range=age_range,
            sort_order=sort_order,
            is_active=True,
            is_graduation_grade=is_grad,
        )
        ctx.session.add(g)
        grades.append(g)
    ctx.session.flush()  # 取 id,後續 fee_templates 需 grade_id
    ctx.class_grades = grades
    return len(grades)


def _seed_job_titles(ctx: SeedContext) -> int:
    from models.employee import JobTitle

    titles: dict = {}
    for role_key, display_name, bonus_grade, sort_order in _JOB_TITLES:
        jt = JobTitle(
            name=display_name,
            is_active=True,
            sort_order=sort_order,
            bonus_grade=bonus_grade,
        )
        ctx.session.add(jt)
        titles[role_key] = jt
    ctx.session.flush()  # 取 id,m01 需 job_title_id
    ctx.job_titles = titles
    return len(titles)


def _seed_insurance(ctx: SeedContext) -> tuple[int, int]:
    """保險級距 + 費率(2025/2026 各一套)。回傳 (bracket 筆數, rate 筆數)。"""
    from models.config import InsuranceBracket, InsuranceRate

    bracket_rows = ref.insurance_brackets()
    for row in bracket_rows:
        ctx.session.add(InsuranceBracket(**row))

    rate_rows = ref.insurance_rates()
    # uq_insurance_rates_active:只能一筆 is_active=True。最新年度為 active,
    # 歷史年度 is_active=False(period-aware resolver 靠 rate_year 查,不靠 active)。
    latest_year = max(_config_years())
    for row in rate_rows:
        row = {**row, "is_active": row.get("rate_year") == latest_year}
        ctx.session.add(InsuranceRate(**row))

    return len(bracket_rows), len(rate_rows)


def _seed_position_salary_configs(ctx: SeedContext) -> int:
    """職位標準底薪(每 config_year 各一套,version=1)。"""
    from models.config import PositionSalaryConfig

    standards = ref.position_salary_standards()
    n = 0
    for year in _config_years():
        cfg = PositionSalaryConfig(
            config_year=year,
            version=1,
            changed_by="seedgen",
            **{k: v for k, v in standards.items() if v is not None},
        )
        # None 值欄位(director/principal)留 nullable 預設;Money 預設亦有值,
        # 此處只覆寫有值欄位以對齊 reference_data。
        ctx.session.add(cfg)
        n += 1
    return n


def _seed_attendance_policies(ctx: SeedContext) -> int:
    """考勤政策(每 config_year 各一套)。"""
    from models.config import AttendancePolicy

    n = 0
    # uq_attendance_policies_active:只能一筆 is_active=True;僅最新年度為 active。
    latest_year = max(_config_years())
    for year in _config_years():
        ctx.session.add(
            AttendancePolicy(
                config_year=year,
                version=1,
                changed_by="seedgen",
                default_work_start="08:00",
                default_work_end="17:00",
                festival_bonus_months=3,
                is_active=(year == latest_year),
                effective_date=date(year, 1, 1),
            )
        )
        n += 1
    return n


def _seed_bonus_configs_and_targets(ctx: SeedContext) -> tuple[int, int]:
    """獎金設定 + 年級目標人數(每 config_year 各一套)。

    回傳 (bonus_config 筆數, grade_target 筆數)。
    school_wide_target 取規模 students(全校在籍目標),供超額獎金引擎使用。
    """
    from models.config import BonusConfig, GradeTarget

    school_target = ctx.config.scale_profile["students"]
    # uq_bonus_configs_active:只能一筆 is_active=True;僅最新年度為 active。
    latest_year = max(_config_years())
    bonus_n = 0
    target_n = 0
    for year in _config_years():
        bc = BonusConfig(
            config_year=year,
            version=1,
            changed_by="seedgen",
            school_wide_target=school_target,
            enrollment_count_mode="month_end",
            is_active=(year == latest_year),
        )
        ctx.session.add(bc)
        ctx.session.flush()  # 取 bc.id 供 grade_target FK
        bonus_n += 1

        # 每年級一筆 grade_target(編制人數門檻;節慶/超額獎金人數級距)。
        for name, _age, _sort, _is_grad in _GRADES:
            ctx.session.add(
                GradeTarget(
                    config_year=year,
                    grade_name=name,
                    bonus_config_id=bc.id,
                    festival_two_teachers=30,
                    festival_one_teacher=20,
                    festival_shared=15,
                    overtime_two_teachers=30,
                    overtime_one_teacher=20,
                    overtime_shared=15,
                )
            )
            target_n += 1
    return bonus_n, target_n


def _seed_leave_quota_templates(ctx: SeedContext) -> int:
    """通用假別額度模板(以 employee_id=NULL 不可——需 FK)。

    LeaveQuota.employee_id NOT NULL 且 FK employees,故此處「模板」改以
    SystemConfig 形式無法表達配額表列;真正每員工配額由 m01/m04 綁定。
    本函式僅落 SystemConfig 記錄「預設年度配額時數」,供 m01 參考,不建
    leave_quotas 列(避免無主 FK)。回傳 0(leave_quotas 不在此建)。
    """
    return 0


def _seed_fee_templates(ctx: SeedContext) -> int:
    """費目模板:年級 × 學期(1/2)× 費目。

    fee_type 取 CHECK 白名單內值(registration/miscellaneous/monthly/
    material/insurance)。school_year 用民國學年。amount 為固定 canonical。
    唯一鍵 (grade_id, school_year, semester, fee_type)。
    """
    from models.fees import FeeTemplate

    roc_year = _academic_year_to_roc(ctx)
    # (fee_type, name, amount, due_offset)
    fee_items: list[tuple[str, str, int, int]] = [
        ("registration", "註冊費", 12000, 14),
        ("monthly", "月費", 11000, 7),
        ("material", "材料費", 2500, 14),
        ("insurance", "保險費", 600, 14),
        ("miscellaneous", "雜費", 3000, 14),
    ]
    n = 0
    for g in ctx.class_grades:
        for semester in (1, 2):
            for fee_type, name, amount, due_offset in fee_items:
                ctx.session.add(
                    FeeTemplate(
                        grade_id=g.id,
                        school_year=roc_year,
                        semester=semester,
                        fee_type=fee_type,
                        name=name,
                        amount=amount,
                        due_date_offset_days=due_offset,
                        is_active=True,
                        created_by="seedgen",
                    )
                )
                n += 1
    return n


def _seed_shift_types(ctx: SeedContext) -> int:
    """班別模板(name 唯一)。"""
    from models.shift import ShiftType

    shifts: list[tuple[str, str, str, int]] = [
        ("早班", "07:30", "16:30", 1),
        ("正常班", "08:00", "17:00", 2),
        ("晚班", "09:30", "18:30", 3),
        ("行政班", "08:30", "17:30", 4),
    ]
    for name, ws, we, order in shifts:
        ctx.session.add(
            ShiftType(
                name=name,
                work_start=ws,
                work_end=we,
                sort_order=order,
                is_active=True,
            )
        )
    return len(shifts)


def _seed_activity_config(ctx: SeedContext) -> tuple[int, int, int]:
    """才藝課程 + 用品 + 報名設定(單筆)。

    回傳 (course 筆數, supply 筆數, settings 筆數)。
    school_year 用民國學年,semester=1(上學期)。
    """
    from models.activity import (
        ActivityCourse,
        ActivityRegistrationSettings,
        ActivitySupply,
    )

    # 才藝課程/用品以「當前學期」tag(對齊 app current-term 過濾,否則公開報名/列表空)。
    roc_year, term_sem = ctx.current_term()
    # (name, price, sessions, capacity, weekday, start, end, min_age_m, max_age_m)
    courses: list[tuple[str, int, int, int, int, time, time, int, int]] = [
        ("美術創作", 2400, 12, 20, 0, time(16, 0), time(17, 0), 36, 72),
        ("體能律動", 2000, 12, 24, 1, time(16, 0), time(17, 0), 36, 72),
        ("音樂啟蒙", 2200, 12, 20, 2, time(16, 0), time(17, 0), 36, 72),
        ("陶土手作", 2600, 12, 16, 3, time(16, 0), time(17, 0), 48, 72),
        ("珠心算", 2000, 12, 24, 4, time(16, 0), time(17, 0), 48, 72),
    ]
    for name, price, sessions, cap, wd, st, et, min_m, max_m in courses:
        ctx.session.add(
            ActivityCourse(
                name=name,
                price=price,
                sessions=sessions,
                capacity=cap,
                allow_waitlist=True,
                is_active=True,
                school_year=roc_year,
                semester=term_sem,
                min_age_months=min_m,
                max_age_months=max_m,
                meeting_weekday=wd,
                meeting_start_time=st,
                meeting_end_time=et,
            )
        )

    supplies: list[tuple[str, int]] = [
        ("才藝服", 450),
        ("畫具組", 380),
        ("樂器租用", 200),
    ]
    for name, price in supplies:
        ctx.session.add(
            ActivitySupply(
                name=name,
                price=price,
                is_active=True,
                school_year=roc_year,
                semester=term_sem,
            )
        )

    ctx.session.add(
        ActivityRegistrationSettings(
            is_open=True,
            open_at=None,
            close_at=None,
            page_title=f"{roc_year} 學年度上學期課後才藝報名",
            term_label=f"{roc_year}-1",
            event_date_label="每週一至週五 16:00-17:00",
            target_audience="全園在學幼兒",
            form_card_title="課後才藝報名表",
        )
    )
    return len(courses), len(supplies), 1


def _seed_holidays(ctx: SeedContext) -> int:
    """學年內國定假日(date 唯一)。涵蓋 114 學年(2025-08 ~ 2026-07)。"""
    from models.event import Holiday

    # (date, name)
    holidays: list[tuple[date, str]] = [
        (date(2025, 9, 28), "教師節"),
        (date(2025, 10, 10), "國慶日"),
        (date(2025, 10, 25), "光復節"),
        (date(2026, 1, 1), "元旦"),
        (date(2026, 2, 16), "農曆除夕"),
        (date(2026, 2, 17), "春節初一"),
        (date(2026, 2, 18), "春節初二"),
        (date(2026, 2, 19), "春節初三"),
        (date(2026, 2, 28), "和平紀念日"),
        (date(2026, 4, 4), "兒童節"),
        (date(2026, 4, 5), "清明節"),
        (date(2026, 5, 1), "勞動節"),
        (date(2026, 6, 19), "端午節"),
    ]
    for d, name in holidays:
        ctx.session.add(
            Holiday(
                date=d,
                name=name,
                is_active=True,
                source="manual",
                source_year=d.year,
            )
        )
    return len(holidays)


def _seed_policy_versions(ctx: SeedContext) -> int:
    """隱私/個資政策版本(供 m09 parent_consent_logs 綁定)。"""
    from datetime import datetime

    from models.consent import PolicyVersion

    versions: list[tuple[str, datetime, str, str]] = [
        (
            "1.0",
            datetime(2025, 8, 1, 0, 0, 0),
            "docs/policies/privacy-v1.0.pdf",
            "個人資料保護政策初版(114 學年適用)",
        ),
        (
            "1.1",
            datetime(2026, 1, 1, 0, 0, 0),
            "docs/policies/privacy-v1.1.pdf",
            "個資政策修訂版(新增家長端 PII retention 條款)",
        ),
    ]
    for version, eff, doc_path, summary in versions:
        ctx.session.add(
            PolicyVersion(
                version=version,
                effective_at=eff,
                document_path=doc_path,
                summary=summary,
            )
        )
    return len(versions)


def _seed_approval_policies(ctx: SeedContext) -> int:
    """簽核政策(預設審核矩陣,doc_type='all')。

    值對齊 api/approval_settings.DEFAULT_POLICIES(申請人角色 → 可審核角色)。
    """
    from models.approval import ApprovalPolicy

    default_policies: list[tuple[str, str]] = [
        ("teacher", "supervisor,hr,admin"),
        ("supervisor", "hr,admin"),
        ("hr", "admin"),
        ("admin", "admin"),
    ]
    for submitter_role, approver_roles in default_policies:
        ctx.session.add(
            ApprovalPolicy(
                doc_type="all",
                submitter_role=submitter_role,
                approver_roles=approver_roles,
                is_active=True,
            )
        )
    return len(default_policies)


def _seed_appraisal_catalog(ctx: SeedContext) -> int:
    """考核計分目錄(15 項,code 唯一)。走 reference_data.appraisal_catalog()。"""
    from models.appraisal import AppraisalScoreItemCatalog

    rows = ref.appraisal_catalog()
    for row in rows:
        ctx.session.add(AppraisalScoreItemCatalog(**row))
    return len(rows)


def _seed_system_configs(ctx: SeedContext) -> int:
    """系統設定(config_key 唯一)。記錄學年/規模/預設假別額度等通用參數。"""
    from models.config import SystemConfig

    roc_year = _academic_year_to_roc(ctx)
    configs: list[tuple[str, str, str, str]] = [
        (
            "current_school_year",
            str(roc_year),
            "academic",
            "目前學年(民國)",
        ),
        ("current_semester", "1", "academic", "目前學期(1=上,2=下)"),
        (
            "default_annual_leave_hours",
            "56",
            "leave",
            "預設特休年度配額時數(7 天)",
        ),
        (
            "default_sick_leave_hours",
            "240",
            "leave",
            "預設病假年度配額時數(30 天)",
        ),
        (
            "default_personal_leave_hours",
            "112",
            "leave",
            "預設事假年度配額時數(14 天)",
        ),
        ("school_name", "義華幼兒園", "general", "園所名稱"),
    ]
    for key, value, ctype, desc in configs:
        ctx.session.add(
            SystemConfig(
                config_key=key,
                config_value=value,
                config_type=ctype,
                description=desc,
            )
        )
    return len(configs)


def seed(ctx: SeedContext) -> None:
    """建立設定型 + 法定參考表。

    執行序:先建被依賴者(class_grades / job_titles),再建其餘設定表。
    每表 ctx.log(table, n)。本模組不 commit(由 orchestrator 跑完統一 commit)。
    """
    # 1) 被依賴的核心定義(先 flush 取 id)
    ctx.log("class_grades", _seed_class_grades(ctx))
    ctx.log("job_titles", _seed_job_titles(ctx))

    # 2) 法定參考表(保險級距/費率;每年度兩套)
    bracket_n, rate_n = _seed_insurance(ctx)
    ctx.log("insurance_brackets", bracket_n)
    ctx.log("insurance_rates", rate_n)

    # 3) period-aware 設定(每 config_year 各一套)
    ctx.log("position_salary_configs", _seed_position_salary_configs(ctx))
    ctx.log("attendance_policies", _seed_attendance_policies(ctx))
    bonus_n, target_n = _seed_bonus_configs_and_targets(ctx)
    ctx.log("bonus_configs", bonus_n)
    ctx.log("grade_targets", target_n)

    # 4) 費目模板(年級 × 學期 × 費目)
    ctx.log("fee_templates", _seed_fee_templates(ctx))

    # 5) 排班 / 才藝 / 假日 / 政策 / 簽核 / 考核目錄 / 系統設定
    ctx.log("shift_types", _seed_shift_types(ctx))
    course_n, supply_n, settings_n = _seed_activity_config(ctx)
    ctx.log("activity_courses", course_n)
    ctx.log("activity_supplies", supply_n)
    ctx.log("activity_registration_settings", settings_n)
    ctx.log("holidays", _seed_holidays(ctx))
    ctx.log("policy_versions", _seed_policy_versions(ctx))
    ctx.log("approval_policies", _seed_approval_policies(ctx))
    ctx.log("appraisal_score_item_catalog", _seed_appraisal_catalog(ctx))
    ctx.log("system_configs", _seed_system_configs(ctx))

    # leave_quotas 模板不在此建(LeaveQuota.employee_id NOT NULL + FK,
    # 無主員工列無法落庫;每員工配額由 m01/m04 綁定;預設時數記於 system_configs)。
    _seed_leave_quota_templates(ctx)
