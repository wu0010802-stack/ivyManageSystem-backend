"""薪資快照 router (api/salary/snapshots.py) 對應 Out schemas — Phase 3.5。

涵蓋 4 grandfather endpoint（全 admin 後台 + self gate via router）：

- GET    /salaries/snapshots                       → SalarySnapshotListOut
- GET    /salaries/snapshots/{snapshot_id}         → SalarySnapshotDetailOut
- POST   /salaries/snapshots                       → SalarySnapshotCreateResultOut
- GET    /salaries/snapshots/{snapshot_id}/diff    → SalarySnapshotDiffOut

Out of scope (defer)：無（全 JSON，無 StreamingResponse / FileResponse）。

PII 註解：
- ``employee_name`` 為員工姓名快照（admin 後台必顯示；自家後台、與
  Sentry exempt 機制同精神，仍標 pii-allow）。
- 所有 ``*_salary`` / ``*_bonus`` / ``*_insurance_*`` / ``hourly_*`` /
  ``pension_*`` / ``*_deduction`` 等金額欄位皆為 SalaryRecord 快照值，命中
  ``salary`` / ``bonus_amount`` / ``insured`` substring 必標 pii-allow（admin/hr/
  本人 self gate 在 router `_enforce_self_or_full_salary`）。
- ``snapshot_remark`` / ``captured_by`` 為操作者 username 與管理員自填備註，
  非家長/學生 PII，不標 pii-allow。
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel

# ============ Shared sub-schemas ============


class SalarySnapshotSummaryOut(IvyBaseModel):
    """列表端點用的精簡 metadata（對應 service `_snapshot_summary()`）。

    `captured_at` 由 service 用 ``.isoformat()`` 轉成 str；無 tz 處理交由 service。
    """

    id: int
    salary_record_id: Optional[int] = (
        None  # pii-allow: SalaryRecord FK（非金額；trigger 'salary' substring）
    )
    employee_id: int
    employee_name: Optional[str] = None  # pii-allow: 員工姓名快照（admin 後台必顯示）
    salary_year: int  # pii-allow: 年份（非金額；trigger 'salary' substring）
    salary_month: int  # pii-allow: 月份（非金額；trigger 'salary' substring）
    snapshot_type: str
    captured_at: Optional[str] = None
    captured_by: Optional[str] = None
    source_version: Optional[int] = None
    snapshot_remark: Optional[str] = None
    net_salary: float = 0  # pii-allow: 員工實發金額（self gate 在 router）


# ============ GET /salaries/snapshots ============


class SalarySnapshotListOut(IvyBaseModel):
    """列表端點 wrapper。"""

    snapshots: list[SalarySnapshotSummaryOut]


# ============ GET /salaries/snapshots/{snapshot_id} ============


class SalarySnapshotDetailOut(IvyBaseModel):
    """單筆快照完整欄位（summary + 全部 SalaryRecord 反射複製欄位）。

    Payload 欄位來源：service ``_PAYLOAD_COLUMNS``（SalarySnapshot 與
    SalaryRecord 欄位交集，扣掉 metadata）。本 schema **必須宣告 _PAYLOAD_COLUMNS
    的每一欄**，否則該欄值雖 persist 且已進 payload dict，仍會被 response_model
    靜默丟棄、detail API 少回（2026-06-25 設計審查：原漏宣告 extra_allowance /
    extra_allowance_label / appraisal_year_end_bonus / supplementary_health_employee
    / unused_leave_payout 5 欄即如此，舊註解誤稱「SalarySnapshot model 未含」已過時）。
    新增 SalaryRecord/Snapshot 欄位時須同步補上對應欄位；
    tests/test_salary_snapshot_column_parity.py 已加 schema↔payload parity 守衛強制。
    """

    # ── summary 部分 ──────────────────────────────────────────────────
    id: int
    salary_record_id: Optional[int] = (
        None  # pii-allow: SalaryRecord FK（非金額；trigger 'salary' substring）
    )
    employee_id: int
    employee_name: Optional[str] = None  # pii-allow: 員工姓名快照（admin 後台必顯示）
    salary_year: int  # pii-allow: 年份（非金額；trigger 'salary' substring）
    salary_month: int  # pii-allow: 月份（非金額；trigger 'salary' substring）
    snapshot_type: str
    captured_at: Optional[str] = None
    captured_by: Optional[str] = None
    source_version: Optional[int] = None
    snapshot_remark: Optional[str] = None

    # ── 設定版本 FK ─────────────────────────────────────────────────
    bonus_config_id: Optional[int] = None
    attendance_policy_id: Optional[int] = None

    # ── Money 欄位（self gate 在 router）─────────────────────────────
    base_salary: Optional[float] = None  # pii-allow: 員工底薪
    festival_bonus: Optional[float] = None  # pii-allow: 節慶獎金
    overtime_bonus: Optional[float] = None  # pii-allow: 超額獎金
    performance_bonus: Optional[float] = None  # pii-allow: 績效獎金
    special_bonus: Optional[float] = None  # pii-allow: 特別獎金/紅利
    overtime_pay: Optional[float] = None  # pii-allow: 加班費（薪資組成）
    meeting_overtime_pay: Optional[float] = None  # pii-allow: 園務會議加班費
    meeting_absence_deduction: Optional[float] = None  # pii-allow: 園務會議缺席扣節金
    birthday_bonus: Optional[float] = None  # pii-allow: 生日禮金（薪資組成）
    hourly_rate: Optional[float] = None  # pii-allow: 時薪率（薪資組成）
    hourly_total: Optional[float] = None  # pii-allow: 時薪總計
    labor_insurance_employee: Optional[float] = None  # pii-allow: 勞保員工自付
    labor_insurance_employer: Optional[float] = None  # pii-allow: 勞保雇主負擔
    health_insurance_employee: Optional[float] = None  # pii-allow: 健保員工自付
    health_insurance_employer: Optional[float] = None  # pii-allow: 健保雇主負擔
    pension_employee: Optional[float] = None  # pii-allow: 勞退自提
    pension_employer: Optional[float] = None  # pii-allow: 勞退雇提
    late_deduction: Optional[float] = None  # pii-allow: 遲到扣款
    early_leave_deduction: Optional[float] = None  # pii-allow: 早退扣款
    missing_punch_deduction: Optional[float] = None  # pii-allow: 未打卡扣款
    leave_deduction: Optional[float] = None  # pii-allow: 請假扣款
    absence_deduction: Optional[float] = None  # pii-allow: 曠職扣款
    other_deduction: Optional[float] = None  # pii-allow: 其他扣款
    gross_salary: Optional[float] = None  # pii-allow: 應發總額
    total_deduction: Optional[float] = None  # pii-allow: 扣款總額
    net_salary: Optional[float] = None  # pii-allow: 實發金額
    bonus_amount: Optional[float] = None  # pii-allow: 獨立轉帳獎金金額
    supervisor_dividend: Optional[float] = None  # pii-allow: 主管紅利
    # 設計審查 2026-06-25 QW5：以下 5 欄已在 SalarySnapshot model 並進 _PAYLOAD_COLUMNS
    # 序列化，但本 schema 原漏宣告 → response_model 靜默丟棄，detail API 該回卻沒回。
    extra_allowance: Optional[float] = None  # pii-allow: 額外加給
    extra_allowance_label: Optional[str] = (
        None  # 額外加給標籤（admin 自定，非家長/學生 PII）
    )
    appraisal_year_end_bonus: Optional[float] = None  # pii-allow: 考核年終獎金
    supplementary_health_employee: Optional[float] = None  # pii-allow: 二代健保補充保費
    unused_leave_payout: Optional[float] = None  # pii-allow: 未休假折現

    # ── 計數 / Float / Bool / 備註 ─────────────────────────────────
    work_hours: Optional[float] = None
    late_count: Optional[int] = None
    early_leave_count: Optional[int] = None
    missing_punch_count: Optional[int] = None
    absent_count: Optional[int] = None
    bonus_separate: Optional[bool] = None
    remark: Optional[str] = None


# ============ GET /salaries/snapshots/{snapshot_id}/diff ============


class SalarySnapshotDiffChangeOut(IvyBaseModel):
    """單一欄位變動（snapshot vs current SalaryRecord）。

    `snapshot` / `current` 來源為任意 SalaryRecord/Snapshot column 值
    （Money / Int / Bool / Text / None），形態異質故用 ``Any``。
    """

    field: str
    snapshot: Optional[Any] = None  # pii-allow: 薪資欄位歷史值（self gate 在 router）
    current: Optional[Any] = None  # pii-allow: 薪資欄位當前值（self gate 在 router）


class SalarySnapshotDiffOut(IvyBaseModel):
    """快照與當前 SalaryRecord 的欄位差異對比結果。"""

    snapshot_id: int
    current_record_id: Optional[int] = None
    current_version: Optional[int] = None
    changes: list[SalarySnapshotDiffChangeOut]
    has_current_record: bool


# ============ POST /salaries/snapshots ============


class SalarySnapshotCreateResultOut(IvyBaseModel):
    """手動補拍快照成功回傳 — `{message, count, captured_by}`。

    不重用 `_common.MutationResultOut`（其 shape 為 `{message, id}`）；
    本 endpoint 為批次建立，沒有單一 id，且 router 額外回傳 `captured_by`
    供前端 audit 顯示。
    """

    message: str
    count: int
    captured_by: Optional[str] = None
