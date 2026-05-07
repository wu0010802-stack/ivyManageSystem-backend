"""政府開放資料同步排程。

啟用方式（沿用 IvyKids 既有 pattern，例：services/graduation_scheduler.py）：
- 環境變數 GOV_DATA_SYNC_ENABLED=1
- main.py on_startup() 呼叫 asyncio.create_task(loop_forever())

行為：
- 每 24h 檢查；若上一次成功 fetch 已超過 30 天 → 觸發 sync_now()
- 提供 sync_now() 同步函式，供 api/gov_data_sync.py 的 sync-now endpoint 直接呼叫
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from models.database import (
    GovDataSnapshot,
    InsuranceBracket,
    InsuranceBracketsStaging,
    MinimumWageHistory,
    MinimumWageStaging,
    session_scope,
)
from services.gov_data import composer, fetcher, parser
from services.gov_data.utils import compute_brackets_diff

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = int(os.getenv("GOV_DATA_CHECK_INTERVAL", str(24 * 3600)))
MIN_DAYS_BETWEEN_SYNC = int(os.getenv("GOV_DATA_MIN_DAYS_BETWEEN_SYNC", "30"))

BRACKETS_SOURCE_KEYS = [
    "mol_labor_brackets",
    "mol_labor_premium",
    "mol_pension",
    "nhi_brackets",
    "nhi_premium",
]


def is_enabled() -> bool:
    return os.getenv("GOV_DATA_SYNC_ENABLED", "0") == "1"


def sync_now() -> dict:
    """執行一次完整同步（fetch + compose + staging）。

    Returns:
        {"snapshot_ids": {source: id}, "brackets_staging_id": int|None,
         "minimum_wage_staging_id": int|None}
    """
    snapshot_ids = fetcher.fetch_all()
    brackets_staging_id = _compose_and_stage_brackets(snapshot_ids)
    minimum_wage_staging_id = _compose_and_stage_minimum_wage(snapshot_ids)
    return {
        "snapshot_ids": snapshot_ids,
        "brackets_staging_id": brackets_staging_id,
        "minimum_wage_staging_id": minimum_wage_staging_id,
    }


def _compose_and_stage_brackets(snapshot_ids: dict) -> int | None:
    """5 個源全部 200 才合成；任一缺/失敗則跳過。"""
    with session_scope() as s:
        snaps = {}
        for src in BRACKETS_SOURCE_KEYS:
            sid = snapshot_ids.get(src)
            if sid is None:
                logger.info("compose brackets skipped: missing %s", src)
                return None
            snap = s.get(GovDataSnapshot, sid)
            if snap is None or snap.http_status != 200 or snap.raw_payload is None:
                logger.info("compose brackets skipped: %s not 200", src)
                return None
            snaps[src] = snap

        try:
            labor_b = parser.parse_mol_labor_brackets(
                snaps["mol_labor_brackets"].raw_payload
            )
            labor_p = parser.parse_mol_labor_premium(
                snaps["mol_labor_premium"].raw_payload
            )
            pension = parser.parse_mol_pension(snaps["mol_pension"].raw_payload)
            nhi_b = parser.parse_nhi_brackets(snaps["nhi_brackets"].raw_payload)
            nhi_p = parser.parse_nhi_premium(snaps["nhi_premium"].raw_payload)
        except parser.ParserError as exc:
            logger.error("compose brackets parser failed: %s", exc)
            return None

        composed_from = {src: snaps[src].id for src in BRACKETS_SOURCE_KEYS}
        try:
            composed = composer.compose_brackets(
                effective_year=labor_b.effective_year,
                labor_brackets=labor_b,
                labor_premium=labor_p,
                pension=pension,
                nhi_brackets=nhi_b,
                nhi_premium=nhi_p,
                composed_from=composed_from,
            )
        except composer.ComposeError as exc:
            logger.error("compose brackets failed: %s", exc)
            return None

        # diff vs 現行
        current_rows = (
            s.query(InsuranceBracket)
            .filter(InsuranceBracket.effective_year == composed.effective_year)
            .all()
        )
        current_dicts = [
            {
                "amount": r.amount,
                "labor_employee": r.labor_employee,
                "labor_employer": r.labor_employer,
                "health_employee": r.health_employee,
                "health_employer": r.health_employer,
                "pension": r.pension,
            }
            for r in current_rows
        ]
        new_dicts = [
            {
                "amount": br.amount,
                "labor_employee": br.labor_employee,
                "labor_employer": br.labor_employer,
                "health_employee": br.health_employee,
                "health_employer": br.health_employer,
                "pension": br.pension,
            }
            for br in composed.rows
        ]
        diff = compute_brackets_diff(current_dicts, new_dicts)

        # 若無變化且當年已有 promoted staging，不重複落 staging
        if not diff["added"] and not diff["removed"] and not diff["modified"]:
            existing_promoted = (
                s.query(InsuranceBracketsStaging)
                .filter(
                    InsuranceBracketsStaging.effective_year == composed.effective_year,
                    InsuranceBracketsStaging.status == "promoted",
                )
                .first()
            )
            if existing_promoted:
                logger.info(
                    "compose brackets %d: no diff, skip new staging",
                    composed.effective_year,
                )
                return None

        st = InsuranceBracketsStaging(
            effective_year=composed.effective_year,
            composed_from=composed.composed_from,
            brackets=new_dicts,
            rates=composed.rates,
            diff_summary=diff,
            status="pending",
        )
        s.add(st)
        s.flush()
        logger.info("staged brackets %d (id=%d)", composed.effective_year, st.id)
        return st.id


def _compose_and_stage_minimum_wage(snapshot_ids: dict) -> int | None:
    sid = snapshot_ids.get("mol_minimum_wage")
    if sid is None:
        return None
    with session_scope() as s:
        snap = s.get(GovDataSnapshot, sid)
        if snap is None or snap.http_status != 200 or snap.raw_payload is None:
            return None
        try:
            result = parser.parse_mol_minimum_wage(snap.raw_payload)
            eff_date, monthly, hourly = composer.compose_minimum_wage(result)
        except (parser.ParserError, composer.ComposeError) as exc:
            logger.error("compose minimum_wage failed: %s", exc)
            return None

        existing = (
            s.query(MinimumWageHistory).filter_by(effective_date=eff_date).first()
        )
        if existing:
            logger.info("minimum_wage %s already in history, skip", eff_date)
            return None

        existing_staging = (
            s.query(MinimumWageStaging)
            .filter_by(effective_date=eff_date, status="pending")
            .first()
        )
        if existing_staging:
            logger.info("minimum_wage %s already pending staging, skip", eff_date)
            return existing_staging.id

        st = MinimumWageStaging(
            effective_date=eff_date,
            monthly=monthly,
            hourly=hourly,
            source_snapshot_id=snap.id,
            status="pending",
        )
        s.add(st)
        s.flush()
        return st.id


async def loop_forever():
    """asyncio loop：每 CHECK_INTERVAL_SEC 檢查一次是否要 sync。"""
    while True:
        try:
            with session_scope() as s:
                latest_success = (
                    s.query(GovDataSnapshot.fetched_at)
                    .filter(GovDataSnapshot.http_status == 200)
                    .order_by(GovDataSnapshot.fetched_at.desc())
                    .first()
                )
            if latest_success is None or (
                datetime.utcnow() - latest_success[0]
                > timedelta(days=MIN_DAYS_BETWEEN_SYNC)
            ):
                logger.info("gov_data scheduler: triggering sync_now")
                await asyncio.to_thread(sync_now)
        except Exception:
            logger.exception("gov_data scheduler loop error")
        await asyncio.sleep(CHECK_INTERVAL_SEC)
