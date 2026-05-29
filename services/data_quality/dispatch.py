"""services/data_quality/dispatch.py — 4 線出口 (log + persist + sentry + line)。"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from models.data_quality import DataQualityReport
from services.data_quality._base import Violation
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger("data_quality")


def emit(
    violation: Violation,
    session: Session,
    *,
    line_queue: list,
) -> bool:
    """寫一條 violation 進 4 線：log + persist + （新 open 時）Sentry + 累積到 line_queue。

    Returns: True 若這次是「新 open」（push Sentry + 加入 LINE queue）；
    False 表示同 dedup_key 已有 open / ignored row，只更新 last_seen_at。
    """
    # 1. log
    logger.warning(
        "data_quality violation: rule=%s entity=%s/%s severity=%s",
        violation.rule_code,
        violation.entity_type,
        violation.entity_id,
        violation.severity,
    )

    # 2. persist + dedup
    existing: Optional[DataQualityReport] = (
        session.query(DataQualityReport)
        .filter(
            DataQualityReport.dedup_key == violation.dedup_key,
            DataQualityReport.status.in_(["open", "ignored"]),
        )
        .first()
    )
    if existing is not None:
        existing.last_seen_at = now_taipei_naive()
        session.commit()
        return False

    row = DataQualityReport(
        rule_code=violation.rule_code,
        severity=violation.severity,
        entity_type=violation.entity_type,
        entity_id=violation.entity_id,
        summary=violation.summary,
        dedup_key=violation.dedup_key,
        status="open",
    )
    session.add(row)
    session.commit()

    # 3+4: Sentry + LINE 累積（呼叫端 flush）
    _emit_sentry(violation)
    line_queue.append(violation)

    return True


def _emit_sentry(violation: Violation) -> None:
    """Sentry capture_message（warning level）。"""
    try:
        import sentry_sdk

        level_map = {"P0": "error", "P1": "warning", "P2": "info"}
        sentry_sdk.capture_message(
            f"data_quality: {violation.rule_code}",
            level=level_map.get(violation.severity, "warning"),
            tags={
                "rule_code": violation.rule_code,
                "entity_type": violation.entity_type,
                "severity": violation.severity,
            },
        )
    except Exception:
        logger.warning("sentry capture_message failed for %s", violation.rule_code)


def _get_line_service():
    """Lazy import 為了：(a) 避免 services/data_quality 直接 import main.py；
    (b) test 可 monkeypatch 此函式換 fake。"""
    try:
        from main import line_service

        return line_service
    except Exception:
        return None


def flush_line_digest(line_queue: list[Violation]) -> None:
    """把累積的 violation 合成一則 LINE 訊息推給老闆。空 queue 不推。

    格式：
      資料品質告警
      P0 | rule_code | summary
      P1 | rule_code | summary
      P2 | rule_code | summary
      ...另 N 條請至後台 DataQuality 查看
    """
    if not line_queue:
        return

    line_service = _get_line_service()
    if line_service is None:
        logger.warning("line_service unavailable, skip LINE digest")
        return

    head = line_queue[:3]
    rest = len(line_queue) - 3
    body_lines = [f"{v.severity} | {v.rule_code} | {v.summary}" for v in head]
    if rest > 0:
        body_lines.append(f"...另 {rest} 條請至後台 DataQuality 查看")

    text = "資料品質告警\n" + "\n".join(body_lines)
    try:
        line_service._push(text)
    except Exception:
        logger.exception("line_service push failed for data_quality digest")
