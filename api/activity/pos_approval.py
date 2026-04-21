"""
api/activity/pos_approval.py — 才藝課 POS 日結簽核端點

老闆每日核對 POS 流水後簽核某日，凍結 snapshot。不阻擋既有收款流程（事後核對）。

端點：
  GET    /pos/daily-close/{date}       查某日簽核狀態（未簽核回即時 preview）
  POST   /pos/daily-close/{date}       簽核（凍結 snapshot）；已簽核回 409
  DELETE /pos/daily-close/{date}       解鎖重簽（寫 ApprovalLog action=cancelled）
  GET    /pos/daily-close/pending      列出有交易但未簽核的日期
  GET    /pos/reconciliation           按日對帳（snapshot 或即時）
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func

from models.database import (
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ApprovalLog,
    get_session,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

from ._shared import TAIPEI_TZ, compute_daily_snapshot

logger = logging.getLogger(__name__)
router = APIRouter()


_RECONCILIATION_MAX_DAYS = 92

# 「現金」key 固定於 by_method_net / by_method_json；compute_daily_snapshot 對
# payment_method 為 NULL 的紀錄會歸類為「未指定」，真正的現金類別才走此 key
_CASH_METHOD_KEY = "現金"


# ── Pydantic schemas ────────────────────────────────────────────────────


class DailyCloseCreate(BaseModel):
    note: Optional[str] = Field(None, max_length=500)
    actual_cash_count: Optional[int] = Field(
        None, ge=0, le=9_999_999, description="實際現金盤點金額（可選）"
    )


# ── 內部輔助 ────────────────────────────────────────────────────────────


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail="date 格式必須為 YYYY-MM-DD")


def _doc_id_for(d: date) -> int:
    """將 close_date 編碼為 ApprovalLog.doc_id（Integer）：YYYYMMDD。"""
    return int(d.strftime("%Y%m%d"))


def _serialize_close(row: ActivityPosDailyClose) -> dict:
    try:
        by_method_net = json.loads(row.by_method_json or "{}")
    except (TypeError, ValueError):
        by_method_net = {}
    return {
        "date": row.close_date.isoformat(),
        "is_approved": True,
        "approver_username": row.approver_username,
        "approved_at": (
            row.approved_at.isoformat(timespec="seconds") if row.approved_at else None
        ),
        "note": row.note,
        "payment_total": row.payment_total,
        "refund_total": row.refund_total,
        "net_total": row.net_total,
        "transaction_count": row.transaction_count,
        "by_method": by_method_net,
        "actual_cash_count": row.actual_cash_count,
        "cash_variance": row.cash_variance,
    }


def _live_preview(session, target_date: date) -> dict:
    """未簽核日的即時 preview：沿用 compute_daily_snapshot 的 by_method_net 結構。"""
    snap = compute_daily_snapshot(session, target_date)
    return {
        "date": target_date.isoformat(),
        "is_approved": False,
        "approver_username": None,
        "approved_at": None,
        "note": None,
        "payment_total": snap["payment_total"],
        "refund_total": snap["refund_total"],
        "net_total": snap["net"],
        "transaction_count": snap["transaction_count"],
        "by_method": snap["by_method_net"],
        "actual_cash_count": None,
        "cash_variance": None,
    }


# ── 端點 1：列出有交易但未簽核的日期（靜態路徑需優先於 /{date_str}） ─────


@router.get("/pos/daily-close/pending")
async def pending_daily_closes(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設 30 天前"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD，預設今日"),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """列出指定區間內『有交易但未簽核』的日期，供老闆批次處理積壓日結。"""
    today = datetime.now(TAIPEI_TZ).date()
    end = _parse_date(end_date) if end_date else today
    start = _parse_date(start_date) if start_date else end - timedelta(days=30)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date 不可晚於 end_date")
    if (end - start).days > _RECONCILIATION_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"區間不可超過 {_RECONCILIATION_MAX_DAYS} 天",
        )

    session = get_session()
    try:
        tx_rows = (
            session.query(
                ActivityPaymentRecord.payment_date,
                ActivityPaymentRecord.type,
                func.count(ActivityPaymentRecord.id),
                func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
            )
            .filter(
                ActivityPaymentRecord.payment_date >= start,
                ActivityPaymentRecord.payment_date <= end,
            )
            .group_by(
                ActivityPaymentRecord.payment_date,
                ActivityPaymentRecord.type,
            )
            .all()
        )
        by_date: dict = {}
        for pd, rec_type, cnt, amt in tx_rows:
            if pd is None:
                continue
            slot = by_date.setdefault(
                pd, {"transaction_count": 0, "payment_total": 0, "refund_total": 0}
            )
            slot["transaction_count"] += int(cnt or 0)
            if rec_type == "payment":
                slot["payment_total"] += int(amt or 0)
            else:
                slot["refund_total"] += int(amt or 0)

        approved_dates = {
            d
            for (d,) in session.query(ActivityPosDailyClose.close_date)
            .filter(
                ActivityPosDailyClose.close_date >= start,
                ActivityPosDailyClose.close_date <= end,
            )
            .all()
        }

        pending = [
            {
                "date": d.isoformat(),
                "transaction_count": v["transaction_count"],
                "payment_total": v["payment_total"],
                "refund_total": v["refund_total"],
                "net_total": v["payment_total"] - v["refund_total"],
            }
            for d, v in sorted(by_date.items())
            if d not in approved_dates
        ]
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "pending": pending,
        }
    finally:
        session.close()


# ── 端點 2：查某日簽核狀態 ────────────────────────────────────────────


@router.get("/pos/daily-close/{date_str}")
async def get_daily_close(
    date_str: str,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """查某日日結簽核狀態。未簽核時 is_approved=False 並附即時 preview。"""
    target = _parse_date(date_str)
    session = get_session()
    try:
        row = (
            session.query(ActivityPosDailyClose)
            .filter(ActivityPosDailyClose.close_date == target)
            .first()
        )
        if row:
            return _serialize_close(row)
        return _live_preview(session, target)
    finally:
        session.close()


# ── 端點 3：簽核某日 ─────────────────────────────────────────────────


@router.post("/pos/daily-close/{date_str}", status_code=status.HTTP_201_CREATED)
async def approve_daily_close(
    date_str: str,
    body: DailyCloseCreate,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """老闆簽核某日 POS 流水：凍結 snapshot，同時寫 ApprovalLog。"""
    target = _parse_date(date_str)
    today = datetime.now(TAIPEI_TZ).date()
    if target > today:
        raise HTTPException(status_code=400, detail="不可簽核未來日期")

    session = get_session()
    try:
        existing = (
            session.query(ActivityPosDailyClose)
            .filter(ActivityPosDailyClose.close_date == target)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail="該日已簽核，請先解鎖（DELETE）後再重簽",
            )

        snap = compute_daily_snapshot(session, target)
        by_method_net = snap["by_method_net"]
        cash_snapshot = int(by_method_net.get(_CASH_METHOD_KEY, 0))

        cash_variance = None
        if body.actual_cash_count is not None:
            cash_variance = body.actual_cash_count - cash_snapshot

        row = ActivityPosDailyClose(
            close_date=target,
            approver_username=current_user.get("username", ""),
            approved_at=datetime.now(),
            note=(body.note or None),
            payment_total=snap["payment_total"],
            refund_total=snap["refund_total"],
            net_total=snap["net"],
            transaction_count=snap["transaction_count"],
            by_method_json=json.dumps(by_method_net, ensure_ascii=False),
            actual_cash_count=body.actual_cash_count,
            cash_variance=cash_variance,
        )
        session.add(row)

        # 稽核軌跡：與 leave / overtime 等走同一個 ApprovalLog 表
        session.add(
            ApprovalLog(
                doc_type="activity_pos_daily",
                doc_id=_doc_id_for(target),
                action="approved",
                approver_username=current_user.get("username", ""),
                approver_role=current_user.get("role"),
                comment=body.note,
            )
        )
        session.commit()
        session.refresh(row)
        logger.warning(
            "POS 日結簽核：date=%s approver=%s net=%d variance=%s",
            target.isoformat(),
            current_user.get("username", ""),
            snap["net"],
            cash_variance,
        )
        request.state.audit_entity_id = target.isoformat()
        request.state.audit_summary = (
            f"POS 日結簽核：{target.isoformat()} 淨額 NT${snap['net']}"
        )
        request.state.audit_changes = {
            "close_date": target.isoformat(),
            "net_total": snap["net"],
            "payment_total": snap["payment_total"],
            "refund_total": snap["refund_total"],
            "transaction_count": snap["transaction_count"],
            "actual_cash_count": body.actual_cash_count,
            "cash_variance": cash_variance,
            "note": body.note,
        }
        return _serialize_close(row)
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception("POS 日結簽核失敗 date=%s", target)
        raise HTTPException(status_code=500, detail="簽核失敗，請稍後重試")
    finally:
        session.close()


# ── 端點 3：解鎖重簽 ─────────────────────────────────────────────────


@router.delete("/pos/daily-close/{date_str}", status_code=status.HTTP_204_NO_CONTENT)
async def unlock_daily_close(
    date_str: str,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """解除某日簽核鎖定；寫 ApprovalLog(action='cancelled') 作稽核軌跡。"""
    target = _parse_date(date_str)
    session = get_session()
    try:
        row = (
            session.query(ActivityPosDailyClose)
            .filter(ActivityPosDailyClose.close_date == target)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="該日尚未簽核，無需解鎖")

        original_approver = row.approver_username
        original_at = (
            row.approved_at.isoformat(timespec="seconds") if row.approved_at else "?"
        )

        session.delete(row)
        session.add(
            ApprovalLog(
                doc_type="activity_pos_daily",
                doc_id=_doc_id_for(target),
                action="cancelled",
                approver_username=current_user.get("username", ""),
                approver_role=current_user.get("role"),
                comment=f"解鎖；原簽核人 {original_approver} @ {original_at}",
            )
        )
        session.commit()
        logger.warning(
            "POS 日結解鎖：date=%s unlocker=%s original=%s@%s",
            target.isoformat(),
            current_user.get("username", ""),
            original_approver,
            original_at,
        )
        request.state.audit_entity_id = target.isoformat()
        request.state.audit_summary = f"POS 日結解鎖：{target.isoformat()}"
        request.state.audit_changes = {
            "close_date": target.isoformat(),
            "original_approver": original_approver,
            "original_approved_at": original_at,
        }
        return None
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        logger.exception("POS 日結解鎖失敗 date=%s", target)
        raise HTTPException(status_code=500, detail="解鎖失敗，請稍後重試")
    finally:
        session.close()


# ── 端點 5：對帳匯總（按日，snapshot 或即時） ────────────────────


@router.get("/pos/reconciliation")
async def pos_reconciliation(
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """按日列出應收 vs 實收對帳：已簽核日用 snapshot、未簽核日即時算。

    對帳語義：expected_cash = snapshot 或即時的 by_method[現金]，
    actual_cash = 老闆盤點的 actual_cash_count（僅已簽核日才有），
    variance = actual_cash - expected_cash。
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        raise HTTPException(status_code=400, detail="start_date 不可晚於 end_date")
    if (end - start).days > _RECONCILIATION_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"區間不可超過 {_RECONCILIATION_MAX_DAYS} 天",
        )

    session = get_session()
    try:
        approved_rows = {
            row.close_date: row
            for row in session.query(ActivityPosDailyClose)
            .filter(
                ActivityPosDailyClose.close_date >= start,
                ActivityPosDailyClose.close_date <= end,
            )
            .all()
        }
        # 有交易的日期（僅這些日會出現在結果中）
        tx_dates = {
            pd
            for (pd,) in session.query(ActivityPaymentRecord.payment_date.distinct())
            .filter(
                ActivityPaymentRecord.payment_date >= start,
                ActivityPaymentRecord.payment_date <= end,
            )
            .all()
            if pd is not None
        }
        all_dates = sorted(tx_dates | set(approved_rows.keys()))

        items = []
        agg_payment = 0
        agg_refund = 0
        agg_variance = 0
        variance_has_value = False
        for d in all_dates:
            row = approved_rows.get(d)
            if row is not None:
                data = _serialize_close(row)
                expected_cash = int(data["by_method"].get(_CASH_METHOD_KEY, 0))
                actual_cash = data["actual_cash_count"]
                variance = data["cash_variance"]
            else:
                data = _live_preview(session, d)
                expected_cash = int(data["by_method"].get(_CASH_METHOD_KEY, 0))
                actual_cash = None
                variance = None
            agg_payment += data["payment_total"]
            agg_refund += data["refund_total"]
            if variance is not None:
                agg_variance += variance
                variance_has_value = True
            items.append(
                {
                    "date": d.isoformat(),
                    "is_approved": data["is_approved"],
                    "payment_total": data["payment_total"],
                    "refund_total": data["refund_total"],
                    "net_total": data["net_total"],
                    "transaction_count": data["transaction_count"],
                    "expected_cash": expected_cash,
                    "actual_cash": actual_cash,
                    "variance": variance,
                }
            )

        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "items": items,
            "totals": {
                "payment_total": agg_payment,
                "refund_total": agg_refund,
                "net_total": agg_payment - agg_refund,
                "variance_total": agg_variance if variance_has_value else None,
            },
        }
    finally:
        session.close()
