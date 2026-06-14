"""m02_students:招生訪視 → 學生(lifecycle 狀態機,經 set_lifecycle_status)

→ 監護人(含 PII)與綁定碼。學生分配到班級,涵蓋各 lifecycle 狀態。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`ctx.config`(規模/學年/today)
- m01:`ctx.classrooms`(已建班級,回填過 homeroom)、`ctx.users`(含一個 role='admin')

產出(寫入 ctx registry,供後續模組使用):
- `ctx.students`:全體學生
- `ctx.students_active`:lifecycle_status == 'active' 的學生
- `ctx.guardians`:全體監護人

設計重點:
- 先建 RecruitmentVisit(招生訪視)作為 Student.recruitment_visit_id 的 FK 來源,
  每個 visit 至多對一個 student(partial unique index uq_students_recruitment_visit_id)。
- 學生 lifecycle 多數 active,少量 enrolled/on_leave/prospect/withdrawn/graduated;
  **所有非 active 的狀態一律經 utils.student_lifecycle.set_lifecycle_status 變更**
  (禁 raw 指派),以維護 terminal_entered_at 與 audit_log。
- 每生 1~2 位 guardians(含 phone/email/name PII)+ 少量 guardian_binding_codes。
- 全部走 ctx.rng / Faker,決定論可重現。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from models.classroom import Student
from models.guardian import Guardian
from models.parent_binding import GuardianBindingCode
from models.recruitment import RecruitmentVisit
from utils.student_lifecycle import set_lifecycle_status

from ..context import SeedContext
from ..fake import Faker

# lifecycle 目標分佈:大多數 active,其餘狀態各保留少量以涵蓋各態。
# 以「每 N 名學生」的權重切片(順序決定論),總和必涵蓋計畫要求的至少 4 種值。
# 鍵為 lifecycle_status,值為相對權重(整數)。
_LIFECYCLE_WEIGHTS: list[tuple[str, int]] = [
    ("active", 80),  # 正式在學(多數)
    ("enrolled", 5),  # 已報到未開學
    ("on_leave", 4),  # 休學
    ("prospect", 4),  # 招生訪視中
    ("withdrawn", 4),  # 退學(終態)
    ("graduated", 3),  # 畢業(終態)
]

# 監護人關係與性別(母=女、父=男)對照,供 Faker 取對應性別姓名/身分證。
_GUARDIAN_ROLES = [
    ("母親", "F", True),  # (relation, gender, is_primary 候選)
    ("父親", "M", False),
]


def _build_lifecycle_plan(total: int, rng) -> list[str]:
    """回傳長度 total 的 lifecycle_status 序列(決定論)。

    依 _LIFECYCLE_WEIGHTS 配額切片,先各狀態保證至少 1 名(只要 total 夠),
    其餘補 active;最後以 rng 洗牌打散,避免同班全同狀態。
    """
    statuses: list[str] = []
    non_active = [(s, w) for s, w in _LIFECYCLE_WEIGHTS if s != "active"]
    # 各非 active 狀態先保底:total 較小時等比縮減,至少 0。
    for status, weight in non_active:
        n = max(0, round(total * weight / 100))
        # 至少給 1 名(只要還有額度),確保 lifecycle_status 至少 4 種值。
        if n == 0 and total >= 12:
            n = 1
        statuses.extend([status] * n)
    # 其餘補 active(下限保證 active 仍為多數)。
    remaining = total - len(statuses)
    if remaining < 0:
        # 規模極小時非 active 配額溢出,截斷並補足 active。
        statuses = statuses[:total]
        remaining = 0
    statuses.extend(["active"] * remaining)
    rng.shuffle(statuses)
    return statuses


def _make_recruitment_visit(
    ctx: SeedContext,
    fake: Faker,
    child_name: str,
    birthday,
    grade_name: str | None,
    phone: str,
    address: str,
) -> RecruitmentVisit:
    """建立一筆招生訪視作為 Student 的 FK 來源。

    month 用民國月份字串(如 "114.08"),散佈在學年起日後數月,
    has_deposit/enrolled 旗標多數為已預繳已報到(對齊在學學生)。
    """
    cfg = ctx.config
    # 訪視月份:學年起日往前數月到學年起日後幾個月之間(招生通常開學前)。
    offset_months = ctx.rng.randint(-6, 1)
    visit_dt = cfg.year_start + timedelta(days=offset_months * 30)
    roc_year = visit_dt.year - 1911
    month_str = f"{roc_year}.{visit_dt.month:02d}"
    return RecruitmentVisit(
        month=month_str,
        seq_no=str(ctx.rng.randint(1, 99)),
        visit_date=visit_dt.isoformat(),
        child_name=child_name,
        birthday=birthday,
        grade=grade_name,
        phone=phone,
        address=address,
        district=fake.rng.choice(["中正", "信義", "大安", "中山", "松山"]),
        source=fake.rng.choice(["親友介紹", "官網", "路過", "社群"]),
        has_deposit=True,
        enrolled=True,
        created_at=datetime.combine(visit_dt, datetime.min.time()),
    )


def seed(ctx: SeedContext) -> None:
    """建立招生訪視/學生(lifecycle 狀態機)/監護人。"""
    cfg = ctx.config
    fake = Faker(ctx.rng)
    n_students = cfg.scale_profile["students"]

    # m01 已建班級;若上游尚未就緒(理論上 orchestrator 保證),退化為空清單,
    # 仍可建學生(classroom_id 留 None),不致整體中斷。
    classrooms = list(ctx.classrooms or [])

    # 取一個 admin user 當 lifecycle 變更的 actor(audit_log user_id);無則 None。
    admin_user = None
    for user in (ctx.users or {}).values():
        if getattr(user, "role", None) == "admin":
            admin_user = user
            break
    actor_user_id = getattr(admin_user, "id", None) if admin_user else None

    lifecycle_plan = _build_lifecycle_plan(n_students, ctx.rng)

    students: list[Student] = []
    visits: list[RecruitmentVisit] = []

    enroll_year = cfg.academic_year  # 民國學年(發號學年)
    for idx in range(n_students):
        gender_token = ctx.rng.choice(["男", "女"])
        gender_for_id = "M" if gender_token == "男" else "F"
        name = fake.name(gender_for_id)
        # 幼兒園學生年齡約 3~6 歲。
        birthday = fake.birthday(3, 6, ref=cfg.today)
        phone = fake.phone()
        address = fake.address()

        # 班級分配:round-robin 攤平(每班 ~24)。
        classroom = classrooms[idx % len(classrooms)] if classrooms else None
        classroom_id = getattr(classroom, "id", None) if classroom else None
        grade_name = None
        if classroom is not None:
            grade = getattr(classroom, "grade", None)
            grade_name = getattr(grade, "name", None)

        # 先建招生訪視(FK 來源)。
        visit = _make_recruitment_visit(
            ctx, fake, name, birthday, grade_name, phone, address
        )
        ctx.session.add(visit)
        ctx.session.flush()  # 取得 visit.id 供 Student FK
        visits.append(visit)

        # 學號:S + 學年(3) + 班序(2) + 流水(3),決定論不碰撞。
        class_seq = (idx % len(classrooms) + 1) if classrooms else 0
        sid = f"S{enroll_year:03d}{class_seq:02d}{idx + 1:03d}"

        # 入學日:在學學生散佈於學年起日前後;具體狀態日後由 lifecycle 決定。
        enrollment_date = cfg.year_start - timedelta(days=ctx.rng.randint(0, 400))

        target_status = lifecycle_plan[idx]
        # prospect(訪視中尚未報到)語意上不該掛班級;其餘掛班級。
        student_classroom_id = None if target_status == "prospect" else classroom_id

        student = Student(
            student_id=sid,
            name=name,
            gender=gender_token,
            birthday=birthday,
            classroom_id=student_classroom_id,
            enrollment_date=enrollment_date,
            # lifecycle 一律先建為預設 active,非 active 者下方走 util 變更。
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
            enrollment_school_year=enroll_year,
            enrollment_seq=idx + 1,  # 永久流水號,(year, seq) 唯一
            parent_name=None,
            parent_phone=phone,
            address=address,
            id_number=fake.id_number(gender_for_id),
            nationality="本國",
            is_active=(target_status in ("active", "enrolled", "on_leave")),
            status="在學" if target_status == "active" else None,
        )
        ctx.session.add(student)
        students.append(student)

    # flush 取得 student.id(guardians / lifecycle audit 需要)。
    ctx.session.flush()

    # ---- lifecycle 變更:非 active 一律走 set_lifecycle_status ----
    for student, target in zip(students, lifecycle_plan):
        if target == "active":
            continue
        set_lifecycle_status(
            ctx.session,
            student,
            target,
            actor_user_id=actor_user_id,
            audit=True,
            reason="seedgen 初始化 lifecycle 分佈",
        )

    # ---- guardians(每生 1~2 位,含 PII)+ 少量 binding_codes ----
    guardians: list[Guardian] = []
    binding_codes: list[GuardianBindingCode] = []
    for student in students:
        # 第一位必為主要聯絡人(母親);60% 再補父親。
        n_guardians = 2 if ctx.rng.random() < 0.6 else 1
        for order in range(n_guardians):
            relation, g_gender, is_primary = _GUARDIAN_ROLES[order]
            guardian = Guardian(
                student_id=student.id,
                user_id=None,  # 未綁定(綁定靠 binding_code claim)
                name=fake.name(g_gender),
                phone=fake.phone(),
                email=f"guardian{student.id}_{order}@example.test",
                relation=relation,
                is_primary=is_primary,
                is_emergency=True,
                can_pickup=True,
                sort_order=order + 1,
            )
            ctx.session.add(guardian)
            guardians.append(guardian)

    ctx.session.flush()  # 取得 guardian.id 供 binding_code FK

    # 少量綁定碼:約每 8 位 guardian 簽發 1 張(未使用、24h 後過期)。
    # created_by 須為合法 User(行政);無 admin 時跳過(FK NOT NULL)。
    if actor_user_id is not None:
        now = datetime.now()
        for guardian in guardians:
            if ctx.rng.random() < 0.125:  # ~1/8
                raw = f"seed-bind-{guardian.id}-{ctx.rng.randint(0, 10**8)}"
                code_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                binding_codes.append(
                    GuardianBindingCode(
                        guardian_id=guardian.id,
                        code_hash=code_hash,
                        expires_at=now + timedelta(hours=24),
                        used_at=None,
                        used_by_user_id=None,
                        created_by=actor_user_id,
                    )
                )
        if binding_codes:
            ctx.session.add_all(binding_codes)

    # ---- 寫入 registry ----
    ctx.students = students
    ctx.students_active = [s for s in students if s.lifecycle_status == "active"]
    ctx.guardians = guardians

    # ---- log 筆數 ----
    ctx.log("recruitment_visits", len(visits))
    ctx.log("students", len(students))
    ctx.log("guardians", len(guardians))
    if binding_codes:
        ctx.log("guardian_binding_codes", len(binding_codes))
