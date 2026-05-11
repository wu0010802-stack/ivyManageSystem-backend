"""
api/salary/festival.py — 節慶獎金預覽

含 2 個 endpoint：
- GET /salaries/festival-bonus                    當月各員工節慶獎金預覽
- GET /salaries/festival-bonus/period-accrual     發放期累積至今明細

跨組 helper `_active_employees_in_month_filter` 仍在 api.salary.__init__
（calculate 也用），endpoint 內以 lazy import 取最新 reference；
下一刀（calculate）會把它與其他跨組 helper 統一搬到 _shared 模組。

_salary_engine 採 endpoint 內 lazy import 取 service injection 後的最新值。
"""

import calendar as _cal
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models.base import session_scope
from models.database import Classroom, Employee, MeetingRecord
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.student_enrollment import (
    classroom_student_count_map,
    count_students_active_on,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.salary_access import (
    resolve_salary_viewer_employee_id as _resolve_salary_viewer_employee_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/salaries/festival-bonus")
def get_festival_bonus(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """
    Return breakdown of festival bonus calculation
    """
    # Lazy import：_salary_engine 與 _active_employees_in_month_filter 仍在
    # __init__；於 endpoint 內取，避免 import-time 抓到 None / 跨組複製。
    from . import _active_employees_in_month_filter, _salary_engine

    # F-013：跨員工彙總端點，僅 admin/hr 可使用；其他持 SALARY_READ 的角色一律 403。
    # 對齊 _enforce_self_or_full_salary 的精神：非全員視野者不可看到全體節慶獎金明細。
    if _resolve_salary_viewer_employee_id(current_user) is not None:
        raise HTTPException(status_code=403, detail="僅可查詢本人薪資")
    with session_scope() as session:
        # 使用啟動時已載入設定的 singleton，避免每次請求重跑 4 次 DB 查詢
        engine = (
            _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
        )

        _, _fb_last = _cal.monthrange(year, month)
        month_end = date(year, month, _fb_last)

        # 批次預先查詢共用資料，避免 N+1
        school_active = count_students_active_on(session, month_end)
        cls_count_map = classroom_student_count_map(session, month_end)
        classroom_map = {
            c.id: c
            for c in session.query(Classroom).options(joinedload(Classroom.grade)).all()
        }

        # 當月實際在職的員工（與薪資計算同一條件，避免 festival-bonus 預覽與實發分叉）
        employees = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(_active_employees_in_month_filter(year, month))
            .all()
        )

        results = []
        for emp in employees:
            ctx = {
                "session": session,
                "employee": emp,
                "classroom": (
                    classroom_map.get(emp.classroom_id) if emp.classroom_id else None
                ),
                "school_active_students": school_active,
                "classroom_count_map": cls_count_map,
            }
            bonus_data = engine.calculate_festival_bonus_breakdown(
                emp.id, year, month, _ctx=ctx
            )
            results.append(bonus_data)

        return results


@router.get("/salaries/festival-bonus/period-accrual")
def get_festival_bonus_period_accrual(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """
    回傳該月所屬發放期「到目前為止」的節慶獎金、超額獎金、會議缺席扣款累積明細。
    發放月（2/6/9/12）回空 rows + is_distribution_month=True。
    DB 往返 O(月數)：每月一組共用資料 batch prefetch，與單月 get_festival_bonus 相同策略。
    """
    from . import _active_employees_in_month_filter, _salary_engine

    # F-013：跨員工彙總端點，僅 admin/hr 可使用；其他角色 403。
    if _resolve_salary_viewer_employee_id(current_user) is not None:
        raise HTTPException(status_code=403, detail="僅可查詢本人薪資")

    from services.salary.utils import (
        get_bonus_distribution_month,
        get_current_period_passed_months,
    )

    passed_months = get_current_period_passed_months(year, month)
    if not passed_months:
        return {
            "is_distribution_month": get_bonus_distribution_month(month),
            "period_start_year": None,
            "period_start_month": None,
            "current_year": year,
            "current_month": month,
            "distribution_year": None,
            "distribution_month": None,
            "rows": [],
        }

    # 發放月：本期之後最近的 2/6/9/12（12 月 → 次年 2）
    distribution_month = next((c for c in (2, 6, 9, 12) if c > month), 2)
    # distribution_year：查詢月 12 已於上方 is_distribution_month 分支提早返回，
    # 主路徑下 distribution_month 必定 > month 且落於同一年；保留變數以對前端語意清晰。
    distribution_year = year + 1 if month == 12 else year

    with session_scope() as session:
        engine = (
            _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
        )

        # 員工過濾：與 get_festival_bonus 一致，只包含當前查詢月在職的員工。
        # 商業語意：節慶獎金以發放月當日在職為條件，期中離職者即使已累積部分獎金
        # 亦不會於發放月領取，預覽功能維持此規則，避免管理者對實發金額產生誤判。
        employees = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(_active_employees_in_month_filter(year, month))
            .all()
        )

        # 跨月共用：副班導 / 美師 → 班級清單映射，shared_classes 反查 O(1)
        # （audit B.P0.2，避免 _build_classroom_context_from_db 在每員工各跑一次 query）。
        all_active_classrooms = (
            session.query(Classroom)
            .options(joinedload(Classroom.grade))
            .filter(Classroom.is_active == True)  # noqa: E712
            .all()
        )
        classroom_map_shared = {c.id: c for c in all_active_classrooms}
        assistant_to_classes_map: dict[int, list] = {}
        art_to_classes_map: dict[int, list] = {}
        for _c in all_active_classrooms:
            if _c.assistant_teacher_id:
                assistant_to_classes_map.setdefault(_c.assistant_teacher_id, []).append(
                    _c
                )
            if _c.art_teacher_id:
                art_to_classes_map.setdefault(_c.art_teacher_id, []).append(_c)

        monthly_ctx_cache: dict[tuple[int, int], dict] = {}
        for y, m in passed_months:
            _, last_day = _cal.monthrange(y, m)
            month_end = date(y, m, last_day)
            month_start = date(y, m, 1)
            # 預載當月會議缺席數（依 employee_id 分組），取代 calculate_period_accrual_row
            # 在迴圈內每員工一次的 MeetingRecord.count() 查詢（B.P0.1）。
            meeting_absent_rows = (
                session.query(
                    MeetingRecord.employee_id,
                    func.count(MeetingRecord.id),
                )
                .filter(
                    MeetingRecord.meeting_date >= month_start,
                    MeetingRecord.meeting_date <= month_end,
                    MeetingRecord.attended == False,  # noqa: E712
                )
                .group_by(MeetingRecord.employee_id)
                .all()
            )
            meeting_absent_count_map = {
                int(emp_id): int(cnt or 0) for emp_id, cnt in meeting_absent_rows
            }
            monthly_ctx_cache[(y, m)] = {
                "month_end": month_end,
                "school_active": count_students_active_on(session, month_end),
                "cls_count_map": classroom_student_count_map(session, month_end),
                "classroom_map": classroom_map_shared,
                "meeting_absent_count_map": meeting_absent_count_map,
            }

        rows = []
        for emp in employees:
            monthly = []
            for y, m in passed_months:
                ctx_cache = monthly_ctx_cache[(y, m)]
                per_month_ctx = {
                    "session": session,
                    "employee": emp,
                    "classroom": (
                        ctx_cache["classroom_map"].get(emp.classroom_id)
                        if emp.classroom_id
                        else None
                    ),
                    "school_active_students": ctx_cache["school_active"],
                    "classroom_count_map": ctx_cache["cls_count_map"],
                    "meeting_absent_count_map": ctx_cache.get(
                        "meeting_absent_count_map", {}
                    ),
                    "assistant_to_classes_map": assistant_to_classes_map,
                    "art_to_classes_map": art_to_classes_map,
                }
                try:
                    row = engine.calculate_period_accrual_row(
                        emp.id, y, m, _ctx=per_month_ctx
                    )
                except Exception:
                    logger.exception(
                        "period-accrual 計算失敗 emp=%s year=%s month=%s",
                        emp.id,
                        y,
                        m,
                    )
                    row = {
                        "festival_bonus": 0,
                        "overtime_bonus": 0,
                        "meeting_absence_deduction": 0,
                        "category": "",
                        "error": "計算失敗",
                    }
                monthly.append({"year": y, "month": m, **row})

            fb_total = sum(r["festival_bonus"] for r in monthly)
            ot_total = sum(r["overtime_bonus"] for r in monthly)
            ded_total = sum(r["meeting_absence_deduction"] for r in monthly)
            category = next(
                (r.get("category") for r in monthly if r.get("category")), ""
            )

            rows.append(
                {
                    "employee_id": emp.id,
                    "name": emp.name,
                    "category": category,
                    "monthly": monthly,
                    "totals": {
                        "festival_bonus": fb_total,
                        "overtime_bonus": ot_total,
                        "meeting_absence_deduction": ded_total,
                        "net_estimate": max(0, fb_total + ot_total - ded_total),
                    },
                }
            )

        return {
            "is_distribution_month": False,
            "period_start_year": passed_months[0][0],
            "period_start_month": passed_months[0][1],
            "current_year": year,
            "current_month": month,
            "distribution_year": distribution_year,
            "distribution_month": distribution_month,
            "rows": rows,
        }
