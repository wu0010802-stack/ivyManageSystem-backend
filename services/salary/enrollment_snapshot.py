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
    """按日加權平均在籍：Σ(每日在籍數) ÷ 當月日曆天數，1 位小數（L3）。"""
    raise NotImplementedError("daily_weighted 於 L3 實作")


def generate_snapshot(
    session,
    year: int,
    month: int,
    *,
    mode: str = MODE_MONTH_END,
    updated_by: str | None = None,
    force: bool = False,
) -> dict:
    """產生/重產該月快照（upsert）。

    - 已確認（is_confirmed）列預設保留不覆寫（HR 手調過的數字是結算依據），
      force=True 才強制覆寫並清除確認狀態。
    - 回傳 {"generated": 寫入列數, "changes": [異動明細]}（呼叫端自行 commit）。
    """
    from models.enrollment_snapshot import ClassEnrollmentSnapshot

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
    live = compute_live_counts(session, year, month, _live_mode(session))
    return _num(live["school"]), {k: _num(v) for k, v in live["classes"].items()}


def _live_mode(session) -> str:
    """無快照時的即時計算模式（L3 接 BonusConfig.enrollment_count_mode）。"""
    return MODE_MONTH_END
