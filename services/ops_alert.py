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


def notify_scheduler_failure(
    *,
    scheduler_name: str,
    error: BaseException,
    consecutive_failures: int,
) -> None:
    """通知排程器連續失敗；caller（scheduler_observability）已過節流判斷。

    group_id 未設或 LineService 未注入時 no-op（log warn）；
    push 例外吞掉並 log，不可影響 scheduler loop。
    """
    cfg = settings.ops_alert
    if not cfg.line_group_id:
        logger.warning(
            "排程器連續失敗但 OPS_ALERT_LINE_GROUP_ID 未設；"
            "scheduler=%s consecutive=%d error=%s",
            scheduler_name,
            consecutive_failures,
            error,
        )
        return

    if _line_service is None:
        logger.warning(
            "LineService 未注入（init_ops_alert_service 未呼叫）；"
            "scheduler failure alert scheduler=%s 跳過 LINE push",
            scheduler_name,
        )
        return

    text = (
        f"🚨 排程器連續失敗\n"
        f"scheduler：{scheduler_name}\n"
        f"連續失敗：{consecutive_failures} 次\n"
        f"錯誤：{type(error).__name__}: {error}\n"
        f"env：{settings.core.env}"
    )

    try:
        _line_service.push_text_to_group(cfg.line_group_id, text)
    except Exception as e:
        logger.error(
            "Scheduler failure alert push 失敗 (scheduler=%s): %s",
            scheduler_name,
            e,
            exc_info=True,
        )


_HIGH_RISK_KIND_LABELS = {
    "hard_delete": "硬刪除（不可復原）",
    "blocked": "越權嘗試被伺服器擋下",
    "permission_change": "權限／角色變更",
}


def notify_high_risk_audit(
    *,
    risk_kind: str,
    action: str,
    entity_type: str,
    summary: str | None,
    username: str | None = None,
) -> None:
    """高風險稽核事件（硬刪 / 提權-角色變更 / 越權嘗試）主動 LINE 告警。

    原本高風險事件觸達完全被動：只靠前端每 60s 輪詢紅點，且分頁隱藏時跳過 → 下班 /
    假日無人開後台即無人知曉。caller（utils.audit）已過 per-risk_kind cooldown 判斷。

    group_id 未設或 LineService 未注入時 no-op（log warn）；push 例外吞掉並 log，
    不可影響稽核寫入主流程。summary 由 caller 傳入時已遮罩 PII。
    """
    cfg = settings.ops_alert
    if not cfg.line_group_id:
        logger.warning(
            "高風險稽核事件但 OPS_ALERT_LINE_GROUP_ID 未設；"
            "risk_kind=%s action=%s entity=%s",
            risk_kind,
            action,
            entity_type,
        )
        return

    if _line_service is None:
        logger.warning(
            "LineService 未注入（init_ops_alert_service 未呼叫）；"
            "high-risk audit alert risk_kind=%s 跳過 LINE push",
            risk_kind,
        )
        return

    label = _HIGH_RISK_KIND_LABELS.get(risk_kind, risk_kind)
    text = (
        f"🔴 高風險操作\n"
        f"類型：{label}\n"
        f"動作：{action} / {entity_type}\n"
        f"操作者：{username or '未知'}\n"
        f"摘要：{summary or '(無)'}\n"
        f"env：{settings.core.env}"
    )

    try:
        _line_service.push_text_to_group(cfg.line_group_id, text)
    except Exception as e:
        logger.error(
            "High-risk audit alert push 失敗 (risk_kind=%s): %s",
            risk_kind,
            e,
            exc_info=True,
        )


def notify_student_sync_failure(
    *,
    student_id: int,
    failed_registration_ids: list[int],
    reason: str = "部分軟刪失敗（savepoint 回滾）",
) -> None:
    """學生離園同步軟刪部分失敗時主動告警。

    讓管理員知曉哪幾筆報名未成功軟刪（可能仍佔名額、有未退款付款金額），
    以便人工跟進補刪或退款。

    group_id 未設或 LineService 未注入時 no-op（log warn）；
    push 例外吞掉並 log，不可影響 caller 的 deactivate 主流程。
    """
    cfg = settings.ops_alert
    if not cfg.line_group_id:
        logger.warning(
            "學生離園同步軟刪失敗但 OPS_ALERT_LINE_GROUP_ID 未設；"
            "student_id=%s failed_reg_ids=%s",
            student_id,
            failed_registration_ids,
        )
        return

    if _line_service is None:
        logger.warning(
            "LineService 未注入（init_ops_alert_service 未呼叫）；"
            "student sync failure alert student_id=%s 跳過 LINE push",
            student_id,
        )
        return

    ids_str = ", ".join(str(i) for i in failed_registration_ids)
    text = (
        f"⚠️ 學生離園同步部分失敗\n"
        f"學生 ID：{student_id}\n"
        f"未成功軟刪的報名 ID：{ids_str}\n"
        f"原因：{reason}\n"
        f"⚠ 上述報名可能仍佔名額或有未退款付款金額，請人工至後台跟進補刪／退款。\n"
        f"env：{settings.core.env}"
    )

    try:
        _line_service.push_text_to_group(cfg.line_group_id, text)
    except Exception as e:
        logger.error(
            "Student sync failure alert push 失敗 (student_id=%s): %s",
            student_id,
            e,
            exc_info=True,
        )


def reset_for_tests() -> None:
    """測試 helper：清空注入的 LineService。"""
    global _line_service
    _line_service = None
