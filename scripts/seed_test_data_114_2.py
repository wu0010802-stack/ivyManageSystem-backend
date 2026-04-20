"""scripts/seed_test_data_114_2.py — 114 學年下學期測試資料 seed

用法:
    cd ~/Desktop/ivy-backend
    python -m scripts.seed_test_data_114_2 --step all
    python -m scripts.seed_test_data_114_2 --step students,fees

冪等:重複執行只會補缺,不會重複寫入。

設計重點:
- 學期:民國 114/2 = 西元 2026/02-2026/07
- 將既有 162 名 active 學生由 115/1 班(15-22)依年級降級回 114/2 班(3-10)
- 為 114/2 大班(1, 2)新建 30 名「即將畢業」學生
- 補:guardians, fee_items+records, attendance, shift, leaves, overtime,
       activity_courses+sessions+attendances, announcements, school_events,
       student_assessments+incidents+dismissal+communication,
       deduction_types+bonus_types, salary 6/7 月
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import date, datetime, timedelta, time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import and_, func, or_

# 觸發所有 model 載入(避免 cross-table FK 解析失敗)
import models.database  # noqa: F401
from models.base import session_scope
from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivityRegistrationSettings,
    ActivitySession,
    ActivitySupply,
    ParentInquiry,
    RegistrationCourse,
    RegistrationSupply,
)
from models.attendance import Attendance
from models.auth import User
from models.classroom import (
    Classroom,
    Student,
    StudentAssessment,
    StudentAttendance,
    StudentIncident,
)
from models.config import AttendancePolicy, BonusConfig
from models.dismissal import StudentDismissalCall
from models.employee import Employee
from models.event import (
    Announcement,
    Holiday,
    MeetingRecord,
    SchoolEvent,
    WorkdayOverride,
)
from models.fees import FeeItem, StudentFeeRecord
from models.guardian import Guardian
from models.leave import LeaveQuota, LeaveRecord
from models.overtime import OvertimeRecord, PunchCorrectionRequest
from models.salary import (
    BonusType,
    DeductionType,
    SalaryRecord,
)
from models.shift import DailyShift, ShiftAssignment, ShiftType, ShiftSwapRequest
from models.student_log import ParentCommunicationLog, StudentChangeLog
from models.student_transfer import StudentClassroomTransfer

random.seed(20260419)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_114_2")

# ===== 學期常數 =====
SCHOOL_YEAR = 114
SEMESTER = 2
PERIOD = f"{SCHOOL_YEAR}-{SEMESTER}"

# 114/2 = 2026/02 - 2026/07
TERM_START = date(2026, 2, 1)
TERM_END = date(2026, 7, 31)
TODAY = date(2026, 4, 19)

# 115/1 → 114/2 班級降級映射(學生回到上學期應屬班)
GRADE_DOWN_MAP = {
    15: 3,  # 大班 → 中班
    16: 4,  # 大班 → 中班
    17: 5,  # 中班 → 小班
    18: 6,  # 中班 → 小班
    19: 7,  # 中班 → 小班
    20: 8,  # 小班 → 幼幼班
    21: 9,  # 小班 → 幼幼班
    22: 10,  # 小班 → 幼幼班
}


# ============================================================
# Helper
# ============================================================
def _date_range(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def _is_workday(d: date, holiday_set: set[date], workday_set: set[date]) -> bool:
    """週一到週五 + 排除假日 + 補班日為工作日"""
    if d in holiday_set:
        return False
    if d in workday_set:
        return True
    return d.weekday() < 5


def _random_phone() -> str:
    return "09" + "".join(str(random.randint(0, 9)) for _ in range(8))


SURNAMES = list(
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪邱曾廖賴徐周葉蘇莊呂江何蕭羅高潘簡朱鍾彭游詹胡施沈余趙盧梁顏柯孫魏翁戴范方宋鄧杜傅侯曹溫薛丁馬唐卓藍馮姚石董尤巫姜湯汪倪"
)
GIVEN_NAMES_BOY = [
    "承翰",
    "宥廷",
    "宸睿",
    "品翔",
    "睿恩",
    "宇軒",
    "柏宏",
    "彥廷",
    "辰希",
    "凱翔",
    "立翔",
    "祥宇",
    "彥廷",
    "致軒",
    "禹辰",
    "亦嘉",
    "佑恩",
    "晨曦",
    "晉彥",
    "皓軒",
    "彥謙",
    "睿廷",
    "信宏",
    "亮廷",
    "韋翔",
    "崇瀚",
]
GIVEN_NAMES_GIRL = [
    "子瑄",
    "宥恩",
    "雅婷",
    "若曦",
    "妤蓁",
    "羽彤",
    "柔安",
    "詠晴",
    "亦晴",
    "于柔",
    "佩瑩",
    "宥彤",
    "祐熙",
    "彤恩",
    "苡晴",
    "婉柔",
    "婕安",
    "歆妍",
    "睿涵",
    "嘉恩",
    "禹彤",
    "若芸",
    "翊婷",
    "巧彤",
    "宥茵",
    "穎萱",
]


def _random_name(gender: str) -> str:
    surname = random.choice(SURNAMES)
    given = random.choice(GIVEN_NAMES_BOY if gender == "男" else GIVEN_NAMES_GIRL)
    return surname + given


# ============================================================
# Step 1: 學生班級遷移
# ============================================================
def step_students():
    """將學生從 115/1 班搬回 114/2,並補大班學生"""
    logger.info("=== Step 1: 學生班級遷移 ===")
    with session_scope() as session:
        # 1a) 找出 admin user 用作 transferred_by
        admin = session.query(User).filter_by(username="admin").first()
        admin_id = admin.id if admin else None

        # 1b) 把現有學生搬回 114/2(避開 id 160, 161 髒資料,我們稍後處理)
        moved = 0
        for from_id, to_id in GRADE_DOWN_MAP.items():
            students = (
                session.query(Student)
                .filter(
                    Student.classroom_id == from_id,
                    Student.is_active == True,  # noqa: E712
                )
                .all()
            )
            for stu in students:
                stu.classroom_id = to_id
                # 補 transfer log
                session.add(
                    StudentClassroomTransfer(
                        student_id=stu.id,
                        from_classroom_id=from_id,
                        to_classroom_id=to_id,
                        transferred_at=datetime(2026, 2, 1, 9, 0),
                        transferred_by=admin_id,
                    )
                )
                moved += 1
        logger.info("已將 %d 名學生從 115/1 搬回 114/2", moved)

        # 1c) 處理髒資料:把 id 160, 161 改名/移到 14/2 大班
        dirty = session.query(Student).filter(Student.id.in_([160, 161])).all()
        if dirty:
            for s in dirty:
                if s.name in ("123", "吳逸倫"):
                    s.lifecycle_status = "withdrawn"
                    s.is_active = False
                    s.withdrawal_date = date(2026, 2, 5)
                    s.notes = "[seed] 既有測試髒資料,標記退學"

        # 1d) 為 114/2 大班 (id 1, 2) 新建 30 名學生
        existing_big = (
            session.query(Student)
            .filter(
                Student.classroom_id.in_([1, 2]),
                Student.is_active == True,  # noqa: E712
            )
            .count()
        )
        if existing_big >= 30:
            logger.info("114/2 大班已有 %d 學生,跳過新建", existing_big)
            return

        target_per_class = 15
        new_count = 0
        for cls_id in [1, 2]:
            cur = (
                session.query(Student)
                .filter(
                    Student.classroom_id == cls_id,
                    Student.is_active == True,  # noqa: E712
                )
                .count()
            )
            need = max(0, target_per_class - cur)
            for i in range(need):
                gender = random.choice(["男", "女"])
                # 大班學生 5-6 歲,出生年 2020-2021
                birth_year = random.choice([2020, 2021])
                birth = date(birth_year, random.randint(1, 12), random.randint(1, 28))
                # student_id 格式:S114201XX (114/2/01班/序號)
                seq = 1
                while True:
                    sid = f"S1142{cls_id:02d}{seq:02d}"
                    if not session.query(Student).filter_by(student_id=sid).first():
                        break
                    seq += 1
                stu = Student(
                    student_id=sid,
                    name=_random_name(gender),
                    gender=gender,
                    birthday=birth,
                    classroom_id=cls_id,
                    enrollment_date=date(2024, 8, 1),  # 已就讀兩年
                    lifecycle_status="active",
                    is_active=True,
                    parent_phone=_random_phone(),
                    parent_name="家長" + str(random.randint(100, 999)),
                )
                session.add(stu)
                session.flush()
                # 補 transfer log
                session.add(
                    StudentClassroomTransfer(
                        student_id=stu.id,
                        from_classroom_id=None,
                        to_classroom_id=cls_id,
                        transferred_at=datetime(2024, 8, 1, 9, 0),
                        transferred_by=admin_id,
                    )
                )
                # 補 change log(入學)
                session.add(
                    StudentChangeLog(
                        student_id=stu.id,
                        school_year=113,
                        semester=1,
                        event_type="入學",
                        event_date=date(2024, 8, 1),
                        classroom_id=cls_id,
                        to_classroom_id=cls_id,
                        reason="新生報名",
                        recorded_by=admin_id,
                    )
                )
                new_count += 1
        logger.info("已為 114/2 大班新建 %d 名學生", new_count)


# ============================================================
# Step 2: 家長(guardians)
# ============================================================
def step_guardians():
    """每個 active 學生補 1-2 位家長"""
    logger.info("=== Step 2: 家長 ===")
    with session_scope() as session:
        students = (
            session.query(Student).filter(Student.is_active == True).all()
        )  # noqa: E712
        added = 0
        for stu in students:
            existing = (
                session.query(Guardian)
                .filter(
                    Guardian.student_id == stu.id,
                    Guardian.deleted_at.is_(None),
                )
                .count()
            )
            if existing >= 1:
                continue
            # 主要家長(母親)
            mother = Guardian(
                student_id=stu.id,
                name=stu.parent_name
                or (
                    random.choice(SURNAMES)
                    + random.choice(["美玲", "雅惠", "佳怡", "淑芬", "麗華"])
                ),
                phone=stu.parent_phone or _random_phone(),
                relation="母親",
                is_primary=True,
                is_emergency=True,
                can_pickup=True,
                sort_order=1,
            )
            session.add(mother)
            added += 1
            # 50% 補父親
            if random.random() < 0.6:
                father = Guardian(
                    student_id=stu.id,
                    name=random.choice(SURNAMES)
                    + random.choice(["志明", "建宏", "俊傑", "文哲", "家豪"]),
                    phone=_random_phone(),
                    relation="父親",
                    is_primary=False,
                    is_emergency=True,
                    can_pickup=True,
                    sort_order=2,
                )
                session.add(father)
                added += 1
        logger.info("已新增 %d 位 guardians", added)


# ============================================================
# Step 3: 學費(fee_items + student_fee_records)
# ============================================================
FEE_TEMPLATES = [
    {"name": "114下學期 學費", "amount": 35000, "classroom_id": None},
    {"name": "114下學期 月費", "amount": 6000, "classroom_id": None},
    {"name": "114下學期 餐點費", "amount": 4500, "classroom_id": None},
    {"name": "114下學期 教材費", "amount": 2000, "classroom_id": None},
    {"name": "114下學期 校外教學費", "amount": 1500, "classroom_id": None},
    {"name": "114下學期 保險費", "amount": 350, "classroom_id": None},
]


def step_fees():
    """建立學費項目並為每個學生產生繳費記錄"""
    logger.info("=== Step 3: 學費 ===")
    with session_scope() as session:
        # 3a) Fee items
        for tpl in FEE_TEMPLATES:
            existing = (
                session.query(FeeItem)
                .filter_by(name=tpl["name"], period=PERIOD)
                .first()
            )
            if not existing:
                session.add(
                    FeeItem(
                        name=tpl["name"],
                        amount=tpl["amount"],
                        classroom_id=tpl["classroom_id"],
                        period=PERIOD,
                        is_active=True,
                    )
                )
        session.flush()
        items = session.query(FeeItem).filter_by(period=PERIOD).all()
        logger.info("FeeItem 總數:%d", len(items))

        # 3b) student_fee_records
        students = (
            session.query(Student)
            .filter(
                Student.is_active == True,  # noqa: E712
            )
            .all()
        )
        added = 0
        for stu in students:
            cls = (
                session.query(Classroom).get(stu.classroom_id)
                if stu.classroom_id
                else None
            )
            for it in items:
                exists = (
                    session.query(StudentFeeRecord)
                    .filter_by(
                        student_id=stu.id,
                        fee_item_id=it.id,
                    )
                    .first()
                )
                if exists:
                    continue
                # 70% 已繳、25% 部分繳、5% 未繳
                roll = random.random()
                if roll < 0.7:
                    paid = it.amount
                    status = "paid"
                    pay_date = date(2026, 2, random.randint(10, 28))
                    method = random.choice(["現金", "轉帳"])
                elif roll < 0.95:
                    paid = it.amount // 2
                    status = "unpaid"
                    pay_date = date(2026, 2, random.randint(10, 28))
                    method = "現金"
                else:
                    paid = 0
                    status = "unpaid"
                    pay_date = None
                    method = None
                session.add(
                    StudentFeeRecord(
                        student_id=stu.id,
                        student_name=stu.name,
                        classroom_name=cls.name if cls else None,
                        fee_item_id=it.id,
                        fee_item_name=it.name,
                        amount_due=it.amount,
                        amount_paid=paid,
                        status=status,
                        payment_date=pay_date,
                        payment_method=method,
                        period=PERIOD,
                    )
                )
                added += 1
        logger.info("已新增 %d 筆學生繳費記錄", added)


# ============================================================
# Step 4: 員工考勤
# ============================================================
def step_attendance():
    """為每位 active 員工補 2026/02-04 每個工作日的打卡記錄"""
    logger.info("=== Step 4: 員工考勤 ===")
    with session_scope() as session:
        holidays = {
            h.date
            for h in session.query(Holiday)
            .filter(
                Holiday.date >= TERM_START,
                Holiday.date <= TODAY,
            )
            .all()
        }
        workday_overrides = {
            w.date
            for w in session.query(WorkdayOverride)
            .filter(
                WorkdayOverride.date >= TERM_START,
                WorkdayOverride.date <= TODAY,
            )
            .all()
        }

        emps = (
            session.query(Employee).filter(Employee.is_active == True).all()
        )  # noqa: E712
        added = 0
        for emp in emps:
            ws = emp.work_start_time or "08:00"
            we = emp.work_end_time or "17:00"
            try:
                ws_h, ws_m = map(int, ws.split(":"))
                we_h, we_m = map(int, we.split(":"))
            except Exception:
                ws_h, ws_m, we_h, we_m = 8, 0, 17, 0

            for d in _date_range(TERM_START, TODAY):
                if not _is_workday(d, holidays, workday_overrides):
                    continue
                exists = (
                    session.query(Attendance)
                    .filter_by(
                        employee_id=emp.id,
                        attendance_date=d,
                    )
                    .first()
                )
                if exists:
                    continue
                # 95% 正常、3% 遲到、2% 早退
                roll = random.random()
                is_late = is_early = False
                late_min = early_min = 0
                if roll < 0.03:
                    is_late = True
                    late_min = random.choice([5, 10, 15, 20])
                    pi = datetime(d.year, d.month, d.day, ws_h, ws_m) + timedelta(
                        minutes=late_min
                    )
                else:
                    pi = datetime(d.year, d.month, d.day, ws_h, ws_m) - timedelta(
                        minutes=random.randint(0, 5)
                    )
                if roll >= 0.97:
                    is_early = True
                    early_min = random.choice([10, 20])
                    po = datetime(d.year, d.month, d.day, we_h, we_m) - timedelta(
                        minutes=early_min
                    )
                else:
                    po = datetime(d.year, d.month, d.day, we_h, we_m) + timedelta(
                        minutes=random.randint(0, 10)
                    )

                session.add(
                    Attendance(
                        employee_id=emp.id,
                        attendance_date=d,
                        punch_in_time=pi,
                        punch_out_time=po,
                        status="normal",
                        is_late=is_late,
                        is_early_leave=is_early,
                        is_missing_punch_in=False,
                        is_missing_punch_out=False,
                        late_minutes=late_min,
                        early_leave_minutes=early_min,
                    )
                )
                added += 1
        logger.info("已新增 %d 筆員工打卡記錄", added)


# ============================================================
# Step 5: shift_assignments(每員工每週)
# ============================================================
def step_shifts():
    logger.info("=== Step 5: 排班 ===")
    with session_scope() as session:
        emps = (
            session.query(Employee).filter(Employee.is_active == True).all()
        )  # noqa: E712
        shift_types = (
            session.query(ShiftType).filter(ShiftType.is_active == True).all()
        )  # noqa: E712
        if not shift_types:
            logger.warning("無 shift_types,跳過排班")
            return
        # 找 2026/02-2026/04 各週週一
        d = TERM_START
        while d.weekday() != 0:
            d += timedelta(days=1)
        added = 0
        while d <= TODAY:
            for emp in emps:
                exists = (
                    session.query(ShiftAssignment)
                    .filter_by(
                        employee_id=emp.id,
                        week_start_date=d,
                    )
                    .first()
                )
                if exists:
                    continue
                # 班導師用「正值(班導)」,司機用「早車/晚車」,其他人輪流
                title = (emp.title or "").strip()
                position = (emp.position or "").strip()
                if "司機" in title:
                    st = next(
                        (s for s in shift_types if "早車" in s.name), shift_types[0]
                    )
                elif position == "班導":
                    st = next(
                        (s for s in shift_types if "正值(班導)" in s.name),
                        shift_types[0],
                    )
                elif position == "副班導":
                    st = next(
                        (s for s in shift_types if "正值(副班導)" in s.name),
                        shift_types[0],
                    )
                else:
                    st = random.choice(shift_types)
                session.add(
                    ShiftAssignment(
                        employee_id=emp.id,
                        shift_type_id=st.id,
                        week_start_date=d,
                    )
                )
                added += 1
            d += timedelta(days=7)
        logger.info("已新增 %d 筆 shift_assignments", added)

        # 換班申請 1 筆
        if not session.query(ShiftSwapRequest).first() and len(emps) >= 2:
            session.add(
                ShiftSwapRequest(
                    requester_id=emps[2].id,
                    target_id=emps[3].id,
                    swap_date=date(2026, 4, 25),
                    requester_shift_type_id=shift_types[0].id,
                    target_shift_type_id=shift_types[1].id,
                    reason="家中有事需與同事互調",
                    status="pending",
                )
            )


# ============================================================
# Step 6: 加班、補打卡、請假配額(2026 年度)
# ============================================================
def step_overtime_punch():
    logger.info("=== Step 6: 加班、補打卡、請假配額 ===")
    with session_scope() as session:
        emps = (
            session.query(Employee).filter(Employee.is_active == True).all()
        )  # noqa: E712
        teachers = [
            e
            for e in emps
            if (e.title or "").endswith("教師") or "教保" in (e.title or "")
        ]

        # 加班(週六加班 5 筆)
        sample = random.sample(teachers, min(5, len(teachers)))
        for i, emp in enumerate(sample):
            d = date(2026, 3, 7) + timedelta(days=i * 7)
            exists = (
                session.query(OvertimeRecord)
                .filter_by(
                    employee_id=emp.id,
                    overtime_date=d,
                )
                .first()
            )
            if exists:
                continue
            session.add(
                OvertimeRecord(
                    employee_id=emp.id,
                    overtime_date=d,
                    overtime_type="weekend",
                    start_time=datetime(d.year, d.month, d.day, 8, 0),
                    end_time=datetime(d.year, d.month, d.day, 12, 0),
                    hours=4.0,
                    overtime_pay=4 * (float(emp.base_salary or 30000) / 30 / 8) * 1.34,
                    is_approved=True,
                    approved_by="admin",
                    reason="校外教學佈置",
                )
            )

        # 補打卡 3 筆
        sample2 = random.sample(emps, min(3, len(emps)))
        for emp in sample2:
            d = date(2026, 3, random.randint(10, 28))
            exists = (
                session.query(PunchCorrectionRequest)
                .filter_by(
                    employee_id=emp.id,
                    attendance_date=d,
                )
                .first()
            )
            if exists:
                continue
            session.add(
                PunchCorrectionRequest(
                    employee_id=emp.id,
                    attendance_date=d,
                    correction_type="punch_in",
                    requested_punch_in=datetime(d.year, d.month, d.day, 8, 0),
                    reason="忘記打卡,有同事可作證",
                    is_approved=None,
                )
            )

        # 請假配額(2026 全員)
        for emp in emps:
            for lt, hours in [
                ("annual", 56),
                ("sick", 240),
                ("personal", 112),
                ("menstrual", 24),
            ]:
                exists = (
                    session.query(LeaveQuota)
                    .filter_by(
                        employee_id=emp.id,
                        year=2026,
                        leave_type=lt,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    LeaveQuota(
                        employee_id=emp.id,
                        year=2026,
                        leave_type=lt,
                        total_hours=hours,
                    )
                )


# ============================================================
# Step 7: 才藝課程 + 場次 + 報名 + 點名
# ============================================================
ACTIVITY_COURSES = [
    {"name": "陶藝創作", "price": 2400, "sessions": 12, "capacity": 15},
    {"name": "幼兒律動", "price": 2000, "sessions": 12, "capacity": 20},
    {"name": "美語故事", "price": 2800, "sessions": 12, "capacity": 18},
    {"name": "圍棋啟蒙", "price": 2200, "sessions": 12, "capacity": 12},
    {"name": "畫畫創作", "price": 2400, "sessions": 12, "capacity": 18},
    {"name": "幼兒街舞", "price": 2200, "sessions": 12, "capacity": 16},
    {"name": "兒童瑜伽", "price": 2000, "sessions": 12, "capacity": 14},
    {"name": "音樂律動", "price": 2200, "sessions": 12, "capacity": 18},
]


def step_activities():
    logger.info("=== Step 7: 才藝課程 ===")
    with session_scope() as session:
        # 7a) ActivityRegistrationSettings 開放
        s = session.query(ActivityRegistrationSettings).first()
        if s and not s.is_open:
            s.is_open = True
            s.term_label = "114 下學期"
            s.page_title = "114 下藝童趣｜課後才藝報名"
            s.event_date_label = "2026-02-23"
            s.target_audience = "本園在學幼兒"
            s.form_card_title = "114 下藝童趣 · 2026-02-23"

        # 7b) Courses + Supplies
        for c in ACTIVITY_COURSES:
            exists = (
                session.query(ActivityCourse)
                .filter_by(
                    name=c["name"],
                    school_year=SCHOOL_YEAR,
                    semester=SEMESTER,
                )
                .first()
            )
            if not exists:
                session.add(
                    ActivityCourse(
                        name=c["name"],
                        price=c["price"],
                        sessions=c["sessions"],
                        capacity=c["capacity"],
                        school_year=SCHOOL_YEAR,
                        semester=SEMESTER,
                        description=f"{c['name']}課程,共 {c['sessions']} 堂",
                        is_active=True,
                    )
                )
        for sname, sprice in [("陶藝材料包", 300), ("律動服", 500), ("美語講義", 250)]:
            exists = (
                session.query(ActivitySupply)
                .filter_by(
                    name=sname,
                    school_year=SCHOOL_YEAR,
                    semester=SEMESTER,
                )
                .first()
            )
            if not exists:
                session.add(
                    ActivitySupply(
                        name=sname,
                        price=sprice,
                        school_year=SCHOOL_YEAR,
                        semester=SEMESTER,
                        is_active=True,
                    )
                )
        session.flush()

        # 7c) 報名(每門課隨機抓 8-12 名學生報名)
        courses = (
            session.query(ActivityCourse)
            .filter_by(
                school_year=SCHOOL_YEAR,
                semester=SEMESTER,
            )
            .all()
        )
        students = (
            session.query(Student).filter(Student.is_active == True).all()
        )  # noqa: E712
        # 每位學生最多報 2 門。預載 DB 既有「學生 → 已報課程」避免 re-run 重複。
        student_courses: dict[int, list[int]] = {}
        existing_pairs = (
            session.query(ActivityRegistration.student_id, RegistrationCourse.course_id)
            .join(
                RegistrationCourse,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                ActivityRegistration.school_year == SCHOOL_YEAR,
                ActivityRegistration.semester == SEMESTER,
                ActivityRegistration.is_active == True,  # noqa: E712
                ActivityRegistration.student_id.isnot(None),
            )
            .all()
        )
        for sid, cid in existing_pairs:
            student_courses.setdefault(sid, []).append(cid)

        for course in courses:
            already = (
                session.query(RegistrationCourse)
                .join(
                    ActivityRegistration,
                    ActivityRegistration.id == RegistrationCourse.registration_id,
                )
                .filter(
                    RegistrationCourse.course_id == course.id,
                    ActivityRegistration.is_active == True,  # noqa: E712
                    ActivityRegistration.school_year == SCHOOL_YEAR,
                    ActivityRegistration.semester == SEMESTER,
                )
                .count()
            )
            need = max(0, random.randint(8, 12) - already)
            # 排除:已報滿 2 門 + 已報該課的學生
            candidates = [
                s
                for s in students
                if len(student_courses.get(s.id, [])) < 2
                and course.id not in student_courses.get(s.id, [])
            ]
            random.shuffle(candidates)
            for stu in candidates[:need]:
                cls = (
                    session.query(Classroom).get(stu.classroom_id)
                    if stu.classroom_id
                    else None
                )
                # 找此學生本學期是否已有 registration
                reg = (
                    session.query(ActivityRegistration)
                    .filter_by(
                        student_id=stu.id,
                        school_year=SCHOOL_YEAR,
                        semester=SEMESTER,
                        is_active=True,
                    )
                    .first()
                )
                if not reg:
                    reg = ActivityRegistration(
                        student_name=stu.name,
                        birthday=stu.birthday.isoformat() if stu.birthday else None,
                        class_name=cls.name if cls else None,
                        classroom_id=stu.classroom_id,
                        student_id=stu.id,
                        parent_phone=stu.parent_phone or _random_phone(),
                        email=f"parent_{stu.id}@example.com",
                        school_year=SCHOOL_YEAR,
                        semester=SEMESTER,
                        match_status="matched",
                        is_active=True,
                        paid_amount=course.price,
                        is_paid=True,
                    )
                    session.add(reg)
                    session.flush()
                else:
                    reg.paid_amount = (reg.paid_amount or 0) + course.price
                session.add(
                    RegistrationCourse(
                        registration_id=reg.id,
                        course_id=course.id,
                        status="enrolled",
                        price_snapshot=course.price,
                    )
                )
                student_courses.setdefault(stu.id, []).append(course.id)

        # 7d) 場次:每門課每週固定一天,2026/03 開始上 6 堂(已上 6 堂,還有 6 堂未來)
        for course in courses:
            week_day = hash(course.name) % 5  # 0=週一 ~ 4=週五
            d = date(2026, 3, 2)
            while d.weekday() != week_day:
                d += timedelta(days=1)
            for i in range(course.sessions or 12):
                sess_date = d + timedelta(weeks=i)
                exists = (
                    session.query(ActivitySession)
                    .filter_by(
                        course_id=course.id,
                        session_date=sess_date,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    ActivitySession(
                        course_id=course.id,
                        session_date=sess_date,
                        notes=f"第 {i + 1} 堂",
                    )
                )
        session.flush()

        # 7e) 點名:過去場次補出席記錄
        sessions_q = (
            session.query(ActivitySession)
            .join(
                ActivityCourse,
                ActivityCourse.id == ActivitySession.course_id,
            )
            .filter(
                ActivityCourse.school_year == SCHOOL_YEAR,
                ActivityCourse.semester == SEMESTER,
                ActivitySession.session_date <= TODAY,
            )
            .all()
        )
        for sess in sessions_q:
            regs = (
                session.query(ActivityRegistration, RegistrationCourse)
                .join(
                    RegistrationCourse,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id == sess.course_id,
                    ActivityRegistration.is_active == True,  # noqa: E712
                    RegistrationCourse.status == "enrolled",
                )
                .all()
            )
            for reg, _rc in regs:
                exists = (
                    session.query(ActivityAttendance)
                    .filter_by(
                        session_id=sess.id,
                        registration_id=reg.id,
                    )
                    .first()
                )
                if exists:
                    continue
                # 90% 出席
                session.add(
                    ActivityAttendance(
                        session_id=sess.id,
                        registration_id=reg.id,
                        student_id=reg.student_id,
                        is_present=random.random() < 0.9,
                        recorded_by="admin",
                    )
                )


# ============================================================
# Step 8: 公告、校事件
# ============================================================
ANNOUNCEMENTS = [
    {
        "title": "114 下學期開學通知",
        "content": "本學期開學日 2026/02/16,請家長注意作息調整。",
        "priority": "important",
        "pinned": True,
        "date": date(2026, 2, 1),
    },
    {
        "title": "114 下學期才藝課報名開放",
        "content": "課後才藝即日起開放報名,額滿為止。",
        "priority": "normal",
        "pinned": False,
        "date": date(2026, 2, 5),
    },
    {
        "title": "228 連假行事曆",
        "content": "2/28-3/1 連假,3/2 正常上課。",
        "priority": "normal",
        "pinned": False,
        "date": date(2026, 2, 20),
    },
    {
        "title": "戶外教學行前說明",
        "content": "4/15 中、大班戶外教學,請依時繳交回條。",
        "priority": "important",
        "pinned": False,
        "date": date(2026, 3, 25),
    },
    {
        "title": "母親節慶祝活動",
        "content": "5/8 全園母親節慶祝,歡迎家長蒞臨。",
        "priority": "normal",
        "pinned": False,
        "date": date(2026, 4, 10),
    },
    {
        "title": "畢業典禮報名",
        "content": "大班畢業典禮 6/25 舉行,家長請至教室領取邀請函。",
        "priority": "important",
        "pinned": True,
        "date": date(2026, 4, 18),
    },
]


def step_announcements_events():
    logger.info("=== Step 8: 公告、校事件 ===")
    with session_scope() as session:
        admin_emp = session.query(Employee).filter_by(employee_id="E020").first()
        if not admin_emp:
            admin_emp = session.query(Employee).first()

        for a in ANNOUNCEMENTS:
            exists = session.query(Announcement).filter_by(title=a["title"]).first()
            if exists:
                continue
            ann = Announcement(
                title=a["title"],
                content=a["content"],
                priority=a["priority"],
                is_pinned=a["pinned"],
                created_by=admin_emp.id,
                created_at=datetime.combine(a["date"], time(9, 0)),
            )
            session.add(ann)

        events = [
            {
                "title": "開學日",
                "event_date": date(2026, 2, 16),
                "event_type": "general",
            },
            {
                "title": "228 和平紀念日",
                "event_date": date(2026, 2, 28),
                "event_type": "holiday",
            },
            {
                "title": "兒童節活動",
                "event_date": date(2026, 4, 4),
                "event_type": "activity",
            },
            {
                "title": "戶外教學(中、大班)",
                "event_date": date(2026, 4, 15),
                "event_type": "activity",
            },
            {
                "title": "母親節慶祝",
                "event_date": date(2026, 5, 8),
                "event_type": "activity",
            },
            {
                "title": "端午節連假",
                "event_date": date(2026, 6, 19),
                "end_date": date(2026, 6, 21),
                "event_type": "holiday",
            },
            {
                "title": "畢業典禮",
                "event_date": date(2026, 6, 25),
                "event_type": "activity",
            },
            {
                "title": "結業式",
                "event_date": date(2026, 6, 30),
                "event_type": "general",
            },
        ]
        for ev in events:
            exists = (
                session.query(SchoolEvent)
                .filter_by(
                    title=ev["title"],
                    event_date=ev["event_date"],
                )
                .first()
            )
            if exists:
                continue
            session.add(SchoolEvent(**ev, is_all_day=True))


# ============================================================
# Step 9: 學生評量、事件、接送、家長通訊
# ============================================================
def step_student_records():
    logger.info("=== Step 9: 學生評量/事件/接送/家長通訊 ===")
    with session_scope() as session:
        students = (
            session.query(Student).filter(Student.is_active == True).all()
        )  # noqa: E712
        admin = session.query(User).filter_by(username="admin").first()

        # 9a) 期中評量(每位學生 1 筆)
        for stu in students[:80]:
            exists = (
                session.query(StudentAssessment)
                .filter_by(
                    student_id=stu.id,
                    semester=PERIOD,
                    assessment_type="期中",
                )
                .first()
            )
            if exists:
                continue
            session.add(
                StudentAssessment(
                    student_id=stu.id,
                    semester=PERIOD,
                    assessment_type="期中",
                    domain=random.choice(
                        ["身體動作與健康", "語文", "認知", "社會", "情緒", "美感"]
                    ),
                    rating=random.choices(["優", "良", "需加強"], weights=[40, 50, 10])[
                        0
                    ],
                    content=f"{stu.name} 本學期表現{random.choice(['積極主動', '穩定進步', '專注力提升', '社交互動良好'])}。",
                    suggestions=random.choice(
                        ["持續鼓勵", "建議多練習團體活動", "建議家長配合作息調整"]
                    ),
                    assessment_date=date(2026, 4, random.randint(5, 18)),
                    recorded_by=admin.id if admin else None,
                )
            )

        # 9b) 學生事件(20 筆)
        sample = random.sample(students, min(20, len(students)))
        for i, stu in enumerate(sample):
            occurred = datetime(
                2026,
                random.choice([2, 3, 4]),
                random.randint(1, 28),
                random.randint(9, 16),
                0,
            )
            exists = (
                session.query(StudentIncident)
                .filter_by(
                    student_id=stu.id,
                    occurred_at=occurred,
                )
                .first()
            )
            if exists:
                continue
            session.add(
                StudentIncident(
                    student_id=stu.id,
                    incident_type=random.choice(["身體健康", "意外受傷", "行為觀察"]),
                    severity=random.choice(["輕微", "輕微", "中度"]),
                    occurred_at=occurred,
                    description=random.choice(
                        [
                            "戶外活動時不慎跌倒,膝蓋輕微擦傷,已消毒處理。",
                            "午餐時不太願意進食,觀察情緒。",
                            "與同學爭執玩具,經安撫後和解。",
                            "下午發燒 38 度,通知家長帶回。",
                        ]
                    ),
                    action_taken="已即時處理並聯絡家長。",
                    parent_notified=True,
                    parent_notified_at=occurred + timedelta(minutes=30),
                    recorded_by=admin.id if admin else None,
                )
            )

        # 9c) 接送叫號(今天部分學生)
        active_classrooms = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        for cls_id in active_classrooms:
            students_in = (
                session.query(Student)
                .filter(
                    Student.classroom_id == cls_id,
                    Student.is_active == True,  # noqa: E712
                )
                .limit(3)
                .all()
            )
            for stu in students_in:
                exists = (
                    session.query(StudentDismissalCall)
                    .filter(
                        StudentDismissalCall.student_id == stu.id,
                        func.date(StudentDismissalCall.requested_at) == TODAY,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    StudentDismissalCall(
                        student_id=stu.id,
                        classroom_id=cls_id,
                        requested_by_user_id=admin.id if admin else 1,
                        requested_at=datetime(
                            TODAY.year,
                            TODAY.month,
                            TODAY.day,
                            16,
                            random.randint(0, 30),
                        ),
                        status="pending",
                    )
                )

        # 9d) 家長通訊紀錄(40 筆)
        sample2 = random.sample(students, min(40, len(students)))
        for stu in sample2:
            d = date(2026, random.choice([2, 3, 4]), random.randint(1, 18))
            exists = (
                session.query(ParentCommunicationLog)
                .filter_by(
                    student_id=stu.id,
                    communication_date=d,
                )
                .first()
            )
            if exists:
                continue
            session.add(
                ParentCommunicationLog(
                    student_id=stu.id,
                    communication_date=d,
                    communication_type=random.choice(
                        ["電話", "LINE", "面談", "家聯簿"]
                    ),
                    topic=random.choice(
                        ["學習狀況", "生活習慣", "健康", "缺席通知", "活動報名"]
                    ),
                    content="與家長溝通孩子近期表現,家長表示理解並會配合。",
                    recorded_by=admin.id if admin else None,
                )
            )


# ============================================================
# Step 10: 扣款類型、獎金類型
# ============================================================
def step_allowances():
    logger.info("=== Step 10: 扣款/獎金類型 ===")
    with session_scope() as session:
        deductions = [
            ("late_deduction", "遲到扣款", "discipline"),
            ("early_leave_deduction", "早退扣款", "discipline"),
            ("absence_deduction", "曠職扣款", "discipline"),
            ("union_fee", "工會費", "other"),
        ]
        for code, name, cat in deductions:
            exists = session.query(DeductionType).filter_by(code=code).first()
            if not exists:
                session.add(
                    DeductionType(
                        code=code,
                        name=name,
                        category=cat,
                        is_active=True,
                    )
                )

        bonuses = [
            ("festival_bonus", "節慶獎金", True),
            ("performance_bonus", "績效獎金", False),
            ("birthday_bonus", "生日禮金", False),
            ("special_bonus", "特別獎金", True),
        ]
        for code, name, sep in bonuses:
            exists = session.query(BonusType).filter_by(code=code).first()
            if not exists:
                session.add(
                    BonusType(
                        code=code,
                        name=name,
                        is_separate_transfer=sep,
                        is_active=True,
                    )
                )
        session.flush()


# ============================================================
# Step 11: 補薪資 6, 7 月(簡化:複製 5 月並標 finalized=False)
# ============================================================
def step_salary():
    logger.info("=== Step 11: 補薪資 6, 7 月 ===")
    with session_scope() as session:
        # 取每個員工的 5 月薪資
        may_records = (
            session.query(SalaryRecord)
            .filter_by(
                salary_year=2026,
                salary_month=5,
            )
            .all()
        )
        for src in may_records:
            for tgt_month in [6, 7]:
                exists = (
                    session.query(SalaryRecord)
                    .filter_by(
                        employee_id=src.employee_id,
                        salary_year=2026,
                        salary_month=tgt_month,
                    )
                    .first()
                )
                if exists:
                    continue
                # 複製主要欄位
                copy = SalaryRecord(
                    employee_id=src.employee_id,
                    bonus_config_id=src.bonus_config_id,
                    attendance_policy_id=src.attendance_policy_id,
                    salary_year=2026,
                    salary_month=tgt_month,
                    base_salary=src.base_salary,
                    festival_bonus=src.festival_bonus if tgt_month == 6 else 0,
                    overtime_bonus=src.overtime_bonus,
                    performance_bonus=src.performance_bonus,
                    special_bonus=src.special_bonus,
                    overtime_pay=src.overtime_pay,
                    meeting_overtime_pay=src.meeting_overtime_pay,
                    birthday_bonus=src.birthday_bonus,
                    work_hours=src.work_hours,
                    hourly_rate=src.hourly_rate,
                    hourly_total=src.hourly_total,
                    labor_insurance_employee=src.labor_insurance_employee,
                    labor_insurance_employer=src.labor_insurance_employer,
                    health_insurance_employee=src.health_insurance_employee,
                    health_insurance_employer=src.health_insurance_employer,
                    pension_employee=src.pension_employee,
                    pension_employer=src.pension_employer,
                    gross_salary=src.gross_salary,
                    total_deduction=src.total_deduction,
                    net_salary=src.net_salary,
                    is_finalized=False,
                    remark=f"[seed] 由 5 月複製,待重新計算",
                )
                session.add(copy)


# ============================================================
# Step 12: 會議記錄(2026/02-04 每月一次)
# ============================================================
def step_meetings():
    logger.info("=== Step 12: 園務會議 ===")
    with session_scope() as session:
        emps = (
            session.query(Employee).filter(Employee.is_active == True).all()
        )  # noqa: E712
        meeting_dates = [date(2026, 2, 14), date(2026, 3, 14), date(2026, 4, 11)]
        for d in meeting_dates:
            for emp in emps:
                exists = (
                    session.query(MeetingRecord)
                    .filter_by(
                        employee_id=emp.id,
                        meeting_date=d,
                    )
                    .first()
                )
                if exists:
                    continue
                attended = random.random() < 0.93
                session.add(
                    MeetingRecord(
                        employee_id=emp.id,
                        meeting_date=d,
                        meeting_type="staff_meeting",
                        attended=attended,
                        overtime_hours=2.0 if attended else 0,
                        overtime_pay=200 if attended else 0,
                    )
                )


# ============================================================
# 主程式
# ============================================================
ALL_STEPS = {
    "students": step_students,
    "guardians": step_guardians,
    "fees": step_fees,
    "attendance": step_attendance,
    "shifts": step_shifts,
    "overtime": step_overtime_punch,
    "activities": step_activities,
    "announcements": step_announcements_events,
    "student_records": step_student_records,
    "allowances": step_allowances,
    "salary": step_salary,
    "meetings": step_meetings,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--step",
        default="all",
        help=f"逗號分隔。可選:all 或 {','.join(ALL_STEPS.keys())}",
    )
    args = parser.parse_args()

    if args.step == "all":
        steps = list(ALL_STEPS.keys())
    else:
        steps = [s.strip() for s in args.step.split(",") if s.strip()]

    for s in steps:
        if s not in ALL_STEPS:
            logger.error("未知 step: %s", s)
            sys.exit(1)
        try:
            ALL_STEPS[s]()
        except Exception:
            logger.exception("Step %s 失敗", s)
            sys.exit(1)
    logger.info("所有 step 執行完成")


if __name__ == "__main__":
    main()
