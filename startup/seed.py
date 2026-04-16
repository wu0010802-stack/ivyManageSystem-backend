"""
startup/seed.py — 預設資料 Seed 函式

所有 seed 函式皆為冪等（重複執行不會產生重複資料）。
"""

import logging
import os

from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    AttendancePolicy,
    BonusConfig as DBBonusConfig,
    GradeTarget,
    InsuranceRate,
    JobTitle,
    User,
    Employee,
    ShiftType,
    ApprovalPolicy,
    ActivityRegistrationSettings,
)

logger = logging.getLogger(__name__)

OFFICIAL_JOB_TITLES = [
    "園長",
    "幼兒園教師",
    "教保員",
    "助理教保員",
    "司機",
    "廚工",
    "職員",
]


def _is_production() -> bool:
    return os.environ.get("ENV", "development").lower() in ("production", "prod")


def seed_class_grades():
    """確保標準年級存在於 class_grades 表（幂等，只補缺漏，不覆蓋已有）"""
    from models.classroom import ClassGrade

    default_grades = [
        {"name": "大班", "age_range": "5-6歲", "sort_order": 1},
        {"name": "中班", "age_range": "4-5歲", "sort_order": 2},
        {"name": "小班", "age_range": "3-4歲", "sort_order": 3},
        {"name": "幼幼班", "age_range": "2歲以下", "sort_order": 4},
    ]
    session = get_session()
    try:
        added = 0
        for g in default_grades:
            if not session.query(ClassGrade).filter_by(name=g["name"]).first():
                session.add(ClassGrade(**g))
                added += 1
        # 停用已廢除的幼兒班（保留歷史資料，但不再出現於下拉選單）
        obsolete = session.query(ClassGrade).filter_by(name="幼兒班").first()
        if obsolete and obsolete.is_active:
            obsolete.is_active = False
            added += 1
            logger.info("seed_class_grades：停用已廢除年級「幼兒班」")
        if added:
            session.commit()
            logger.info("seed_class_grades：新增或更新 %d 個預設年級", added)
    except Exception:
        session.rollback()
        logger.exception("seed_class_grades 失敗")
    finally:
        session.close()


def seed_job_titles():
    session = get_session()
    try:
        existing_titles = {jt.name: jt for jt in session.query(JobTitle).all()}
        changed = False

        for i, name in enumerate(OFFICIAL_JOB_TITLES, start=1):
            job_title = existing_titles.get(name)
            if job_title:
                if not job_title.is_active or job_title.sort_order != i:
                    job_title.is_active = True
                    job_title.sort_order = i
                    changed = True
            else:
                session.add(JobTitle(name=name, sort_order=i, is_active=True))
                changed = True

        official_set = set(OFFICIAL_JOB_TITLES)
        for name, legacy_title in existing_titles.items():
            if name not in official_set and legacy_title.is_active:
                changed = True
                legacy_title.is_active = False

        if changed:
            session.commit()
            logger.info("Job titles synced to official bureau list.")
    finally:
        session.close()


def seed_default_configs():
    """初始化預設系統設定"""
    session = get_session()
    try:
        if session.query(AttendancePolicy).count() == 0:
            policy = AttendancePolicy(
                default_work_start="08:00",
                default_work_end="17:00",
                late_deduction=50,
                early_leave_deduction=50,
                missing_punch_deduction=0,
                festival_bonus_months=3,
                is_active=True,
            )
            session.add(policy)
            logger.info("Seeded default attendance policy.")

        if session.query(DBBonusConfig).count() == 0:
            config = DBBonusConfig(
                config_year=2026,
                head_teacher_ab=2000,
                head_teacher_c=1500,
                assistant_teacher_ab=1200,
                assistant_teacher_c=1200,
                principal_festival=6500,
                director_festival=3500,
                leader_festival=2000,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                principal_dividend=5000,
                director_dividend=4000,
                leader_dividend=3000,
                vice_leader_dividend=1500,
                overtime_head_normal=400,
                overtime_head_baby=450,
                overtime_assistant_normal=100,
                overtime_assistant_baby=150,
                school_wide_target=160,
                is_active=True,
            )
            session.add(config)
            logger.info("Seeded default bonus config.")

        if session.query(GradeTarget).count() == 0:
            grade_targets = [
                {
                    "grade_name": "大班",
                    "festival_two_teachers": 27,
                    "festival_one_teacher": 14,
                    "festival_shared": 20,
                    "overtime_two_teachers": 25,
                    "overtime_one_teacher": 13,
                    "overtime_shared": 20,
                },
                {
                    "grade_name": "中班",
                    "festival_two_teachers": 25,
                    "festival_one_teacher": 13,
                    "festival_shared": 18,
                    "overtime_two_teachers": 23,
                    "overtime_one_teacher": 12,
                    "overtime_shared": 18,
                },
                {
                    "grade_name": "小班",
                    "festival_two_teachers": 23,
                    "festival_one_teacher": 12,
                    "festival_shared": 16,
                    "overtime_two_teachers": 21,
                    "overtime_one_teacher": 11,
                    "overtime_shared": 16,
                },
                {
                    "grade_name": "幼幼班",
                    "festival_two_teachers": 15,
                    "festival_one_teacher": 7,
                    "festival_shared": 12,
                    "overtime_two_teachers": 14,
                    "overtime_one_teacher": 7,
                    "overtime_shared": 12,
                },
            ]
            for gt in grade_targets:
                session.add(GradeTarget(config_year=2026, **gt))
            logger.info("Seeded default grade targets.")

        if session.query(InsuranceRate).count() == 0:
            rate = InsuranceRate(
                rate_year=2026,
                labor_rate=0.125,
                labor_employee_ratio=0.20,
                labor_employer_ratio=0.70,
                labor_government_ratio=0.10,
                health_rate=0.0517,
                health_employee_ratio=0.30,
                health_employer_ratio=0.60,
                pension_employer_rate=0.06,
                average_dependents=0.56,
                is_active=True,
            )
            session.add(rate)
            logger.info("Seeded default insurance rates.")

        session.commit()
    finally:
        session.close()


def seed_shift_types():
    """初始化預設班別"""
    session = get_session()
    try:
        if session.query(ShiftType).count() == 0:
            defaults = [
                {
                    "name": "早值",
                    "work_start": "08:00",
                    "work_end": "17:00",
                    "sort_order": 1,
                },
                {
                    "name": "正值(班導)",
                    "work_start": "09:00",
                    "work_end": "18:00",
                    "sort_order": 2,
                },
                {
                    "name": "正值(副班導)",
                    "work_start": "07:00",
                    "work_end": "16:30",
                    "sort_order": 3,
                },
                {
                    "name": "次值",
                    "work_start": "08:30",
                    "work_end": "18:00",
                    "sort_order": 4,
                },
                {
                    "name": "無值週",
                    "work_start": "08:00",
                    "work_end": "17:30",
                    "sort_order": 5,
                },
                {
                    "name": "早車",
                    "work_start": "07:00",
                    "work_end": "16:30",
                    "sort_order": 6,
                },
                {
                    "name": "晚車",
                    "work_start": "08:30",
                    "work_end": "18:00",
                    "sort_order": 7,
                },
                {
                    "name": "早上等接",
                    "work_start": "07:30",
                    "work_end": "17:00",
                    "sort_order": 8,
                },
            ]
            for st in defaults:
                session.add(ShiftType(**st))
            session.commit()
            logger.info("Seeded default shift types.")
    finally:
        session.close()


def seed_default_admin():
    """建立初始管理員帳號。

    優先從環境變數讀取帳密：
      ADMIN_INIT_USERNAME  （預設: admin）
      ADMIN_INIT_PASSWORD  （正式環境必須設定）

    正式環境（ENV=production）若未設定 ADMIN_INIT_PASSWORD，
    則不自動建立帳號，避免弱密碼遺留——請部署後手動透過環境變數設定。
    開發環境退而使用預設值 admin/admin123，並強制標記 must_change_password。
    """
    from utils.auth import hash_password

    init_username = os.environ.get("ADMIN_INIT_USERNAME", "").strip()
    init_password = os.environ.get("ADMIN_INIT_PASSWORD", "").strip()

    if not init_password:
        if _is_production():
            logger.error(
                "正式環境尚未設定 ADMIN_INIT_PASSWORD，"
                "系統不會自動建立管理員帳號。"
                "請設定環境變數後重新啟動：\n"
                "  ADMIN_INIT_USERNAME=<帳號>  ADMIN_INIT_PASSWORD=<強密碼>"
            )
            return
        else:
            init_username = init_username or "admin"
            init_password = "admin123"
            logger.warning(
                "開發環境使用預設管理員帳號 admin/admin123，"
                "已標記 must_change_password=True。請勿在正式環境使用！"
            )
            must_change = True
    else:
        init_username = init_username or "admin"
        must_change = False

    session = get_session()
    try:
        if session.query(User).filter(User.role == "admin").count() > 0:
            return

        emp = session.query(Employee).first()
        if not emp:
            emp = Employee(
                employee_id="ADMIN001",
                name="系統管理員",
                position="管理員",
            )
            session.add(emp)
            session.flush()

        admin_user = User(
            employee_id=emp.id,
            username=init_username,
            password_hash=hash_password(init_password),
            role="admin",
            permissions=-1,
            must_change_password=must_change,
        )
        session.add(admin_user)
        session.commit()
        logger.info("已建立初始管理員帳號：%s（linked to %s）", init_username, emp.name)
    except IntegrityError:
        session.rollback()
        logger.info(
            "seed_default_admin：另一進程已搶先建立管理員帳號（username=%s），忽略此次 seed。",
            init_username,
        )
    finally:
        session.close()


def seed_approval_policies():
    """初始化預設審核政策（若表為空則 seed 4 筆預設值）"""
    from api.approval_settings import DEFAULT_POLICIES

    session = get_session()
    try:
        if session.query(ApprovalPolicy).count() == 0:
            for p in DEFAULT_POLICIES:
                session.add(
                    ApprovalPolicy(
                        doc_type="all",
                        submitter_role=p["submitter_role"],
                        approver_roles=p["approver_roles"],
                        is_active=True,
                    )
                )
            session.commit()
            logger.info("Seeded default approval policies.")
    finally:
        session.close()


def seed_activity_settings():
    """初始化課後才藝報名設定 singleton（若不存在則建立）"""
    session = get_session()
    try:
        if session.query(ActivityRegistrationSettings).count() == 0:
            session.add(
                ActivityRegistrationSettings(
                    is_open=False,
                    open_at=None,
                    close_at=None,
                )
            )
            session.commit()
            logger.info("Seeded default activity registration settings.")
    finally:
        session.close()
