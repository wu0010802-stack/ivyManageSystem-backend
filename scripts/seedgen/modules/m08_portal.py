"""m08_portal:公告(含收件人/已讀/家長收件人)、聯絡簿(含 ack/reply)、

放學接送、學校行事曆、工作日覆寫、行事曆確認。
(園務會議 MeetingRecord 已移至 m04_leave_ot——會議是薪資輸入,須在 m06 之前產生。)

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`ctx.config`(學年/today/closed_months)
- m01:`ctx.employees`、`ctx.employees_by_role`、`ctx.classrooms`、`ctx.users`
       (含 username='admin'/'teacher'/'parent' 已知帳號)
- m02:`ctx.students`、`ctx.students_active`、`ctx.guardians`

時間規則(對齊計畫):只生到 closed + in_progress 月份(上限 config.today),
不生 future。closed 月資料視為「已完成/已核/已讀」;in_progress 月(2026-02)
留部分 pending(接送單 pending、聯絡簿草稿、家長未讀)。

設計重點:
- 公告(announcements)>0、聯絡簿(student_contact_book_entries)>0(計畫硬要求)。
- 公告收件人分兩軌:員工端 AnnouncementRecipient(空=全員可見)+
  家長端 AnnouncementParentRecipient(scope all/classroom/student)。
- 公告已讀:員工端 AnnouncementRead + 家長端 AnnouncementParentRead。
- 聯絡簿每位 active 學生在 closed/部分日期一筆;已發布者家長補 ack/reply。
- 放學接送(StudentDismissalCall):closed 月已完成,當月留部分 pending。
- 行事曆(SchoolEvent)+ 工作日覆寫(WorkdayOverride)+ 家長簽閱
  (EventAcknowledgment,需 requires_acknowledgment 事件)。
- 家長端(users role='parent')目前僅一個已知帳號;家長面紀錄(ack/reply/
  parent_read/event_ack)一律掛在此 parent user,並對映到其可代表的學生/班級,
  以維持 FK 合法且語意一致。
- 全部走 ctx.rng,決定論可重現;naive datetime(對齊既有欄位 now_taipei_naive)。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from models.contact_book import (
    CONTACT_BOOK_BOWEL,
    CONTACT_BOOK_MOODS,
    StudentContactBookAck,
    StudentContactBookEntry,
    StudentContactBookReply,
)
from models.dismissal import StudentDismissalCall
from models.event import (
    Announcement,
    AnnouncementParentRead,
    AnnouncementParentRecipient,
    AnnouncementRead,
    AnnouncementRecipient,
    EventAcknowledgment,
    SchoolEvent,
    WorkdayOverride,
)

from ..context import SeedContext

# 公告範本(title, content, priority)。priority 自由字串 normal/important/urgent。
_ANNOUNCEMENT_TEMPLATES: list[tuple[str, str, str]] = [
    ("開學注意事項", "新學期開始,請家長協助孩子調整作息,準時到園。", "important"),
    ("流感季節防疫提醒", "近期流感盛行,請落實勤洗手,有發燒症狀請在家休息。", "urgent"),
    ("親子運動會通知", "本園將舉辦親子運動會,歡迎家長踴躍參加。", "normal"),
    ("月底繳費提醒", "本月學雜費請於月底前完成繳納,謝謝配合。", "normal"),
    (
        "校外教學行前說明",
        "下週校外教學,請依清單為孩子準備物品並準時集合。",
        "important",
    ),
    ("園所放假公告", "依行事曆本週五為彈性放假日,當日不上課。", "normal"),
    ("健康檢查通知", "本月將進行幼兒健康檢查,請填妥同意書後繳回。", "important"),
    ("才藝課程開放報名", "本學期才藝課程開放報名,額滿為止,請把握。", "normal"),
]

# 學校行事曆事件範本(title, event_type, requires_ack)。
_EVENT_TEMPLATES: list[tuple[str, str, bool]] = [
    ("園務會議", "meeting", False),
    ("親子運動會", "activity", True),
    ("中秋節放假", "holiday", False),
    ("校外教學", "activity", True),
    ("教學觀摩日", "general", False),
    ("期末成果發表", "activity", True),
]

# 聯絡簿教師備註範本。
_CONTACT_NOTES: list[str] = [
    "今天午餐吃得很好,午睡也很安穩。",
    "上課很專心,主動舉手回答問題。",
    "與同學相處融洽,分享玩具給朋友。",
    "今天有點想家,午睡後情緒就穩定了。",
    "下午點心吃得不多,請家長留意。",
]

# 聯絡簿家長回覆範本。
_CONTACT_REPLIES: list[str] = [
    "謝謝老師的用心照顧!",
    "好的,我們在家會多注意。",
    "辛苦老師了,孩子回家很開心。",
    "收到,謝謝通知。",
]


def _naive_dt(d: date, hour: int = 9, minute: int = 0) -> datetime:
    """以日期 + 時分組出 naive datetime(對齊既有欄位語意)。"""
    return datetime.combine(d, time(hour=hour, minute=minute))


def _parent_user(ctx: SeedContext):
    """取一個 role='parent' 的 User 當家長面紀錄 actor;無則 None。"""
    for user in (ctx.users or {}).values():
        if getattr(user, "role", None) == "parent":
            return user
    return None


def _admin_employee(ctx: SeedContext):
    """取一個發佈公告/會議用的 employee(優先 admin,其次任意);無則 None。"""
    by_role = ctx.employees_by_role or {}
    for key in ("admin", "supervisor", "accountant"):
        bucket = by_role.get(key) or []
        if bucket:
            return bucket[0]
    emps = ctx.employees or []
    return emps[0] if emps else None


def _seed_announcements(ctx: SeedContext, author_emp, parent_user) -> None:
    """建公告 + 員工/家長收件人 + 員工/家長已讀。

    closed 月公告已被多數員工讀過;in_progress 月留部分未讀。
    家長端收件人涵蓋 all / classroom / student 三種 scope。
    """
    session = ctx.session
    if author_emp is None:
        return  # created_by NOT NULL,無 employee 則整段跳過(理論上 m01 已建)

    closed = ctx.closed_months()
    months = list(closed) + [ctx.current_month()]  # 含進行中月
    employees = ctx.employees or []
    classrooms = ctx.classrooms or []
    students = ctx.students_active or ctx.students or []

    n_ann = 0
    n_emp_recipient = 0
    n_emp_read = 0
    n_parent_recipient = 0
    n_parent_read = 0

    # 每月發 1~2 則公告。
    for mi, (year, month) in enumerate(months):
        is_current = (year, month) == ctx.current_month()
        # 該月發布日:月中某個工作日。
        publish_day = min(ctx.rng.randint(5, 20), 28)
        publish_date = date(year, month, publish_day)
        if publish_date > ctx.config.today:
            publish_date = ctx.config.today
        n_this_month = ctx.rng.randint(1, 2)
        for k in range(n_this_month):
            tmpl = _ANNOUNCEMENT_TEMPLATES[(mi * 2 + k) % len(_ANNOUNCEMENT_TEMPLATES)]
            title, content, priority = tmpl
            created = _naive_dt(publish_date, hour=8, minute=30)
            ann = Announcement(
                title=f"{title}({year}/{month:02d})",
                content=content,
                priority=priority,
                is_pinned=(priority == "urgent" and k == 0),
                publish_at=created,
                expires_at=None,
                created_by=author_emp.id,
                created_at=created,
                updated_at=created,
            )
            session.add(ann)
            session.flush()  # 取得 ann.id 供 recipient/read FK
            n_ann += 1

            # 員工端收件人:約半數公告指定部分收件人,其餘留空(=全員可見)。
            if employees and ctx.rng.random() < 0.5:
                k_recip = min(len(employees), ctx.rng.randint(2, 5))
                chosen = ctx.rng.sample(employees, k_recip)
                for emp in chosen:
                    session.add(
                        AnnouncementRecipient(
                            announcement_id=ann.id, employee_id=emp.id
                        )
                    )
                    n_emp_recipient += 1

            # 員工端已讀:closed 月多數已讀;in_progress 月僅少數。
            read_ratio = 0.3 if is_current else 0.8
            for emp in employees:
                if ctx.rng.random() < read_ratio:
                    session.add(
                        AnnouncementRead(
                            announcement_id=ann.id,
                            employee_id=emp.id,
                            read_at=_naive_dt(
                                publish_date, hour=ctx.rng.randint(9, 17)
                            ),
                        )
                    )
                    n_emp_read += 1

            # 家長端收件人:scope 輪替 all / classroom / student。
            scope = ("all", "classroom", "student")[(mi * 2 + k) % 3]
            classroom_id = None
            student_id = None
            if scope == "classroom" and classrooms:
                classroom_id = ctx.rng.choice(classrooms).id
            elif scope == "student" and students:
                student_id = ctx.rng.choice(students).id
            else:
                scope = "all"
            session.add(
                AnnouncementParentRecipient(
                    announcement_id=ann.id,
                    scope=scope,
                    classroom_id=classroom_id,
                    student_id=student_id,
                    guardian_id=None,
                )
            )
            n_parent_recipient += 1

            # 家長端已讀(只掛一個已知 parent user;closed 月多已讀)。
            if parent_user is not None and not is_current:
                if ctx.rng.random() < 0.7:
                    session.add(
                        AnnouncementParentRead(
                            announcement_id=ann.id,
                            user_id=parent_user.id,
                            read_at=_naive_dt(
                                publish_date, hour=ctx.rng.randint(18, 21)
                            ),
                        )
                    )
                    n_parent_read += 1

    ctx.log("announcements", n_ann)
    if n_emp_recipient:
        ctx.log("announcement_recipients", n_emp_recipient)
    if n_emp_read:
        ctx.log("announcement_reads", n_emp_read)
    if n_parent_recipient:
        ctx.log("announcement_parent_recipients", n_parent_recipient)
    if n_parent_read:
        ctx.log("announcement_parent_reads", n_parent_read)


def _seed_contact_book(ctx: SeedContext, parent_user) -> None:
    """每位 active 學生在抽樣日期建聯絡簿,closed 已發布並補家長 ack/reply。

    為避免逐日爆量(規模 standard 170 生 × 全學年工作日),每月抽 2~3 個工作日,
    且只對有班級的在籍學生建。in_progress 月保留草稿(published_at=None)。
    """
    session = ctx.session
    students = [
        s
        for s in (ctx.students_active or [])
        if getattr(s, "classroom_id", None) is not None
    ]
    if not students:
        # 退化:無 active 學生則用任意有班級學生,確保聯絡簿 > 0。
        students = [
            s
            for s in (ctx.students or [])
            if getattr(s, "classroom_id", None) is not None
        ]
    if not students:
        return

    closed = ctx.closed_months()
    months = list(closed) + [ctx.current_month()]

    n_entry = 0
    n_ack = 0
    n_reply = 0

    # 取每位學生班導當建立者(employee);無則留 None(欄位 nullable)。
    homeroom_by_classroom: dict[int, int] = {}
    for c in ctx.classrooms or []:
        ht = getattr(c, "head_teacher_id", None)
        if ht is not None:
            homeroom_by_classroom[c.id] = ht

    parent_user_id = getattr(parent_user, "id", None)

    for year, month in months:
        is_current = (year, month) == ctx.current_month()
        # 該月工作日,截到 today(進行中月)。
        from ..calendar import workdays as _workdays

        upto = ctx.config.today if is_current else None
        wd = _workdays(year, month, upto=upto)
        if not wd:
            continue
        # 每月抽 2~3 天(決定論)。
        n_days = min(len(wd), ctx.rng.randint(2, 3))
        sampled = sorted(ctx.rng.sample(wd, n_days))

        for log_date in sampled:
            for student in students:
                classroom_id = student.classroom_id
                created_emp = homeroom_by_classroom.get(classroom_id)
                # in_progress 月:約一半留草稿(published_at=None)。
                published = None
                if not is_current or ctx.rng.random() < 0.5:
                    published = _naive_dt(log_date, hour=16, minute=0)
                entry = StudentContactBookEntry(
                    student_id=student.id,
                    classroom_id=classroom_id,
                    log_date=log_date,
                    mood=ctx.rng.choice(CONTACT_BOOK_MOODS),
                    meal_lunch=ctx.rng.randint(0, 3),
                    meal_snack=ctx.rng.randint(0, 3),
                    nap_minutes=ctx.rng.choice([0, 30, 60, 90, 120]),
                    bowel=ctx.rng.choice(CONTACT_BOOK_BOWEL),
                    temperature_c=round(36.0 + ctx.rng.randint(0, 15) / 10.0, 1),
                    teacher_note=ctx.rng.choice(_CONTACT_NOTES),
                    learning_highlight=None,
                    created_by_employee_id=created_emp,
                    published_at=published,
                    version=1,
                    deleted_at=None,
                    created_at=_naive_dt(log_date, hour=15, minute=30),
                    updated_at=_naive_dt(log_date, hour=15, minute=30),
                )
                session.add(entry)
                n_entry += 1

                # 已發布者:家長 ack + 部分 reply(掛已知 parent user)。
                if published is not None and parent_user_id is not None:
                    session.flush()  # 取得 entry.id
                    if ctx.rng.random() < 0.6:
                        session.add(
                            StudentContactBookAck(
                                entry_id=entry.id,
                                guardian_user_id=parent_user_id,
                                read_at=_naive_dt(log_date, hour=20, minute=0),
                            )
                        )
                        n_ack += 1
                    if ctx.rng.random() < 0.25:
                        session.add(
                            StudentContactBookReply(
                                entry_id=entry.id,
                                guardian_user_id=parent_user_id,
                                body=ctx.rng.choice(_CONTACT_REPLIES),
                                client_request_id=None,
                                deleted_at=None,
                                created_at=_naive_dt(log_date, hour=20, minute=30),
                            )
                        )
                        n_reply += 1

    ctx.log("student_contact_book_entries", n_entry)
    if n_ack:
        ctx.log("student_contact_book_acks", n_ack)
    if n_reply:
        ctx.log("student_contact_book_replies", n_reply)


def _seed_dismissal_calls(ctx: SeedContext, parent_user) -> None:
    """放學接送單:抽樣日期 × 部分學生;closed 月已完成,當月留 pending。"""
    session = ctx.session
    parent_user_id = getattr(parent_user, "id", None)
    if parent_user_id is None:
        return  # requested_by_user_id NOT NULL

    students = [
        s
        for s in (ctx.students_active or [])
        if getattr(s, "classroom_id", None) is not None
    ]
    if not students:
        return

    by_role = ctx.employees_by_role or {}
    homeroom_emps = by_role.get("homeroom") or []
    ack_emp_id = homeroom_emps[0].id if homeroom_emps else None
    if ack_emp_id is None:
        emps = ctx.employees or []
        ack_emp_id = emps[0].id if emps else None

    closed = ctx.closed_months()
    months = list(closed) + [ctx.current_month()]

    n_call = 0
    for year, month in months:
        is_current = (year, month) == ctx.current_month()
        from ..calendar import workdays as _workdays

        upto = ctx.config.today if is_current else None
        wd = _workdays(year, month, upto=upto)
        if not wd:
            continue
        # 每月抽 1~2 天。
        n_days = min(len(wd), ctx.rng.randint(1, 2))
        sampled = ctx.rng.sample(wd, n_days)
        for call_date in sampled:
            # 抽部分學生(每天 ~10 名)發接送單。
            k = min(len(students), 10)
            chosen = ctx.rng.sample(students, k)
            for student in chosen:
                requested_at = _naive_dt(call_date, hour=15, minute=30)
                if is_current and ctx.rng.random() < 0.5:
                    # 當月部分留 pending(未確認)。
                    status = "pending"
                    ack_at = None
                    ack_by = None
                    comp_at = None
                    comp_by = None
                else:
                    status = "completed"
                    ack_at = _naive_dt(call_date, hour=15, minute=35)
                    ack_by = ack_emp_id
                    comp_at = _naive_dt(call_date, hour=16, minute=0)
                    comp_by = ack_emp_id
                session.add(
                    StudentDismissalCall(
                        student_id=student.id,
                        classroom_id=student.classroom_id,
                        requested_by_user_id=parent_user_id,
                        requested_at=requested_at,
                        status=status,
                        acknowledged_by_employee_id=ack_by,
                        acknowledged_at=ack_at,
                        completed_by_employee_id=comp_by,
                        completed_at=comp_at,
                        note=None,
                    )
                )
                n_call += 1

    if n_call:
        ctx.log("student_dismissal_calls", n_call)


def _seed_school_events(ctx: SeedContext, parent_user) -> None:
    """學校行事曆事件 + 工作日覆寫 + 需簽閱事件的家長簽閱紀錄。"""
    session = ctx.session
    parent_user_id = getattr(parent_user, "id", None)
    students = ctx.students_active or ctx.students or []

    closed = ctx.closed_months()
    months = list(closed) + [ctx.current_month()]

    n_event = 0
    n_ack = 0
    for mi, (year, month) in enumerate(months):
        is_current = (year, month) == ctx.current_month()
        tmpl = _EVENT_TEMPLATES[mi % len(_EVENT_TEMPLATES)]
        title, event_type, requires_ack = tmpl
        event_day = min(ctx.rng.randint(10, 25), 28)
        event_date = date(year, month, event_day)
        if event_date > ctx.config.today:
            event_date = ctx.config.today
        ack_deadline = event_date + timedelta(days=7) if requires_ack else None
        event = SchoolEvent(
            title=f"{title}({year}/{month:02d})",
            description=f"{title}相關說明,請家長留意。",
            event_date=event_date,
            end_date=None,
            event_type=event_type,
            is_all_day=True,
            start_time=None,
            end_time=None,
            location="園所大廳" if event_type == "activity" else None,
            is_active=True,
            requires_acknowledgment=requires_ack,
            ack_deadline=ack_deadline,
            recurrence_rule=None,
            created_at=_naive_dt(event_date, hour=8, minute=0),
            updated_at=_naive_dt(event_date, hour=8, minute=0),
        )
        session.add(event)
        n_event += 1

        # 需簽閱事件 + closed 月:家長補一筆簽閱(掛已知 parent user + 一名學生)。
        if requires_ack and not is_current and parent_user_id is not None and students:
            session.flush()  # 取得 event.id
            student = students[0]
            session.add(
                EventAcknowledgment(
                    event_id=event.id,
                    user_id=parent_user_id,
                    student_id=student.id,
                    acknowledged_at=_naive_dt(event_date, hour=19, minute=0),
                    signature_name="家長簽",
                    signature_attachment_id=None,
                    signature_uploaded_at=None,
                )
            )
            n_ack += 1

    if n_event:
        ctx.log("school_events", n_event)
    if n_ack:
        ctx.log("event_acknowledgments", n_ack)

    # 工作日覆寫(補班日):學年內補一筆代表性 WorkdayOverride。
    override_date = ctx.config.year_start + timedelta(days=120)
    if override_date <= ctx.config.today:
        session.add(
            WorkdayOverride(
                date=override_date,
                name="補班日",
                description="配合國定假日連假調整之補班日(seedgen)",
                is_active=True,
                source="manual",
                source_year=ctx.config.academic_year,
                synced_at=None,
            )
        )
        ctx.log("workday_overrides", 1)


def seed(ctx: SeedContext) -> None:
    """建立公告/聯絡簿/接送/行事曆等 portal 資料。

    園務會議(MeetingRecord)已移至 m04_leave_ot 產生:會議是「薪資輸入」,
    必須在 m06 結算薪資之前存在(否則 meeting_overtime_pay 結算為 0)。
    """
    parent_user = _parent_user(ctx)
    author_emp = _admin_employee(ctx)

    _seed_announcements(ctx, author_emp, parent_user)
    _seed_contact_book(ctx, parent_user)
    _seed_dismissal_calls(ctx, parent_user)
    _seed_school_events(ctx, parent_user)
