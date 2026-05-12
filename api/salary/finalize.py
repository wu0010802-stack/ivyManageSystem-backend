"""
api/salary/finalize.py — 薪資封存／解封管理

含 2 個 endpoint + 2 個 schemas + 3 個 helper：
- POST   /salaries/finalize-month        月封存（force / 缺漏員工 / stale 預檢）
- DELETE /salaries/{record_id}/finalize  解除封存（雙簽 + 原因）

公開 schemas / helpers (FinalizeMonthRequest / UnfinalizeSalaryRequest /
_recalculate_salary_record_totals) 由 api.salary.__init__ re-export 維持
原 public surface（test 仍可 from api.salary import ...）。
"""

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_

from models.database import get_session, Employee, SalaryRecord
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


def _invalidate_finance_summary_cache():
    """Lazy back-import：避免 finalize.py ↔ __init__.py 雙向 module-level import。

    monkeypatch 此 helper 仍可作用於 __init__.py 那份；本檔只需呼叫實作。
    """
    from . import _invalidate_finance_summary_cache as _impl

    _impl()
# ============ 薪資封存管理 ============


class FinalizeMonthRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    force: bool = Field(
        False,
        description=(
            "True 時略過「當月在職員工對齊」完整性檢查，仍會封存現有記錄。"
            "若有員工當月在職但無薪資記錄，預設會拒絕封存以避免漏發。"
        ),
    )
    force_reason: Optional[str] = Field(
        None,
        max_length=500,
        description=(
            "force=True 時必填的封存原因（≥ 10 字），會寫入每筆 record.remark "
            "與 audit_summary，供日後稽核回溯為何漏發/略過完整性檢查。"
        ),
    )

    @model_validator(mode="after")
    def _force_requires_reason(self):
        if self.force:
            cleaned = (self.force_reason or "").strip()
            if len(cleaned) < 10:
                raise ValueError(
                    "force=True 時必須在 force_reason 填寫原因（至少 10 字）"
                )
            self.force_reason = cleaned
        return self


def _recalculate_salary_record_totals(record: SalaryRecord):
    """重算 SalaryRecord 聚合欄位 — 委派至 services.salary.totals.recompute_record_totals。

    保留此 wrapper 以維持既有 test/外部 import 相容(tests/test_salary_manual_adjust.py
    與其他 module 直接 from api.salary import _recalculate_salary_record_totals)。
    """
    recompute_record_totals(record)


def _find_missing_salary_employees(session, year: int, month: int) -> list[dict]:
    """回傳當月在職但無任何 SalaryRecord 的員工清單（用於 finalize 前完整性檢查）。

    在職定義對齊 gov_reports._active_employees / salary proration 守衛：
    hire_date <= 月末 且 (resign_date 為 None 或 >= 月初)。
    """
    last_day = _cal.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    active_rows = (
        session.query(Employee.id, Employee.name)
        .filter(
            or_(Employee.hire_date.is_(None), Employee.hire_date <= month_end),
            or_(
                Employee.resign_date.is_(None),
                Employee.resign_date >= month_start,
            ),
        )
        .order_by(Employee.name)
        .all()
    )
    if not active_rows:
        return []
    existing_ids = {
        row[0]
        for row in session.query(SalaryRecord.employee_id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .all()
    }
    return [
        {"id": e.id, "name": e.name} for e in active_rows if e.id not in existing_ids
    ]


def _find_stale_salary_employees(session, year: int, month: int) -> list[dict]:
    """回傳該月 SalaryRecord 仍標 needs_recalc=True 的員工清單。

    用途:封存完整性檢查補強。批次重算單筆失敗、假單/加班審核降級時會把
    對應 SalaryRecord 標 stale,本 helper 讓 finalize 能擋下這類記錄。
    已封存(is_finalized=True)的記錄本來就不該再變動,故排除。
    """
    rows = (
        session.query(SalaryRecord, Employee.name)
        .join(Employee, SalaryRecord.employee_id == Employee.id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
            SalaryRecord.needs_recalc == True,
            SalaryRecord.is_finalized != True,
        )
        .order_by(Employee.name)
        .all()
    )
    return [{"id": r.employee_id, "name": name} for r, name in rows]


@router.post("/salaries/finalize-month")
def finalize_salary_month(
    data: FinalizeMonthRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """封存整月薪資（封存後禁止重新計算，需手動解封才能修改）"""
    from utils.advisory_lock import acquire_salary_lock

    # ── force=True 必須具備 ACTIVITY_PAYMENT_APPROVE ────────────────
    # Why: force 會略過 missing/stale 完整性檢查，等同允許漏發/封存舊資料；
    # 拉高權限要求避免單一 SALARY_WRITE 即可一鍵封存。reason 由 schema 強制必填。
    if data.force and not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "force=True 強制封存需具備『金流簽核』權限（ACTIVITY_PAYMENT_APPROVE），"
                "請改由具該權限者執行，或先補齊缺漏/重算後再封存"
            ),
        )

    with session_scope() as session:
        # 整月鎖，阻止同月任何重算發生於封存期間
        acquire_salary_lock(session, year=data.year, month=data.month)

        # 第一輪查詢:用於決定要鎖哪些 emp_id;rows 本身在 lock 取得後會 refresh。
        records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.salary_year == data.year,
                SalaryRecord.salary_month == data.month,
                SalaryRecord.is_finalized != True,
            )
            .all()
        )
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"{data.year} 年 {data.month} 月無可封存的薪資記錄（可能尚未計算，或全部已封存）",
            )

        # 對每位員工取鎖，與 bulk/manual 重算路徑互斥。
        # 必須在 missing/stale 檢查之前完成,否則檢查與封存之間可能有並發 mark_salary_stale。
        for r in records:
            acquire_salary_lock(
                session,
                employee_id=r.employee_id,
                year=data.year,
                month=data.month,
            )

        # 取鎖後 refresh 既有 record,確保看到的 needs_recalc / is_finalized 為 lock 後最新值。
        # Why: pg_advisory_xact_lock 取得後,session.refresh 會以 READ COMMITTED 拉到最新 commit;
        #      避免「query → 並發 mark_stale → 取鎖 → 仍以舊記憶體值封存」的 TOCTOU。
        for r in records:
            session.refresh(r)
        # 過濾掉鎖前一刻被其他流程封存的 record
        records = [r for r in records if not r.is_finalized]
        if not records:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{data.year} 年 {data.month} 月在取得封存鎖時,所有候選紀錄皆已被其他流程封存"
                ),
            )

        # force=True 路徑：先快照被略過的清單，稍後寫入 audit / 每筆 remark
        # (在 lock 與 refresh 之後,避免快照與真實封存之間再發生變化)
        skipped_missing: list[dict] = []
        skipped_stale: list[dict] = []
        if data.force:
            skipped_missing = _find_missing_salary_employees(
                session, data.year, data.month
            )
            skipped_stale = _find_stale_salary_employees(session, data.year, data.month)

        # 完整性檢查：當月在職員工是否都有薪資記錄（含已封存者）
        # 在取得 per-emp lock + refresh 之後執行,確保 stale 旗標反映 lock 後狀態。
        if not data.force:
            missing = _find_missing_salary_employees(session, data.year, data.month)
            if missing:
                names = "、".join(f"{m['name']}(#{m['id']})" for m in missing[:20])
                more = f"…等 {len(missing)} 人" if len(missing) > 20 else ""
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{data.year} 年 {data.month} 月有 {len(missing)} 位在職員工尚無薪資記錄："
                        f"{names}{more}。請先完成薪資計算，或於請求帶 force=true 強制封存（漏發風險自負）。"
                    ),
                )
            # stale 檢查:批次重算單筆失敗、假單/加班審核降級會將 SalaryRecord
            # 標 needs_recalc=True;此處擋下,避免封存到「上游事件後未成功重算」
            # 的舊薪資。force=True 仍可繞過(維持原 missing 一致的逃生口)。
            stale = _find_stale_salary_employees(session, data.year, data.month)
            if stale:
                names = "、".join(f"{s['name']}(#{s['id']})" for s in stale[:20])
                more = f"…等 {len(stale)} 人" if len(stale) > 20 else ""
                logger.warning(
                    "finalize 攔截:%d 年 %d 月有 %d 筆 needs_recalc=True 薪資",
                    data.year,
                    data.month,
                    len(stale),
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{data.year} 年 {data.month} 月有 {len(stale)} 位員工的薪資需重算："
                        f"{names}{more}。請先重新計算薪資,或於請求帶 force=true 強制封存(將封存舊資料,自負漏算/錯算風險)。"
                    ),
                )

        now = datetime.now()
        operator = current_user.get("username") or current_user.get("name") or "管理員"

        # force 路徑：把被略過的清單與原因寫進每筆 record.remark，留稽核痕跡
        force_remark_suffix = ""
        if data.force:
            missing_summary = (
                "、".join(f"{m['name']}(#{m['id']})" for m in skipped_missing[:20])
                or "無"
            )
            stale_summary = (
                "、".join(f"{s['name']}(#{s['id']})" for s in skipped_stale[:20])
                or "無"
            )
            force_remark_suffix = (
                f"\n[{now.strftime('%Y-%m-%d %H:%M')}] FORCE 封存（操作者：{operator}）"
                f"\n原因：{data.force_reason}"
                f"\n略過缺漏（{len(skipped_missing)} 人）：{missing_summary}"
                f"\n略過待重算（{len(skipped_stale)} 人）：{stale_summary}"
            )

        for r in records:
            r.is_finalized = True
            r.finalized_at = now
            r.finalized_by = operator
            if force_remark_suffix:
                r.remark = (r.remark or "") + force_remark_suffix
            _snapshot_svc.create_finalize_snapshot(session, r, operator)
        logger.info(
            "整月薪資封存：%d/%d，共 %d 筆，操作者=%s%s",
            data.year,
            data.month,
            len(records),
            operator,
            (
                f"，FORCE（缺漏 {len(skipped_missing)}/待重算 {len(skipped_stale)}）"
                f"，原因={data.force_reason}"
                if data.force
                else ""
            ),
        )

        # AuditMiddleware summary：把 force 詳情塞進稽核 row（不會被 body mask 掉）
        if data.force:
            request.state.audit_summary = (
                f"FORCE 封存 {data.year}/{data.month} 共 {len(records)} 筆"
                f"（缺漏 {len(skipped_missing)}、待重算 {len(skipped_stale)}）"
                f"；原因：{data.force_reason}；by {operator}"
            )

        count = len(records)
        finalized_at_iso = now.isoformat()

    _invalidate_finance_summary_cache()
    return {
        "message": f"已封存 {data.year} 年 {data.month} 月共 {count} 筆薪資記錄",
        "count": count,
        "finalized_by": operator,
        "finalized_at": finalized_at_iso,
        "force": data.force,
        "skipped_missing": skipped_missing if data.force else [],
        "skipped_stale": skipped_stale if data.force else [],
    }


class UnfinalizeSalaryRequest(BaseModel):
    """解除單筆薪資封存的請求 schema。

    解封等同打開「結帳鎖定」窗口讓上游資料可被修改後重新封存，是高風險操作。
    比照 force 封存：要求原因 ≥10 字 + ACTIVITY_PAYMENT_APPROVE 二人覆核。
    """

    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=(
            "解封原因（至少 10 字）。會寫入 record.remark 與 audit_summary，供日後稽核"
            "回溯為何重開結帳鎖定。"
        ),
    )


@router.delete("/salaries/{record_id}/finalize")
def unfinalize_salary(
    record_id: int,
    data: UnfinalizeSalaryRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """解除單筆薪資封存（危險操作，要求 reason + ACTIVITY_PAYMENT_APPROVE）"""
    if current_user.get("role") not in ("admin", "hr"):
        raise HTTPException(
            status_code=403, detail="薪資封存解除僅限系統管理員或人事主管操作"
        )
    # ── 比照 force 封存：解封等於重開結帳鎖定窗口，需金流簽核 ───────────────
    # Why: 原設計 unfinalize 只要 SALARY_WRITE + admin/hr，操作者可先解封單筆、
    # 改上游資料/手動調整、再重新封存，繞過原本封存代表的結帳語意。
    if not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "解除薪資封存需具備『金流簽核』權限（ACTIVITY_PAYMENT_APPROVE），"
                "請改由具該權限者執行"
            ),
        )
    reason_cleaned = data.reason.strip()

    from utils.advisory_lock import acquire_salary_lock

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        # 不得解除自己的薪資封存（避免一人完成「封存→解封→自我調整」）
        require_not_self_salary_record(
            current_user, record.employee_id, action="解除自己的薪資封存"
        )
        acquire_salary_lock(
            session,
            employee_id=record.employee_id,
            year=record.salary_year,
            month=record.salary_month,
        )
        session.refresh(record)
        if not record.is_finalized:
            raise HTTPException(status_code=409, detail="此筆薪資尚未封存，無需解封")
        operator = current_user.get("username") or current_user.get("name") or "管理員"
        finalized_by_before = record.finalized_by or "未知"
        finalized_at_before = (
            record.finalized_at.isoformat() if record.finalized_at else "未知"
        )
        logger.warning(
            "薪資封存解除！record_id=%d，employee_id=%d，%d/%d，操作者=%s，原因=%s",
            record_id,
            record.employee_id,
            record.salary_year,
            record.salary_month,
            operator,
            reason_cleaned,
        )
        record.is_finalized = False
        audit_note = (
            f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 封存解除"
            f"（操作者：{operator}）"
            f"\n原封存：{finalized_by_before} @ {finalized_at_before}"
            f"\n原因：{reason_cleaned}"
        )
        record.remark = (record.remark or "") + audit_note
        request.state.audit_summary = (
            f"解除薪資封存：record_id={record_id} employee_id={record.employee_id} "
            f"{record.salary_year}/{record.salary_month}；原封存：{finalized_by_before}；"
            f"操作者：{operator}；原因：{reason_cleaned}"
        )
    # 解封後 finance_summary 快取需失效（已封存月份的薪資金額會回變動態）
    _invalidate_finance_summary_cache()
    return {"message": "已解除封存，操作記錄已寫入備註欄位"}


