"""m09_parent:家長端家園溝通資料。

職責:
- 家長 User 帳號:m01 只建一個共用 `parent` 帳號;本模組為部分在籍學生的主要
  監護人各建一個 parent User 並回填 Guardian.user_id(支援一家長多孩語意:
  以 phone 去重,同手機共用同一 User)。
- `parent_message_threads` + `parent_messages`:家長 ↔ 班導 1對1 thread,
  append-only 訊息(parent/teacher 交錯),維護 last_message_at / *_last_read_at。
- `notification_preferences`:稀疏 row 模型,為部分 parent User 關閉少數 event。
- `parent_consent_log`:每位 parent User 對 service_essential 同意(綁 m00
  policy_versions 最新版),少量補 photo_publish / line_push,極少撤回。
- `student_leave_requests`:家長端發起的學生請假(病假/事假),closed 月 approved、
  當月 pending,審核者為該生班導 User。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:policy_versions 已落庫(本模組查回最新版)。
- m01:`ctx.users`(含 admin/teacher/各員工 staff 帳號)、`ctx.classrooms`
  (head_teacher_id 指向班導 Employee)。
- m02:`ctx.students` / `ctx.students_active` / `ctx.guardians`。

時間規則:訊息與請假只生到 closed + in_progress 月份(上限 ctx.config.today),
不生 future。金額無涉(本模組不算錢)。全部走 ctx.rng / Faker,決定論可重現。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from models.consent import (
    CONSENT_SCOPE_LINE_PUSH,
    CONSENT_SCOPE_PHOTO_PUBLISH,
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.parent_message import ParentMessage, ParentMessageThread
from models.parent_notification import (
    PARENT_NOTIFICATION_EVENT_TYPES,
    ParentNotificationPreference,
)
from models.student_leave import StudentLeaveRequest

from ..context import SeedContext
from ..fake import Faker

# 固定測試密碼(對齊 m01 _TEST_PASSWORD;dev DB 測試用,絕不上 prod)。
_TEST_PASSWORD = "ivytest123"

# 家長 ↔ 班導訊息腳本(parent/teacher 交錯,語意自然)。
_THREAD_SCRIPTS: list[list[tuple[str, str]]] = [
    [
        ("parent", "老師早安,今天孩子有點咳嗽,麻煩多留意一下,謝謝!"),
        ("teacher", "好的,我們會特別注意,午休後再回報狀況給您。"),
        ("parent", "麻煩老師了,感恩。"),
    ],
    [
        ("parent", "請問這週的才藝課需要帶什麼用品嗎?"),
        ("teacher", "這週畫畫課,我們園所會準備,家長不用額外帶喔。"),
    ],
    [
        ("teacher", "今天孩子在團體活動表現很棒,主動幫忙收拾玩具,給您分享一下。"),
        ("parent", "謝謝老師告知,回家我們也會鼓勵他。"),
    ],
    [
        ("parent", "老師午安,明天孩子要早退看牙醫,大約三點會來接。"),
        ("teacher", "收到,我們會幫孩子準備好,三點在門口等您。"),
    ],
]

# 家長請假腳本(假別, 原因)。
_LEAVE_POOL: list[tuple[str, str]] = [
    ("病假", "發燒在家休養"),
    ("病假", "腸胃炎就醫"),
    ("病假", "感冒咳嗽"),
    ("事假", "家庭出遊"),
    ("事假", "返鄉探親"),
    ("事假", "家中有事"),
]


def _build_employee_user_map(ctx: SeedContext) -> dict[int, object]:
    """以 Employee.id → User 建立對照(供班級班導 → 班導 User 解析)。

    ctx.users 以 username 為鍵;staff User 的 employee_id 指回 Employee。
    """
    emp_user: dict[int, object] = {}
    for user in (ctx.users or {}).values():
        emp_id = getattr(user, "employee_id", None)
        if emp_id is not None:
            emp_user[emp_id] = user
    return emp_user


def _teacher_user_for_student(
    ctx: SeedContext,
    student,
    emp_user: dict[int, object],
    fallback_teacher,
):
    """解析某學生班級的班導 User;無法解析時退回 fallback(已知 teacher 帳號)。"""
    classroom_id = getattr(student, "classroom_id", None)
    if classroom_id is not None:
        for classroom in ctx.classrooms or []:
            if getattr(classroom, "id", None) == classroom_id:
                head_id = getattr(classroom, "head_teacher_id", None)
                if head_id is not None and head_id in emp_user:
                    return emp_user[head_id]
                break
    return fallback_teacher


def _make_parent_user(faker: Faker, username: str, display_name: str):
    """建立家長 User(parent 角色,無 Permission;固定測試密碼)。"""
    from models.auth import User
    from utils.auth import hash_password

    return User(
        username=username,
        password_hash=hash_password(_TEST_PASSWORD),
        role="parent",
        employee_id=None,
        permission_names=[],  # parent 無任何 Permission(僅走 Portal)
        is_active=True,
        display_name=display_name,
    )


def _months_for_generation(ctx: SeedContext) -> list[tuple[tuple[int, int], bool]]:
    """回傳 [(year, month), is_closed] 清單:closed + 當月(in_progress)。"""
    closed = list(ctx.closed_months())
    current = ctx.current_month()
    result: list[tuple[tuple[int, int], bool]] = [(ym, True) for ym in closed]
    if current not in closed:
        result.append((current, False))
    return result


def seed(ctx: SeedContext) -> None:
    """建立家長帳號/訊息/通知偏好/同意紀錄/家長請假。"""
    session = ctx.session
    rng = ctx.rng
    faker = Faker(rng)

    students_active = list(ctx.students_active or [])
    guardians = list(ctx.guardians or [])
    if not students_active or session is None:
        # 上游尚未就緒(stub 階段或單測)→ 不產生,保持冪等。
        return

    # 已知 teacher 帳號作為 thread/審核 fallback(班導無法解析時用)。
    fallback_teacher = (ctx.users or {}).get("teacher")
    emp_user = _build_employee_user_map(ctx)

    # ── 1) 家長 User:為部分在籍學生的主要監護人建帳號 + 回填 Guardian.user_id ──
    # 以 student_id 索引主要監護人(is_primary 優先,否則取 sort_order 最小者)。
    primary_by_student: dict[int, object] = {}
    for g in guardians:
        sid = getattr(g, "student_id", None)
        if sid is None:
            continue
        cur = primary_by_student.get(sid)
        if cur is None:
            primary_by_student[sid] = g
        elif getattr(g, "is_primary", False) and not getattr(cur, "is_primary", False):
            primary_by_student[sid] = g

    # 同手機共用同一 User(一家長多孩);否則一生一 parent User。
    parent_user_by_phone: dict[str, object] = {}
    parent_users: list[object] = []
    # 取約 70% 在籍學生綁定家長帳號(其餘留未綁,模擬尚未 claim binding_code)。
    bound_students: list[object] = []
    seq = 0
    for student in students_active:
        if rng.random() >= 0.7:
            continue
        guardian = primary_by_student.get(getattr(student, "id", None))
        if guardian is None:
            continue
        phone = getattr(guardian, "phone", None) or f"seed{seq}"
        user = parent_user_by_phone.get(phone)
        if user is None:
            seq += 1
            username = f"parent_{seq:04d}"
            display_name = getattr(guardian, "name", None) or "家長"
            user = _make_parent_user(faker, username, display_name)
            session.add(user)
            parent_user_by_phone[phone] = user
            parent_users.append(user)
            ctx.users[username] = user
        # 回填 Guardian.user_id(SET NULL FK,一 User 可被多筆 Guardian 引用)。
        guardian.user_id = None  # 先佔位,flush 後設 id
        bound_students.append((student, guardian, user))

    session.flush()  # 取得 parent User.id 供 Guardian.user_id / thread / consent

    for _student, guardian, user in bound_students:
        guardian.user_id = user.id

    ctx.log("users", len(parent_users))

    # ── 2) 親師訊息 thread + messages ───────────────────────────────────
    months = _months_for_generation(ctx)
    # 訊息發生月:有 closed 月取最後一個 closed,否則當月。
    closed_months = [ym for ym, is_closed in months if is_closed]
    msg_anchor_month = closed_months[-1] if closed_months else months[-1][0]

    thread_n = 0
    message_n = 0
    seen_triple: set[tuple[int, int, int]] = set()
    for student, guardian, parent_user in bound_students:
        # 約 60% 綁定家長與班導有對話。
        if rng.random() >= 0.6:
            continue
        teacher_user = _teacher_user_for_student(
            ctx, student, emp_user, fallback_teacher
        )
        if teacher_user is None:
            continue
        triple = (
            getattr(parent_user, "id"),
            getattr(teacher_user, "id"),
            getattr(student, "id"),
        )
        # UNIQUE(parent, teacher, student):去重,避免同三元組重複 thread。
        if triple in seen_triple:
            continue
        seen_triple.add(triple)

        script = rng.choice(_THREAD_SCRIPTS)
        # thread 起始時間:錨定月內某工作日上午。
        ay, am = msg_anchor_month
        day = min(rng.randint(3, 25), 28)
        base_dt = datetime(ay, am, day, 8, 30) + timedelta(minutes=rng.randint(0, 240))

        thread = ParentMessageThread(
            parent_user_id=parent_user.id,
            teacher_user_id=teacher_user.id,
            student_id=student.id,
        )
        session.add(thread)
        session.flush()  # 取得 thread.id 供 messages FK
        thread_n += 1

        last_dt = base_dt
        last_parent_read = None
        last_teacher_read = None
        for offset, (role, body) in enumerate(script):
            msg_dt = base_dt + timedelta(minutes=offset * 7)
            last_dt = msg_dt
            sender_user = parent_user if role == "parent" else teacher_user
            session.add(
                ParentMessage(
                    thread_id=thread.id,
                    sender_user_id=sender_user.id,
                    sender_role=role,
                    body=body,
                    client_request_id=f"seed-msg-{thread.id}-{offset}",
                    source="app",
                    created_at=msg_dt,
                )
            )
            message_n += 1
            # 對方讀取時間推進(收到後不久已讀)。
            if role == "parent":
                last_teacher_read = msg_dt + timedelta(minutes=3)
            else:
                last_parent_read = msg_dt + timedelta(minutes=5)

        thread.last_message_at = last_dt
        thread.parent_last_read_at = last_parent_read
        thread.teacher_last_read_at = last_teacher_read

    ctx.log("parent_message_threads", thread_n)
    ctx.log("parent_messages", message_n)

    # ── 3) 通知偏好(稀疏 row):約 30% parent User 關閉 1~2 個 event ────────
    pref_n = 0
    for user in parent_users:
        if rng.random() >= 0.3:
            continue
        # 從事件型別中抽 1~2 個關閉(其餘缺 row = 預設開啟)。
        n_off = rng.randint(1, 2)
        events = rng.sample(
            list(PARENT_NOTIFICATION_EVENT_TYPES),
            min(n_off, len(PARENT_NOTIFICATION_EVENT_TYPES)),
        )
        for event_type in events:
            session.add(
                ParentNotificationPreference(
                    user_id=user.id,
                    event_type=event_type,
                    channel="line",
                    enabled=False,
                )
            )
            pref_n += 1
    ctx.log("notification_preferences", pref_n)

    # ── 4) 同意紀錄:每位 parent User 對 service_essential 同意(綁最新政策版本) ──
    latest_policy = (
        session.query(PolicyVersion)
        .order_by(PolicyVersion.effective_at.desc(), PolicyVersion.id.desc())
        .first()
    )
    consent_n = 0
    if latest_policy is not None:
        for user in parent_users:
            # 同意時間:政策生效後、稍後不久(模擬登入時重簽)。
            consent_dt = latest_policy.effective_at + timedelta(
                days=rng.randint(0, 30), hours=rng.randint(0, 23)
            )
            # 4a) service_essential 必同意(LIFF 登入前置)。
            session.add(
                ParentConsentLog(
                    user_id=user.id,
                    policy_version_id=latest_policy.id,
                    scope=CONSENT_SCOPE_SERVICE_ESSENTIAL,
                    consented=True,
                    consented_at=consent_dt,
                    ip_address="203.0.113." + str(rng.randint(1, 254)),
                    user_agent="Mozilla/5.0 (LIFF seedgen)",
                )
            )
            consent_n += 1
            # 4b) ~70% 同意 photo_publish。
            if rng.random() < 0.7:
                session.add(
                    ParentConsentLog(
                        user_id=user.id,
                        policy_version_id=latest_policy.id,
                        scope=CONSENT_SCOPE_PHOTO_PUBLISH,
                        consented=True,
                        consented_at=consent_dt + timedelta(seconds=5),
                        ip_address="203.0.113." + str(rng.randint(1, 254)),
                        user_agent="Mozilla/5.0 (LIFF seedgen)",
                    )
                )
                consent_n += 1
            # 4c) ~50% 同意 line_push;其中極少數後續撤回(再寫一筆 consented=False)。
            if rng.random() < 0.5:
                session.add(
                    ParentConsentLog(
                        user_id=user.id,
                        policy_version_id=latest_policy.id,
                        scope=CONSENT_SCOPE_LINE_PUSH,
                        consented=True,
                        consented_at=consent_dt + timedelta(seconds=10),
                        ip_address="203.0.113." + str(rng.randint(1, 254)),
                        user_agent="Mozilla/5.0 (LIFF seedgen)",
                    )
                )
                consent_n += 1
                if rng.random() < 0.1:
                    session.add(
                        ParentConsentLog(
                            user_id=user.id,
                            policy_version_id=latest_policy.id,
                            scope=CONSENT_SCOPE_LINE_PUSH,
                            consented=False,
                            consented_at=consent_dt + timedelta(days=20),
                            note="家長於設定關閉 LINE 推播",
                        )
                    )
                    consent_n += 1
    ctx.log("parent_consent_log", consent_n)

    # ── 5) 學生請假(家長端發起):病假/事假,closed→approved、當月→pending ──
    today = ctx.config.today
    leave_n = 0
    for ym, is_closed in months:
        y, m = ym
        status = "approved" if is_closed else "pending"
        # 該月可請假的最後一日(當月截到 today)。
        upper = today if not is_closed else _month_end(y, m)
        for student, guardian, parent_user in bound_students:
            # 約 20% 綁定學生該月有一次家長請假。
            if rng.random() >= 0.2:
                continue
            leave_type, reason = rng.choice(_LEAVE_POOL)
            start = _pick_weekday(y, m, upper, rng)
            if start is None:
                continue
            # 1~2 日請假(end 不超過 upper)。
            span = rng.randint(0, 1)
            end = min(start + timedelta(days=span), upper)
            reviewer = _teacher_user_for_student(
                ctx, student, emp_user, fallback_teacher
            )
            reviewed_at = None
            reviewed_by = None
            if status == "approved" and reviewer is not None:
                reviewed_by = reviewer.id
                reviewed_at = datetime(start.year, start.month, start.day, 9, 0)
            session.add(
                StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=parent_user.id,
                    applicant_guardian_id=getattr(guardian, "id", None),
                    leave_type=leave_type,
                    start_date=start,
                    end_date=end,
                    reason=reason,
                    status=status,
                    reviewed_by=reviewed_by,
                    reviewed_at=reviewed_at,
                    review_note="家長申請,已核准" if status == "approved" else None,
                    client_request_id=f"seed-leave-{student.id}-{y}{m:02d}",
                )
            )
            leave_n += 1
    ctx.log("student_leave_requests", leave_n)


def _month_end(year: int, month: int) -> date:
    """回傳該月末日(避免額外 import calendar 工具)。"""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _pick_weekday(year: int, month: int, upper: date, rng) -> date | None:
    """在 [該月首日, upper] 範圍內隨機挑一個工作日(週一~週五)。"""
    first = date(year, month, 1)
    if upper < first:
        return None
    candidates: list[date] = []
    d = first
    while d <= upper:
        if d.weekday() < 5:
            candidates.append(d)
        d += timedelta(days=1)
    if not candidates:
        return None
    return rng.choice(candidates)
