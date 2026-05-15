"""半年考核 Excel I/O（import + export）。

依照 `114上考核表` Excel 結構：
- 每行一人，16 項加減分為欄
- import：upsert score_items（unique on participant_id + item_code）
- export：cycle 總表 / 個人考核表 / 轉帳名冊
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    AppraisalSummary,
    Grade,
    RoleGroup,
)
from models.employee import Employee

logger = logging.getLogger(__name__)

# Excel 欄名 → catalog item_code
EXCEL_COL_TO_ITEM_CODE: dict[str, str] = {
    "請休假": "LEAVE",
    "遲到早退": "LATE_EARLY",
    "未打卡": "NO_CLOCK",
    "園務會議未參加": "MISS_PRESCHOOL_MEETING",
    "9/13機構會議": "ORG_MEETING_0913",
    "11/15機構會議": "ORG_MEETING_1115",
    "11/15自強活動": "TEAM_ACTIVITY_1115",
    "9/15休學人數": "DROPOUT_0915",
    "3/15休學人數": "DROPOUT_0315",
    "幼兒意外": "CHILD_INCIDENT",
    "3/15舊生註冊率": "RETURNING_RATE_0315",
    "帶班人數": "CLASS_SIZE",
    "才藝班參加率": "AFTER_CLASS_RATE",
    "特教生": "SPED",
    "獎懲": "REWARD_PUNISH",
    "其他調整": "OTHER_ADJUST",
}


def _role_label(rg: RoleGroup) -> str:
    return {
        RoleGroup.SUPERVISOR: "主管",
        RoleGroup.HEAD_TEACHER: "班導/會計",
        RoleGroup.ASSISTANT: "副班導",
        RoleGroup.STAFF: "辦公室",
        RoleGroup.COOK: "廚工",
    }.get(rg, rg.value)


def _grade_label(g: Grade) -> str:
    return {
        Grade.OUTSTANDING: "優",
        Grade.GOOD: "甲",
        Grade.PASS: "乙",
        Grade.WARN: "丙",
        Grade.FAIL: "丁",
    }[g]


def _decimal_or_none(raw) -> Optional[Decimal]:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


# ── Import ──────────────────────────────────────────────────────────────────


def import_cycle_excel(
    db: Session,
    cycle: AppraisalCycle,
    file_bytes: bytes,
    *,
    actor_user_id: Optional[int] = None,
) -> dict:
    """匯入半年考核 Excel：每行一個 participant，補上對應 score_items。

    冪等：(cycle_id, employee_id, item_code) UNIQUE 走 upsert。
    回傳 stats dict（created_participants / created_items / updated_items / skipped）。
    """
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    ws = wb.active

    header_row = [c.value for c in ws[1]]
    # 欄位定位
    col_idx: dict[str, int] = {}
    for idx, name in enumerate(header_row):
        if not isinstance(name, str):
            continue
        key = name.strip()
        if key in EXCEL_COL_TO_ITEM_CODE or key in ("姓名", "員工編號", "role_group"):
            col_idx[key] = idx

    if "姓名" not in col_idx and "員工編號" not in col_idx:
        raise ValueError("excel_missing_name_or_employee_id_column")

    # 先 load catalog 確認 item_code 都存在
    catalog_codes = {
        c
        for (c,) in db.execute(
            select(AppraisalScoreItemCatalog.code).where(
                AppraisalScoreItemCatalog.is_active == True  # noqa: E712
            )
        ).all()
    }

    stats = {
        "created_participants": 0,
        "created_items": 0,
        "updated_items": 0,
        "skipped_rows": 0,
    }

    for row in ws.iter_rows(min_row=2, values_only=True):
        # 解析員工
        emp_id_val = row[col_idx["員工編號"]] if "員工編號" in col_idx else None
        emp_name = row[col_idx["姓名"]] if "姓名" in col_idx else None

        emp: Optional[Employee] = None
        if emp_id_val:
            emp = db.execute(
                select(Employee).where(Employee.employee_id == str(emp_id_val))
            ).scalar_one_or_none()
        if emp is None and emp_name:
            emp = db.execute(
                select(Employee).where(Employee.name == str(emp_name))
            ).scalar_one_or_none()
        if emp is None:
            stats["skipped_rows"] += 1
            continue

        # 取/建 participant
        participant = db.execute(
            select(AppraisalParticipant).where(
                AppraisalParticipant.cycle_id == cycle.id,
                AppraisalParticipant.employee_id == emp.id,
            )
        ).scalar_one_or_none()
        if participant is None:
            participant = AppraisalParticipant(
                cycle_id=cycle.id,
                employee_id=emp.id,
                role_group=RoleGroup.ASSISTANT,
            )
            db.add(participant)
            db.flush()
            stats["created_participants"] += 1

        # 處理每個 score item 欄
        for excel_col, item_code in EXCEL_COL_TO_ITEM_CODE.items():
            if excel_col not in col_idx:
                continue
            if item_code not in catalog_codes:
                continue
            delta = _decimal_or_none(row[col_idx[excel_col]])
            if delta is None:
                continue

            existing = db.execute(
                select(AppraisalScoreItem).where(
                    AppraisalScoreItem.participant_id == participant.id,
                    AppraisalScoreItem.item_code == item_code,
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    AppraisalScoreItem(
                        participant_id=participant.id,
                        item_code=item_code,
                        score_delta=delta,
                        created_by=actor_user_id,
                    )
                )
                stats["created_items"] += 1
            else:
                existing.score_delta = delta
                stats["updated_items"] += 1

    db.flush()
    return stats


# ── Export ──────────────────────────────────────────────────────────────────


def build_cycle_report(db: Session, cycle: AppraisalCycle) -> bytes:
    """全校學期總表 Excel：每位 participant 一列。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "考核總表"

    title_text = (
        f"考核總表 {cycle.academic_year} 學年度 "
        f"{'第一' if cycle.semester.value == 'FIRST' else '第二'}學期"
    )
    ws.append([title_text])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)

    headers = [
        "員工編號",
        "員工",
        "Role Group",
        "基本分",
        "項目加減分",
        "總分",
        "等級",
        "獎金",
        "簽核狀態",
    ]
    ws.append(headers)
    for cell in ws[2]:
        cell.font = Font(bold=True)

    rows = db.execute(
        select(AppraisalSummary, AppraisalParticipant, Employee)
        .join(
            AppraisalParticipant,
            AppraisalParticipant.id == AppraisalSummary.participant_id,
        )
        .join(Employee, Employee.id == AppraisalParticipant.employee_id)
        .where(AppraisalSummary.cycle_id == cycle.id)
        .order_by(AppraisalParticipant.role_group, Employee.id)
    ).all()

    for s, p, emp in rows:
        ws.append(
            [
                emp.employee_id or "",
                emp.name or "",
                _role_label(p.role_group),
                float(s.base_score),
                float(s.item_score_sum),
                float(s.total_score),
                _grade_label(s.grade),
                float(s.bonus_amount),
                s.status.value,
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_transfer_roster(db: Session, cycle: AppraisalCycle) -> bytes:
    """轉帳名冊：只列 bonus_amount > 0 的 finalized summary。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "考核獎金轉帳"

    ws.append([f"{cycle.academic_year} 學年度考核獎金轉帳名冊"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)

    ws.append(["員工編號", "員工", "銀行代碼", "銀行帳號", "戶名", "金額", "備註"])
    for cell in ws[2]:
        cell.font = Font(bold=True)

    rows = db.execute(
        select(AppraisalSummary, AppraisalParticipant, Employee)
        .join(
            AppraisalParticipant,
            AppraisalParticipant.id == AppraisalSummary.participant_id,
        )
        .join(Employee, Employee.id == AppraisalParticipant.employee_id)
        .where(
            AppraisalSummary.cycle_id == cycle.id,
            AppraisalSummary.bonus_amount > 0,
        )
        .order_by(Employee.id)
    ).all()

    for s, p, emp in rows:
        ws.append(
            [
                emp.employee_id or "",
                emp.name or "",
                getattr(emp, "bank_code", "") or "",
                getattr(emp, "bank_account", "") or "",
                getattr(emp, "bank_account_name", "") or emp.name or "",
                float(s.bonus_amount),
                s.leave_note or "",
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_participant_sheet(db: Session, participant: AppraisalParticipant) -> bytes:
    """個人考核表（對齊 Excel 原始版面）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "個人考核表"

    emp = db.get(Employee, participant.employee_id)
    cycle = db.get(AppraisalCycle, participant.cycle_id)
    summary = db.execute(
        select(AppraisalSummary).where(
            AppraisalSummary.participant_id == participant.id
        )
    ).scalar_one_or_none()

    ws["A1"] = "教職員考核表"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"姓名：{emp.name or ''}"
    ws["B2"] = f"職稱：{emp.title_name if hasattr(emp, 'title_name') else (emp.title or '')}"
    ws["A3"] = (
        f"學年期：{cycle.academic_year} 學年度 "
        f"{'上' if cycle.semester.value == 'FIRST' else '下'}學期"
    )
    ws["A4"] = f"考核期間：{cycle.start_date} ~ {cycle.end_date}"

    ws["A6"] = "基本分數"
    ws["B6"] = (
        float(participant.base_score) if summary is None else float(summary.base_score)
    )
    ws["A7"] = "項目加減分合計"
    ws["B7"] = 0 if summary is None else float(summary.item_score_sum)
    ws["A8"] = "總分"
    ws["B8"] = 0 if summary is None else float(summary.total_score)
    ws["A9"] = "等級"
    ws["B9"] = "未結算" if summary is None else _grade_label(summary.grade)
    ws["A10"] = "考核獎金（試算）"
    ws["B10"] = 0 if summary is None else float(summary.bonus_amount)

    ws["A12"] = "項目明細"
    ws["A12"].font = Font(bold=True)
    ws.append([])
    ws.append(["項目代碼", "扣加分", "原始值", "備註"])
    items = (
        db.execute(
            select(AppraisalScoreItem)
            .where(AppraisalScoreItem.participant_id == participant.id)
            .order_by(AppraisalScoreItem.item_code)
        )
        .scalars()
        .all()
    )
    for it in items:
        ws.append(
            [
                it.item_code,
                float(it.score_delta),
                float(it.raw_value) if it.raw_value is not None else "",
                it.note or "",
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
