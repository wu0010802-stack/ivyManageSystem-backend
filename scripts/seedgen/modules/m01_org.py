"""m01_org:組織核心實體(employees → classrooms → users),處理員工↔班級

循環 FK 回填,建立已知測試帳號(admin/teacher/parent)與各員工登入帳號。

執行序保證(orchestrator):m00 已落庫 class_grades / job_titles 並寫入
``ctx.class_grades`` / ``ctx.job_titles``;本模組只透過 ctx registry 取依賴。

角色配比(對齊「共享契約」employees_by_role 的 role key):
    supervisor / admin / accountant / homeroom / assistant / art / support
其中 homeroom 數量 = 班級數(scale_profile["classrooms"]),art 為才藝時薪
(employee_type='hourly'),其餘 employee_type='regular'。

循環 FK 處理:Employee.classroom_id 與 Classroom.head_teacher_id 互指。
先 flush employees 取得 id → 建 classroom 並回填 head_teacher_id →
再回填班導 employee.classroom_id。
"""

from __future__ import annotations

from datetime import date, timedelta

from ..context import SeedContext
from ..fake import Faker
from .. import reference_data

# role key → 對應 User.role(角色字串,對齊 utils/permissions.ROLE_TEMPLATES key)。
# accountant/supervisor/admin 有專屬角色;homeroom/assistant/art/support 為一般教職員
# 走 teacher 角色(class-scoped)。
_ROLE_TO_USER_ROLE: dict[str, str] = {
    "supervisor": "supervisor",
    "admin": "admin",
    "accountant": "accountant",
    "homeroom": "teacher",
    "assistant": "teacher",
    "art": "teacher",
    "support": "teacher",
}

# role key → JobTitle 顯示名稱(m00 未提供對應 job_title 時的 fallback 建立用)。
_ROLE_TO_TITLE_NAME: dict[str, str] = {
    "supervisor": "主任",
    "admin": "行政",
    "accountant": "會計",
    "homeroom": "班導師",
    "assistant": "助教",
    "art": "才藝老師",
    "support": "支援人員",
}

# 工號前綴(role key → 兩碼前綴),工號格式 <prefix><序號三碼>。
_ROLE_TO_EMP_PREFIX: dict[str, str] = {
    "supervisor": "SV",
    "admin": "AD",
    "accountant": "AC",
    "homeroom": "HT",
    "assistant": "AT",
    "art": "AR",
    "support": "SP",
}

# role key → Employee.position(職務字串)。**必填,不可留 NULL**:薪資引擎
# calculate_festival_bonus_breakdown / _calculate_bonuses 的第一道閘是
# `if not position: is_eligible=False`(engine.py:2033),position 為空會讓
# 節慶獎金與超額獎金對「所有角色(含主管/帶班/辦公室)」一律歸零。
# 字串需對齊引擎可辨識值:辦公室走 OFFICE_FESTIVAL_BONUS_BASE({司機/美編/行政}),
# 帶班/主管的金額另由 classroom 關聯 / supervisor_role 決定,position 僅供過閘。
_ROLE_TO_POSITION: dict[str, str] = {
    "supervisor": "主任",  # 主管:節慶走 supervisor_role 分支(3500);position 過閘用
    "admin": "行政",  # 辦公室:OFFICE_FESTIVAL_BONUS_BASE['行政']=2000
    "accountant": "行政",  # 會計歸辦公室職員,走辦公室節慶(2000)
    "homeroom": "幼兒園教師",  # 班導:帶班分支走 head_teacher,A 級
    "assistant": "助理教保員",  # 副班導:帶班分支走 assistant_teacher,C 級
    "art": "幼兒園教師",  # 才藝/美語老師:帶班分支走 art_teacher(三級皆 2000)
    "support": "助理教保員",  # 支援人員:無帶班/非主管/非辦公室→節慶 0,position 僅過閘
}

# 已知測試帳號的固定密碼(dev DB 測試用,絕不上 prod)。
_TEST_PASSWORD = "ivytest123"

# 才藝時薪老師目標人數(scale 不足時自動收斂,確保總數不溢出)。
_ART_TARGET = 4
# 支援人員目標人數。
_SUPPORT_TARGET = 2


def _build_role_plan(n_employees: int, n_classrooms: int) -> dict[str, int]:
    """依員工總數與班級數推導各 role 的人數配比。

    固定:1 supervisor / 1 admin / 1 accountant。
    homeroom = 班級數(每班一位班導)。
    art ~ 4、support ~ 2(規模不足時收斂)。
    assistant = 剩餘名額(至少 0),讓總數恰為 n_employees(或盡量逼近)。
    """
    plan: dict[str, int] = {
        "supervisor": 1,
        "admin": 1,
        "accountant": 1,
        "homeroom": n_classrooms,
    }
    fixed = sum(plan.values())
    remaining = max(n_employees - fixed, 0)

    art = min(_ART_TARGET, remaining)
    remaining -= art
    support = min(_SUPPORT_TARGET, remaining)
    remaining -= support
    assistant = remaining  # 其餘全給助教

    plan["assistant"] = assistant
    plan["art"] = art
    plan["support"] = support
    return plan


def _resolve_job_title(ctx: SeedContext, role: str):
    """取得 role 對應的 JobTitle:優先用 m00 寫入 ctx.job_titles 的對照。

    ctx.job_titles 可能以 role key 或職稱顯示名稱為鍵(m00 落庫慣例)。
    依序嘗試:role key → 顯示名稱 → None(交由 caller 不綁 job_title_id)。
    本模組不自行建 JobTitle(那是 m00 的職責),避免重複落庫。
    """
    jt_map = ctx.job_titles or {}
    title_name = _ROLE_TO_TITLE_NAME[role]
    if role in jt_map:
        return jt_map[role]
    if title_name in jt_map:
        return jt_map[title_name]
    return None


def _hire_date(faker: Faker, today: date) -> date:
    """到職日散佈於 today 前 0~6 年(讓年資/年終 proration 有變化)。"""
    days_back = faker.rng.randint(30, 6 * 365)
    return today - timedelta(days=days_back)


def _make_employee(ctx: SeedContext, faker: Faker, role: str, seq: int):
    """建立單一 Employee(尚未 flush)。"""
    from models.employee import Employee, EmployeeType

    gender = faker.rng.choice(["男", "女"])
    name = faker.name(gender)
    is_hourly = role == "art"
    emp_type = EmployeeType.HOURLY.value if is_hourly else EmployeeType.REGULAR.value

    base = reference_data.base_salary_for_role(role)
    # base 可能為 None(director/principal 標準留空) → 用合理預設底薪,避免 NULL 計薪。
    base_salary = base if base is not None else 45000

    job_title = _resolve_job_title(ctx, role)
    prefix = _ROLE_TO_EMP_PREFIX[role]
    employee_id = f"{prefix}{seq:03d}"

    today = ctx.config.today
    emp = Employee(
        employee_id=employee_id,
        name=name,
        id_number=faker.id_number(gender),
        employee_type=emp_type,
        job_title_id=job_title.id if job_title is not None else None,
        title=_ROLE_TO_TITLE_NAME[role],
        gender=gender,
        email=f"{employee_id.lower()}@ivytest.local",
        phone=faker.phone(),
        address=faker.address(),
        hire_date=_hire_date(faker, today),
        birthday=faker.birthday(24, 55, ref=today),
        is_active=True,
        # 月薪制:底薪 + 投保金額(取底薪近似,m06 跑真引擎會以級距解析);時薪制底薪 0。
        base_salary=0 if is_hourly else base_salary,
        hourly_rate=reference_data.ART_TEACHER_HOURLY_RATE if is_hourly else 0,
        insurance_salary_level=0 if is_hourly else base_salary,
        # 職務(必填):空值會讓引擎節慶/超額獎金對全角色歸零(見 _ROLE_TO_POSITION 註解)。
        position=_ROLE_TO_POSITION[role],
        # bonus_grade 刻意留 NULL:引擎以 job_title.name("班導師"/"助教")查 DB
        # grade_map(job_titles.bonus_grade=B)得正確等級。若硬塞 emp.bonus_grade='A',
        # _get_effective_bonus_title 會把職稱改成"幼兒園教師"(不在 grade_map)→
        # fallback 'C' 級、壓低 festival base(head 2000→1500)。
        supervisor_role=("主任" if role == "supervisor" else None),
        dependents=faker.rng.choice([0, 0, 0, 1, 2]),
    )
    return emp


def _make_classroom(ctx: SeedContext, faker: Faker, idx: int, grade, homeroom_emp):
    """建立單一 Classroom(綁 grade、回填 head_teacher_id)。"""
    from models.classroom import Classroom

    name = f"{getattr(grade, 'name', '班')}{idx + 1}班"
    # 以「當前學期」tag 班級,才會出現在 app current_only 過濾(否則班級列表全空)。
    term_year, term_sem = ctx.current_term()
    classroom = Classroom(
        name=name,
        school_year=term_year,
        semester=term_sem,
        grade_id=getattr(grade, "id", None),
        capacity=30,
        head_teacher_id=homeroom_emp.id if homeroom_emp is not None else None,
        class_code=f"C{idx + 1:02d}",
        is_active=True,
    )
    return classroom


def _make_user(faker: Faker, username: str, role: str, employee_id, perms):
    """建立單一 User(固定測試密碼)。"""
    from utils.auth import hash_password
    from models.auth import User

    return User(
        username=username,
        password_hash=hash_password(_TEST_PASSWORD),
        role=role,
        employee_id=employee_id,
        permission_names=perms,
        is_active=True,
        display_name=None,
    )


def seed(ctx: SeedContext) -> None:
    """建立員工 → 班級 → 登入帳號,並回填循環 FK。"""
    from utils.permissions import ROLE_TEMPLATES

    session = ctx.session
    faker = Faker(ctx.rng)
    profile = ctx.config.scale_profile
    n_employees = profile["employees"]
    n_classrooms = profile["classrooms"]

    plan = _build_role_plan(n_employees, n_classrooms)

    # --- 1) 建 Employee(各 role),依 role 累計工號序號 ---
    employees: list = []
    employees_by_role: dict[str, list] = {}
    for role in (
        "supervisor",
        "admin",
        "accountant",
        "homeroom",
        "assistant",
        "art",
        "support",
    ):
        count = plan.get(role, 0)
        bucket: list = []
        for i in range(count):
            emp = _make_employee(ctx, faker, role, seq=i + 1)
            session.add(emp)
            employees.append(emp)
            bucket.append(emp)
        employees_by_role[role] = bucket

    # flush 取得 employee.id(供 classroom.head_teacher_id 與 user.employee_id)。
    session.flush()
    ctx.employees = employees
    ctx.employees_by_role = employees_by_role
    ctx.log("employees", len(employees))

    # --- 2) 建 Classroom,回填 head_teacher_id;再回填班導 classroom_id ---
    homerooms = employees_by_role.get("homeroom", [])
    assistants = employees_by_role.get("assistant", [])
    arts = employees_by_role.get("art", [])
    grades = ctx.class_grades or []

    classrooms: list = []
    for idx in range(n_classrooms):
        homeroom_emp = homerooms[idx] if idx < len(homerooms) else None
        # grade 輪替分配(規模可能班級數 > 年級數)。
        grade = grades[idx % len(grades)] if grades else None
        classroom = _make_classroom(ctx, faker, idx, grade, homeroom_emp)
        # 助教/才藝老師輪替綁班(有則綁)。
        if assistants:
            classroom.assistant_teacher_id = assistants[idx % len(assistants)].id
        if arts:
            classroom.art_teacher_id = arts[idx % len(arts)].id
        session.add(classroom)
        classrooms.append(classroom)

    session.flush()  # 取得 classroom.id 供班導 classroom_id 回填。

    # 回填班導的 classroom_id(Employee.classroom_id 指回所帶班級)。
    for idx, classroom in enumerate(classrooms):
        if idx < len(homerooms):
            homerooms[idx].classroom_id = classroom.id

    ctx.classrooms = classrooms
    ctx.log("classrooms", len(classrooms))

    # --- 3) 建 User:已知測試帳號 admin/teacher/parent + 各員工 staff 帳號 ---
    users: dict[str, object] = {}

    # 3a) admin 已知帳號:綁第一個 admin 員工,wildcard 權限。
    admin_emp = employees_by_role.get("admin", [None])[0]
    admin_user = _make_user(
        faker,
        username="admin",
        role="admin",
        employee_id=admin_emp.id if admin_emp is not None else None,
        perms=list(ROLE_TEMPLATES["admin"]),  # ['*']
    )
    session.add(admin_user)
    users["admin"] = admin_user

    # 3b) teacher 已知帳號:綁第一個班導,teacher 角色模板(class-scoped)。
    teacher_emp = homerooms[0] if homerooms else None
    teacher_user = _make_user(
        faker,
        username="teacher",
        role="teacher",
        employee_id=teacher_emp.id if teacher_emp is not None else None,
        perms=list(ROLE_TEMPLATES["teacher"]),
    )
    session.add(teacher_user)
    users["teacher"] = teacher_user

    # 3c) parent 已知帳號:無員工關聯,parent 角色(無任何 Permission)。
    parent_user = _make_user(
        faker,
        username="parent",
        role="parent",
        employee_id=None,
        perms=list(ROLE_TEMPLATES["parent"]),  # []
    )
    session.add(parent_user)
    users["parent"] = parent_user

    # 3d) 其餘員工各建 staff User(username = 工號小寫;已被已知帳號綁定的員工不重建)。
    bound_emp_ids = {e.id for e in (admin_emp, teacher_emp) if e is not None}
    for role, bucket in employees_by_role.items():
        user_role = _ROLE_TO_USER_ROLE[role]
        # permission_names=None → runtime 走角色模板(DB roles 表為單一來源,
        # NULL 分支 fallback in-code ROLE_TEMPLATES)。此處顯式塞 None 不寫死。
        for emp in bucket:
            if emp.id in bound_emp_ids:
                continue
            username = emp.employee_id.lower()
            staff_user = _make_user(
                faker,
                username=username,
                role=user_role,
                employee_id=emp.id,
                perms=None,
            )
            session.add(staff_user)
            users[username] = staff_user

    ctx.users = users
    ctx.log("users", len(users))
