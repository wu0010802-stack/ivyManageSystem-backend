"""api/fees/generation.py — 批次產生費用記錄

兩條路徑共存（c2 將砍掉舊 POST /generate、把 /generate-from-templates 改名為 /generate）：
- POST /generate-from-templates：學年/學期+多 fee_types 範本驅動，月費自動展 6 月
- POST /generate（舊路徑）：以單一 FeeItem 為錨點，全校或指定班級批次建立
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from models.base import session_scope
from models.classroom import (
    Classroom,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    Student,
)
from models.fees import FeeItem, FeeTemplate, StudentFeeRecord
from utils.auth import require_staff_permission
from utils.permissions import Permission

from ._helpers import (
    GenerateFromTemplatesRequest,
    GenerateRequest,
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


def _ensure_fee_items_for_templates(
    session, keys: list, template_by_id: dict, period: str
) -> dict:
    """為每個 (template, target_month) 確保有對應的 placeholder FeeItem。

    Why: 既有 StudentFeeRecord.fee_item_id 是 NOT NULL,且 (student_id, fee_item_id)
    有 unique 約束。新流程以 source_template_id+target_month 驅動,但需相容舊欄位。
    每個 (template, month) 配一個 FeeItem 作為錨點,避免月費 6 筆共用同 fee_item
    撞 uq_student_fee_item。
    """
    out: dict = {}
    for tpl_id, tm in keys:
        tpl = template_by_id[tpl_id]
        fi_name = f"{tpl.name} ({tm})" if tm else tpl.name
        existing = (
            session.query(FeeItem)
            .filter(FeeItem.name == fi_name, FeeItem.period == period)
            .first()
        )
        if existing:
            out[(tpl_id, tm)] = existing.id
            continue
        fi = FeeItem(
            name=fi_name,
            amount=tpl.amount,
            classroom_id=None,
            period=period,
            is_active=True,
        )
        session.add(fi)
        session.flush()
        out[(tpl_id, tm)] = fi.id
    return out


@router.post("/generate-from-templates")
def generate_from_templates(
    payload: GenerateFromTemplatesRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """依該學年/學期所有啟用範本,為符合條件的在學學生產生 FeeRecord。

    - 範圍:fee_templates 表 is_active=True 且 (school_year, semester, fee_type) 命中。
    - 學生過濾:Classroom.school_year/semester 命中 + Student.lifecycle 為
      active/enrolled + Student.is_active。on_leave/withdrawn/transferred/graduated 跳過。
    - 月費展開:上學期 8-1 月、下學期 2-7 月 共 6 張單據。
    - 冪等:已存在 (student_id, source_template_id, target_month) 跳過。
    - dry_run:回傳 created/skipped 估算但不寫入 DB。
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
        template_by_id = {t.id: t for t in templates}

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
        now = datetime.now()
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
                    due_date_val = date.today() + timedelta(
                        days=tpl.due_date_offset_days
                    )
                    new_records.append(
                        {
                            "student_id": student.id,
                            "student_name": student.name,
                            "classroom_name": classroom.name,
                            # fee_item_id 留 None,稍後 (僅非 dry_run) 依
                            # (source_template_id, target_month) 填入
                            "fee_item_id": None,
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

        if not payload.dry_run and new_records:
            # 依 (template, target_month) 唯一鍵預先 ensure FeeItem
            unique_keys = sorted(
                {(r["source_template_id"], r["target_month"]) for r in new_records},
                key=lambda x: (x[0], x[1] or ""),
            )
            fee_item_map = _ensure_fee_items_for_templates(
                session, unique_keys, template_by_id, period_str
            )
            for r in new_records:
                r["fee_item_id"] = fee_item_map[
                    (r["source_template_id"], r["target_month"])
                ]
            session.bulk_insert_mappings(StudentFeeRecord, new_records)

        request.state.audit_entity_id = f"{payload.school_year}-{payload.semester}"
        request.state.audit_summary = (
            f"批次產生費用({','.join(payload.fee_types)}): "
            f"created={created} skipped={skipped} dry_run={payload.dry_run}"
        )
        request.state.audit_changes = {
            "action": "fee_generate_from_templates",
            "school_year": payload.school_year,
            "semester": payload.semester,
            "fee_types": payload.fee_types,
            "dry_run": payload.dry_run,
            "created": created,
            "skipped": skipped,
        }

        if not payload.dry_run:
            _invalidate_finance_summary_cache()

        return {
            "created": created,
            "skipped": skipped,
            "dry_run": payload.dry_run,
            "preview": preview,
        }


@router.post("/generate")
def generate_fee_records(
    payload: GenerateRequest,
    request: Request,
    _: None = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """批次為指定班級或全校的在校學生產生費用記錄"""
    with session_scope() as session:
        fee_item = (
            session.query(FeeItem).filter(FeeItem.id == payload.fee_item_id).first()
        )
        if not fee_item:
            raise HTTPException(status_code=404, detail="費用項目不存在")
        if not fee_item.is_active:
            raise HTTPException(status_code=400, detail="費用項目已停用，無法產生記錄")

        # 查詢在校學生（LEFT JOIN classroom 取班級名稱）
        q = (
            session.query(Student, Classroom)
            .outerjoin(Classroom, Student.classroom_id == Classroom.id)
            .filter(Student.is_active == True)
        )
        if payload.classroom_id:
            q = q.filter(Student.classroom_id == payload.classroom_id)
        elif fee_item.classroom_id:
            q = q.filter(Student.classroom_id == fee_item.classroom_id)

        students = q.all()

        # 一次查完已存在的 student_id，避免 N 次單筆查詢
        existing_student_ids = {
            r.student_id
            for r in session.query(StudentFeeRecord.student_id)
            .filter(StudentFeeRecord.fee_item_id == payload.fee_item_id)
            .all()
        }

        now = datetime.now()
        created = 0
        skipped = 0
        new_records = []
        for student, classroom in students:
            if student.id in existing_student_ids:
                skipped += 1
                continue

            new_records.append(
                {
                    "student_id": student.id,
                    "student_name": student.name,
                    "classroom_name": classroom.name if classroom else "",
                    "fee_item_id": fee_item.id,
                    "fee_item_name": fee_item.name,
                    "amount_due": fee_item.amount,
                    "amount_paid": 0,
                    "status": "unpaid",
                    "period": fee_item.period,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            created += 1

        if new_records:
            session.bulk_insert_mappings(StudentFeeRecord, new_records)

        # 結構化 diff：批次操作通常一次掃整班，必須留下「誰、何時、產了多少筆」軌跡
        # 學生 id 列表只取前 50 個避免 changes 撐爆 64KB 上限（middleware 會 truncate）
        sampled_student_ids = [
            s.id for s, _c in students if s.id not in existing_student_ids
        ][:50]
        request.state.audit_entity_id = str(payload.fee_item_id)
        request.state.audit_summary = (
            f"批次產生費用記錄：{fee_item.name}（{fee_item.period}）"
            f" 新建 {created} 筆、跳過 {skipped} 筆"
        )
        request.state.audit_changes = {
            "action": "fee_generate_records",
            "fee_item_id": payload.fee_item_id,
            "fee_item_name": fee_item.name,
            "amount_due": fee_item.amount,
            "period": fee_item.period,
            "scope_classroom_id": payload.classroom_id or fee_item.classroom_id,
            "candidate_count": len(students),
            "created": created,
            "skipped": skipped,
            "sampled_student_ids": sampled_student_ids,
            "sampled_student_ids_truncated": created > len(sampled_student_ids),
        }

    logger.info(
        "批次產生費用記錄 fee_item_id=%s 新建=%s 跳過=%s",
        payload.fee_item_id,
        created,
        skipped,
    )
    return {"created": created, "skipped": skipped}
