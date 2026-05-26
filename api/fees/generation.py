"""api/fees/generation.py — 批次產生費用記錄（範本驅動單一入口）。

c2 後僅保留 POST /generate（原 /generate-from-templates 改名）：
- 學年/學期+多 fee_types 範本驅動
- 月費自動展 6 月
- 冪等鍵：(student_id, source_template_id, target_month)
- 不再寫入 FeeItem（c2 已 DROP TABLE fee_items；c3 已 DROP COLUMN fee_item_id）。
"""

import logging
from datetime import date, datetime, timedelta
from utils.taipei_time import now_taipei_naive

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from models.base import session_scope
from models.classroom import (
    Classroom,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    Student,
)
from models.fees import FeeTemplate, StudentFeeRecord
from utils.auth import require_staff_permission
from utils.finance_guards import require_finance_approve
from utils.permissions import Permission

from ._helpers import (
    FEE_PAYMENT_APPROVAL_THRESHOLD,
    GenerateFromTemplatesRequest,
    _invalidate_finance_summary_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _semester_months(school_year: int, semester: int) -> list[str]:
    """民國年+學期 → YYYY-MM list。

    上學期 (semester=1): 8-12 月本年 + 1 月隔年
    下學期 (semester=2): 2-7 月本年
    """
    western = school_year + 1911
    if semester == 1:
        return [f"{western}-{m:02d}" for m in range(8, 13)] + [f"{western + 1}-01"]
    return [f"{western + 1}-{m:02d}" for m in range(2, 8)]


@router.post("/generate")
def generate_from_templates(
    payload: GenerateFromTemplatesRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """依該學年/學期所有啟用範本，為符合條件的在學學生產生 FeeRecord。

    - 範圍：fee_templates 表 is_active=True 且 (school_year, semester, fee_type) 命中。
    - 學生過濾：Classroom.school_year/semester 命中 + Student.lifecycle 為
      active/enrolled + Student.is_active。on_leave/withdrawn/transferred/graduated 跳過。
    - 月費展開：上學期 8-1 月、下學期 2-7 月 共 6 張單據。
    - 冪等：已存在 (student_id, source_template_id, target_month) 跳過。
    - dry_run：回傳 created/skipped 估算但不寫入 DB。
    """
    with session_scope() as session:
        # 1) 載入符合條件的範本
        templates = (
            session.query(FeeTemplate)
            .filter(
                FeeTemplate.school_year == payload.school_year,
                FeeTemplate.semester == payload.semester,
                FeeTemplate.fee_type.in_(payload.fee_types),
                FeeTemplate.is_active == True,
            )
            .all()
        )
        template_by_grade_type = {(t.grade_id, t.fee_type): t for t in templates}

        # 2) 載入該學期班級 + 在學學生
        rows = (
            session.query(Student, Classroom)
            .join(Classroom, Student.classroom_id == Classroom.id)
            .filter(
                Classroom.school_year == payload.school_year,
                Classroom.semester == payload.semester,
                Classroom.is_active == True,
                Student.is_active == True,
                Student.lifecycle_status.in_([LIFECYCLE_ACTIVE, LIFECYCLE_ENROLLED]),
            )
            .all()
        )

        # 3) 預載既存 (student_id, source_template_id, target_month) 冪等鍵
        existing_keys: set = set()
        if templates:
            existing = (
                session.query(
                    StudentFeeRecord.student_id,
                    StudentFeeRecord.source_template_id,
                    StudentFeeRecord.target_month,
                )
                .filter(
                    StudentFeeRecord.source_template_id.in_([t.id for t in templates])
                )
                .all()
            )
            existing_keys = {(s, tid, m) for s, tid, m in existing}

        period_str = f"{payload.school_year}-{payload.semester}"
        now = now_taipei_naive()
        new_records: list = []
        created = 0
        skipped = 0
        preview: list = []

        for student, classroom in rows:
            if not classroom.grade_id:
                continue
            for ft in payload.fee_types:
                tpl = template_by_grade_type.get((classroom.grade_id, ft))
                if not tpl:
                    continue

                months = (
                    _semester_months(payload.school_year, payload.semester)
                    if ft == "monthly"
                    else [None]
                )
                for tm in months:
                    key = (student.id, tpl.id, tm)
                    if key in existing_keys:
                        skipped += 1
                        continue
                    record_name = f"{tpl.name}{f' ({tm})' if tm else ''}"
                    due_date_val = date.today() + timedelta(  # noqa: DTZ011
                        days=tpl.due_date_offset_days
                    )
                    new_records.append(
                        {
                            "student_id": student.id,
                            "student_name": student.name,
                            "classroom_name": classroom.name,
                            "fee_item_name": record_name,
                            "amount_due": tpl.amount,
                            "amount_paid": 0,
                            "status": "unpaid",
                            "period": period_str,
                            "due_date": due_date_val,
                            "fee_type": tpl.fee_type,
                            "source_template_id": tpl.id,
                            "target_month": tm,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    created += 1
                    if len(preview) < 50:
                        preview.append(
                            {
                                "student_id": student.id,
                                "student_name": student.name,
                                "classroom_name": classroom.name,
                                "fee_item_name": record_name,
                                "amount_due": tpl.amount,
                                "target_month": tm,
                            }
                        )
                    existing_keys.add(key)

        # 不論 dry_run 都先算 Σ amount_due,讓事後稽核能對照預估與實寫金額。
        total_amount_due = sum(int(r["amount_due"] or 0) for r in new_records)

        # 先寫 audit context（含 total_amount_due），讓守衛擋下時 AuditMiddleware
        # 仍能記錄「誰想批次寫入多少錢但被擋」。
        request.state.audit_entity_id = f"{payload.school_year}-{payload.semester}"
        request.state.audit_summary = (
            f"批次產生費用({','.join(payload.fee_types)}): "
            f"created={created} skipped={skipped} "
            f"total=NT${total_amount_due:,} dry_run={payload.dry_run}"
        )
        request.state.audit_changes = {
            "action": "fee_generate_from_templates",
            "school_year": payload.school_year,
            "semester": payload.semester,
            "fee_types": payload.fee_types,
            "dry_run": payload.dry_run,
            "created": created,
            "skipped": skipped,
            "total_amount_due": total_amount_due,
        }

        # 批量寫入前的金流守衛：Σ amount_due 大於門檻需 ACTIVITY_PAYMENT_APPROVE。
        # Why: 單筆收款門檻只在收款時觸發；範本 amount × 全班學生 × 月份
        # 可一次寫入數百萬，但每筆 amount_due < 50K 永遠不會觸發單筆守衛。
        # dry_run 不寫入故不檢查，給操作者預估數字的機會。
        if not payload.dry_run and new_records:
            require_finance_approve(
                total_amount_due,
                current_user,
                threshold=FEE_PAYMENT_APPROVAL_THRESHOLD,
                action_label=f"批次產生費用記錄（{len(new_records)} 筆合計）",
            )
            try:
                session.bulk_insert_mappings(StudentFeeRecord, new_records)
                session.flush()
            except IntegrityError:
                # 並發雙寫：DB partial unique index 攔截。
                # 月費由 ix_fee_records_monthly_unique 守，非月費由 uq_fee_records_non_monthly_unique 守。
                session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "並發產生衝突：偵測到相同 (學生, 範本, 學期/月份) 記錄已被建立，"
                        "請重新整理頁面後再試"
                    ),
                )

        if not payload.dry_run:
            _invalidate_finance_summary_cache()

        return {
            "created": created,
            "skipped": skipped,
            "dry_run": payload.dry_run,
            "preview": preview,
        }
