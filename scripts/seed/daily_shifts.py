"""scripts/seed/daily_shifts.py — 每日排班（調班/換班）示範資料 seed。

模組：daily_shifts（model `models.shift.DailyShift`）。
語意：DailyShift 是「每日排班」覆寫層，優先於 ShiftAssignment（每週排班）。
- shift_type_id 非 None → 該日明確換成此班別（調班／換班後生效班別）
- shift_type_id is None → 該日明確排休，不繼承週排班（換班後該日不上班）
（見 models/shift.py 的 DailyShift docstring、api/portal/schedule.py 的 daily override 邏輯。）

本 seed 直接寫 DB，為在職員工依其「該週週排班」產生少量每日調班列：
- 取 3 個時間窗的工作日：2025-09 整月、2026-04 整月、2026-05-01 ~ 2026-06-05(TODAY)
- 僅排工作日（週一~週五），日期範圍 2025-08-01 ~ 2026-06-05，絕不生未來
- 每位員工每個合格工作日約 1/4 決定性抽中產生一筆 DailyShift：
    · 多數（~5/6）= 調班：換成「與該週週排班不同」的另一個 active 班別
    · 少數（~1/6）= 明確排休：shift_type_id=None（換班後該日不上班）
- 找不到該員工該週之 ShiftAssignment 則跳過該日（無可調換的基準週班別）

薪資安全（重要）：
DailyShift 會被薪資引擎讀去建 expected_workdays（services/salary/proration.py）。
- 非 None 班別只落在「平日」→ 該日本來就是預期上班日，不改變薪資預期（安全）。
- None（排休）只落在平日 → 僅「移除」一天預期上班（縮小，不會虛增曠職，安全）。
- 絕不在週末產生非 None 班別（否則會虛增應上班日 → 無打卡時誤判曠職扣款）。
本 seed 因此只在週一~週五產生資料；dev DB 目前無任何月份 is_finalized，不踩封存守衛。

冪等契約：唯一鍵 (employee_id, date)（uq_daily_shift_employee_date）。
每筆插入前先 exists 查；重跑必新增 0 筆、不刪改現有資料。
"""

from __future__ import annotations

import logging
import random
from datetime import date, timedelta

from scripts.seed._common import (
    session_scope,
    get_active_employees,
    TODAY,
)
from scripts.seed._common import _is_workday  # noqa: F401  純 helper（工作日判定）
from scripts.seed_test_data_114_2 import _date_range

from models.shift import ShiftType, ShiftAssignment, DailyShift

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_daily_shifts")

# _is_workday(d, holiday_set, workday_set)：seed 不需精確假日曆，傳空集合即
# 退化為「週一~週五」判定（無國定假日扣除、無補班日加入）。
_NO_HOLIDAYS: set[date] = set()
_NO_MAKEUP_DAYS: set[date] = set()

# 取樣時間窗（皆 ≤ TODAY；上限再以 TODAY 夾一次，絕不生未來）
_WINDOWS: tuple[tuple[date, date], ...] = (
    (date(2025, 9, 1), date(2025, 9, 30)),  # 上學期一個月
    (date(2026, 4, 1), date(2026, 4, 30)),  # 下學期一個月
    (date(2026, 5, 1), date(2026, 6, 5)),  # 近一個月（含今天）
)

# 每位員工每個合格工作日抽中產生 DailyShift 的機率（控制總量）
_PICK_RATIO = 0.25
# 抽中後改為「明確排休（shift_type_id=None）」的比例，其餘為調班
_REST_RATIO = 6  # 約 1/6 排休

# 決定性 RNG salt（重跑取到同一批日期 → idempotent）
_RNG_SALT = 11420

# 換班備註（調班）／排休備註
_SWAP_NOTES = (
    "與同事調班",
    "支援其他班級調班",
    "活動支援調班",
    "臨時調整班別",
    "代班調整",
)
_REST_NOTES = (
    "換班後當日排休",
    "調班補休",
    "與同事互換值班，當日不值",
)


def _is_seed_workday(d: date) -> bool:
    return _is_workday(d, _NO_HOLIDAYS, _NO_MAKEUP_DAYS)


def _week_start(d: date) -> date:
    """回傳 d 所屬週的週一（對齊 ShiftAssignment.week_start_date 慣例）。"""
    return d - timedelta(days=d.weekday())


def step() -> int:
    """灌每日排班（調班/換班）示範資料；回傳本次新增筆數。冪等：重跑回 0。"""
    logger.info("=== Seed: 每日排班（daily_shifts / 調班換班）===")
    added = 0
    with session_scope() as session:
        employees = get_active_employees(session)
        if not employees:
            logger.warning("dev DB 無 active 員工，跳過")
            return 0

        # active 班別（供調班挑「不同班別」）
        active_types = (
            session.query(ShiftType)
            .filter(ShiftType.is_active == True)  # noqa: E712
            .order_by(ShiftType.sort_order, ShiftType.id)
            .all()
        )
        if len(active_types) < 2:
            logger.warning("active 班別不足 2 種，無法產生調班，跳過")
            return 0
        active_type_ids = [t.id for t in active_types]

        # 預載所有在職員工的週排班 → {(employee_id, week_start): shift_type_id}
        emp_ids = [e.id for e in employees]
        assignments = (
            session.query(
                ShiftAssignment.employee_id,
                ShiftAssignment.week_start_date,
                ShiftAssignment.shift_type_id,
            )
            .filter(ShiftAssignment.employee_id.in_(emp_ids))
            .all()
        )
        weekly_map: dict[tuple[int, date], int] = {
            (a.employee_id, a.week_start_date): a.shift_type_id for a in assignments
        }
        if not weekly_map:
            logger.warning("dev DB 無 shift_assignments，無調班基準，跳過")
            return 0

        # 預載既有 daily_shifts 的 (employee_id, date) 供冪等判定（一次撈，不逐筆查）
        existing_keys: set[tuple[int, date]] = set(
            session.query(DailyShift.employee_id, DailyShift.date).all()
        )

        # 收集所有合格工作日（落在時間窗、≤ TODAY、週一~週五），去重排序
        workdays: list[date] = []
        seen: set[date] = set()
        for lo, hi in _WINDOWS:
            if hi > TODAY:
                hi = TODAY
            if lo > hi:
                continue
            for d in _date_range(lo, hi):
                if d in seen:
                    continue
                seen.add(d)
                if _is_seed_workday(d):
                    workdays.append(d)
        workdays.sort()

        for emp in employees:
            for d in workdays:
                # 每筆 (員工, 日期) 用獨立決定性 RNG（seed 僅取決於 emp.id 與該日序數）。
                # 關鍵：抽樣決定與「該鍵是否已存在」完全脫鉤，因此既有資料的
                # exists-skip 不會打亂任何 RNG 串流 → 重跑必得同一批抽樣（嚴格冪等）。
                rng = random.Random(
                    _RNG_SALT * 1_000_003 + emp.id * 100_003 + d.toordinal()
                )

                # 決定性抽中
                if rng.random() >= _PICK_RATIO:
                    continue

                # 冪等：唯一鍵 (employee_id, date) 已存在則跳過
                if (emp.id, d) in existing_keys:
                    continue

                # 需有該週週排班作為調班基準，否則跳過
                base_type_id = weekly_map.get((emp.id, _week_start(d)))
                if base_type_id is None:
                    continue

                # 少數明確排休（shift_type_id=None），多數調班為「不同班別」
                if rng.randint(1, _REST_RATIO) == 1:
                    new_type_id: int | None = None
                    note = rng.choice(_REST_NOTES)
                else:
                    # 換成與週排班不同的 active 班別
                    candidates = [t for t in active_type_ids if t != base_type_id]
                    if not candidates:
                        continue
                    new_type_id = rng.choice(candidates)
                    note = rng.choice(_SWAP_NOTES)

                session.add(
                    DailyShift(
                        employee_id=emp.id,
                        shift_type_id=new_type_id,
                        date=d,
                        notes=note,
                    )
                )
                existing_keys.add((emp.id, d))
                added += 1

        session.flush()
        logger.info("每日排班本次新增 %d 筆", added)
    return added


if __name__ == "__main__":
    step()
