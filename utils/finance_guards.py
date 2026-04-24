"""
utils/finance_guards.py — 跨模組金流 A 錢守衛

提供員工薪資（employees.py / salary.py）、學費退款（fees.py）共用的
「自我修改攔截 / 大額審批 / 原因必填」helper，避免單一權限者一人完成敏感金流動作。

Why: 原設計多處只檢查 `SALARY_WRITE / FEES_WRITE / EMPLOYEES_WRITE` 即可操作，
員工可改自己薪資、單人可退大額學費、手動調整無簽核，存在內部舞弊風險。
沿用既有的 `ACTIVITY_PAYMENT_APPROVE` 位元作為「金流簽核」統一門檻，
不新增權限位避免 migration 成本；語意上視為「金流高階審批」（老闆/財務長）。

所有 helper 於違反時直接 raise HTTPException，handler 只需呼叫即可。
"""

from typing import Optional, Iterable

from fastapi import HTTPException

from utils.permissions import Permission, has_permission

# ── 常數 ────────────────────────────────────────────────────────────────
# 金流原因最短字數（防止填「.」或「誤」等敷衍內容）
MIN_FINANCE_REASON_LENGTH = 5

# 大額審批閾值（NT$）— 超過此金額的退款 / 薪資調整等需金流簽核權限
FINANCE_APPROVAL_THRESHOLD = 1000

# 員工自身資料中「屬於金流敏感面」的欄位：不允許員工自己修改
# employees.py PUT 的 update_data 如果含這些 key 且 target_id==自己 → 403
#
# 除了直接的金額欄位外，還包含「間接影響薪資計算」的欄位：
# - hire_date：節慶獎金資格三個月門檻、prorate 底薪
# - job_title_id / title / position：底薪標準、主管紅利、節慶獎金基數
# - supervisor_role：主管紅利等級
# - bonus_grade：節慶獎金職稱等級覆寫
# - classroom_id：帶班身份 → 班級節慶獎金 / 學生數獎金
EMPLOYEE_SALARY_SENSITIVE_FIELDS = frozenset(
    {
        # 直接金額
        "base_salary",
        "hourly_rate",
        "performance_bonus_rate",
        "pension_self_rate",
        "insurance_salary_level",
        "insurance_salary",
        "labor_insurance_salary",
        "health_insurance_salary",
        "employee_type",  # 影響時薪/月薪身份切換
        # 間接影響薪資計算
        "hire_date",
        "job_title_id",
        "title",
        "position",
        "supervisor_role",
        "bonus_grade",
        "classroom_id",
    }
)


# ── Helpers ─────────────────────────────────────────────────────────────


def has_finance_approve(current_user: dict) -> bool:
    """檢查使用者是否具備「金流簽核」權限（沿用 ACTIVITY_PAYMENT_APPROVE 位元）。

    用於：大額退費 / 薪資調整 / 學費退款等敏感金流動作的二簽檢查。
    """
    perms = current_user.get("permissions", 0)
    return has_permission(perms, Permission.ACTIVITY_PAYMENT_APPROVE)


def _current_user_employee_id(current_user: dict) -> Optional[int]:
    """從 current_user 取出對應的 employee_id（pure admin 帳號回 None）。"""
    eid = current_user.get("employee_id")
    return int(eid) if eid else None


def require_not_self_edit(
    current_user: dict,
    target_employee_id: int,
    update_fields: Iterable[str],
    *,
    sensitive_fields: Iterable[str] = EMPLOYEE_SALARY_SENSITIVE_FIELDS,
) -> None:
    """若操作者與目標員工相同，且 update_fields 含任一敏感欄位 → 403。

    - `update_fields`：handler 本次實際要更新的欄位名 set（從 pydantic model_dump）
    - `sensitive_fields`：視為敏感金流的欄位（預設為薪資類）

    純管理員（無 employee_id）不會被擋，讓 HR/財務長可正常改他人資料；
    一般員工只要有 EMPLOYEES_WRITE 也不該能調自己薪資。
    """
    self_eid = _current_user_employee_id(current_user)
    if not self_eid or self_eid != target_employee_id:
        return
    touched = {f for f in update_fields if f in sensitive_fields}
    if not touched:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"不得修改自己的金流敏感欄位（{', '.join(sorted(touched))}），"
            f"請由主管或 HR 代為處理"
        ),
    )


def require_not_self_salary_record(
    current_user: dict,
    record_employee_id: int,
    *,
    action: str = "調整自己的薪資紀錄",
) -> None:
    """擋「自己改自己的 SalaryRecord」— 用於 manual-adjust 等寫入端點。

    純管理員（無 employee_id）不擋。
    """
    self_eid = _current_user_employee_id(current_user)
    if self_eid and self_eid == record_employee_id:
        raise HTTPException(
            status_code=403, detail=f"不得{action}，請由主管或 HR 代為操作"
        )


def require_finance_approve(
    amount: int,
    current_user: dict,
    *,
    threshold: int = FINANCE_APPROVAL_THRESHOLD,
    action_label: str = "金流動作",
) -> None:
    """若 amount 超過閾值，檢查 has_finance_approve；否則 403。

    amount 可為絕對變動金額（退款金額、薪資調整 delta）。
    """
    if amount > threshold and not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{action_label} NT${amount:,} 超過 NT${threshold:,} 審批閾值，"
                f"需由具備『金流簽核』權限者（ACTIVITY_PAYMENT_APPROVE）執行"
            ),
        )


def require_adjustment_reason(
    reason: Optional[str], *, min_length: int = MIN_FINANCE_REASON_LENGTH
) -> str:
    """驗證原因字串必填且去除空白後 ≥ min_length 字；成功回傳 cleaned 字串。"""
    cleaned = (reason or "").strip()
    if len(cleaned) < min_length:
        raise HTTPException(
            status_code=400,
            detail=f"必須填寫原因（至少 {min_length} 個字，不可敷衍）",
        )
    return cleaned
