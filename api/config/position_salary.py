"""api/config/position_salary.py — 職位標準底薪設定 + 比對/同步。

4 個 endpoint + 2 個 Pydantic schema + 3 個 helper：
- GET  /position-salary           取得標準底薪
- PUT  /position-salary           更新標準底薪（版本 +1，標 needs_recalc）
- GET  /position-salary/compare   比對員工底薪 vs 標準
- POST /position-salary/sync      將員工底薪同步至標準（含金流守衛）

_salary_engine 經 lazy back-import 取得（同 .line 模式）。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from models.database import (
    get_session,
    PositionSalaryConfig,
    SalaryRecord,
)
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.finance_guards import (
    MIN_FINANCE_REASON_LENGTH,
    require_adjustment_reason,
    require_finance_approve,
    require_not_self_salary_record,
)
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter()

# Why: 防止先把標準設成天文數字，再透過 sync_position_salary 繞過 manual-adjust
# 的單員工調薪審批；le 與 manual-adjust 統一上限。
_POSITION_SALARY_MAX = 500_000.0


class PositionSalaryUpdate(BaseModel):
    """職位標準底薪設定更新

    每欄位 le=_POSITION_SALARY_MAX：與 manual-adjust 同一上限，避免天文數字標準
    透過 sync 繞過手動調薪審批。
    """

    head_teacher_a: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    head_teacher_b: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    head_teacher_c: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    assistant_teacher_a: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    assistant_teacher_b: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    assistant_teacher_c: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    admin_staff: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    english_teacher: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    art_teacher: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    designer: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    nurse: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    driver: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    kitchen_staff: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    director: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)
    principal: Optional[float] = Field(None, ge=0, le=_POSITION_SALARY_MAX)


class PositionSalarySyncRequest(BaseModel):
    """職位薪資同步請求。

    P1 Security：必填 adjustment_reason 供稽核（≥ MIN_FINANCE_REASON_LENGTH 字）；
    與 manual-adjust 一致，避免批次調薪不留原因。
    """

    employee_ids: list[int] = Field(
        default_factory=list, description="空清單表示同步全部可對應員工"
    )
    adjustment_reason: str = Field(
        ...,
        min_length=MIN_FINANCE_REASON_LENGTH,
        max_length=200,
        description=(
            f"批次調薪原因（≥ {MIN_FINANCE_REASON_LENGTH} 字），會寫入操作日誌與 audit"
        ),
    )


def _resolve_grade(emp) -> str:
    """決定員工等級（a/b/c）。
    優先用 bonus_grade 欄位；若未設定則依職稱推算：
      - 教保員、助理教保員 → b
      - 其餘 → c
    """
    if emp.bonus_grade and emp.bonus_grade.lower() in ("a", "b", "c"):
        return emp.bonus_grade.lower()
    title = emp.title or ""
    if title == "幼兒園教師":
        return "a"
    if title in ("教保員", "助理教保員"):
        return "b"
    return "c"


def _map_employee_to_standard_key(emp) -> str | None:
    """將員工職稱 + 職位 + 等級映射至 PositionSalaryConfig 欄位名稱。
    回傳 None 表示此員工不適用標準底薪（例如：園長、主任、時薪制）。
    """
    title = emp.title or ""
    position = emp.position or ""

    # 時薪制（base_salary=0）跳過
    if (emp.base_salary or 0) == 0:
        return None
    # 領導職（園長、主任）為特例薪，不比對
    # 領導職改為回傳對應 key（由 _get_standard_salary 決定是否有值）
    if position == "主任" or title == "主任":
        return "director"
    if position == "園長" or title == "園長":
        return "principal"

    if "司機" in title:
        return "driver"
    if "廚" in title:
        return "kitchen_staff"
    if "美師" in title or "藝術" in title:
        return "art_teacher"
    if position == "行政":
        return "admin_staff"
    if position in ("班導", "班導師") or (title == "組長" and position == "班導"):
        return f"head_teacher_{_resolve_grade(emp)}"
    if position in ("副班導", "副班導師"):
        return f"assistant_teacher_{_resolve_grade(emp)}"
    return None


def _get_standard_salary(config_row, key: str):
    """從 PositionSalaryConfig 取得指定欄位值。
    director / principal 允許回傳 None（表示留空、不比對）。
    """
    _defaults = {
        "head_teacher_a": 39240,
        "head_teacher_b": 37160,
        "head_teacher_c": 33000,
        "assistant_teacher_a": 35240,
        "assistant_teacher_b": 33000,
        "assistant_teacher_c": 29500,
        "admin_staff": 37160,
        "english_teacher": 32500,
        "art_teacher": 30000,
        "designer": 30000,
        "nurse": 29800,
        "driver": 30000,
        "kitchen_staff": 29700,
        "director": None,
        "principal": None,
    }
    raw = getattr(config_row, key, None) if config_row else None
    if raw is None:
        raw = _defaults.get(key)
    return float(raw) if raw is not None else None


@router.get("/position-salary")
async def get_position_salary(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得職位標準底薪設定"""
    session = get_session()
    try:
        config = (
            session.query(PositionSalaryConfig)
            .order_by(PositionSalaryConfig.id.desc())
            .first()
        )
        if not config:
            # 回傳預設值
            return {
                "id": None,
                "head_teacher_a": 39240,
                "head_teacher_b": 37160,
                "head_teacher_c": 33000,
                "assistant_teacher_a": 35240,
                "assistant_teacher_b": 33000,
                "assistant_teacher_c": 29500,
                "admin_staff": 37160,
                "english_teacher": 32500,
                "art_teacher": 30000,
                "designer": 30000,
                "nurse": 29800,
                "driver": 30000,
                "kitchen_staff": 29700,
                "director": None,
                "principal": None,
                "version": 0,
                "changed_by": None,
            }
        return {
            "id": config.id,
            "head_teacher_a": config.head_teacher_a,
            "head_teacher_b": config.head_teacher_b,
            "head_teacher_c": config.head_teacher_c,
            "assistant_teacher_a": config.assistant_teacher_a,
            "assistant_teacher_b": config.assistant_teacher_b,
            "assistant_teacher_c": config.assistant_teacher_c,
            "admin_staff": getattr(config, "admin_staff", 37160),
            "english_teacher": getattr(config, "english_teacher", 32500),
            "art_teacher": getattr(config, "art_teacher", 30000),
            "designer": getattr(config, "designer", 30000),
            "nurse": getattr(config, "nurse", 29800),
            "driver": getattr(config, "driver", 30000),
            "kitchen_staff": getattr(config, "kitchen_staff", 29700),
            "director": getattr(config, "director", None),
            "principal": getattr(config, "principal", None),
            "version": config.version,
            "changed_by": config.changed_by,
        }
    finally:
        session.close()


@router.put("/position-salary")
async def update_position_salary(
    data: PositionSalaryUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新職位標準底薪設定（無資料則 insert，有則版本 +1）"""
    from . import _salary_engine  # lazy back-import

    session = get_session()
    try:
        config = (
            session.query(PositionSalaryConfig)
            .order_by(PositionSalaryConfig.id.desc())
            .first()
        )
        update_data = data.dict(exclude_none=True)
        operator = current_user.get("username", "")

        if config:
            for key, value in update_data.items():
                setattr(config, key, value)
            config.version = (config.version or 1) + 1
            config.changed_by = operator
        else:
            config = PositionSalaryConfig(
                changed_by=operator,
                **update_data,
            )
            session.add(config)

        # 職位標準底薪改版會影響薪資反查比對結果（_get_standard_salary）→
        # 標所有未封存薪資 needs_recalc=True，避免 finalize 以舊標準封存。
        # 封存 (is_finalized=True) 的不動，維持結帳鎖定語意。
        stale_marked = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.is_finalized != True)
            .update({SalaryRecord.needs_recalc: True}, synchronize_session=False)
        )
        session.commit()
        if stale_marked:
            logger.warning(
                "職位標準底薪設定更新後標記 %d 筆未封存薪資為 needs_recalc",
                stale_marked,
            )
        # engine 載入時會 cache PositionSalaryConfig，需 reload 才能讓後續
        # simulate / 重算讀到新版本（其餘 PUT /api/config/* 端點都已遵循）。
        if _salary_engine is not None:
            _salary_engine.load_config_from_db()
        logger.warning("職位標準底薪設定已更新，操作人：%s", operator)
        return {
            "message": "職位標準底薪設定已更新",
            "version": config.version,
            "salary_records_marked_stale": stale_marked,
        }
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/position-salary/compare")
def compare_position_salary(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """比對所有在職員工的底薪與職位標準，回傳有差異（或無法對應）的員工清單。"""
    from models.database import Employee

    session = get_session()
    try:
        config = (
            session.query(PositionSalaryConfig)
            .order_by(PositionSalaryConfig.id.desc())
            .first()
        )
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        result = []
        for emp in employees:
            key = _map_employee_to_standard_key(emp)
            if key is None:
                continue
            standard = _get_standard_salary(config, key)
            if standard is None:
                # 留空標準（如園長）→ 跳過比對
                continue
            current = float(emp.base_salary or 0)
            diff = current - standard
            result.append(
                {
                    "employee_id": emp.id,
                    "name": emp.name,
                    "title": emp.title,
                    "position": emp.position,
                    "bonus_grade": emp.bonus_grade,
                    "standard_key": key,
                    "current_salary": current,
                    "standard_salary": standard,
                    "diff": diff,
                    "in_sync": abs(diff) < 1,
                }
            )

        result.sort(key=lambda x: (x["in_sync"], x["name"]))
        return {
            "employees": result,
            "out_of_sync": sum(1 for r in result if not r["in_sync"]),
        }
    finally:
        session.close()


@router.post("/position-salary/sync")
def sync_position_salary(
    data: PositionSalarySyncRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """將指定員工（或全部）的底薪更新為職位標準底薪。

    P1 Security：權限改用 SALARY_WRITE（移除 SETTINGS_WRITE 旁路）；
    員工不得同步自己的薪資；任一員工 |delta| > FINANCE_APPROVAL_THRESHOLD
    需 ACTIVITY_PAYMENT_APPROVE；逐員工 audit_changes 寫入 request.state。
    """
    from models.database import Employee

    cleaned_reason = require_adjustment_reason(data.adjustment_reason)

    session = get_session()
    try:
        config = (
            session.query(PositionSalaryConfig)
            .order_by(PositionSalaryConfig.id.desc())
            .first()
        )
        query = session.query(Employee).filter(Employee.is_active == True)
        if data.employee_ids:
            query = query.filter(Employee.id.in_(data.employee_ids))
        employees = query.all()

        # 第一輪：dry-run 算出每位 delta，用於守衛 + audit；通過後再 mutate。
        planned_updates = []  # [(emp, old, new, delta_abs)]
        skipped = []
        for emp in employees:
            key = _map_employee_to_standard_key(emp)
            if key is None:
                skipped.append(emp.name)
                continue
            standard = _get_standard_salary(config, key)
            if standard is None:
                skipped.append(emp.name)
                continue
            old = float(emp.base_salary or 0)
            if abs(old - standard) >= 1:
                planned_updates.append(
                    (emp, old, float(standard), abs(float(standard) - old))
                )

        # 自我修改攔截：員工不得 sync 自己（即使只是「同步至標準」也屬調薪）
        for emp, _old, _new, _delta in planned_updates:
            require_not_self_salary_record(
                current_user,
                emp.id,
                action="同步自己的薪資至職位標準",
            )

        # 大額調薪簽核：任一員工的 |delta| 超過閾值即整批需金流簽核
        max_delta = max((d for _, _, _, d in planned_updates), default=0)
        if max_delta > 0:
            require_finance_approve(
                int(max_delta),
                current_user,
                action_label=f"批次同步職位標準底薪（最大單員工調幅 NT${int(max_delta):,}）",
            )

        updated = []
        for emp, old, standard, _delta in planned_updates:
            emp.base_salary = standard
            updated.append({"name": emp.name, "old": old, "new": standard})

        session.commit()
        operator = current_user.get("username", "")
        logger.warning(
            "職位標準底薪同步：操作人 %s，更新 %d 人，原因：%s，名單：%s",
            operator,
            len(updated),
            cleaned_reason,
            [u["name"] for u in updated],
        )
        # 中央稽核：逐員工 old/new 寫入 audit_changes 供 AuditLog 留底
        request.state.audit_summary = (
            f"職位標準底薪同步：更新 {len(updated)} 人；原因：{cleaned_reason}"
        )
        request.state.audit_changes = {
            "reason": cleaned_reason,
            "updated": updated,
            "skipped": skipped,
            "max_single_delta": int(max_delta),
        }
        return {"updated": updated, "skipped": skipped, "total_updated": len(updated)}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
