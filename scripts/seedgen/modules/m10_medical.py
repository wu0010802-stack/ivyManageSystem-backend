"""m10_medical:過敏紀錄、用藥醫囑 + 給藥 log、身體量測、發展里程碑、

成長報告、觀察紀錄,以及少量醫療存取 log。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`ctx.config`(學年/today/closed_months/current_month)
- m01:`ctx.employees`、`ctx.users`(含 staff user 帶 employee_id)
- m02:`ctx.students`、`ctx.students_active`

時間規則(對齊計畫):只生到 closed + in_progress 月份(上限 config.today),
不生 future。closed 月資料視為「已執行/已完成」;in_progress 月(2026-02)
的用藥 log 留部分 pending(尚未給藥)。

設計重點:
- 用藥醫囑(student_medication_orders)與給藥 log(student_medication_logs)
  皆 > 0(計畫硬要求)。
- 敏感欄位(StudentAllergy.allergen/reaction_symptom/first_aid_note)為
  `EncryptedText` 型別,ORM 層透明加密(環境已設 MEDICAL_FIELD_ENCRYPTION_KEY);
  本模組只需傳明文,落庫自動加密,無需手動處理。
- `*_by` 欄位 FK 目標不同:
  * StudentObservation.recorded_by / StudentAllergy.created_by /
    StudentMedicationOrder.created_by / StudentMedicationLog.administered_by
    → users.id(取 staff user)
  * StudentMeasurement.created_by / StudentMilestone.created_by /
    StudentGrowthReport.generated_by → employees.id(取 employee)
- StudentMedicationLog 有 partial unique(order_id, scheduled_time where
  correction_of IS NULL):每張 order 每時段僅一筆原始 log,本模組嚴守不重建。
- 全部走 ctx.rng,決定論可重現;naive datetime(對齊既有欄位 now_taipei_naive)。
"""

from __future__ import annotations

from datetime import date, datetime, time

from models.medical_access_log import (
    MEDICAL_FIELD_ALLERGY,
    MEDICAL_FIELD_BUNDLE,
    MEDICAL_FIELD_MEDICATION,
    MedicalAccessLog,
)
from models.portfolio import (
    ALLERGY_SEVERITIES,
    MILESTONE_SOURCE_MANUAL,
    MILESTONE_TYPE_BIRTHDAY,
    MILESTONE_TYPE_FIRST_DAY,
    MILESTONE_TYPE_PERFECT_ATTENDANCE_MONTH,
    OBSERVATION_DOMAINS,
    StudentAllergy,
    StudentGrowthReport,
    StudentMeasurement,
    StudentMedicationLog,
    StudentMedicationOrder,
    StudentMilestone,
    StudentObservation,
)

from ..context import SeedContext

# 過敏原範本(allergen, reaction_symptom, first_aid_note)。
_ALLERGENS: list[tuple[str, str, str]] = [
    ("花生", "皮膚紅疹、嘴唇腫脹", "立即停止食用,給予抗組織胺,必要時送醫。"),
    ("乳製品", "腹瀉、腹痛", "暫停乳製品,補充水分,觀察症狀。"),
    ("蛋類", "蕁麻疹、嘔吐", "避免含蛋食物,出現呼吸困難立即送醫。"),
    ("海鮮", "全身搔癢、咳嗽", "停止進食,給予急救藥物,通知家長。"),
    ("塵蟎", "打噴嚏、流鼻水", "保持環境清潔,避免接觸絨毛玩具。"),
]

# 用藥醫囑範本(medication_name, dose, time_slots, note)。
_MEDICATIONS: list[tuple[str, str, list[str], str]] = [
    ("退燒藥水", "5ml", ["08:30", "12:30"], "發燒超過 38.5 度時給予。"),
    ("止咳糖漿", "5ml", ["12:00"], "午餐後服用。"),
    ("抗生素", "1 顆", ["08:30", "12:30", "16:00"], "三餐飯後,需服完整療程。"),
    ("過敏藥", "半顆", ["08:00"], "早上一次。"),
]

# 觀察敘事範本。
_OBSERVATION_NOTES: list[str] = [
    "今天主動幫忙收拾教具,展現良好的責任感。",
    "在團體討論中勇敢分享自己的想法,語言表達進步明顯。",
    "與同學合作完成積木城堡,社交互動越來越成熟。",
    "畫畫時很有創意,對顏色搭配展現美感。",
    "情緒管理進步,遇到挫折時能用語言表達需求。",
]

# 成長報告教師敘述範本。
_REPORT_NARRATIVES: list[str] = [
    "本學期在各領域均有穩定成長,尤其在語文與社會互動表現亮眼。",
    "孩子的學習態度積極,生活自理能力逐步提升,值得肯定。",
    "在認知與動作發展上進步顯著,建議持續鼓勵其探索精神。",
]


def _naive_dt(d: date, hour: int = 9, minute: int = 0) -> datetime:
    """以日期 + 時分組出 naive datetime(對齊既有欄位語意)。"""
    return datetime.combine(d, time(hour=hour, minute=minute))


def _staff_user_id(ctx: SeedContext) -> int | None:
    """取一個非家長(staff)User 的 id 當醫療紀錄 actor;無則 None。

    過敏/用藥/觀察的 `*_by` 欄位 FK 指向 users.id。
    """
    for user in (ctx.users or {}).values():
        if getattr(user, "role", None) != "parent":
            uid = getattr(user, "id", None)
            if uid is not None:
                return uid
    return None


def _employee_id(ctx: SeedContext):
    """取一個 employee.id 當量測/里程碑/成長報告 actor;無則 None。

    量測/里程碑/成長報告的 created_by/generated_by FK 指向 employees.id。
    """
    emps = ctx.employees or []
    return emps[0].id if emps else None


def _active_students(ctx: SeedContext) -> list:
    """取在籍學生;退化用全體學生,確保資料 > 0。"""
    students = list(ctx.students_active or [])
    if not students:
        students = list(ctx.students or [])
    return students


def _seed_allergies(ctx: SeedContext, actor_user_id: int | None) -> None:
    """約 18% 學生有 1 筆長期過敏紀錄(allergen 等敏感欄位 ORM 自動加密)。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    n = 0
    for student in students:
        if ctx.rng.random() >= 0.18:
            continue
        allergen, symptom, first_aid = ctx.rng.choice(_ALLERGENS)
        created = _naive_dt(ctx.config.year_start, hour=9)
        session.add(
            StudentAllergy(
                student_id=student.id,
                allergen=allergen,  # EncryptedText:傳明文,ORM 自動加密
                severity=ctx.rng.choice(ALLERGY_SEVERITIES),
                reaction_symptom=symptom,
                first_aid_note=first_aid,
                active=True,
                created_by=actor_user_id,
                created_at=created,
                updated_at=created,
            )
        )
        n += 1

    if n:
        ctx.log("student_allergies", n)


def _seed_medications(ctx: SeedContext, actor_user_id: int | None) -> None:
    """用藥醫囑 + 預建給藥 log(每時段一筆)。

    對抽樣的 closed 工作日建 order,並依 time_slots 預建 N 筆 log(已給藥);
    in_progress 月(當日 today)抽部分建 order,其給藥 log 留 pending。
    嚴守 partial unique:每張 order 每時段僅一筆原始 log(correction_of=NULL)。
    """
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    from ..calendar import workdays as _workdays

    closed = ctx.closed_months()
    current = ctx.current_month()
    months = list(closed) + [current]

    n_order = 0
    n_log = 0

    for year, month in months:
        is_current = (year, month) == current
        upto = ctx.config.today if is_current else None
        wd = _workdays(year, month, upto=upto)
        if not wd:
            continue
        # 每月抽 1~2 個工作日有臨時用藥單。
        n_days = min(len(wd), ctx.rng.randint(1, 2))
        sampled = ctx.rng.sample(wd, n_days)
        for order_date in sampled:
            # 每天抽 1~3 名學生有用藥單。
            k = min(len(students), ctx.rng.randint(1, 3))
            chosen = ctx.rng.sample(students, k)
            for student in chosen:
                med_name, dose, slots, note = ctx.rng.choice(_MEDICATIONS)
                created = _naive_dt(order_date, hour=8)
                order = StudentMedicationOrder(
                    student_id=student.id,
                    order_date=order_date,
                    medication_name=med_name,
                    dose=dose,
                    time_slots=list(slots),  # JSON 陣列
                    note=note,
                    created_by=actor_user_id,
                    source="teacher",
                    created_at=created,
                    updated_at=created,
                )
                session.add(order)
                session.flush()  # 取得 order.id 供 log FK
                n_order += 1

                # 依 time_slots 預建給藥 log,每時段一筆(原始 log)。
                for slot in slots:
                    hh, mm = slot.split(":")
                    if is_current and ctx.rng.random() < 0.5:
                        # 進行中月:部分時段尚未給藥(pending)。
                        administered_at = None
                        administered_by = None
                        skipped = False
                        skipped_reason = None
                    else:
                        administered_at = _naive_dt(
                            order_date, hour=int(hh), minute=int(mm)
                        )
                        administered_by = actor_user_id
                        skipped = False
                        skipped_reason = None
                    session.add(
                        StudentMedicationLog(
                            order_id=order.id,
                            scheduled_time=slot,
                            administered_at=administered_at,
                            administered_by=administered_by,
                            skipped=skipped,
                            skipped_reason=skipped_reason,
                            note=None,
                            correction_of=None,
                            created_at=created,
                        )
                    )
                    n_log += 1

    if n_order:
        ctx.log("student_medication_orders", n_order)
    if n_log:
        ctx.log("student_medication_logs", n_log)


def _seed_measurements(ctx: SeedContext, actor_emp_id) -> None:
    """每位學生在學年起與學期中各一筆身高體重量測(至少一量測值,符合 CHECK)。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    # 量測日:學年起 + 學期中(closed 月之一);只取 ≤ today。
    measure_dates = [ctx.config.year_start]
    closed = ctx.closed_months()
    if closed:
        my, mm = closed[len(closed) // 2]
        measure_dates.append(date(my, mm, 15))

    n = 0
    for student in students:
        for md in measure_dates:
            if md > ctx.config.today:
                continue
            # 身高 90~120cm、體重 14~26kg(決定論)。
            height = 90.0 + ctx.rng.randint(0, 300) / 10.0
            weight = 14.0 + ctx.rng.randint(0, 120) / 10.0
            session.add(
                StudentMeasurement(
                    student_id=student.id,
                    measured_on=md,
                    height_cm=round(height, 2),
                    weight_kg=round(weight, 2),
                    head_circumference_cm=round(
                        48.0 + ctx.rng.randint(0, 40) / 10.0, 2
                    ),
                    vision_left=None,
                    vision_right=None,
                    note=None,
                    created_by=actor_emp_id,
                )
            )
            n += 1

    if n:
        ctx.log("student_measurements", n)


def _seed_milestones(ctx: SeedContext, actor_emp_id) -> None:
    """每位學生一筆「入學日」里程碑;部分學生加生日/全勤里程碑。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    n = 0
    for student in students:
        # 入學日里程碑(每位學生一筆)。
        achieved = getattr(student, "enrollment_date", None) or ctx.config.year_start
        if achieved > ctx.config.today:
            achieved = ctx.config.year_start
        session.add(
            StudentMilestone(
                student_id=student.id,
                milestone_type=MILESTONE_TYPE_FIRST_DAY,
                achieved_on=achieved,
                title="第一天上學",
                description="歡迎加入我們的大家庭!",
                icon="🎒",
                source_type=MILESTONE_SOURCE_MANUAL,
                source_ref_type=None,
                source_ref_id=None,
                created_by=actor_emp_id,
            )
        )
        n += 1

        # 約 30% 學生加一筆生日或全勤里程碑。
        if ctx.rng.random() < 0.3:
            bday = getattr(student, "birthday", None)
            if bday is not None:
                # 取學年內最近一次生日(同月日,落在學年範圍)。
                cand = date(
                    ctx.config.year_start.year, max(bday.month, 1), max(bday.day, 1)
                )
                if cand > ctx.config.today or cand < ctx.config.year_start:
                    cand = ctx.config.year_start
                session.add(
                    StudentMilestone(
                        student_id=student.id,
                        milestone_type=MILESTONE_TYPE_BIRTHDAY,
                        achieved_on=cand,
                        title="生日快樂",
                        description="祝你生日快樂,健康快樂長大!",
                        icon="🎂",
                        source_type=MILESTONE_SOURCE_MANUAL,
                        source_ref_type=None,
                        source_ref_id=None,
                        created_by=actor_emp_id,
                    )
                )
                n += 1
            else:
                closed = ctx.closed_months()
                if closed:
                    my, mm = closed[0]
                    session.add(
                        StudentMilestone(
                            student_id=student.id,
                            milestone_type=MILESTONE_TYPE_PERFECT_ATTENDANCE_MONTH,
                            achieved_on=date(my, mm, 28),
                            title="全勤寶寶",
                            description="這個月每天都到園,好棒!",
                            icon="⭐",
                            source_type=MILESTONE_SOURCE_MANUAL,
                            source_ref_type=None,
                            source_ref_id=None,
                            created_by=actor_emp_id,
                        )
                    )
                    n += 1

    if n:
        ctx.log("student_milestones", n)


def _seed_observations(ctx: SeedContext, actor_user_id: int | None) -> None:
    """抽樣學生在 closed 月各建 1~2 筆日常正向觀察。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    from ..calendar import workdays as _workdays

    closed = ctx.closed_months()
    if not closed:
        return

    # 為避免爆量,只對約 40% 學生在每個 closed 月抽一個工作日各建一筆。
    sampled_students = [s for s in students if ctx.rng.random() < 0.4]
    if not sampled_students:
        sampled_students = students[: max(1, len(students) // 3)]

    n = 0
    for year, month in closed:
        wd = _workdays(year, month, upto=None)
        if not wd:
            continue
        obs_date = ctx.rng.choice(wd)
        for student in sampled_students:
            if ctx.rng.random() >= 0.5:
                continue
            is_highlight = ctx.rng.random() < 0.2
            session.add(
                StudentObservation(
                    student_id=student.id,
                    observation_date=obs_date,
                    domain=ctx.rng.choice(OBSERVATION_DOMAINS),
                    narrative=ctx.rng.choice(_OBSERVATION_NOTES),
                    rating=ctx.rng.randint(3, 5),
                    is_highlight=is_highlight,
                    recorded_by=actor_user_id,
                    deleted_at=None,
                    created_at=_naive_dt(obs_date, hour=16),
                    updated_at=_naive_dt(obs_date, hour=16),
                )
            )
            n += 1

    if n:
        ctx.log("student_observations", n)


def _seed_growth_reports(ctx: SeedContext, actor_emp_id) -> None:
    """約 25% 學生有一份上學期成長報告(status=ready)。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students:
        return

    # 上學期:學年起(8/1) ~ 隔年 1/31。
    period_start = ctx.config.year_start
    period_end = date(ctx.config.year_start.year + 1, 1, 31)
    if period_end > ctx.config.today:
        period_end = ctx.config.today
    generated = _naive_dt(period_end, hour=10)

    n = 0
    for student in students:
        if ctx.rng.random() >= 0.25:
            continue
        session.add(
            StudentGrowthReport(
                student_id=student.id,
                period_label="114上學期",
                period_start=period_start,
                period_end=period_end,
                status="ready",
                file_path=f"2026/01/growth_{student.id}.pdf",
                file_size=ctx.rng.randint(80000, 400000),
                error_message=None,
                generated_by=actor_emp_id,
                generated_at=generated,
                created_at=generated,
                line_sent_at=None,
                parent_first_viewed_at=None,
                parent_view_count=0,
                teacher_narrative=ctx.rng.choice(_REPORT_NARRATIVES),
            )
        )
        n += 1

    if n:
        ctx.log("student_growth_reports", n)


def _seed_access_logs(ctx: SeedContext, actor_user_id: int | None) -> None:
    """少量醫療欄位取用稽核(個資法 §6 特種個資讀取留痕)。"""
    session = ctx.session
    students = _active_students(ctx)
    if not students or actor_user_id is None:
        return

    closed = ctx.closed_months()
    if not closed:
        return

    field_choices = [
        MEDICAL_FIELD_ALLERGY,
        MEDICAL_FIELD_MEDICATION,
        MEDICAL_FIELD_BUNDLE,
    ]
    reasons = [
        "點名頁顯示過敏 badge,確認急救處置。",
        "家長詢問用藥狀況,核對當日醫囑。",
        "健康檢查前彙整學生醫療資訊。",
    ]

    n = 0
    # 每個 closed 月抽幾筆取用稽核(掛在抽樣學生上)。
    for year, month in closed:
        k = min(len(students), ctx.rng.randint(2, 4))
        chosen = ctx.rng.sample(students, k)
        for student in chosen:
            session.add(
                MedicalAccessLog(
                    user_id=actor_user_id,
                    student_id=student.id,
                    field_name=ctx.rng.choice(field_choices),
                    reason=ctx.rng.choice(reasons),
                    accessed_at=_naive_dt(
                        date(year, month, min(ctx.rng.randint(5, 25), 28)),
                        hour=ctx.rng.randint(9, 16),
                    ),
                    ip_address="127.0.0.1",
                )
            )
            n += 1

    if n:
        ctx.log("medical_access_log", n)


def seed(ctx: SeedContext) -> None:
    """建立醫療/健康/發展/觀察資料 + 少量醫療取用稽核。"""
    actor_user_id = _staff_user_id(ctx)
    actor_emp_id = _employee_id(ctx)

    _seed_allergies(ctx, actor_user_id)
    _seed_medications(ctx, actor_user_id)
    _seed_measurements(ctx, actor_emp_id)
    _seed_milestones(ctx, actor_emp_id)
    _seed_observations(ctx, actor_user_id)
    _seed_growth_reports(ctx, actor_emp_id)
    _seed_access_logs(ctx, actor_user_id)
