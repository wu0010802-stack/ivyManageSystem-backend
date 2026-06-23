"""月度在籍人數快照 service（L2，spec 2026-06-13-enrollment-count-correctness）。

薪資引擎的獎金人數來源統一走 resolve_bonus_counts：
  有快照 → 快照（HR 看過/手調/確認的數字，歷史重算永遠重現）
  無快照 → compute_live_counts 即時計算（與既有行為零漂移）
"""

from __future__ import annotations

import calendar
import logging
from datetime import date

from services.student_enrollment import (
    classroom_student_count_map,
    count_students_active_on,
)
from utils.rounding import round_half_up

logger = logging.getLogger(__name__)

MODE_MONTH_END = "month_end"
MODE_DAILY_WEIGHTED = "daily_weighted"


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _num(value):
    """Numeric/float 正規化：整數值回 int（顯示與既有 int 行為一致），其餘 float。"""
    f = float(value or 0)
    return int(f) if f == int(f) else f


def compute_live_counts(session, year: int, month: int, mode: str = MODE_MONTH_END):
    """即時計算該月人數：{"school": n, "classes": {classroom_id: n}}。

    month_end 模式 = 既有語意（月底單日快照，含 L1a withdrawal 過濾與
    L1b 轉班歷史感知）。daily_weighted 見 L3。
    """
    if mode == MODE_DAILY_WEIGHTED:
        return _compute_daily_weighted_counts(session, year, month)
    target = _month_end(year, month)
    return {
        "school": count_students_active_on(session, target),
        "classes": classroom_student_count_map(session, target),
    }


def _compute_daily_weighted_counts(session, year: int, month: int):
    """按日加權平均在籍：Σ(每日在籍數) ÷ 當月日曆天數，1 位小數（L3）。

    邊界對齊 month_end 模式的 student_active_on_filter：
    - 在籍區間 = [max(月初, enrollment_date), min(月底, graduation_date,
      withdrawal_date - 1 天)]（withdrawal 當日不在籍、graduation 當日在籍）
    - 班級歸屬依 StudentClassroomTransfer 按日分段（轉班日起屬新班），
      語意同 classroom_student_count_map 的歷史感知。
    """
    from collections import defaultdict
    from datetime import timedelta

    from sqlalchemy import or_

    from models.database import Student
    from models.student_transfer import StudentClassroomTransfer

    days_in_month = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, days_in_month)

    students = (
        session.query(
            Student.id,
            Student.classroom_id,
            Student.enrollment_date,
            Student.graduation_date,
            Student.withdrawal_date,
        )
        .filter(
            or_(
                Student.enrollment_date.is_(None), Student.enrollment_date <= month_end
            ),
            or_(
                Student.graduation_date.is_(None),
                Student.graduation_date >= month_start,
            ),
            or_(
                Student.withdrawal_date.is_(None),
                Student.withdrawal_date > month_start,
            ),
        )
        .all()
    )
    if not students:
        return {"school": 0.0, "classes": {}}

    transfers_by_student: dict[int, list] = defaultdict(list)
    rows = (
        session.query(
            StudentClassroomTransfer.student_id,
            StudentClassroomTransfer.from_classroom_id,
            StudentClassroomTransfer.to_classroom_id,
            StudentClassroomTransfer.transferred_at,
        )
        .filter(StudentClassroomTransfer.student_id.in_([s.id for s in students]))
        .order_by(
            StudentClassroomTransfer.transferred_at.asc(),
            StudentClassroomTransfer.id.asc(),
        )
        .all()
    )
    for r in rows:
        transfers_by_student[r.student_id].append(r)

    class_days: dict[int, int] = defaultdict(int)
    school_days = 0
    for s in students:
        seg_start = max(month_start, s.enrollment_date or month_start)
        active_end = month_end
        if s.graduation_date:
            active_end = min(active_end, s.graduation_date)
        if s.withdrawal_date:
            active_end = min(active_end, s.withdrawal_date - timedelta(days=1))
        if seg_start > active_end:
            continue
        school_days += (active_end - seg_start).days + 1

        # 起始班：≤ seg_start 最後一筆轉班的 to；無則更晚轉班的 from；再無則現態
        transfers = transfers_by_student.get(s.id, [])
        current_class = s.classroom_id
        later_transfers = []
        last_before = None
        for t in transfers:
            if t.transferred_at.date() <= seg_start:
                last_before = t
            else:
                later_transfers.append(t)
        if last_before is not None:
            current_class = last_before.to_classroom_id
        elif later_transfers:
            current_class = later_transfers[0].from_classroom_id

        for t in later_transfers:
            t_date = t.transferred_at.date()
            if t_date > active_end:
                break
            if current_class is not None:
                class_days[current_class] += (t_date - seg_start).days
            current_class = t.to_classroom_id
            seg_start = t_date
        if current_class is not None:
            class_days[current_class] += (active_end - seg_start).days + 1

    return {
        "school": round_half_up(school_days / days_in_month, 1),
        "classes": {
            cid: round_half_up(days / days_in_month, 1)
            for cid, days in class_days.items()
            if days > 0
        },
    }


def generate_snapshot(
    session,
    year: int,
    month: int,
    *,
    mode: str | None = None,
    updated_by: str | None = None,
    force: bool = False,
) -> dict:
    """產生/重產該月快照（upsert）。

    - 已確認（is_confirmed）列預設保留不覆寫（HR 手調過的數字是結算依據），
      force=True 才強制覆寫並清除確認狀態。
    - 回傳 {"generated": 寫入列數, "changes": [異動明細]}（呼叫端自行 commit）。
    """
    from models.enrollment_snapshot import ClassEnrollmentSnapshot

    if mode is None:
        mode = _live_mode(session, year)
    live = compute_live_counts(session, year, month, mode)
    existing_rows = (
        session.query(ClassEnrollmentSnapshot)
        .filter(
            ClassEnrollmentSnapshot.snapshot_year == year,
            ClassEnrollmentSnapshot.snapshot_month == month,
        )
        .all()
    )
    by_classroom = {row.classroom_id: row for row in existing_rows}

    # 目標集合：全校列（None）＋ live 出現的班 ＋ 既有快照中的班（班清空 → 更新為 0）
    targets: dict[int | None, float] = {None: float(live["school"])}
    for cid, n in live["classes"].items():
        targets[cid] = float(n)
    for cid in by_classroom:
        targets.setdefault(cid, 0.0)

    changes: list[dict] = []
    generated = 0
    for cid, new_value in targets.items():
        row = by_classroom.get(cid)
        if row is None:
            session.add(
                ClassEnrollmentSnapshot(
                    snapshot_year=year,
                    snapshot_month=month,
                    classroom_id=cid,
                    student_count=new_value,
                    count_mode=mode,
                    updated_by=updated_by,
                )
            )
            generated += 1
            changes.append(
                {"classroom_id": cid, "before": None, "after": _num(new_value)}
            )
            continue
        if row.is_confirmed and not force:
            continue
        old_value = float(row.student_count or 0)
        if old_value != new_value or row.count_mode != mode:
            changes.append(
                {
                    "classroom_id": cid,
                    "before": _num(old_value),
                    "after": _num(new_value),
                }
            )
        row.student_count = new_value
        row.count_mode = mode
        row.updated_by = updated_by
        if force and row.is_confirmed:
            row.is_confirmed = False
            row.confirmed_by = None
            row.confirmed_at = None
        generated += 1

    return {"generated": generated, "changes": changes}


def unconfirmed_distribution_months(session, year: int, month: int):
    """發放月結算前 gate：回「尚未產生或尚未確認」的涵蓋月清單（決策2）。

    非發放月回 []（人數不進累計，不設限）。發放月（2/6/9/12）逐一檢查
    get_distribution_period_months 的涵蓋月：該月無任何快照列、或有任一列
    未 is_confirmed → 列為待辦。calculate 端點據此決定是否 raise 422。
    """
    from models.enrollment_snapshot import ClassEnrollmentSnapshot
    from services.salary.utils import get_distribution_period_months

    covered = get_distribution_period_months(year, month)
    pending = []
    for y, m in covered:
        rows = (
            session.query(ClassEnrollmentSnapshot.is_confirmed)
            .filter(
                ClassEnrollmentSnapshot.snapshot_year == y,
                ClassEnrollmentSnapshot.snapshot_month == m,
            )
            .all()
        )
        if not rows or not all(confirmed for (confirmed,) in rows):
            pending.append((y, m))
    return pending


def get_snapshot_counts(session, year: int, month: int):
    """讀該月快照：{"school": n, "classes": {...}}；無任何列回 None。

    全校列缺漏（理論上不會）時以班級列合計補。
    """
    from models.enrollment_snapshot import ClassEnrollmentSnapshot

    rows = (
        session.query(ClassEnrollmentSnapshot)
        .filter(
            ClassEnrollmentSnapshot.snapshot_year == year,
            ClassEnrollmentSnapshot.snapshot_month == month,
        )
        .all()
    )
    if not rows:
        return None
    classes = {
        row.classroom_id: _num(row.student_count)
        for row in rows
        if row.classroom_id is not None
    }
    school_row = next((row for row in rows if row.classroom_id is None), None)
    school = (
        _num(school_row.student_count)
        if school_row is not None
        else _num(sum(classes.values()))
    )
    return {"school": school, "classes": classes}


def resolve_bonus_counts(session, year: int, month: int):
    """獎金人數統一入口：回 (school_total, class_count_map)。

    有快照讀快照；無快照即時計算（mode 由 BonusConfig.enrollment_count_mode
    決定，見 L3；表缺欄位/無設定時為 month_end，與既有行為零漂移）。
    """
    snapshot = get_snapshot_counts(session, year, month)
    if snapshot is not None:
        return snapshot["school"], snapshot["classes"]
    live = compute_live_counts(session, year, month, _live_mode(session, year))
    return _num(live["school"]), {k: _num(v) for k, v in live["classes"].items()}


def _live_mode(session, year: int | None = None) -> str:
    """無快照時的即時計算模式：讀 BonusConfig.enrollment_count_mode（L3）。

    優先以年度解析（config_resolver，歷史重算對齊該年度設定）；缺年度列
    fallback 最新 active；整表空（dev/測試/全新部署）→ month_end（零漂移）。
    """
    from models.config import BonusConfig

    if session.query(BonusConfig.id).first() is None:
        return MODE_MONTH_END
    row = None
    if year is not None:
        try:
            from services.salary.config_resolver import resolve_config

            row = resolve_config(session, BonusConfig, year, year_col="config_year")
        except Exception:
            row = None
    if row is None:
        row = (
            session.query(BonusConfig)
            .filter(BonusConfig.is_active.is_(True))
            .order_by(BonusConfig.id.desc())
            .first()
        )
    mode = getattr(row, "enrollment_count_mode", None) if row is not None else None
    return mode or MODE_MONTH_END
