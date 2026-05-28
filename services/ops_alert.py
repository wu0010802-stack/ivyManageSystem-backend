"""Ops 告警通道 — 薄包裝 LineService.push_text_to_group。

DSN/group_id 缺即 no-op；異常吞掉並 log，不可影響 caller (middleware) 主流程。

LineService 由 main.py 透過 init_ops_alert_service() 注入（與其他 service
注入 pattern 一致：dismissal/growth_reports/portal_notify/comp_grant 等）。
未注入時自動 no-op（log warn）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import settings

if TYPE_CHECKING:
    from services.line_service import LineService

logger = logging.getLogger(__name__)

_line_service: "LineService | None" = None


def init_ops_alert_service(line_service: "LineService") -> None:
    """由 main.py startup 注入單例 LineService。"""
    global _line_service
    _line_service = line_service


def notify_slow_request_burst(
    *,
    path: str,
    count: int,
    window_seconds: int,
    sample_elapsed_ms: float,
    sample_status: int,
) -> None:
    """通知慢請求突發；caller 已過 threshold + cooldown 判斷。"""
    cfg = settings.ops_alert
    if not cfg.line_group_id:
        logger.warning(
            "Slow request burst detected but OPS_ALERT_LINE_GROUP_ID 未設；"
            "path=%s count=%d window=%ds sample=%.0fms/status=%d",
            path,
            count,
            window_seconds,
            sample_elapsed_ms,
            sample_status,
        )
        return

    if _line_service is None:
        logger.warning(
            "LineService 未注入（init_ops_alert_service 未呼叫）；"
            "slow request alert path=%s 跳過 LINE push",
            path,
        )
        return

    text = (
        f"⚠️ 慢請求突發\n"
        f"endpoint：{path}\n"
        f"窗口：{window_seconds}s 內 {count} 次 > 2000ms\n"
        f"範例：{sample_elapsed_ms:.0f}ms / status={sample_status}\n"
        f"env：{settings.core.env}"
    )

    try:
        _line_service.push_text_to_group(cfg.line_group_id, text)
    except Exception as e:
        logger.error(
            "Slow request alert push 失敗 (path=%s): %s",
            path,
            e,
            exc_info=True,
        )


def reset_for_tests() -> None:
    """測試 helper：清空注入的 LineService。"""
    global _line_service
    _line_service = None
