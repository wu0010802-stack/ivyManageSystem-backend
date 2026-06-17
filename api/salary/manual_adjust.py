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
from utils.taipei_time import now_taipei_naive
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


def _recompute_record_current_supplementary(session, record, insurance_service):
    """C13：以引擎相同純函式重算 record 當月獎金補充保費，並把差額併回
    health_insurance_employee（informational column supplementary_health_employee 同步）。

    Why: manual_adjust 改 BONUS_FIELDS_FOR_YTD 後只標 stale、只重算 total/net，當月補充
    保費未即時重算 → 到下次引擎重算之間 total_deduction/net 暫時失準。重用
    _resolve_health_insured_salary + calculate_bonus_supplementary_fee（與
    engine._finalize_breakdown 同一份計算，零 drift）即時重算；record 的 5 個獎金欄位即
    覆寫感知當前值。insurance_service 為 None 或計算失敗時優雅跳過，沿用 mark stale +
    finalize gate 收斂（不影響封存時的權威值正確性）。
    """
    if insurance_service is None:
        return
    try:
        from models.database import Employee
        from services.salary.supplementary_premium import (
            BONUS_FIELDS_FOR_YTD,
            DEFAULT_SUPPLEMENTARY_PREMIUM_RATE,
            _resolve_health_insured_salary,
            calculate_bonus_supplementary_fee,
        )

        emp = session.get(Employee, record.employee_id)
        if emp is None:
            return
        # bug #7：原手刻 emp_dict 用 emp.base_salary（個人底薪）+ 未帶 health_exempt /
        # 分項投保等欄位，與正式引擎 _load_emp_dict（走 _resolve_standard_base 取職位標準
        # 底薪）口徑不一致 → 投保基底偏差使當月補充保費/健保/實發暫時算錯。改用引擎
        # singleton 的 _load_emp_dict 建 emp_dict，確保基底與引擎零漂移。
        # singleton 未注入時退回一個空配置的引擎（_position_salary_standards 為空 →
        # _resolve_standard_base 沿用 emp.base_salary，等同舊行為），確保即時重算不靜默跳過。
        from . import _salary_engine

        engine = _salary_engine
        if engine is None:
            from services.salary_engine import SalaryEngine

            engine = SalaryEngine(load_from_db=False)
        emp_dict = engine._load_emp_dict(emp)
        health_insured_salary = _resolve_health_insured_salary(
            emp_dict, insurance_service
        )
        rate_setting = getattr(insurance_service, "supplementary_health_rate", None)
        rate = (
            DEFAULT_SUPPLEMENTARY_PREMIUM_RATE
            if rate_setting is None
            else float(rate_setting)
        )
        bonus_total = sum(float(getattr(record, f) or 0) for f in BONUS_FIELDS_FOR_YTD)
        new_fee = calculate_bonus_supplementary_fee(
            session,
            record.employee_id,
            record.salary_year,
            record.salary_month,
            breakdown_bonus_total=bonus_total,
            health_insured_salary=health_insured_salary,
            rate=rate,
        )
        old_fee = float(record.supplementary_health_employee or 0)
        if new_fee == old_fee:
            return
        record.health_insurance_employee = round_half_up(
            float(record.health_insurance_employee or 0) + (new_fee - old_fee)
        )
        record.supplementary_health_employee = new_fee
    except Exception:
        logger.warning(
            "manual_adjust 當月補充保費即時重算失敗（沿用 stale+gate 收斂）",
            exc_info=True,
        )


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
    extra_allowance: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    extra_allowance_label: Optional[str] = Field(None, max_length=50)


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
    "extra_allowance": "額外加給",
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
        _invalidate_salary_report_cache,
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

        # C9：本次調整前「已被人工鎖定」的欄位集合。對這些欄位「再調整」屬於跨請求
        # 拆筆風險——前次調整的 |偏移| 已從引擎 baseline 偏離(且本端點不持久化 baseline，
        # 無法回算精確累積偏移)，故對「重調已鎖定欄位」一律視為跨門檻、要求金流簽核，
        # 封死「多次各 < 門檻、每次 old_value 變成上次人工值」的繞過路徑。
        prior_locked_fields = set(record.manual_overrides or [])

        changed_parts = []
        modified_fields = []  # 本次寫過的欄位名單,稍後合併入 record.manual_overrides
        total_abs_delta = 0  # 本次請求所有欄位變動絕對值合計（涵蓋拆欄繞過）
        # C9：對「本次調整且已被前次人工鎖定」的欄位，累加其調整前值作為「相對 baseline
        # 已累積的人工偏移」近似量（獎金/扣款欄 baseline 趨近 0）。把它疊進金流簽核門檻
        # 基準，封死「多次各 < 門檻、靠 old_value 變成上次人工值」的跨請求拆筆繞過。
        prior_offset_abs = 0
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
            if field in prior_locked_fields:
                prior_offset_abs += abs(old_value)

        # 額外加給名目（文字欄，與數值欄分流）；設值時加入 manual_overrides，
        # 使 _fill_salary_record 的 _apply 在重算時跳過覆寫、保留名目（與金額欄同步鎖定）。
        if "extra_allowance_label" in payload:
            new_label = (payload.get("extra_allowance_label") or "").strip() or None
            old_label = record.extra_allowance_label
            if new_label != old_label:
                record.extra_allowance_label = new_label
                changed_parts.append(
                    f"額外加給名目 {old_label or '（空）'}→{new_label or '（空）'}"
                )
                modified_fields.append("extra_allowance_label")

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

        # ── A 錢守衛：本次所有欄位 |delta| 合計（含 festival_bonus 連動）+ 已鎖定欄位的
        # 歷史人工偏移 > 門檻需金流簽核 ──
        # Why: 舊版用「單欄位最大變動」作門檻，會計可一次調 N 欄各 999，總和達數千元
        # 而仍各自低於門檻，繞過 ACTIVITY_PAYMENT_APPROVE。改用合計門檻封死拆欄路徑；
        # 再疊上 prior_offset_abs（相對 baseline 的歷史偏移近似量），封死跨請求拆筆。
        require_finance_approve(
            total_abs_delta + prior_offset_abs,
            current_user,
            action_label="薪資單欄位調整總額",
        )

        # 將本次寫過的欄位名稱合併進 manual_overrides;後續上游事件觸發的重算,
        # _fill_salary_record 會跳過清單內的欄位,避免覆寫人工調整。
        existing_overrides = set(record.manual_overrides or [])
        record.manual_overrides = sorted(existing_overrides | set(modified_fields))

        # C13 / U3：改到 YTD 累計獎金欄位、或直接編輯 health_insurance_employee 時，當月補充
        # 保費即時重算並併回 health_insurance_employee（須在 _recalculate 前，使 fee 進
        # total_deduction）。
        from services.salary.supplementary_premium import (
            BONUS_FIELDS_FOR_YTD as _BONUS_YTD_FIELDS,
        )

        # U3（2026-06-17，業主裁示「HR 輸入視為基礎健保、系統疊加補充保費」）：HR 直接編輯
        # health_insurance_employee = 重設為基礎（原併入的當月補充保費 fee 被移除）。先把
        # informational 的 supplementary_health_employee 歸零，使下方 _recompute 以「全額」
        # 而非 diff 把當月補充保費重新疊回 health_insurance_employee，維持「health_insurance_
        # employee 恆含 fee」不變量；否則該欄被鎖後法定補充保費漏扣（既不在 health_insurance
        # 也不入 total_deduction）。
        he_edited = "health_insurance_employee" in modified_fields
        if he_edited:
            record.supplementary_health_employee = 0

        if (set(modified_fields) & set(_BONUS_YTD_FIELDS)) or he_edited:
            from . import _insurance_service

            _recompute_record_current_supplementary(session, record, _insurance_service)

        _recalculate_salary_record_totals(record)

        if (record.net_salary or 0) < 0:
            raise HTTPException(
                status_code=400,
                detail=f"調整後淨薪資為負數（{record.net_salary} 元），請確認扣款設定是否正確",
            )

        # 改到 YTD 累計獎金欄位（補充保費基底）時，把當月及同年「之後」未封存月份標
        # needs_recalc。Why: 二代健保補充保費採 per-payment 增額制，依賴前月正確落帳；手動
        # 改某月獎金會使當月自身與後月的補充保費基底失準（本端點只重算 total/net，不重算
        # 補充保費）。沿用 insurance 級距異動的 stale 傳播模式，強制 finalize 前重算。
        from services.salary.supplementary_premium import BONUS_FIELDS_FOR_YTD
        from services.salary.utils import mark_salary_stale_from_month

        if set(modified_fields) & set(BONUS_FIELDS_FOR_YTD):
            mark_salary_stale_from_month(
                session, record.employee_id, record.salary_year, record.salary_month
            )

        operator = current_user.get("username") or current_user.get("name") or "管理員"
        audit_note = (
            f"[{now_taipei_naive().strftime('%Y-%m-%d %H:%M')}] 手動編輯："
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

        audit_summary = (
            f"手動調整薪資 #{record.id} (員工 {record.employee_id}, "
            f"{record.salary_year}/{record.salary_month:02d}) "
            f"v{current_version}→v{new_version}：" + "；".join(changed_parts)
        )

        # 結構化 audit state（供 downstream middleware / 觀測層消費）。
        request.state.audit_entity_id = str(record.id)
        request.state.audit_summary = audit_summary

        # 同交易內寫 AuditLog（金流路徑：主資料 + 稽核共生死，避免 middleware
        # fire-and-forget 在 CI / threadpool 故障時丟稽核）。write_audit_in_session
        # 內部會設 request.state.audit_skip=True 防 middleware 二次寫入。
        from utils.audit import write_audit_in_session

        write_audit_in_session(
            session,
            request,
            action="UPDATE",
            entity_type="salary",
            entity_id=str(record.id),
            summary=audit_summary,
            changes={"fields": modified_fields, "reason": adjustment_reason},
        )

    # session_scope 退出後 commit，再失效 finance summary 與薪資報表快取
    _invalidate_finance_summary_cache()
    _invalidate_salary_report_cache()

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
                "extra_allowance": record.extra_allowance or 0,
                "extra_allowance_label": record.extra_allowance_label,
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
