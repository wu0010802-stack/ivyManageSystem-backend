"""staging → 正式表 promote / dismiss。

副作用：
- 寫入 insurance_brackets / minimum_wage_history（產品表）
- 觸發 _bulk_mark_salary_stale_for_year（既有 helper at api/insurance.py:79）
- 嘗試 reload InsuranceService global singleton 的 in-memory cache
- audit reason 必須 ≥ 10 字（與 api/insurance.py InsuranceBracketsBulkUpsert 一致）

冪等：staging.status 已非 pending 時 raise 409。
"""

from __future__ import annotations

import logging
from datetime import datetime

from models.database import (
    InsuranceBracket,
    InsuranceBracketsStaging,
    MinimumWageHistory,
    MinimumWageStaging,
    session_scope,
)

logger = logging.getLogger(__name__)

REASON_MIN_LEN = 10


class PromoteError(Exception):
    """staging promote/dismiss 失敗。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _validate_reason(reason: str) -> None:
    if not reason or len(reason.strip()) < REASON_MIN_LEN:
        raise PromoteError(
            status_code=400,
            code="REASON_TOO_SHORT",
            message=f"reason 必須 ≥ {REASON_MIN_LEN} 字（外部資料寫入需稽核軌跡）",
        )


def _try_reload_insurance_service(year: int) -> None:
    """嘗試 reload main.py 的 insurance_service singleton；失敗只 log，不阻塞 promote。

    main.py 在啟動時建立 module-level `insurance_service`。測試環境不一定有，故 try。
    """
    try:
        from main import insurance_service  # 延遲 import 避免循環

        insurance_service.load_brackets_from_db(year=year)
    except Exception as exc:  # noqa: BLE001
        logger.info("skip reload insurance_service after promote: %s", exc)


def promote_brackets(staging_id: int, decided_by: str, reason: str) -> None:
    _validate_reason(reason)

    # 延遲 import 避免循環
    from api.insurance import _bulk_mark_salary_stale_for_year

    affected = 0
    effective_year: int | None = None
    with session_scope() as s:
        st = s.get(InsuranceBracketsStaging, staging_id)
        if st is None:
            raise PromoteError(404, "STAGING_NOT_FOUND", f"staging {staging_id} 不存在")
        if st.status != "pending":
            raise PromoteError(
                409, "STAGING_ALREADY_DECIDED", f"staging {staging_id} 已 {st.status}"
            )

        effective_year = st.effective_year

        # 1. 刪除該年度既有 brackets
        s.query(InsuranceBracket).filter(
            InsuranceBracket.effective_year == effective_year
        ).delete(synchronize_session=False)

        # 2. 寫入新 brackets（InsuranceBracket 無 source_snapshot_id 欄位）
        for row in st.brackets:
            s.add(
                InsuranceBracket(
                    effective_year=effective_year,
                    amount=row["amount"],
                    labor_employee=row["labor_employee"],
                    labor_employer=row["labor_employer"],
                    health_employee=row["health_employee"],
                    health_employer=row["health_employer"],
                    pension=row["pension"],
                )
            )

        # 3. mark stale（沿用既有 helper）
        affected = _bulk_mark_salary_stale_for_year(s, effective_year)

        # 4. 標 staging promoted
        st.status = "promoted"
        st.decided_by = decided_by
        st.decided_at = datetime.utcnow()
        st.decision_reason = reason
        s.flush()

    # 5. reload service singleton（出 transaction 後）
    if effective_year is not None:
        _try_reload_insurance_service(effective_year)

    logger.info(
        "promoted brackets staging=%d year=%d marked_stale=%d by=%s",
        staging_id,
        effective_year,
        affected,
        decided_by,
    )


def dismiss_brackets(staging_id: int, decided_by: str, reason: str) -> None:
    _validate_reason(reason)
    with session_scope() as s:
        st = s.get(InsuranceBracketsStaging, staging_id)
        if st is None:
            raise PromoteError(404, "STAGING_NOT_FOUND", f"staging {staging_id} 不存在")
        if st.status != "pending":
            raise PromoteError(
                409, "STAGING_ALREADY_DECIDED", f"staging {staging_id} 已 {st.status}"
            )
        st.status = "dismissed"
        st.decided_by = decided_by
        st.decided_at = datetime.utcnow()
        st.decision_reason = reason
        s.flush()


def promote_minimum_wage(staging_id: int, decided_by: str, reason: str) -> None:
    _validate_reason(reason)
    with session_scope() as s:
        st = s.get(MinimumWageStaging, staging_id)
        if st is None:
            raise PromoteError(404, "STAGING_NOT_FOUND", f"staging {staging_id} 不存在")
        if st.status != "pending":
            raise PromoteError(
                409, "STAGING_ALREADY_DECIDED", f"staging {staging_id} 已 {st.status}"
            )
        s.add(
            MinimumWageHistory(
                effective_date=st.effective_date,
                monthly=st.monthly,
                hourly=st.hourly,
                source_snapshot_id=st.source_snapshot_id,
                confirmed_by=decided_by,
                confirm_reason=reason,
            )
        )
        st.status = "promoted"
        st.decided_by = decided_by
        st.decided_at = datetime.utcnow()
        st.decision_reason = reason
        s.flush()


def dismiss_minimum_wage(staging_id: int, decided_by: str, reason: str) -> None:
    _validate_reason(reason)
    with session_scope() as s:
        st = s.get(MinimumWageStaging, staging_id)
        if st is None:
            raise PromoteError(404, "STAGING_NOT_FOUND", f"staging {staging_id} 不存在")
        if st.status != "pending":
            raise PromoteError(
                409, "STAGING_ALREADY_DECIDED", f"staging {staging_id} 已 {st.status}"
            )
        st.status = "dismissed"
        st.decided_by = decided_by
        st.decided_at = datetime.utcnow()
        st.decision_reason = reason
        s.flush()
