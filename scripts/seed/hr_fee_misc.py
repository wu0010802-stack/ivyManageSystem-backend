"""scripts/seed/hr_fee_misc.py — 人事 / 學費 / 行事曆 雜項空表 seed。

灌使用者面向但 dev DB 目前空白的幾張表，供前端手測有資料可看。
每張表各自 exists 冪等：重跑新增 0 筆、不刪改現有。

涵蓋（皆已確認 model 存在且有對應 router/UI）：
1. enrollment_certificates    — 在學證明（api/gov_moe/certificates.py）
2. overtime_comp_leave_grants — 加班換補休 grant（api/overtimes.py 等）
3. art_teacher_payroll_entries— 才藝/美術老師鐘點薪資（api/art_teacher_payroll.py）
4. student_fee_adjustments    — 學費調整/減免（api/fees/adjustments.py）
5. workday_overrides          — 補班/補假日（api/calendar_admin.py）

日期界線：事件日期 ≤ TODAY(2026-06-05)，絕不生未來；證書/補休「效期」欄位可跨未來。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func

from scripts.seed._common import (
    session_scope,
    get_active_students,
    get_active_employees,
    get_admin_user,
    rand_date_between,
    TERM1,
    TERM2,
    TODAY,
)

from models.gov_moe import EnrollmentCertificate
from models.overtime import OvertimeRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.art_teacher_payroll import ArtTeacherPayrollEntry
from models.fees import StudentFeeAdjustment, StudentFeeRecord
from models.event import WorkdayOverride
from models.classroom import Classroom

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. enrollment_certificates（在學證明）
# ===========================================================================
def _seed_enrollment_certificates(session) -> int:
    """為 ~10 名學生各開 1 張在學證明。

    冪等鍵：student_id（每生本 seed 只開 1 張）。year/seq 仿 router：
    year = issue_date.year、seq = 該年現有 max(seq)+1（沿用既有不衝突）。
    """
    students = get_active_students(session, limit=10)
    admin = get_admin_user(session)
    issued_by = admin.id if admin else None

    purposes = [
        "報稅扶養親屬證明",
        "申請育兒津貼",
        "戶政事務所遷戶籍用",
        "公司請領補助",
        "幼兒就學補助申請",
    ]

    added = 0
    for idx, stu in enumerate(students):
        exists = (
            session.query(EnrollmentCertificate)
            .filter(EnrollmentCertificate.student_id == stu.id)
            .first()
        )
        if exists:
            continue

        # 開立日期：本學年內、≤ TODAY
        issue_date = rand_date_between(TERM2[0], TODAY)
        year = issue_date.year
        # 每筆即時重算 seq，避免同一批次內 year+seq 唯一鍵衝突
        last_seq = (
            session.query(func.max(EnrollmentCertificate.seq))
            .filter(EnrollmentCertificate.year == year)
            .scalar()
        )
        seq = (last_seq or 0) + 1

        cert = EnrollmentCertificate(
            student_id=stu.id,
            year=year,
            seq=seq,
            purpose=purposes[idx % len(purposes)],
            copies=1 + (idx % 2),  # 1 或 2 份
            issue_date=issue_date,
            issued_by_user_id=issued_by,
        )
        session.add(cert)
        session.flush()  # 取得 seq 立刻可見，供下一筆 max() 算對
        added += 1

    return added


# ===========================================================================
# 2. overtime_comp_leave_grants（加班換補休 grant）
# ===========================================================================
def _seed_overtime_comp_leave_grants(session) -> int:
    """為既有 approved 加班記錄產生補休 grant。

    model overtime_record_id 為 unique（per-OT 一筆 grant），冪等鍵即此欄。
    仿 api/overtimes.py 邏輯：granted_hours=ot.hours、granted_at=ot.overtime_date、
    expires_at=granted_at+365 天（效期可跨未來，合法）。
    consumed_hours 製造已用/未用兩種狀態增加手測覆蓋。
    """
    ots = (
        session.query(OvertimeRecord)
        .filter(OvertimeRecord.status == "approved")
        .filter(OvertimeRecord.hours > 0)
        .order_by(OvertimeRecord.id)
        .all()
    )

    added = 0
    for i, ot in enumerate(ots):
        exists = (
            session.query(OvertimeCompLeaveGrant)
            .filter(OvertimeCompLeaveGrant.overtime_record_id == ot.id)
            .first()
        )
        if exists:
            continue

        granted_at = ot.overtime_date
        # 每隔一筆製造「已用部分時數」，但守 consumed_hours <= granted_hours
        consumed = round(ot.hours / 2, 2) if (i % 2 == 1) else 0.0

        grant = OvertimeCompLeaveGrant(
            overtime_record_id=ot.id,
            employee_id=ot.employee_id,
            granted_hours=ot.hours,
            granted_at=granted_at,
            expires_at=granted_at + timedelta(days=365),
            consumed_hours=consumed,
            status="active",
        )
        session.add(grant)
        # 與 router 對齊：標記該 OT 已發補休配額，避免重複發放
        ot.comp_leave_granted = True
        added += 1

    return added


# ===========================================================================
# 3. art_teacher_payroll_entries（才藝/美術老師鐘點薪資）
# ===========================================================================
def _seed_art_teacher_payroll_entries(session) -> int:
    """為美術老師建數筆鐘點 entry。

    美術老師判定：title/position 含「美」或被 classroom.art_teacher_id 指到。
    每老師為某一過去月份建 1~2 筆（不同科目/班級）。
    冪等鍵：(employee_id, salary_year, salary_month, subject)。
    """
    emps = get_active_employees(session)

    # classroom.art_teacher_id 指到的員工
    art_ids = {
        r[0]
        for r in session.query(Classroom.art_teacher_id)
        .filter(Classroom.art_teacher_id.isnot(None))
        .distinct()
        .all()
        if r[0]
    }

    def _is_art(e) -> bool:
        blob = f"{e.title or ''}{e.position or ''}".lower()
        return ("美" in blob) or ("art" in blob) or (e.id in art_ids)

    art_teachers = [e for e in emps if _is_art(e)]

    admin = get_admin_user(session)
    created_by = admin.username if admin else None

    # 過去月份（≤ TODAY 所在月）。TODAY=2026-06-05 → 用 4 月、5 月。
    target_months = [(2026, 4), (2026, 5)]
    subjects = ["美術才藝", "課後美語", "黏土創作"]

    added = 0
    for ti, emp in enumerate(art_teachers):
        # 每位老師至少 2 筆：固定一個月、可能跨兩科目
        for si in range(2):
            year, month = target_months[(ti + si) % len(target_months)]
            subject = subjects[(ti + si) % len(subjects)]

            exists = (
                session.query(ArtTeacherPayrollEntry)
                .filter(
                    ArtTeacherPayrollEntry.employee_id == emp.id,
                    ArtTeacherPayrollEntry.salary_year == year,
                    ArtTeacherPayrollEntry.salary_month == month,
                    ArtTeacherPayrollEntry.subject == subject,
                )
                .first()
            )
            if exists:
                continue

            hours = 8.0 + 2.0 * si  # 8 / 10 堂
            rate = 400 + 50 * (ti % 3)  # 400 / 450 / 500 元/堂
            base = int(round(hours * rate))
            excess = 0
            activity = 500 if si == 1 else 0
            total = base + excess + activity

            entry = ArtTeacherPayrollEntry(
                employee_id=emp.id,
                salary_year=year,
                salary_month=month,
                subject=subject,
                classroom_label=f"週{(ti % 5) + 1}",
                hours=hours,
                hourly_rate=rate,
                base_amount=base,
                excess_amount=excess,
                activity_bonus=activity,
                total_amount=total,
                note="dev seed 才藝鐘點",
                created_by=created_by,
                updated_by=created_by,
            )
            session.add(entry)
            added += 1

    return added


# ===========================================================================
# 4. student_fee_adjustments（學費調整 / 減免）
# ===========================================================================
def _seed_student_fee_adjustments(session) -> int:
    """挑既有 student_fee_records 數筆，建減免/折扣調整。

    冪等鍵：(student_id, period, adjustment_type)。
    amount 須 > 0（CheckConstraint ck_fee_adjustments_amount_positive）。
    """
    admin = get_admin_user(session)
    created_by = admin.username if admin else None

    # 取 114-2 有費用紀錄的學生（distinct），挑前 8 位灌折抵
    student_ids = [
        r[0]
        for r in session.query(StudentFeeRecord.student_id)
        .filter(StudentFeeRecord.period == "114-2")
        .filter(StudentFeeRecord.student_id.isnot(None))
        .distinct()
        .order_by(StudentFeeRecord.student_id)
        .limit(8)
        .all()
    ]

    period = "114-2"
    plans = [
        ("sibling_discount", 2000, "手足同園優惠（第二胎）"),
        ("other", 3000, "清寒補助減免"),
    ]

    added = 0
    for idx, sid in enumerate(student_ids):
        # 前半學生給手足優惠、後半給清寒補助，分散兩型
        adj_type, amount, reason = plans[idx % len(plans)]

        exists = (
            session.query(StudentFeeAdjustment)
            .filter(
                StudentFeeAdjustment.student_id == sid,
                StudentFeeAdjustment.period == period,
                StudentFeeAdjustment.adjustment_type == adj_type,
            )
            .first()
        )
        if exists:
            continue

        adj = StudentFeeAdjustment(
            student_id=sid,
            period=period,
            adjustment_type=adj_type,
            amount=amount,
            reason=reason,
            notes="dev seed 學費折抵",
            created_by=created_by,
        )
        session.add(adj)
        added += 1

    return added


# ===========================================================================
# 5. workday_overrides（補班 / 補假日）
# ===========================================================================
def _seed_workday_overrides(session) -> int:
    """建 2~4 筆補班/補假日，日期落 114 學年內、≤ TODAY。

    model date 為 unique，冪等鍵即此欄。
    """
    # 皆為過去日期（≤ TODAY），落學年內
    rows = [
        (date(2025, 9, 27), "颱風停課補課", "受楊柳颱風影響停課，週六補課"),
        (date(2025, 12, 6), "彈性放假補班", "12/8 彈性放假，週六補班"),
        (date(2026, 2, 14), "開學前置備課日", "下學期開學前教師備課，全園上班"),
    ]

    added = 0
    for d, name, desc in rows:
        exists = (
            session.query(WorkdayOverride).filter(WorkdayOverride.date == d).first()
        )
        if exists:
            continue
        session.add(
            WorkdayOverride(
                date=d,
                name=name,
                description=desc,
                is_active=True,
                source="seed",
                source_year=114,
            )
        )
        added += 1

    return added


# ===========================================================================
# 主 step
# ===========================================================================
def step() -> None:
    """灌人事/學費/行事曆雜項空表（各自冪等）。"""
    with session_scope() as session:
        n1 = _seed_enrollment_certificates(session)
        n2 = _seed_overtime_comp_leave_grants(session)
        n3 = _seed_art_teacher_payroll_entries(session)
        n4 = _seed_student_fee_adjustments(session)
        n5 = _seed_workday_overrides(session)

    logger.info("hr_fee_misc seed 完成")
    print(f"enrollment_certificates       新增 {n1} 筆")
    print(f"overtime_comp_leave_grants    新增 {n2} 筆")
    print(f"art_teacher_payroll_entries   新增 {n3} 筆")
    print(f"student_fee_adjustments       新增 {n4} 筆")
    print(f"workday_overrides             新增 {n5} 筆")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
