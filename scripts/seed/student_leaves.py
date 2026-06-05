"""scripts/seed/student_leaves.py — 學生請假（家長為學生請假）示範資料 seed。

模組：student_leave_requests（model `models.student_leave.StudentLeaveRequest`）。
家長端流程：家長替學生送出病假/事假，提交即 status=approved（見
api/parent_portal/leaves.py）；管理端（api/student_leaves.py）僅唯讀檢視。

本 seed 直接寫 DB，灌全 114 學年示範資料：
- 取樣 ~30 名 active 學生，每人跨學年 1~2 筆請假
- 日期落在工作日（_is_workday），範圍 2025-08-01 ~ 2026-06-05（TODAY），絕不生未來
- status 多數 approved（補 reviewed_at + reviewed_by=admin），少數 pending
- leave_type 取 病假/事假；reason 用繁中
- applicant_user_id（NOT NULL，FK→users）：優先取該生 guardian 已綁定的家長 User，
  否則 fallback 到 admin（dev DB 僅極少數 guardian 有綁 user_id）
- applicant_guardian_id（nullable，FK→guardians）：該生主要/首位未軟刪 guardian

冪等契約：每筆插入前以 client_request_id（seed-slr-<student_id>-<n>）exists 查；
重跑必新增 0 筆、不刪改現有資料。亦在腳本內避免同一學生產生重疊的 approved 區間
（對齊 api 的 _check_overlap 語意，但 seed 自管不依賴 API）。
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta

from scripts.seed._common import (
    session_scope,
    get_active_students,
    get_admin_user,
    rand_date_between,
    TERM1,
    TERM2,
    TODAY,
)
from scripts.seed._common import _is_workday  # noqa: F401  純 helper（工作日判定）

from models.student_leave import StudentLeaveRequest, LEAVE_TYPES
from models.guardian import Guardian

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("seed_student_leaves")

# _is_workday(d, holiday_set, workday_set)：seed 不需精確假日曆，傳空集合即
# 退化為「週一~週五」判定（無國定假日扣除、無補班日加入）。
_NO_HOLIDAYS: set[date] = set()
_NO_MAKEUP_DAYS: set[date] = set()


def _is_seed_workday(d: date) -> bool:
    return _is_workday(d, _NO_HOLIDAYS, _NO_MAKEUP_DAYS)


# 取樣學生數（~30）；每人跨學年 1~2 筆
_SAMPLE_STUDENTS = 30
_MAX_PER_STUDENT = 2

# 約 1/6 維持 pending，其餘 approved
_PENDING_RATIO = 6

# 繁中請假事由（病假 / 事假 各一組）
_REASONS_SICK = (
    "感冒發燒在家休養",
    "腸胃炎不適",
    "感冒咳嗽就醫",
    "發燒至 38.5 度",
    "水痘需在家隔離",
    "腸病毒就醫休養",
    "中耳炎回診",
    "急性結膜炎",
)
_REASONS_PERSONAL = (
    "家庭旅遊",
    "返鄉探親",
    "參加親戚婚禮",
    "家中有事",
    "陪同家人就醫",
    "搬家整理",
    "參加才藝比賽",
    "颱風天家長決定在家",
)


def _reason_for(leave_type: str) -> str:
    pool = _REASONS_SICK if leave_type == "病假" else _REASONS_PERSONAL
    return random.choice(pool)


def _primary_guardian(session, student_id: int):
    """取該生主要（is_primary→sort_order→id）且未軟刪的 guardian；無則 None。"""
    return (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student_id,
            Guardian.deleted_at.is_(None),
        )
        .order_by(
            Guardian.is_primary.desc(),
            Guardian.sort_order.asc(),
            Guardian.id.asc(),
        )
        .first()
    )


def _workday_start(term: tuple[date, date]) -> date | None:
    """在 term 範圍（且 ≤ TODAY）內隨機抽一個工作日當請假首日；找不到回 None。"""
    lo, hi = term
    if hi > TODAY:
        hi = TODAY
    if lo > hi:
        return None
    # 嘗試多次抽到工作日（範圍很小時逐日掃）
    for _ in range(30):
        d = rand_date_between(lo, hi)
        if _is_seed_workday(d):
            return d
    # fallback：線性掃描
    d = lo
    while d <= hi:
        if _is_seed_workday(d):
            return d
        d += timedelta(days=1)
    return None


def _span_end(start: date) -> date:
    """請假 1~3 個工作日的結束日：自 start 起向後數 extra 個工作日，
    確保 end_date 仍落在工作日（跨週末會跳過六日），且不生未來。
    """
    extra = random.choice([0, 0, 1, 1, 2])  # 多數 1 天，少數 2~3 工作日
    end = start
    steps = 0
    while steps < extra:
        nxt = end + timedelta(days=1)
        if nxt > TODAY:
            break
        end = nxt
        if _is_seed_workday(end):
            steps += 1
    # 收尾保證落在工作日（理論上 start 已是工作日，迴圈也只在工作日累進）
    while end > start and not _is_seed_workday(end):
        end -= timedelta(days=1)
    return end


def _ranges_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and a_end >= b_start


def step() -> int:
    """灌學生請假示範資料；回傳本次新增筆數。冪等：重跑回 0。"""
    logger.info("=== Seed: 學生請假（student_leave_requests）===")
    added = 0
    with session_scope() as session:
        admin = get_admin_user(session)
        if admin is None:
            logger.warning("找不到任何 User（admin），無法填 applicant_user_id，跳過")
            return 0

        students = get_active_students(session)
        if not students:
            logger.warning("dev DB 無 active 學生，跳過")
            return 0

        # 決定性取樣：固定 seed 讓重跑取到同一批學生（即使資料已存在也 idempotent）
        rng = random.Random(11420)  # 114 學年 + 固定 salt
        sample = students[:]
        rng.shuffle(sample)
        sample = sample[: min(_SAMPLE_STUDENTS, len(sample))]

        for idx, student in enumerate(sample):
            guardian = _primary_guardian(session, student.id)
            guardian_id = guardian.id if guardian else None
            # applicant_user_id NOT NULL：優先綁定家長 User，否則 admin
            applicant_user_id = (
                guardian.user_id
                if (guardian is not None and guardian.user_id is not None)
                else admin.id
            )

            n_leaves = 1 + (idx % _MAX_PER_STUDENT)  # 交錯 1、2 筆
            # 跨學年：第 1 筆放 TERM1，第 2 筆放 TERM2
            terms = [TERM1, TERM2][:n_leaves]

            placed: list[tuple[date, date]] = []  # 本學生已排定的區間（避免重疊）
            for seq, term in enumerate(terms):
                start = _workday_start(term)
                if start is None:
                    continue
                end = _span_end(start)
                if end < start:
                    continue
                # 避免與本學生已排定（含 DB 既有 approved）的區間重疊
                if any(_ranges_overlap(start, end, ps, pe) for ps, pe in placed):
                    # 簡單位移：往後找下一個工作日重抽一次
                    nxt = _workday_start(term)
                    if nxt is None:
                        continue
                    start, end = nxt, _span_end(nxt)
                    if any(_ranges_overlap(start, end, ps, pe) for ps, pe in placed):
                        continue

                leave_type = LEAVE_TYPES[(idx + seq) % len(LEAVE_TYPES)]
                # 多數 approved、少數 pending（決定性）
                is_pending = ((idx + seq) % _PENDING_RATIO) == 0
                status = "pending" if is_pending else "approved"

                client_request_id = f"seed-slr-{student.id}-{seq}"

                # 冪等 pre-check：同 client_request_id 已存在則跳過
                exists = (
                    session.query(StudentLeaveRequest)
                    .filter(StudentLeaveRequest.client_request_id == client_request_id)
                    .first()
                )
                if exists is not None:
                    # 仍登記其區間，避免後續筆與既有重疊
                    placed.append((exists.start_date, exists.end_date))
                    continue

                reviewed_at: datetime | None = None
                reviewed_by: int | None = None
                review_note: str | None = None
                if status == "approved":
                    # 審核時間落在請假開始日當天 09:00（不生未來）
                    rv = datetime(start.year, start.month, start.day, 9, 0)
                    if rv.date() > TODAY:
                        rv = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0)
                    reviewed_at = rv
                    reviewed_by = admin.id
                    review_note = "系統示範資料：已核准"

                item = StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=applicant_user_id,
                    applicant_guardian_id=guardian_id,
                    leave_type=leave_type,
                    start_date=start,
                    end_date=end,
                    reason=_reason_for(leave_type),
                    status=status,
                    reviewed_by=reviewed_by,
                    reviewed_at=reviewed_at,
                    review_note=review_note,
                    client_request_id=client_request_id,
                )
                session.add(item)
                placed.append((start, end))
                added += 1

        session.flush()
        logger.info("學生請假本次新增 %d 筆", added)
    return added


if __name__ == "__main__":
    step()
