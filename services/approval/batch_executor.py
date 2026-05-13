"""兩段提交 batch approval executor。

設計目的
=========
原本 leaves / overtimes batch_approve 兩條路由各自寫了「Pass 1 驗證 → Pass 2
套用 → commit」的兩段提交骨架（2026-05-11 P0-1 修補後一致），但各自有一段
70+ 行的 boilerplate。本 helper 把骨架抽出，讓 caller 只需專注於
domain-specific validator / side_effects / record_loader。

⚠️ 行為模式（fail_fast 旗標）
================================
現行 leaves / overtimes 端點為 **partial-success**：Pass 1 某筆驗失敗會
`failed.append; continue`，**其餘條目仍進 Pass 2 + commit**。原 plan 描述
為 fail-fast 屬誤判，本 helper 用 `fail_fast` 參數涵蓋兩種語意：

- `fail_fast=True` ：Pass 1 收集所有驗失敗後，**任一**失敗即整批 abort
  （無 side_effects、無 commit）。適合新邏輯或業主要求嚴格化的情境。
- `fail_fast=False`（預設）：Pass 1 驗失敗條目落入 `result.failed`，
  通過條目落入 Pass 2 套用 + commit。**保留現行 leaves/overtimes UX**。

選擇哪個模式由 caller 顯式指定，不在本 helper 內預設行為變更。

兩段架構
=========
Pass 1: record_loader 載入記錄 → validator 逐筆驗證（收集所有失敗到
        `result.failed`）。
Pass 2: 對通過 validator 的條目 → 寫 ApprovalLog + 呼叫 side_effects → commit。
Pass 3: caller 在收到 `BatchResult` 後負責 post-commit 動作（如 LINE 推播、
        薪資重算）。本 helper 不涉入。

注意事項
=========
- `record_loader` 由 caller 提供，可在內部決定是否 `with_for_update()` 鎖列。
- `side_effects` 只在 Pass 2 通過後對「全部 succeeded records」呼叫一次，
  方便做 bulk side effect（如預載 LINE user）。要做 per-record side effect
  caller 可在 `side_effects` 內自行 loop。
- `result.succeeded` 為 `list[tuple[record, approval_log_id|None]]`，方便
  caller 在 audit_changes / LINE 推播時直接引用 approval_log_id 而不必再查。
- `_write_approval_log` 失敗（DB 異常）時 row 回傳 None，approval_log_id 為
  None；該紀錄仍視為 succeeded（與既有 router 行為一致：log 寫不進不阻擋核准）。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fastapi import HTTPException

from utils.approval_helpers import _write_approval_log


@dataclass
class BatchResult:
    """`succeeded`: list of (record, approval_log_id | None)
    `failed`: list of {"id": int, "reason": str}
    """

    succeeded: list = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def execute_batch_approval(
    *,
    session,
    doc_type: str,
    record_ids: list[int],
    action: Literal["approve", "reject"],
    actor: dict,
    validator: Callable[[Any, Any], None],
    side_effects: Callable[[Any, list], None],
    record_loader: Callable[[Any, list[int]], list],
    rejection_reason: str | None = None,
    fail_fast: bool = False,
) -> BatchResult:
    """兩段提交 batch approval。

    Args:
        session: SQLAlchemy session（由 caller 控制 lifecycle / close）。
        doc_type: "leave" / "overtime" / "punch_correction"，寫入 ApprovalLog。
        record_ids: 待處理的 record id list。
        action: "approve" 或 "reject"，寫入 ApprovalLog。
        actor: current_user dict（須含 id/username/role）。
        validator: `(session, record) -> None`，驗失敗時 raise HTTPException。
        side_effects: `(session, list[record]) -> None`，只對「Pass 2 通過後即將
            commit」的 records 呼叫一次。在 commit **前**執行（可寫 DB）。
        record_loader: `(session, ids) -> list[record]`，caller 自行決定是否鎖列。
            預期回傳 records 順序對應 record_ids（缺失 id 不出現也可，由 validator
            或 caller 自己處理 "not found"）。
        rejection_reason: action="reject" 時的駁回原因，寫入 ApprovalLog.comment。
        fail_fast: True=任一驗失敗即整批 abort。False=partial-success（保留現行
            leaves/overtimes UX）。預設 False（保守）。

    Returns:
        BatchResult，含 succeeded（list of (record, approval_log_id)）與 failed。
    """
    records = record_loader(session, record_ids)
    result = BatchResult()
    passed: list = []  # records that survived validator

    # ── Pass 1：純驗證 ────────────────────────────────────────────────────
    for rec in records:
        try:
            validator(session, rec)
            passed.append(rec)
        except HTTPException as exc:
            result.failed.append({"id": rec.id, "reason": exc.detail})

    # fail_fast：任一失敗即整批 abort（不寫入、不 commit）
    if fail_fast and result.failed:
        return result

    if not passed:
        return result

    # ── Pass 2：寫 ApprovalLog → side_effects → commit ─────────────────────
    for rec in passed:
        log_row = _write_approval_log(
            session=session,
            doc_type=doc_type,
            doc_id=rec.id,
            action=action,
            approver=actor,
            comment=rejection_reason,
        )
        log_id = log_row.id if log_row else None
        result.succeeded.append((rec, log_id))

    side_effects(session, passed)
    session.commit()
    return result
