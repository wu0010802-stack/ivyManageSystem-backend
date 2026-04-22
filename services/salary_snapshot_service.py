"""薪資快照服務 — 不可變歷史快照的建立/查詢/比對。

SalaryRecord 為可變工作副本（每次重算 UPDATE 覆蓋），
SalarySnapshot 為不可變歷史，固定某時間點的薪資狀態。

快照類型：
- month_end：月底自動（Lazy trigger + 排程雙保險）
- finalize：封存整月時同步寫
- manual：管理員手動補拍

方法：
- create_month_end_snapshots(session, year, month)
- create_finalize_snapshot(session, record, captured_by)
- create_manual_snapshot(session, year, month, captured_by, remark, employee_id=None)
- list_snapshots(session, year, month, employee_id=None)
- get_snapshot_detail(session, snapshot_id)
- diff_with_current(session, snapshot_id)

Session 管理：接受呼叫端傳入的 session，不自行 commit；
例外：排程與 background task 用內建 session_scope helper。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from models.base import session_scope
from models.salary import SalaryRecord, SalarySnapshot
from models.employee import Employee

logger = logging.getLogger(__name__)

SNAPSHOT_TYPES = ("month_end", "finalize", "manual")

# 快照 metadata 欄位（不從 SalaryRecord 複製）
_SNAPSHOT_META_FIELDS = frozenset(
    {
        "id",
        "salary_record_id",
        "employee_id",
        "salary_year",
        "salary_month",
        "snapshot_type",
        "captured_at",
        "captured_by",
        "source_version",
        "snapshot_remark",
    }
)


def _payload_columns() -> list[str]:
    """SalarySnapshot 需從 SalaryRecord 反射複製的欄位清單。

    使用反射自動帶欄位，SalaryRecord 新增 Money 欄位時
    兩表同步即可，service 不需改動。
    """
    snap_cols = {c.name for c in sa_inspect(SalarySnapshot).columns}
    rec_cols = {c.name for c in sa_inspect(SalaryRecord).columns}
    return sorted((snap_cols & rec_cols) - _SNAPSHOT_META_FIELDS)


_PAYLOAD_COLUMNS = _payload_columns()


def _copy_record_to_snapshot(
    record: SalaryRecord,
    snapshot_type: str,
    captured_by: Optional[str],
    snapshot_remark: Optional[str] = None,
) -> SalarySnapshot:
    """把 SalaryRecord 欄位複製到新的 SalarySnapshot 實例（未 add 進 session）。"""
    if snapshot_type not in SNAPSHOT_TYPES:
        raise ValueError(f"無效 snapshot_type={snapshot_type}；允許：{SNAPSHOT_TYPES}")
    snap = SalarySnapshot(
        salary_record_id=record.id,
        employee_id=record.employee_id,
        salary_year=record.salary_year,
        salary_month=record.salary_month,
        snapshot_type=snapshot_type,
        captured_at=datetime.now(),
        captured_by=captured_by,
        source_version=record.version or 1,
        snapshot_remark=snapshot_remark,
    )
    for col in _PAYLOAD_COLUMNS:
        setattr(snap, col, getattr(record, col, None))
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# 建立快照
# ─────────────────────────────────────────────────────────────────────────────


def create_month_end_snapshots(
    session: Session, year: int, month: int, captured_by: str = "system"
) -> int:
    """為該月所有 SalaryRecord 建立 type='month_end' 快照；idempotent。

    已存在 (emp, year, month, 'month_end') 的跳過，回傳新建筆數。
    呼叫端負責 commit。
    """
    records = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .all()
    )
    if not records:
        return 0

    existing_emp_ids = {
        row[0]
        for row in session.query(SalarySnapshot.employee_id)
        .filter(
            SalarySnapshot.salary_year == year,
            SalarySnapshot.salary_month == month,
            SalarySnapshot.snapshot_type == "month_end",
        )
        .all()
    }

    created = 0
    for r in records:
        if r.employee_id in existing_emp_ids:
            continue
        session.add(_copy_record_to_snapshot(r, "month_end", captured_by))
        created += 1
    if created:
        logger.info(
            "salary month_end snapshot: %d/%d created for %d/%d by %s",
            created,
            len(records),
            year,
            month,
            captured_by,
        )
    return created


def create_finalize_snapshot(
    session: Session, record: SalaryRecord, captured_by: str
) -> SalarySnapshot:
    """封存單筆時同步寫 type='finalize' 快照。呼叫端負責 commit。"""
    snap = _copy_record_to_snapshot(record, "finalize", captured_by)
    session.add(snap)
    return snap


def create_manual_snapshot(
    session: Session,
    year: int,
    month: int,
    captured_by: str,
    remark: Optional[str] = None,
    employee_id: Optional[int] = None,
) -> int:
    """手動補拍快照；employee_id=None 表示整月所有員工。呼叫端負責 commit。"""
    q = session.query(SalaryRecord).filter(
        SalaryRecord.salary_year == year,
        SalaryRecord.salary_month == month,
    )
    if employee_id is not None:
        q = q.filter(SalaryRecord.employee_id == employee_id)
    records = q.all()
    for r in records:
        session.add(_copy_record_to_snapshot(r, "manual", captured_by, remark))
    if records:
        logger.info(
            "salary manual snapshot: %d created for %d/%d by %s (emp=%s)",
            len(records),
            year,
            month,
            captured_by,
            employee_id,
        )
    return len(records)


# ─────────────────────────────────────────────────────────────────────────────
# 查詢
# ─────────────────────────────────────────────────────────────────────────────


def _snapshot_summary(snap: SalarySnapshot, emp_name: Optional[str] = None) -> dict:
    """list 端點用的精簡 metadata。"""
    return {
        "id": snap.id,
        "salary_record_id": snap.salary_record_id,
        "employee_id": snap.employee_id,
        "employee_name": emp_name,
        "salary_year": snap.salary_year,
        "salary_month": snap.salary_month,
        "snapshot_type": snap.snapshot_type,
        "captured_at": snap.captured_at.isoformat() if snap.captured_at else None,
        "captured_by": snap.captured_by,
        "source_version": snap.source_version,
        "snapshot_remark": snap.snapshot_remark,
        "net_salary": snap.net_salary or 0,
    }


def list_snapshots(
    session: Session,
    year: int,
    month: int,
    employee_id: Optional[int] = None,
) -> list[dict]:
    """列出該月快照（精簡 metadata）。"""
    q = (
        session.query(SalarySnapshot, Employee.name)
        .outerjoin(Employee, SalarySnapshot.employee_id == Employee.id)
        .filter(
            SalarySnapshot.salary_year == year,
            SalarySnapshot.salary_month == month,
        )
    )
    if employee_id is not None:
        q = q.filter(SalarySnapshot.employee_id == employee_id)
    rows = q.order_by(SalarySnapshot.captured_at.desc()).all()
    return [_snapshot_summary(snap, name) for snap, name in rows]


def get_snapshot_detail(session: Session, snapshot_id: int) -> Optional[dict]:
    """回傳快照完整欄位（含所有金額/計數）。"""
    row = (
        session.query(SalarySnapshot, Employee.name)
        .outerjoin(Employee, SalarySnapshot.employee_id == Employee.id)
        .filter(SalarySnapshot.id == snapshot_id)
        .first()
    )
    if not row:
        return None
    snap, emp_name = row
    data = _snapshot_summary(snap, emp_name)
    for col in _PAYLOAD_COLUMNS:
        data[col] = getattr(snap, col, None)
    return data


def diff_with_current(session: Session, snapshot_id: int) -> Optional[dict]:
    """與當前 SalaryRecord 比對，回變動欄位。

    快照對應的 SalaryRecord 若已被刪（salary_record_id 為 NULL）或 record 不存在，
    以 (employee_id, year, month) 回查；仍查不到則 current 視為 None。
    """
    snap = (
        session.query(SalarySnapshot).filter(SalarySnapshot.id == snapshot_id).first()
    )
    if not snap:
        return None

    record: Optional[SalaryRecord] = None
    if snap.salary_record_id:
        record = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.id == snap.salary_record_id)
            .first()
        )
    if record is None:
        record = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == snap.employee_id,
                SalaryRecord.salary_year == snap.salary_year,
                SalaryRecord.salary_month == snap.salary_month,
            )
            .first()
        )

    changes: list[dict] = []
    for col in _PAYLOAD_COLUMNS:
        snap_val = getattr(snap, col, None)
        cur_val = getattr(record, col, None) if record else None
        if snap_val != cur_val:
            changes.append(
                {
                    "field": col,
                    "snapshot": snap_val,
                    "current": cur_val,
                }
            )
    return {
        "snapshot_id": snap.id,
        "current_record_id": record.id if record else None,
        "current_version": record.version if record else None,
        "changes": changes,
        "has_current_record": record is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 供背景 task / 排程使用的 convenience wrapper（自建 session）
# ─────────────────────────────────────────────────────────────────────────────


def run_month_end_snapshots_job(
    year: int, month: int, captured_by: str = "system"
) -> int:
    """獨立 session + commit；用於 BackgroundTasks / asyncio 排程。"""
    with session_scope() as session:
        created = create_month_end_snapshots(session, year, month, captured_by)
    return created
