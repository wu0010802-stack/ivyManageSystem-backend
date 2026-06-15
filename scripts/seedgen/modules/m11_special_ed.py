"""m11_special_ed:IEP 個別化教育計畫、身障/特教文件、特教加給補助、

月度在籍快照(MOE 月報)、在學證明。對應教育部報送(gov_moe)相關資料。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`ctx.config`(學年/today/closed_months)、`ctx.class_grades`
- m01:`ctx.employees`、`ctx.employees_by_role`(homeroom/assistant/art ...)、
       `ctx.classrooms`(含 head_teacher_id/grade)、`ctx.users`(含 admin)
- m02:`ctx.students`、`ctx.students_active`、`ctx.guardians`

時間規則(對齊計畫):月報快照逐月只生到 closed + in_progress 月份
(上限 config.today),不生 future;IEP/補助以學年/學期為粒度,
上學期(含 today 前)視為已核,下學期(進行中)留審核中。

設計重點:
- 先決定論挑選一小批「特教幼生」(active 且有班級者的 ~8%,至少 3 名,
  上限 12),IEP/身障文件/補助/快照的 disability_count 皆以此批為準,
  使各表內部一致(快照 disability 人數 = 該班特教幼生數)。
- student_iep_records:每位特教幼生在 114 學年兩學期各一筆;school_year 用
  **西元學年**(2025,對齊模型欄位語意:跨表 join 須轉換);
  uq(student_id, school_year, semester) 不碰撞。上學期 approved、下學期
  pending_review。IEP 硬要求 > 0。
- student_disability_documents:每位特教幼生 1~2 份(鑑定證明/身障手冊),
  doc_type 在模型 comment 白名單(鑑定證明/身障手冊/IEP/評估報告/其他)。
- special_education_subsidies:服務特教幼生的班導(teacher_extra)與
  助教(assistant_hourly)每個 closed 學期一筆;amount 走 round_half_up,
  落在 Money 範圍;related_student_ids 為服務的特教幼生 id list(JSON)。
- monthly_enrollment_snapshots:每個 closed + in_progress 月 × 每個有
  學生的班級一筆;人數由該班 active 學生即時統計(total/male/female/
  disability/indigenous/foreign);age_group 由班級年級對映;
  attendance_rate 為百分比×100 整數;uq(year,month,classroom_id,age_group)。
- enrollment_certificates:少量在學證明,seq 於同一 year 內從 1 起遞增
  (uq(year, seq) 不碰撞)。
- 全部走 ctx.rng,決定論可重現;naive datetime(對齊既有欄位語意)。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from models.gov_moe import (
    EnrollmentCertificate,
    MonthlyEnrollmentSnapshot,
    SpecialEducationSubsidy,
    StudentDisabilityDocument,
    StudentIEPRecord,
)
from utils.rounding import round_half_up

from ..context import SeedContext


# 西元學年(114 學年 → 2025);IEP school_year 用西元年(模型 comment 明示)。
def _gregorian_school_year(ctx: SeedContext) -> int:
    return ctx.config.academic_year + 1911


# 班級年級 age_range("2-3歲" ...) → 月報 age_group("2-3" ...) 對照。
_AGE_RANGE_TO_GROUP: dict[str, str] = {
    "2-3歲": "2-3",
    "3-4歲": "3-4",
    "4-5歲": "4-5",
    "5-6歲": "5-6",
}

# 身障文件類型(模型 comment 白名單)。
_DOC_TYPES: list[str] = ["鑑定證明", "身障手冊"]

# 身障類別/等級(供特教幼生標註,僅測試語意,不求醫學精確)。
_DISABILITY_TYPES: list[str] = [
    "發展遲緩",
    "語言障礙",
    "自閉症",
    "智能障礙",
    "聽覺障礙",
]
_DISABILITY_LEVELS: list[str] = ["輕度", "中度"]

# 在學證明申請用途範本。
_CERT_PURPOSES: list[str] = [
    "申請育兒津貼",
    "報稅扶養證明",
    "保險理賠",
    "戶政登記",
    "其他政府補助申請",
]


def _naive_dt(d: date, hour: int = 9, minute: int = 0) -> datetime:
    """以日期 + 時分組出 naive datetime(對齊既有欄位語意)。"""
    return datetime.combine(d, time(hour=hour, minute=minute))


def _is_male(student) -> bool:
    """學生性別判定(m02 存 '男'/'女')。"""
    return getattr(student, "gender", None) in {"男", "M", "male"}


def _classrooms_with_students(ctx: SeedContext) -> dict[int, list]:
    """把有班級的 active 學生(退化為全體有班級者)依 classroom_id 分組。"""
    students = [
        s
        for s in (ctx.students_active or [])
        if getattr(s, "classroom_id", None) is not None
    ]
    if not students:
        students = [
            s
            for s in (ctx.students or [])
            if getattr(s, "classroom_id", None) is not None
        ]
    grouped: dict[int, list] = {}
    for s in students:
        grouped.setdefault(s.classroom_id, []).append(s)
    return grouped


def _pick_special_ed_students(ctx: SeedContext) -> list:
    """決定論挑出一小批特教幼生(有班級的 active 學生的 ~8%,範圍 [3,12])。

    挑選後就地把學生標註 special_needs / disability_type / disability_level,
    使後續快照 disability_count 與 Student 欄位一致(同一交易內 flush 落庫)。
    """
    candidates = [
        s
        for s in (ctx.students_active or [])
        if getattr(s, "classroom_id", None) is not None
    ]
    if not candidates:
        candidates = [
            s
            for s in (ctx.students or [])
            if getattr(s, "classroom_id", None) is not None
        ]
    if not candidates:
        return []

    # 決定論排序後抽樣(避免依賴 list 既有順序的不確定性)。
    candidates = sorted(candidates, key=lambda s: getattr(s, "id", 0) or 0)
    target = max(3, round(len(candidates) * 0.08))
    target = min(target, 12, len(candidates))
    chosen = ctx.rng.sample(candidates, target)

    greg_year = _gregorian_school_year(ctx)
    for s in chosen:
        dtype = ctx.rng.choice(_DISABILITY_TYPES)
        dlevel = ctx.rng.choice(_DISABILITY_LEVELS)
        # 就地標註(欄位在 m02 預設為 None);供月報統計與報送一致。
        s.special_needs = dtype
        s.disability_type = dtype
        s.disability_level = dlevel
        s.disability_cert_no = f"D{greg_year}{getattr(s, 'id', 0) or 0:05d}"
        s.disability_cert_expiry = date(greg_year + 3, 7, 31)
    if chosen:
        ctx.session.flush()  # 落庫學生標註,供快照即時統計
    return chosen


def _iep_jsonb_fields(created_d: date, semester: int) -> dict:
    """IEP jsonb 欄位（短期目標/團隊/會議日期）。

    shape 須對齊 API 契約（api/gov_moe/iep.py IepBase）：short_term_goals /
    iep_team_members = List[dict]、meeting_dates = dict。寫成 list[str]/list 會使
    GET/匯出 IEP 端點對 seed 資料全 500（2026-06-15 運作探測 P2-5）。
    """
    return {
        "short_term_goals": [
            {
                "domain": "語言溝通",
                "goal": "能主動表達基本需求",
                "criterion": "10 次中 8 次達成",
            },
            {
                "domain": "社會互動",
                "goal": "能與同儕進行簡單互動",
                "criterion": "連續 2 週每日達成",
            },
        ],
        "iep_team_members": [
            {"role": "班級導師", "name": "班導"},
            {"role": "特教巡迴輔導教師", "name": "巡輔老師"},
            {"role": "家長", "name": "家長代表"},
        ],
        "meeting_dates": {"initial": created_d.isoformat()},
    }


def _seed_iep_and_docs(ctx: SeedContext, special_students: list) -> None:
    """每位特教幼生:兩學期 IEP + 1~2 份身障文件。"""
    session = ctx.session
    if not special_students:
        return

    greg_year = _gregorian_school_year(ctx)

    # IEP 建立者:優先該班班導,退化任意員工;核定者:主管。
    homeroom_by_classroom: dict[int, int] = {}
    for c in ctx.classrooms or []:
        ht = getattr(c, "head_teacher_id", None)
        if ht is not None:
            homeroom_by_classroom[c.id] = ht
    by_role = ctx.employees_by_role or {}
    supervisor = (by_role.get("supervisor") or [None])[0]
    approver_id = getattr(supervisor, "id", None)
    any_emp_id = None
    if ctx.employees:
        any_emp_id = ctx.employees[0].id

    n_iep = 0
    n_doc = 0
    for student in special_students:
        classroom_id = getattr(student, "classroom_id", None)
        creator_id = homeroom_by_classroom.get(classroom_id, any_emp_id)

        # 兩學期:上學期(sem 1)已核;下學期(sem 2)審核中。
        for semester, status in ((1, "approved"), (2, "pending_review")):
            # 建立/核定時間落在對應學期。
            if semester == 1:
                created_d = date(greg_year, 9, 15)  # 上學期開學後
                approved_id = approver_id
            else:
                created_d = date(greg_year + 1, 2, 20)  # 下學期(進行中)
                approved_id = None
            if created_d > ctx.config.today:
                created_d = ctx.config.today
            iep_jsonb = _iep_jsonb_fields(created_d, semester)
            iep = StudentIEPRecord(
                student_id=student.id,
                school_year=greg_year,  # 西元學年(模型語意)
                semester=semester,
                status=status,
                current_status="目前發展狀況評估(seedgen 測試資料)。",
                long_term_goals="提升語言表達與社會互動能力。",
                short_term_goals=iep_jsonb["short_term_goals"],
                mid_term_evaluation=(
                    "期中已達部分短期目標。" if semester == 1 else None
                ),
                final_evaluation=("期末整體達成度良好。" if semester == 1 else None),
                iep_team_members=iep_jsonb["iep_team_members"],
                meeting_dates=iep_jsonb["meeting_dates"],
                created_by_employee_id=creator_id,
                approved_by_employee_id=approved_id,
                created_at=_naive_dt(created_d, hour=10),
                updated_at=_naive_dt(created_d, hour=10),
                deleted_at=None,
            )
            session.add(iep)
            n_iep += 1

        # 身障文件 1~2 份。
        n_docs_this = ctx.rng.randint(1, 2)
        issued = date(greg_year, 8, 10)
        for di in range(n_docs_this):
            doc_type = _DOC_TYPES[di % len(_DOC_TYPES)]
            session.add(
                StudentDisabilityDocument(
                    student_id=student.id,
                    doc_type=doc_type,
                    file_path=f"/uploads/disability/{student.id}_{doc_type}.pdf",
                    issued_date=issued,
                    expiry_date=date(greg_year + 3, 7, 31),
                    notes="seedgen 測試文件",
                    created_at=_naive_dt(issued, hour=9),
                    updated_at=_naive_dt(issued, hour=9),
                )
            )
            n_doc += 1

    ctx.log("student_iep_records", n_iep)
    if n_doc:
        ctx.log("student_disability_documents", n_doc)


def _seed_subsidies(ctx: SeedContext, special_students: list) -> None:
    """服務特教幼生的班導/助教,每個 closed 學期一筆特教加給/鐘點補助。

    上學期(2025-08~2026-01)closed → 一筆;下學期(2026-02~)進行中 → 不建。
    """
    session = ctx.session
    if not special_students:
        return

    # 特教幼生所屬班級。
    classroom_ids = {getattr(s, "classroom_id", None) for s in special_students}
    classroom_ids.discard(None)
    if not classroom_ids:
        return

    by_role = ctx.employees_by_role or {}
    homerooms = by_role.get("homeroom") or []
    assistants = by_role.get("assistant") or []

    # 對映班級 → 班導 employee。
    classroom_to_homeroom: dict[int, int] = {}
    for c in ctx.classrooms or []:
        ht = getattr(c, "head_teacher_id", None)
        if ht is not None:
            classroom_to_homeroom[c.id] = ht

    greg_year = _gregorian_school_year(ctx)
    # 上學期期間(已 closed)。
    period_start = date(greg_year, 8, 1)
    period_end = date(greg_year + 1, 1, 31)
    # 僅在上學期確實已過(today 已跨過 period_end)才建,避免落 future。
    if period_end > ctx.config.today:
        return

    # 服務的特教幼生 id list(全批)。
    served_ids = sorted(int(s.id) for s in special_students if getattr(s, "id", None))

    n_sub = 0

    # 班導加給(teacher_extra):每個有特教幼生的班級的班導一筆。
    served_homeroom_ids = {
        classroom_to_homeroom[cid]
        for cid in classroom_ids
        if cid in classroom_to_homeroom
    }
    for emp_id in sorted(served_homeroom_ids):
        amount = int(round_half_up(ctx.rng.randint(2000, 5000)))
        session.add(
            SpecialEducationSubsidy(
                subsidy_type="teacher_extra",
                employee_id=emp_id,
                related_student_ids=served_ids,
                period_start=period_start,
                period_end=period_end,
                hours_or_rate=None,
                amount_requested=amount,
                amount_approved=amount,
                status="paid",
                applied_at=_naive_dt(period_end, hour=9),
                approved_at=_naive_dt(period_end + timedelta(days=3), hour=10),
                paid_at=_naive_dt(period_end + timedelta(days=10), hour=14),
                approval_doc_path=None,
                notes="特教班導加給(seedgen)",
                created_at=_naive_dt(period_end, hour=9),
                updated_at=_naive_dt(period_end + timedelta(days=10), hour=14),
            )
        )
        n_sub += 1

    # 助教鐘點(assistant_hourly):取一名助教一筆(若有)。
    if assistants:
        helper = assistants[0]
        hours = round_half_up(ctx.rng.choice([20.0, 24.0, 30.5, 40.0]))
        rate_per_hour = 200
        amount = int(round_half_up(float(hours) * rate_per_hour))
        session.add(
            SpecialEducationSubsidy(
                subsidy_type="assistant_hourly",
                employee_id=helper.id,
                related_student_ids=served_ids,
                period_start=period_start,
                period_end=period_end,
                hours_or_rate=hours,
                amount_requested=amount,
                amount_approved=amount,
                status="paid",
                applied_at=_naive_dt(period_end, hour=9),
                approved_at=_naive_dt(period_end + timedelta(days=3), hour=10),
                paid_at=_naive_dt(period_end + timedelta(days=10), hour=14),
                approval_doc_path=None,
                notes="特教助理鐘點(seedgen)",
                created_at=_naive_dt(period_end, hour=9),
                updated_at=_naive_dt(period_end + timedelta(days=10), hour=14),
            )
        )
        n_sub += 1

    if n_sub:
        ctx.log("special_education_subsidies", n_sub)


def _age_group_for_classroom(classroom) -> str | None:
    """由班級年級 age_range 對映月報 age_group;缺則 None。"""
    grade = getattr(classroom, "grade", None)
    age_range = getattr(grade, "age_range", None) if grade else None
    if age_range is None:
        return None
    return _AGE_RANGE_TO_GROUP.get(age_range)


def _seed_monthly_snapshots(ctx: SeedContext, special_ids: set[int]) -> None:
    """每個 closed + in_progress 月 × 每個有學生班級一筆月報快照。

    人數由該班 active 學生即時統計;disability_count 取該班特教幼生數。
    uq(year, month, classroom_id, age_group) 由「每月每班一筆」自然滿足。
    """
    session = ctx.session
    grouped = _classrooms_with_students(ctx)
    if not grouped:
        return

    classroom_by_id = {c.id: c for c in (ctx.classrooms or [])}
    admin_user = None
    for u in (ctx.users or {}).values():
        if getattr(u, "role", None) == "admin":
            admin_user = u
            break
    generated_by = getattr(admin_user, "username", None)

    months = list(ctx.closed_months()) + [ctx.current_month()]
    n_snap = 0

    for year, month in months:
        is_current = (year, month) == ctx.current_month()
        # 快照日:月末(進行中月取 today)。
        from calendar import monthrange

        last_day = monthrange(year, month)[1]
        snap_d = date(year, month, last_day)
        if snap_d > ctx.config.today:
            snap_d = ctx.config.today
        # 該月預計上課工作日數(進行中月截到 today)。
        from ..calendar import workdays as _workdays

        upto = ctx.config.today if is_current else None
        expected_days = len(_workdays(year, month, upto=upto))

        for classroom_id, members in grouped.items():
            classroom = classroom_by_id.get(classroom_id)
            if classroom is None:
                continue
            age_group = _age_group_for_classroom(classroom)
            total = len(members)
            male = sum(1 for s in members if _is_male(s))
            female = total - male
            disability = sum(
                1 for s in members if (getattr(s, "id", None) in special_ids)
            )
            indigenous = sum(
                1
                for s in members
                if getattr(s, "indigenous_status", None)
                not in (None, "", "無", "非原住民")
            )
            foreign = sum(
                1
                for s in members
                if getattr(s, "nationality", None) not in (None, "", "本國")
            )
            # 出席天數:約 92%~98% 出席率(決定論)。
            actual_days = expected_days
            if expected_days:
                miss_ratio = ctx.rng.uniform(0.02, 0.08)
                actual_days = expected_days - int(
                    round_half_up(expected_days * miss_ratio)
                )
                if actual_days < 0:
                    actual_days = 0
            # attendance_rate:百分比 × 100 的整數(模型 comment)。
            if expected_days:
                rate = int(round_half_up(actual_days / expected_days * 100 * 100))
            else:
                rate = 0

            session.add(
                MonthlyEnrollmentSnapshot(
                    year=year,
                    month=month,
                    classroom_id=classroom_id,
                    age_group=age_group,
                    total_count=total,
                    male_count=male,
                    female_count=female,
                    disadvantaged_count=0,
                    disability_count=disability,
                    indigenous_count=indigenous,
                    foreign_count=foreign,
                    expected_attendance_days=expected_days,
                    actual_attendance_days=actual_days,
                    attendance_rate=rate,
                    snapshot_date=snap_d,
                    generated_at=_naive_dt(snap_d, hour=23, minute=0),
                    generated_by=generated_by,
                )
            )
            n_snap += 1

    if n_snap:
        ctx.log("monthly_enrollment_snapshots", n_snap)


def _seed_certificates(ctx: SeedContext) -> None:
    """少量在學證明,seq 於同一開立年內從 1 起遞增(uq(year, seq))。"""
    session = ctx.session
    students = [
        s
        for s in (ctx.students_active or [])
        if getattr(s, "classroom_id", None) is not None
    ]
    if not students:
        students = ctx.students or []
    if not students:
        return

    admin_user = None
    for u in (ctx.users or {}).values():
        if getattr(u, "role", None) == "admin":
            admin_user = u
            break
    issued_by_user_id = getattr(admin_user, "id", None)

    # 開立年:用學年下半(西元)當開立年,seq 從 1 起。
    issue_year = _gregorian_school_year(ctx) + 1  # 如 2026
    n_cert = min(len(students), ctx.rng.randint(5, 8))
    chosen = ctx.rng.sample(students, n_cert)
    issue_date = ctx.config.today

    n = 0
    for i, student in enumerate(chosen, start=1):
        session.add(
            EnrollmentCertificate(
                student_id=student.id,
                year=issue_year,
                seq=i,  # 同 year 內遞增,uq(year, seq) 不碰撞
                purpose=ctx.rng.choice(_CERT_PURPOSES),
                copies=ctx.rng.randint(1, 3),
                issue_date=issue_date,
                issued_by_user_id=issued_by_user_id,
                pdf_path=None,
                created_at=_naive_dt(issue_date, hour=11),
            )
        )
        n += 1

    if n:
        ctx.log("enrollment_certificates", n)


def seed(ctx: SeedContext) -> None:
    """建立特教(IEP/身障文件/補助)、月度在籍快照、在學證明。"""
    special_students = _pick_special_ed_students(ctx)
    special_ids = {int(s.id) for s in special_students if getattr(s, "id", None)}

    _seed_iep_and_docs(ctx, special_students)
    _seed_subsidies(ctx, special_students)
    _seed_monthly_snapshots(ctx, special_ids)
    _seed_certificates(ctx)
