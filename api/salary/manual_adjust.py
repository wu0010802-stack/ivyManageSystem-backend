"""
api/salary/manual_adjust.py — 手動調整單筆薪資

含 1 個 endpoint + 1 schema + 2 module 常數 + 1 helper：
- PUT /salaries/{record_id}/manual-adjust

公開 symbol（test 直接 import；由 api.salary.__init__ re-export 維持原 surface）：
- SalaryManualAdjustRequest    (test_salary_manual_adjust_bounds)
- _MANUAL_ADJUST_FIELD_MAX     (test_salary_manual_adjust_bounds)
- _parse_if_match              (test_salary_manual_adjust)

_recalculate_salary_record_totals 與 _invalidate_finance_summary_cache
仍在 api.salary.__init__（finalize 也用），本檔以 lazy import 呼叫，
保持 monkeypatch 仍作用於 __init__ 那份。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, Field

from models.base import session_scope
from models.database import SalaryRecord
from utils.auth import require_staff_permission
from utils.error_messages import SALARY_RECORD_NOT_FOUND
from utils.finance_guards import (
    require_finance_approve,
    require_not_self_salary_record,
)
from utils.permissions import Permission
from utils.rounding import round_half_up

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_if_match(header_value: Optional[str]) -> Optional[int]:
    """解析 If-Match header，支援 W/"3" / "3" / 3 等常見格式。回傳 int 版本號或 None。"""
    if not header_value:
        return None
    raw = header_value.strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    raw = raw.strip('"').strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# 單筆欄位合理上限：從舊版 10,000,000 降為 500,000 — 涵蓋幼稚園業界合法月薪/一次性
# 獎金上限（含 outlier），超過視為誤植或舞弊嘗試。
# Why: 單欄位上限 1000 萬 × 可同時調多欄位 = 單次可偷數千萬。降至 50 萬可有效壓縮
# 舞弊上限，且合法調整不受影響。
_MANUAL_ADJUST_FIELD_MAX = 500_000.0


class SalaryManualAdjustRequest(BaseModel):
    # 必填原因：供稽核追責，避免「from 5000 to 100000」無上下文 audit log
    adjustment_reason: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="手動調整原因（至少 5 字，例：員工自請補發、主管核准一次性獎勵、誤算修正）",
    )
    base_salary: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    performance_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    special_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    festival_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    overtime_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    overtime_pay: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    supervisor_dividend: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    meeting_overtime_pay: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    birthday_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    labor_insurance_employee: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    health_insurance_employee: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    pension_employee: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    leave_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    late_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    early_leave_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    missing_punch_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    meeting_absence_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    absence_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    other_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)


EDITABLE_SALARY_FIELDS = {
    "base_salary": "底薪",
    "performance_bonus": "績效獎金",
    "special_bonus": "特別獎金",
    "festival_bonus": "節慶獎金",
    "overtime_bonus": "超額獎金",
    "overtime_pay": "加班津貼",
    "supervisor_dividend": "主管紅利",
    "meeting_overtime_pay": "會議加班",
    "birthday_bonus": "生日禮金",
    "labor_insurance_employee": "勞保",
    "health_insurance_employee": "健保",
    "pension_employee": "勞退自提",
    "leave_deduction": "請假扣款",
    "late_deduction": "遲到扣款",
    "early_leave_deduction": "早退扣款",
    "missing_punch_deduction": "未打卡扣款",
    "meeting_absence_deduction": "節慶獎金扣減",
    "absence_deduction": "曠職扣款",
    "other_deduction": "其他扣款",
}


@router.put("/salaries/{record_id}/manual-adjust")
def manual_adjust_salary(
    record_id: int,
    data: SalaryManualAdjustRequest,
    response: Response,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """手動調整單筆薪資記錄。

    若請求帶有 If-Match header，需與目前 record.version 相符才能寫入（樂觀鎖）。
    不帶 If-Match 時允許寫入（舊版前端相容），仍會累加版本號。
    成功時於 ETag / X-Record-Version header 回傳新版本。
    """
    from utils.advisory_lock import acquire_salary_lock

    # Lazy import：保持 monkeypatch 仍作用於 __init__ 那份。
    from . import (
        _invalidate_finance_summary_cache,
        _recalculate_salary_record_totals,
    )

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        # ── A 錢守衛：不得調整自己的 SalaryRecord（純管理員帳號不受限）──
        require_not_self_salary_record(
            current_user, record.employee_id, action="調整自己的薪資紀錄"
        )

        # advisory lock：確保與計算引擎不會同時寫入同筆 SalaryRecord
        acquire_salary_lock(
            session,
            employee_id=record.employee_id,
            year=record.salary_year,
            month=record.salary_month,
        )
        # 鎖住後重讀，取得最新狀態
        session.refresh(record)
        if record.is_finalized:
            raise HTTPException(
                status_code=409, detail="此筆薪資已封存，請先解除封存再編輯"
            )

        client_version = _parse_if_match(if_match)
        current_version = int(record.version or 1)
        if client_version is not None and client_version != current_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此筆薪資已被他人修改（目前版本 v{current_version}，"
                    f"你持有 v{client_version}），請重新整理後再編輯"
                ),
            )

        payload = data.model_dump(exclude_unset=True)
        # adjustment_reason 為 schema required；從 payload 拉出後只留實際要寫入的金額欄位
        adjustment_reason = (payload.pop("adjustment_reason", "") or "").strip()
        if not payload:
            raise HTTPException(status_code=400, detail="至少需要提供一個調整欄位")

        # 在套用變更前，記下舊的 festival_bonus / meeting_absence_deduction，
        # 用於 #2 連動：若管理員只改 meeting_absence_deduction，
        # 自動回推 raw festival 並重套新的扣減。
        old_festival_bonus = round_half_up(record.festival_bonus or 0)
        old_meeting_absence = round_half_up(record.meeting_absence_deduction or 0)

        changed_parts = []
        modified_fields = []  # 本次寫過的欄位名單,稍後合併入 record.manual_overrides
        total_abs_delta = 0  # 本次請求所有欄位變動絕對值合計（涵蓋拆欄繞過）
        for field, value in payload.items():
            if field not in EDITABLE_SALARY_FIELDS:
                continue
            old_value = round_half_up(getattr(record, field) or 0)
            new_value = round_half_up(value or 0)
            if old_value == new_value:
                continue
            setattr(record, field, new_value)
            changed_parts.append(
                f"{EDITABLE_SALARY_FIELDS[field]} {old_value}→{new_value}"
            )
            modified_fields.append(field)
            total_abs_delta += abs(new_value - old_value)

        if not changed_parts:
            raise HTTPException(status_code=400, detail="沒有實際變更")

        # 連動：管理員只改 meeting_absence_deduction（未同時手動覆寫 festival_bonus）時，
        # festival_bonus 應跟著 raw 重算：raw = old_festival + old_meeting_absence。
        # 此連動產生的 |delta| 也要納入 total_abs_delta，避免「降 meeting_absence 連動
        # 推高 festival_bonus」拆兩動作繞過 FINANCE_APPROVAL_THRESHOLD。
        meeting_absence_in_payload = "meeting_absence_deduction" in payload
        festival_bonus_in_payload = "festival_bonus" in payload
        if meeting_absence_in_payload and not festival_bonus_in_payload:
            new_meeting_absence = round_half_up(record.meeting_absence_deduction or 0)
            inferred_raw = old_festival_bonus + old_meeting_absence
            recomputed_festival = max(0, inferred_raw - new_meeting_absence)
            if recomputed_festival != old_festival_bonus:
                record.festival_bonus = recomputed_festival
                changed_parts.append(
                    f"節慶獎金（連動）{old_festival_bonus}→{recomputed_festival}"
                )
                # 連動寫入的 festival_bonus 也視為人工調整,同一原則保留不被重算覆寫
                modified_fields.append("festival_bonus")
                total_abs_delta += abs(recomputed_festival - old_festival_bonus)

        # ── A 錢守衛：本次所有欄位 |delta| 合計（含 festival_bonus 連動）> 門檻需金流簽核 ──
        # Why: 舊版用「單欄位最大變動」作門檻，會計可一次調 N 欄各 999，總和達數千元
        # 而仍各自低於門檻，繞過 ACTIVITY_PAYMENT_APPROVE。改用合計門檻封死拆欄路徑。
        require_finance_approve(
            total_abs_delta, current_user, action_label="薪資單欄位調整總額"
        )

        # 將本次寫過的欄位名稱合併進 manual_overrides;後續上游事件觸發的重算,
        # _fill_salary_record 會跳過清單內的欄位,避免覆寫人工調整。
        existing_overrides = set(record.manual_overrides or [])
        record.manual_overrides = sorted(existing_overrides | set(modified_fields))

        _recalculate_salary_record_totals(record)

        if (record.net_salary or 0) < 0:
            raise HTTPException(
                status_code=400,
                detail=f"調整後淨薪資為負數（{record.net_salary} 元），請確認扣款設定是否正確",
            )

        operator = current_user.get("username") or current_user.get("name") or "管理員"
        audit_note = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 手動編輯："
            + "；".join(changed_parts)
            + f"；操作者：{operator}；原因：{adjustment_reason}"
        )
        record.remark = f"{(record.remark or '').strip()}\n{audit_note}".strip()

        record.version = current_version + 1

        logger.warning(
            "手動調整薪資：record_id=%s employee_id=%s fields=%s operator=%s version=%d→%d",
            record.id,
            record.employee_id,
            ",".join(payload.keys()),
            operator,
            current_version,
            record.version,
        )

        new_version = int(record.version)
        response.headers["ETag"] = f'"{new_version}"'
        response.headers["X-Record-Version"] = str(new_version)

        # 結構化 audit：供 AuditMiddleware 寫入 AuditLog（取代原通用「修改薪資」摘要）
        request.state.audit_entity_id = str(record.id)
        request.state.audit_summary = (
            f"手動調整薪資 #{record.id} (員工 {record.employee_id}, "
            f"{record.salary_year}/{record.salary_month:02d}) "
            f"v{current_version}→v{new_version}：" + "；".join(changed_parts)
        )

    # session_scope 退出後 commit，再失效 finance summary 快取
    _invalidate_finance_summary_cache()

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        return {
            "message": "薪資金額已更新",
            "record": {
                "id": record.id,
                "version": new_version,
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "overtime_pay": record.overtime_pay or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "birthday_bonus": record.birthday_bonus or 0,
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "gross_salary": record.gross_salary or 0,
                "total_deduction": record.total_deduction or 0,
                "net_salary": record.net_salary or 0,
                "bonus_amount": record.bonus_amount or 0,
                "bonus_separate": bool(record.bonus_separate),
                "remark": record.remark,
                "manual_overrides": list(record.manual_overrides or []),
            },
        }
