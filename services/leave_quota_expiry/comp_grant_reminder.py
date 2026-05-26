"""7 天前 LINE Bot 推播提醒員工排補休（防被折現）。

scheduler step 之三：每日 tick 撈即將到期 grant 推 LINE + stamp reminder_sent_at。

用法：
    # main.py 啟動時注入（對齊 dismissal_calls.py / portal/__init__.py 等做法）
    from services.leave_quota_expiry.comp_grant_reminder import (
        init_comp_grant_reminder_line_service,
    )
    init_comp_grant_reminder_line_service(line_service)

    # scheduler tick（在 leave_quota_expiry_scheduler.py handle block 內呼叫）
    from services.leave_quota_expiry.comp_grant_reminder import remind_upcoming_comp_grants
    reminder_summary = remind_upcoming_comp_grants(today, session)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from models.employee import Employee
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

if TYPE_CHECKING:
    from services.line_service import LineService

logger = logging.getLogger(__name__)
_TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# module-level singleton，由 main.py 透過 init_comp_grant_reminder_line_service 注入
_line_service: "LineService | None" = None

_REMINDER_TEMPLATE = (
    "您好，您有 {hours:.1f} 小時補休將於 {expires_at} 到期。\n"
    "逾期未休將自動折算工資。建議盡早申請補休假單。"
)


def _build_reminder_flex(hours: float, earliest_expires_at: date) -> dict:
    """建補休到期提醒 Flex bubble。

    結構：紅色標題 + 時數 + 到期日 + 提示文字。
    Phase E 可再加 LIFF button；此版本刻意不加，避免 URL 依賴。

    Args:
        hours: 員工尚未消耗的補休總時數。
        earliest_expires_at: 最早到期日（取所有 grant 中最小值）。

    Returns:
        符合 LINE Flex Message bubble 格式的 dict。
    """
    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "📅 補休到期提醒",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#FFFFFF",
                },
            ],
            "backgroundColor": "#FF6B6B",
            "paddingAll": "12px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": f"您有 {hours:.1f} 小時補休",
                    "size": "md",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"將於 {earliest_expires_at.isoformat()} 到期",
                    "size": "md",
                    "color": "#FF6B6B",
                    "weight": "bold",
                    "wrap": True,
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "逾期未休將自動折算工資。建議盡早申請補休假單。",
                    "size": "sm",
                    "color": "#666666",
                    "wrap": True,
                    "margin": "md",
                },
            ],
            "paddingAll": "16px",
        },
    }


def init_comp_grant_reminder_line_service(svc: "LineService") -> None:
    """由 main.py 在啟動時注入 LineService singleton。"""
    global _line_service
    _line_service = svc


def remind_upcoming_comp_grants(
    today: date,
    session: Session,
    days_ahead: int = 7,
) -> dict:
    """撈 active grant 即將在 days_ahead 天內到期、尚未推播的，推 LINE + stamp reminder_sent_at。

    查詢條件：
        - status = 'active'
        - reminder_sent_at IS NULL
        - expires_at BETWEEN today AND today + days_ahead（含兩端）
        - Employee.is_active = True

    每位員工聚合成一則訊息（sum 未消耗時數、取最早 expires_at），
    推播成功後 stamp 所有相關 grant.reminder_sent_at = now(taipei)。

    Args:
        today: 執行日期（scheduler 傳入；測試可注入任意日期）
        session: SQLAlchemy session（呼叫端管理生命週期）
        days_ahead: 提前幾天提醒（預設 7）

    Returns:
        {
            'reminded_employees': int,  # 成功推播的員工數
            'skipped_no_line': int,     # 無 line_user_id 而略過的員工數
        }
    """
    from models.auth import User  # lazy import 避免循環

    end = today + timedelta(days=days_ahead)
    grants: list[OvertimeCompLeaveGrant] = (
        session.query(OvertimeCompLeaveGrant)
        .join(Employee, Employee.id == OvertimeCompLeaveGrant.employee_id)
        .filter(
            OvertimeCompLeaveGrant.status == "active",
            OvertimeCompLeaveGrant.reminder_sent_at.is_(None),
            OvertimeCompLeaveGrant.expires_at >= today,
            OvertimeCompLeaveGrant.expires_at <= end,
            Employee.is_active.is_(True),
        )
        .all()
    )

    if not grants:
        return {"reminded_employees": 0, "skipped_no_line": 0}

    # 依員工聚合
    grants_by_emp: dict[int, list[OvertimeCompLeaveGrant]] = {}
    for g in grants:
        grants_by_emp.setdefault(g.employee_id, []).append(g)

    reminded = 0
    skipped_no_line = 0

    for emp_id, emp_grants in grants_by_emp.items():
        user: User | None = (
            session.query(User).filter(User.employee_id == emp_id).first()
        )
        if user is None or not getattr(user, "line_user_id", None):
            skipped_no_line += 1
            continue

        line_user_id: str = user.line_user_id  # type: ignore[assignment]

        # 計算尚未消耗的總時數（全消耗但 reminder_sent_at=NULL 也 stamp，避免重撈）
        total_hours = sum(g.granted_hours - g.consumed_hours for g in emp_grants)
        earliest: date = min(g.expires_at for g in emp_grants)

        if total_hours > 0:
            flex = _build_reminder_flex(total_hours, earliest)
            alt_text = f"補休 {total_hours:.1f}h 將於 {earliest.isoformat()} 到期"
            try:
                ok = (
                    _line_service.push_flex_to_user(line_user_id, flex, alt_text)
                    if _line_service
                    else False
                )  # type: ignore[union-attr]
                if ok:
                    now_taipei = datetime.now(_TAIPEI_TZ)
                    for g in emp_grants:
                        g.reminder_sent_at = now_taipei
                    reminded += 1
                    logger.info(
                        "補休 LINE 提醒已送 emp=%d hours=%.1f earliest=%s",
                        emp_id,
                        total_hours,
                        earliest.isoformat(),
                    )
                else:
                    logger.warning("LINE push 失敗 emp=%d", emp_id)
            except Exception:
                logger.exception("補休 LINE 提醒失敗 emp=%d", emp_id)
        else:
            # 時數已全消耗，仍 stamp 防止下次重撈
            now_taipei = datetime.now(_TAIPEI_TZ)
            for g in emp_grants:
                g.reminder_sent_at = now_taipei
            logger.debug("補休時數已耗盡，stamp 防重撈 emp=%d", emp_id)

    return {"reminded_employees": reminded, "skipped_no_line": skipped_no_line}
