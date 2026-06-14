"""m07_activities:課後才藝報名(含候補 waitlist)、用品、POS 付款

(帶 idempotency_key 與 receipt_no)、每日 POS 日結簽核、活動點名、家長提問。

依賴(由 orchestrator 保證已落庫 + 在 ctx registry):
- m00:`activity_courses` / `activity_supplies` / `activity_registration_settings`
  已落庫(school_year=民國學年、semester=1)。本模組以 DB query 取回 m00 建的課程/用品。
- m01:`ctx.users`(含 admin)、`ctx.employees_by_role["art"]`(才藝時薪老師,
  作為點名者 recorded_by / POS 操作者 operator / 日結簽核者的帳號來源)。
- m02:`ctx.students_active`(在籍學生,作為報名來源,連結 ActivityRegistration.student_id)。

設計重點(對齊 models/activity.py 值域/約束):
- 課程容量會被「灌滿 + 候補」:對至少一門課程,前 capacity 名 enrolled,其餘 waitlist,
  確保 registration_courses 同時存在 enrolled 與 waitlist 兩種 status(計畫自我驗證要求)。
- RegistrationCourse.status ∈ {enrolled, waitlist, promoted_pending};本模組只用前兩者。
- ActivityPaymentRecord:type ∈ {payment, refund}(CHECK ck_apr_type),amount > 0
  (CHECK ck_apr_amount_positive),payment_method 固定『現金』,帶 idempotency_key(唯一,
  uq_activity_payment_records_idk)與 receipt_no(POS-YYYYMMDD-XXXX)。已繳費報名才補
  payment;少量再補一筆 refund(同樣正數,靠 type 區分方向)。
- 唯一鍵守護:registration_courses uq_reg_course(reg, course)、registration_supplies
  uq_reg_supply(reg, supply)、activity_sessions uq_activity_session_course_date、
  activity_attendances uq_activity_attendance_session_reg、activity_registrations 的
  partial unique(student_name, birthday, school_year, semester, parent_phone);本模組
  以「每生每學期至多一筆報名」+ course/supply 去重避免撞約束。
- 時間上限:報名/付款/點名/日結一律落在 closed + in_progress 範圍內(≤ ctx.config.today),
  不生 future。

產出:不寫入 ctx registry(下游無依賴),僅 ctx.log 各表筆數。
本模組不 commit(由 orchestrator 跑完統一 commit)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    ParentInquiry,
    RegistrationCourse,
    RegistrationSupply,
)

from ..context import SeedContext
from ..fake import Faker

# 報名涵蓋比例:在籍學生約 45% 報名才藝(其餘不參加,貼近真實參與率)。
_REGISTRATION_RATIO = 0.45

# 已繳費比例(剩餘為未繳費佔位,貼近「先佔位後繳費」的業務常態)。
_PAID_RATIO = 0.75

# 已繳費者中再退費的比例(少量,測退費/對帳路徑)。
_REFUND_RATIO = 0.08

# 家長提問筆數(固定少量;部分已讀/已回覆)。
_INQUIRY_COUNT = 8

# 點名涵蓋的課程已上場次數(每門已上 4 場,對齊 closed 月已進行)。
_SESSIONS_PER_COURSE = 4

# 出席率(其餘視為缺席)。
_PRESENT_RATIO = 0.9


def _parent_phone(student) -> str:
    """取學生的家長手機(報名三欄比對欄);缺則回空字串。"""
    return getattr(student, "parent_phone", None) or ""


def _class_name(ctx: SeedContext, student) -> str | None:
    """以 student.classroom_id 對回 ctx.classrooms 取班級名稱(字串快照)。"""
    cid = getattr(student, "classroom_id", None)
    if cid is None:
        return None
    for c in ctx.classrooms or []:
        if getattr(c, "id", None) == cid:
            return getattr(c, "name", None)
    return None


def _operator_username(ctx: SeedContext) -> str:
    """POS 操作者/簽核者帳號:優先 admin,退化為任一已建 user 帳號。"""
    users = ctx.users or {}
    if "admin" in users:
        return "admin"
    for username in users:
        return username
    return "seedgen"


def _recorder_username(ctx: SeedContext, idx: int) -> str:
    """點名者帳號:輪替才藝老師的工號小寫;無則 admin。"""
    arts = (ctx.employees_by_role or {}).get("art", [])
    if arts:
        emp = arts[idx % len(arts)]
        emp_id = getattr(emp, "employee_id", None)
        if emp_id:
            return emp_id.lower()
    return _operator_username(ctx)


def _load_courses(ctx: SeedContext) -> list[ActivityCourse]:
    """取回 m00 建的 active 課程(本學年上學期)。"""
    roc_year = ctx.config.academic_year
    return list(
        ctx.session.query(ActivityCourse)
        .filter(
            ActivityCourse.is_active.is_(True),
            ActivityCourse.school_year == roc_year,
        )
        .order_by(ActivityCourse.id)
        .all()
    )


def _load_supplies(ctx: SeedContext) -> list[ActivitySupply]:
    """取回 m00 建的 active 用品(本學年上學期)。"""
    roc_year = ctx.config.academic_year
    return list(
        ctx.session.query(ActivitySupply)
        .filter(
            ActivitySupply.is_active.is_(True),
            ActivitySupply.school_year == roc_year,
        )
        .order_by(ActivitySupply.id)
        .all()
    )


def _make_registration(
    ctx: SeedContext,
    fake: Faker,
    student,
    created_at: datetime,
) -> ActivityRegistration:
    """由在籍學生建立一筆報名(自動匹配成功 → student_id/classroom_id 反填)。"""
    roc_year = ctx.config.academic_year
    birthday = getattr(student, "birthday", None)
    birthday_str = birthday.isoformat() if isinstance(birthday, date) else None
    phone = _parent_phone(student)
    return ActivityRegistration(
        student_name=getattr(student, "name", None) or "未命名",
        birthday=birthday_str,
        class_name=_class_name(ctx, student),
        email=f"reg{getattr(student, 'id', 0)}@example.test",
        is_paid=False,  # 由付款邏輯結算後回填
        paid_amount=0,
        is_active=True,
        school_year=roc_year,
        semester=1,
        student_id=getattr(student, "id", None),
        parent_phone=phone,
        classroom_id=getattr(student, "classroom_id", None),
        pending_review=False,
        match_status="matched",
        created_at=created_at,
    )


def seed(ctx: SeedContext) -> None:
    """建立才藝報名/候補/POS 付款/日結/點名/家長提問。"""
    session = ctx.session
    fake = Faker(ctx.rng)
    today = ctx.config.today

    courses = _load_courses(ctx)
    supplies = _load_supplies(ctx)
    if not courses:
        # m00 未建課程(理論上不會發生);無課程則僅建家長提問後返回。
        ctx.log("parent_inquiries", _seed_inquiries(ctx, fake))
        return

    # 報名來源:在籍學生抽樣(決定論)。每生至多一筆報名(避免撞 partial unique)。
    active = list(ctx.students_active or [])
    n_reg = int(len(active) * _REGISTRATION_RATIO)
    reg_students = active[:n_reg]

    # 報名時間:散佈在學年起日後到 today 之間(closed + in_progress,不生 future)。
    base_dt = datetime.combine(ctx.config.year_start, time(9, 0))
    span_days = max((today - ctx.config.year_start).days, 1)

    registrations: list[ActivityRegistration] = []
    for i, student in enumerate(reg_students):
        offset = ctx.rng.randint(0, span_days)
        created_at = base_dt + timedelta(days=offset)
        reg = _make_registration(ctx, fake, student, created_at)
        session.add(reg)
        registrations.append(reg)

    session.flush()  # 取得 registration.id 供 course/supply/payment FK

    # ---- registration_courses:灌滿 + 候補 ----
    # 策略:把報名平均分配到課程;對「第一門課程」刻意超賣以產生 waitlist。
    # 維護每課程已 enrolled 計數,達 capacity 後續報名轉 waitlist。
    enrolled_count: dict[int, int] = {c.id: 0 for c in courses}
    reg_course_n = 0
    waitlist_n = 0

    # 為保證 enrolled 與 waitlist 並存:把較多報名集中到第一門課程(容量較小者更易候補)。
    target_course = min(courses, key=lambda c: (c.capacity or 30))

    for i, reg in enumerate(registrations):
        # 每生報 1~2 門課;第一門固定指向 target_course(製造候補),第二門輪替。
        chosen: list[ActivityCourse] = [target_course]
        if ctx.rng.random() < 0.5 and len(courses) > 1:
            second = courses[i % len(courses)]
            if second.id != target_course.id:
                chosen.append(second)

        seen: set[int] = set()
        for course in chosen:
            if course.id in seen:  # uq_reg_course 守護:同報名同課程不重複
                continue
            seen.add(course.id)
            cap = course.capacity or 30
            if enrolled_count[course.id] < cap:
                status = "enrolled"
                enrolled_count[course.id] += 1
            else:
                status = "waitlist"
                waitlist_n += 1
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status=status,
                    price_snapshot=course.price,
                    created_at=reg.created_at,
                )
            )
            reg_course_n += 1

    # ---- registration_supplies:約 40% 報名加購一項用品 ----
    reg_supply_n = 0
    if supplies:
        for reg in registrations:
            if ctx.rng.random() < 0.4:
                supply = supplies[ctx.rng.randrange(len(supplies))]
                session.add(
                    RegistrationSupply(
                        registration_id=reg.id,
                        supply_id=supply.id,
                        price_snapshot=supply.price,
                        created_at=reg.created_at,
                    )
                )
                reg_supply_n += 1

    session.flush()  # 取 registration_courses / supplies 落庫,供金額彙整

    # ---- POS 付款(payment / refund),帶 idempotency_key + receipt_no ----
    payment_n, refund_n = _seed_payments(ctx, fake, registrations, courses, supplies)

    # ---- POS 日結簽核(對已有付款的若干日各簽一筆) ----
    close_n = _seed_pos_daily_close(ctx, registrations)

    # ---- 活動點名(課程場次 × 報名) ----
    session_n, attendance_n = _seed_sessions_and_attendance(ctx, courses)

    # ---- 家長提問 ----
    inquiry_n = _seed_inquiries(ctx, fake)

    # ---- log 各表筆數 ----
    ctx.log("activity_registrations", len(registrations))
    ctx.log("registration_courses", reg_course_n)
    if reg_supply_n:
        ctx.log("registration_supplies", reg_supply_n)
    if payment_n:
        ctx.log("activity_payment_records", payment_n + refund_n)
    if close_n:
        ctx.log("activity_pos_daily_close", close_n)
    if session_n:
        ctx.log("activity_sessions", session_n)
    if attendance_n:
        ctx.log("activity_attendances", attendance_n)
    ctx.log("parent_inquiries", inquiry_n)


def _seed_payments(
    ctx: SeedContext,
    fake: Faker,
    registrations: list[ActivityRegistration],
    courses: list[ActivityCourse],
    supplies: list[ActivitySupply],
) -> tuple[int, int]:
    """為 enrolled 報名建立 POS 付款(部分已繳費),少量再退費。

    金額取「該報名所有 enrolled 課程 + 用品 price_snapshot 之和」;
    每筆 payment 帶唯一 idempotency_key 與 receipt_no,payment_method='現金'。
    回傳 (payment 筆數, refund 筆數)。
    """
    session = ctx.session
    operator = _operator_username(ctx)

    # 預先建 course/supply 價格表,避免逐筆 query。
    course_price = {c.id: c.price for c in courses}
    supply_price = {s.id: s.price for s in supplies}

    payment_n = 0
    refund_n = 0
    seq = 0
    for reg in registrations:
        # 已繳費抽樣:決定論。未繳費者 paid_amount 維持 0(先佔位)。
        if ctx.rng.random() >= _PAID_RATIO:
            continue

        # 彙整應繳金額:該報名的 enrolled 課程 + 用品。
        total = 0
        rcs = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status == "enrolled",
            )
            .all()
        )
        for rc in rcs:
            total += course_price.get(rc.course_id, rc.price_snapshot or 0)
        rss = (
            session.query(RegistrationSupply)
            .filter(RegistrationSupply.registration_id == reg.id)
            .all()
        )
        for rs in rss:
            total += supply_price.get(rs.supply_id, rs.price_snapshot or 0)

        if total <= 0:
            # 純候補(無 enrolled 課程)不收費。
            continue

        # 付款日:報名建立日之後 0~14 天,但不超過 today。
        reg_date = reg.created_at.date() if reg.created_at else ctx.config.year_start
        pay_date = reg_date + timedelta(days=ctx.rng.randint(0, 14))
        if pay_date > ctx.config.today:
            pay_date = ctx.config.today

        seq += 1
        receipt_no = f"POS-{pay_date.strftime('%Y%m%d')}-{seq:012d}"
        idk = f"seed-pay-{reg.id}-{seq:06d}"
        session.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=total,
                payment_date=pay_date,
                payment_method="現金",
                operator=operator,
                idempotency_key=idk,
                receipt_no=receipt_no,
                notes="seedgen POS 繳費",
                created_at=datetime.combine(pay_date, time(15, 0)),
            )
        )
        payment_n += 1
        reg.paid_amount = total
        reg.is_paid = True

        # 少量退費(正數,type=refund):退一門課程的價(模擬部分退費)。
        if rcs and ctx.rng.random() < _REFUND_RATIO:
            refund_amount = course_price.get(
                rcs[0].course_id, rcs[0].price_snapshot or 0
            )
            if refund_amount > 0:
                refund_date = pay_date + timedelta(days=ctx.rng.randint(1, 10))
                if refund_date > ctx.config.today:
                    refund_date = ctx.config.today
                seq += 1
                session.add(
                    ActivityPaymentRecord(
                        registration_id=reg.id,
                        type="refund",
                        amount=refund_amount,
                        payment_date=refund_date,
                        payment_method="現金",
                        operator=operator,
                        idempotency_key=f"seed-rfd-{reg.id}-{seq:06d}",
                        receipt_no=f"POS-{refund_date.strftime('%Y%m%d')}-{seq:012d}",
                        notes="seedgen POS 部分退費",
                        created_at=datetime.combine(refund_date, time(16, 0)),
                    )
                )
                refund_n += 1
                # 退費後回填累計已繳(退費同樣以正數記,paid_amount 扣除)。
                reg.paid_amount = max(0, (reg.paid_amount or 0) - refund_amount)
                reg.is_paid = (reg.paid_amount or 0) >= total

    return payment_n, refund_n


def _seed_pos_daily_close(
    ctx: SeedContext,
    registrations: list[ActivityRegistration],
) -> int:
    """對有付款的若干日各建一筆 POS 日結簽核(close_date 為 PK,每日一筆)。

    snapshot(payment_total/refund_total/net_total/transaction_count/by_method_json)
    由當日全部未軟刪付款彙整。只簽核已過完整一天(< today)的日結,避免簽核當天。
    """
    session = ctx.session
    approver = _operator_username(ctx)

    # 取所有付款,依 payment_date 分組彙整。
    records = (
        session.query(ActivityPaymentRecord)
        .filter(ActivityPaymentRecord.voided_at.is_(None))
        .all()
    )
    by_date: dict[date, dict[str, int]] = {}
    for r in records:
        d = r.payment_date
        if d >= ctx.config.today:  # 只結已過完整一天的
            continue
        bucket = by_date.setdefault(d, {"payment": 0, "refund": 0, "count": 0})
        if r.type == "payment":
            bucket["payment"] += r.amount
        else:
            bucket["refund"] += r.amount
        bucket["count"] += 1

    # 只簽核最早的若干天(決定論:依日期排序取前 3 天),其餘留未簽核。
    sorted_dates = sorted(by_date.keys())[:3]
    n = 0
    for d in sorted_dates:
        b = by_date[d]
        net = b["payment"] - b["refund"]
        session.add(
            ActivityPosDailyClose(
                close_date=d,
                approver_username=approver,
                approved_at=datetime.combine(d, time(18, 0)),
                note="seedgen 日結簽核",
                payment_total=b["payment"],
                refund_total=b["refund"],
                net_total=net,
                transaction_count=b["count"],
                by_method_json=json.dumps({"現金": net}, ensure_ascii=False),
                actual_cash_count=net,
                cash_variance=0,
                created_at=datetime.combine(d, time(18, 0)),
            )
        )
        n += 1
    return n


def _seed_sessions_and_attendance(
    ctx: SeedContext,
    courses: list[ActivityCourse],
) -> tuple[int, int]:
    """為每門課建立若干已上場次,並對 enrolled 報名點名。

    場次日期取課程 meeting_weekday 在 closed 月內的工作日(≤ today),
    每門最多 _SESSIONS_PER_COURSE 場。點名 is_present 多數 True。
    回傳 (session 筆數, attendance 筆數)。
    """
    session = ctx.session
    today = ctx.config.today

    session_n = 0
    attendance_n = 0
    for ci, course in enumerate(courses):
        # 取該課程的 enrolled 報名(點名對象)。
        rcs = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.course_id == course.id,
                RegistrationCourse.status == "enrolled",
            )
            .order_by(RegistrationCourse.id)
            .all()
        )
        if not rcs:
            continue

        # 取 enrolled 報名對應的 ActivityRegistration(取 student_id 供冗餘欄位)。
        reg_ids = [rc.registration_id for rc in rcs]
        regs = {
            r.id: r
            for r in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(reg_ids))
            .all()
        }

        # 場次日期:自學年起日後第一個符合 meeting_weekday 的日子起,每週一場,取已過的場次。
        weekday = course.meeting_weekday if course.meeting_weekday is not None else 0
        session_dates = _weekly_dates(
            ctx.config.year_start, today, weekday, _SESSIONS_PER_COURSE
        )
        for sd in session_dates:
            sess = ActivitySession(
                course_id=course.id,
                session_date=sd,
                notes="seedgen 場次",
                created_by=_operator_username(ctx),
                created_at=datetime.combine(sd, time(17, 0)),
            )
            session.add(sess)
            session.flush()  # 取 sess.id 供 attendance FK
            session_n += 1

            for rc in rcs:
                reg = regs.get(rc.registration_id)
                present = ctx.rng.random() < _PRESENT_RATIO
                session.add(
                    ActivityAttendance(
                        session_id=sess.id,
                        registration_id=rc.registration_id,
                        student_id=(getattr(reg, "student_id", None) if reg else None),
                        is_present=present,
                        notes=None,
                        recorded_by=_recorder_username(ctx, ci),
                        created_at=datetime.combine(sd, time(17, 30)),
                    )
                )
                attendance_n += 1

    return session_n, attendance_n


def _weekly_dates(
    start: date,
    today: date,
    weekday: int,
    count: int,
) -> list[date]:
    """自 start 起,回傳 count 個落在 [start, today) 內、指定 weekday 的每週日期。

    只取已過完整一天(< today)的場次,避免點名未來/當天課。
    """
    # 找到 start 當週或之後第一個 weekday。
    delta = (weekday - start.weekday()) % 7
    first = start + timedelta(days=delta)
    out: list[date] = []
    d = first
    while len(out) < count and d < today:
        out.append(d)
        d += timedelta(days=7)
    return out


def _seed_inquiries(ctx: SeedContext, fake: Faker) -> int:
    """建立少量家長提問(部分已讀/已回覆)。"""
    session = ctx.session
    today = ctx.config.today
    base_dt = datetime.combine(ctx.config.year_start, time(10, 0))
    span = max((today - ctx.config.year_start).days, 1)

    questions = [
        "請問才藝課程可以中途加退嗎?",
        "美術課需要自備材料嗎?",
        "候補大概多久會遞補上?",
        "繳費可以刷卡嗎?",
        "孩子請假當天的才藝課可以補課嗎?",
        "課程時間和接送時間衝突怎麼辦?",
        "下學期會開哪些新課程?",
        "用品費是否包含在課程費裡?",
    ]
    n = min(_INQUIRY_COUNT, len(questions))
    for i in range(n):
        created = base_dt + timedelta(days=ctx.rng.randint(0, span))
        is_read = i % 2 == 0
        replied_at = None
        reply = None
        if is_read and i % 4 == 0:
            reply = "您好,已收到您的提問,稍後由專人回覆,謝謝。"
            replied_at = created + timedelta(days=1)
        session.add(
            ParentInquiry(
                name=fake.name(ctx.rng.choice(["M", "F"])),
                phone=fake.phone(),
                question=questions[i],
                is_read=is_read,
                reply=reply,
                replied_at=replied_at,
                created_at=created,
            )
        )
    return n
