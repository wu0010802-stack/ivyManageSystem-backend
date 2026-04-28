"""
Attendance - upload endpoints (Excel and CSV)
"""

import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from cachetools import TTLCache
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from models.database import (
    get_session,
    Employee,
    Attendance,
    Classroom,
    ShiftAssignment,
    ShiftType,
    DailyShift,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from utils.errors import raise_safe_500
from utils.storage import get_storage_path
from ._shared import AttendanceUploadRequest
from .records import _assert_upload_months_not_finalized

logger = logging.getLogger(__name__)

_UPLOAD_MODULE = "attendance_imports"


def _upload_dir() -> Path:
    return get_storage_path(_UPLOAD_MODULE)


_EXCEL_EXT_RE = re.compile(r"^\.[a-z0-9]+$")

# ShiftType 很少異動，快取 5 分鐘，避免每次上傳重複查詢
_shift_type_cache: TTLCache = TTLCache(maxsize=1, ttl=300)


def _get_shift_type_id_map(session) -> dict:
    """回傳 {id: ShiftType ORM} 快取 5 分鐘。"""
    cached = _shift_type_cache.get("id_map")
    if cached is not None:
        return cached
    result = {st.id: st for st in session.query(ShiftType).all()}
    _shift_type_cache["id_map"] = result
    return result


def _mark_attendance_upload_stale(session, affected_months: set) -> None:
    """考勤匯入後將實際被異動的 (emp_id, year, month) 批次標 needs_recalc=True。

    Why: 考勤改動會影響遲到/早退/缺打卡/曠職基準等扣款來源,薪資若已算過必須
    標 stale 避免後續 finalize 把舊薪資封存。
    """
    if not affected_months:
        return
    from services.salary.utils import mark_salary_stale

    for emp_id, yr, mo in affected_months:
        try:
            mark_salary_stale(session, emp_id, yr, mo)
        except Exception:
            logger.warning(
                "考勤匯入標記 SalaryRecord stale 失敗 emp=%d %d/%d",
                emp_id,
                yr,
                mo,
                exc_info=True,
            )


router = APIRouter()


@router.post("/upload")
async def upload_attendance(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """上傳打卡記錄 Excel（支持分開的上班/下班時間欄位）"""
    raw_ext = Path(file.filename or "").suffix.lower()
    if (
        not raw_ext
        or not _EXCEL_EXT_RE.match(raw_ext)
        or raw_ext not in {".xlsx", ".xls"}
    ):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔案")

    # 先讀取檔案內容並檢查大小，防止超大檔案耗盡磁碟空間或記憶體
    content = await read_upload_with_size_check(file)
    validate_file_signature(content, raw_ext)

    file_path = _upload_dir() / f"{uuid.uuid4().hex}{raw_ext}"

    with open(file_path, "wb") as f:
        f.write(content)

    try:
        df = pd.read_excel(file_path)
        columns = df.columns.tolist()

        # 新格式：部門, 編號, 姓名, 日期, 星期, 上班時間, 下班時間
        if "上班時間" in columns and "下班時間" in columns:
            session = get_session()
            try:
                employees = (
                    session.query(Employee).filter(Employee.is_active == True).all()
                )
                emp_by_id = {str(emp.employee_id): emp for emp in employees}
                emp_by_name = {emp.name: emp for emp in employees}

                all_classrooms = (
                    session.query(Classroom).filter(Classroom.is_active == True).all()
                )
                head_teacher_map = {
                    c.head_teacher_id for c in all_classrooms if c.head_teacher_id
                }
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

                shift_assignments = session.query(ShiftAssignment).all()
                shift_types = _get_shift_type_id_map(session)
                shift_schedule_map = {}
                for sa in shift_assignments:
                    st = shift_types.get(sa.shift_type_id)
                    if st:
                        shift_schedule_map[(sa.employee_id, sa.week_start_date)] = {
                            "work_start": st.work_start,
                            "work_end": st.work_end,
                            "name": st.name,
                        }

                daily_shift_map = {}

                if "日期" in df.columns:
                    temp_dates = pd.to_datetime(
                        df["日期"], errors="coerce"
                    ).dt.date.dropna()
                    if not temp_dates.empty:
                        min_date, max_date = temp_dates.min(), temp_dates.max()
                        daily_shifts_query = (
                            session.query(DailyShift)
                            .filter(
                                DailyShift.date >= min_date, DailyShift.date <= max_date
                            )
                            .all()
                        )

                        for ds in daily_shifts_query:
                            st = shift_types.get(ds.shift_type_id)
                            if st:
                                daily_shift_map[(ds.employee_id, ds.date)] = {
                                    "work_start": st.work_start,
                                    "work_end": st.work_end,
                                    "name": st.name,
                                }

                results_data = {
                    "total": len(df),
                    "success": 0,
                    "failed": 0,
                    "errors": [],
                    "summary": [],
                }

                # 預先批量收集 employee_ids 與 dates，避免迴圈內 N+1 查詢
                _pre_emp_ids: set = set()
                _pre_dates: set = set()
                for _, _row in df.iterrows():
                    _raw_id = str(_row.get("編號", "")).strip()
                    if _raw_id.endswith(".0"):
                        _raw_id = _raw_id[:-2]
                    _emp = emp_by_id.get(_raw_id) or emp_by_name.get(
                        str(_row.get("姓名", "")).strip()
                    )
                    if _emp:
                        _pre_emp_ids.add(_emp.id)
                    _dv = _row.get("日期")
                    if not pd.isna(_dv):
                        try:
                            _d = (
                                datetime.strptime(str(_dv).strip(), "%Y/%m/%d").date()
                                if isinstance(_dv, str)
                                else pd.to_datetime(_dv).date()
                            )
                            _pre_dates.add(_d)
                        except Exception:
                            pass

                _assert_upload_months_not_finalized(session, _pre_emp_ids, _pre_dates)

                # 累積實際被異動的 (emp_id, year, month),供 commit 後批次標 stale
                _affected_months: set = set()

                attendance_cache: dict = {}
                if _pre_emp_ids and _pre_dates:
                    _cached = (
                        session.query(Attendance)
                        .filter(
                            Attendance.employee_id.in_(_pre_emp_ids),
                            Attendance.attendance_date.in_(_pre_dates),
                        )
                        .all()
                    )
                    attendance_cache = {
                        (a.employee_id, a.attendance_date): a for a in _cached
                    }

                employee_stats = {}

                for idx, row in df.iterrows():
                    try:
                        raw_id = row.get("編號", "")
                        emp_number = str(raw_id).strip()
                        if emp_number.endswith(".0"):
                            emp_number = emp_number[:-2]

                        emp_name = str(row.get("姓名", "")).strip()
                        employee = emp_by_id.get(emp_number) or emp_by_name.get(
                            emp_name
                        )

                        if not employee:
                            results_data["failed"] += 1
                            results_data["errors"].append(
                                f"第 {idx+2} 行: 找不到員工 {emp_name}"
                            )
                            continue

                        date_val = row.get("日期")
                        if pd.isna(date_val):
                            results_data["failed"] += 1
                            results_data["errors"].append(f"第 {idx+2} 行: 日期為空")
                            continue

                        if isinstance(date_val, str):
                            try:
                                attendance_date = datetime.strptime(
                                    date_val, "%Y/%m/%d"
                                ).date()
                            except ValueError:
                                attendance_date = datetime.strptime(
                                    date_val, "%Y-%m-%d"
                                ).date()
                        else:
                            attendance_date = pd.to_datetime(date_val).date()

                        punch_in_time = None
                        punch_in_val = row.get("上班時間")
                        if not pd.isna(punch_in_val) and str(punch_in_val).strip():
                            try:
                                time_str = str(punch_in_val).strip()
                                if ":" in time_str:
                                    parts = time_str.split(":")
                                    hour = int(parts[0])
                                    minute = (
                                        int(parts[1].split(".")[0])
                                        if "." in parts[1]
                                        else int(parts[1])
                                    )
                                    punch_in_time = datetime.combine(
                                        attendance_date,
                                        datetime.strptime(
                                            f"{hour:02d}:{minute:02d}", "%H:%M"
                                        ).time(),
                                    )
                            except (ValueError, IndexError) as e:
                                logger.warning(
                                    "第 %d 行: 上班時間格式無法解析 '%s': %s",
                                    idx + 2,
                                    punch_in_val,
                                    e,
                                )

                        punch_out_time = None
                        punch_out_val = row.get("下班時間")
                        if not pd.isna(punch_out_val) and str(punch_out_val).strip():
                            try:
                                time_str = str(punch_out_val).strip()
                                if ":" in time_str:
                                    parts = time_str.split(":")
                                    hour = int(parts[0])
                                    minute = (
                                        int(parts[1].split(".")[0])
                                        if "." in parts[1]
                                        else int(parts[1])
                                    )
                                    punch_out_time = datetime.combine(
                                        attendance_date,
                                        datetime.strptime(
                                            f"{hour:02d}:{minute:02d}", "%H:%M"
                                        ).time(),
                                    )
                            except (ValueError, IndexError) as e:
                                logger.warning(
                                    "第 %d 行: 下班時間格式無法解析 '%s': %s",
                                    idx + 2,
                                    punch_out_val,
                                    e,
                                )

                        # 跨夜班修正：下班時間早於上班時間表示隔日下班（如 18:00→02:00）
                        if (
                            punch_in_time
                            and punch_out_time
                            and punch_out_time < punch_in_time
                        ):
                            punch_out_time += timedelta(days=1)
                        elif (
                            punch_in_time
                            and punch_out_time
                            and punch_out_time == punch_in_time
                        ):
                            results_data["failed"] += 1
                            results_data["errors"].append(
                                f"第 {idx+2} 行 ({emp_name} {attendance_date}): 上下班時間相同 {punch_in_val}，請確認資料"
                            )
                            continue

                        work_start = datetime.strptime(
                            employee.work_start_time or "08:00", "%H:%M"
                        ).time()
                        work_end = datetime.strptime(
                            employee.work_end_time or "17:00", "%H:%M"
                        ).time()

                        is_late = False
                        is_early_leave = False
                        is_missing_punch_in = punch_in_time is None
                        is_missing_punch_out = punch_out_time is None
                        late_minutes = 0
                        early_leave_minutes = 0
                        status = "normal"

                        if punch_in_time:
                            work_start_dt = datetime.combine(
                                attendance_date, work_start
                            )
                            if punch_in_time > work_start_dt:
                                is_late = True
                                late_minutes = int(
                                    (punch_in_time - work_start_dt).total_seconds() / 60
                                )
                                status = "late"

                        if punch_out_time:
                            work_end_dt = datetime.combine(attendance_date, work_end)
                            if punch_out_time < work_end_dt:
                                is_early_leave = True
                                early_leave_minutes = int(
                                    (work_end_dt - punch_out_time).total_seconds() / 60
                                )
                                status = (
                                    "early_leave"
                                    if status == "normal"
                                    else status + "+early_leave"
                                )

                        if is_missing_punch_in:
                            status = (
                                "missing"
                                if status == "normal"
                                else status + "+missing_in"
                            )
                        if is_missing_punch_out:
                            status = (
                                "missing"
                                if status == "normal"
                                else status + "+missing_out"
                            )

                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        is_driver = "司機" in employee.title_name

                        daily_key = (employee.id, attendance_date)
                        week_monday = attendance_date - timedelta(
                            days=attendance_date.weekday()
                        )
                        shift_key = (employee.id, week_monday)

                        shift_data = None

                        if daily_key in daily_shift_map:
                            shift_data = daily_shift_map[daily_key]
                        elif (
                            is_head_teacher or is_assistant
                        ) and shift_key in shift_schedule_map:
                            shift_data = shift_schedule_map[shift_key]

                        if shift_data and punch_in_time and punch_out_time:
                            shift_start = datetime.strptime(
                                shift_data["work_start"], "%H:%M"
                            ).time()
                            shift_end = datetime.strptime(
                                shift_data["work_end"], "%H:%M"
                            ).time()

                            shift_start_dt = datetime.combine(
                                attendance_date, shift_start
                            )
                            shift_end_dt = datetime.combine(attendance_date, shift_end)
                            # 跨夜班：排班結束在隔日（如 shift_end=02:00 < shift_start=18:00）
                            if shift_end_dt <= shift_start_dt:
                                shift_end_dt += timedelta(days=1)

                            is_late = punch_in_time > shift_start_dt
                            late_minutes = (
                                max(
                                    0,
                                    int(
                                        (punch_in_time - shift_start_dt).total_seconds()
                                        / 60
                                    ),
                                )
                                if is_late
                                else 0
                            )
                            is_early_leave = punch_out_time < shift_end_dt

                            if is_late and is_early_leave:
                                status = "late+early_leave"
                            elif is_late:
                                status = "late"
                            elif is_early_leave:
                                status = "early_leave"
                            else:
                                status = "normal"
                            early_leave_minutes = (
                                max(
                                    0,
                                    int(
                                        (shift_end_dt - punch_out_time).total_seconds()
                                        / 60
                                    ),
                                )
                                if is_early_leave
                                else 0
                            )

                        elif punch_in_time and punch_out_time:
                            duration_minutes = int(
                                (punch_out_time - punch_in_time).total_seconds() / 60
                            )
                            if is_driver:
                                required_duration = 480
                            else:
                                required_duration = 540

                            if duration_minutes >= required_duration:
                                is_late = False
                                is_early_leave = False
                                status = "normal"
                                late_minutes = 0
                                early_leave_minutes = 0

                        department = str(row.get("部門", "")).strip()
                        existing = attendance_cache.get((employee.id, attendance_date))

                        if existing:
                            existing.punch_in_time = punch_in_time
                            existing.punch_out_time = punch_out_time
                            existing.status = status
                            existing.is_late = is_late
                            existing.is_early_leave = is_early_leave
                            existing.is_missing_punch_in = is_missing_punch_in
                            existing.is_missing_punch_out = is_missing_punch_out
                            existing.late_minutes = late_minutes
                            existing.early_leave_minutes = early_leave_minutes
                            existing.remark = f"部門: {department}"
                        else:
                            attendance = Attendance(
                                employee_id=employee.id,
                                attendance_date=attendance_date,
                                punch_in_time=punch_in_time,
                                punch_out_time=punch_out_time,
                                status=status,
                                is_late=is_late,
                                is_early_leave=is_early_leave,
                                is_missing_punch_in=is_missing_punch_in,
                                is_missing_punch_out=is_missing_punch_out,
                                late_minutes=late_minutes,
                                early_leave_minutes=early_leave_minutes,
                                remark=f"部門: {department}",
                            )
                            session.add(attendance)
                            attendance_cache[(employee.id, attendance_date)] = (
                                attendance
                            )

                        results_data["success"] += 1
                        _affected_months.add(
                            (employee.id, attendance_date.year, attendance_date.month)
                        )

                        if emp_name not in employee_stats:
                            employee_stats[emp_name] = {
                                "員工姓名": emp_name,
                                "總出勤天數": 0,
                                "正常天數": 0,
                                "遲到次數": 0,
                                "早退次數": 0,
                                "未打卡(上班)": 0,
                                "未打卡(下班)": 0,
                                "遲到總分鐘": 0,
                            }

                        stats = employee_stats[emp_name]
                        stats["總出勤天數"] += 1
                        if status == "normal":
                            stats["正常天數"] += 1
                        if is_late:
                            stats["遲到次數"] += 1
                            stats["遲到總分鐘"] += late_minutes
                        if is_early_leave:
                            stats["早退次數"] += 1
                        if is_missing_punch_in:
                            stats["未打卡(上班)"] += 1
                        if is_missing_punch_out:
                            stats["未打卡(下班)"] += 1

                    except Exception as e:
                        results_data["failed"] += 1
                        results_data["errors"].append(f"第 {idx+2} 行: {str(e)}")

                # 已異動的 (emp, year, month) 批次標 needs_recalc=True
                # Why: 考勤改動會影響遲到/早退/缺打卡/曠職基準等扣款來源,薪資若已算過
                # 必須標 stale 避免後續 finalize 把舊薪資封存。
                _mark_attendance_upload_stale(session, _affected_months)
                session.commit()

                summary_data = list(employee_stats.values())

                return {
                    "message": f"考勤記錄匯入完成，成功 {results_data['success']} 筆，失敗 {results_data['failed']} 筆",
                    "summary": summary_data,
                    "anomaly_count": results_data["failed"],
                    "anomalies": results_data["errors"][:20],
                }

            finally:
                session.close()

        else:
            # 舊格式：使用原有解析器
            from services.attendance_parser import parse_attendance_file

            results, anomaly_df, summary_df = parse_attendance_file(file_path)

            session = get_session()
            try:
                employees = (
                    session.query(Employee).filter(Employee.is_active == True).all()
                )
                emp_by_name = {emp.name: emp for emp in employees}

                all_classrooms = (
                    session.query(Classroom).filter(Classroom.is_active == True).all()
                )
                head_teacher_map = {
                    c.head_teacher_id for c in all_classrooms if c.head_teacher_id
                }
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

                shift_assignments = session.query(ShiftAssignment).all()
                shift_types_map = _get_shift_type_id_map(session)
                shift_schedule_map = {}
                for sa in shift_assignments:
                    st = shift_types_map.get(sa.shift_type_id)
                    if st:
                        shift_schedule_map[(sa.employee_id, sa.week_start_date)] = {
                            "work_start": st.work_start,
                            "work_end": st.work_end,
                            "name": st.name,
                        }

                db_save_count = 0

                # 預先批量查詢，避免舊格式迴圈內 N+1
                _legacy_emp_ids: set = set()
                _legacy_dates: set = set()
                for _en, _res in results.items():
                    _e = emp_by_name.get(_en)
                    if _e:
                        _legacy_emp_ids.add(_e.id)
                        for _d in _res.details:
                            _legacy_dates.add(_d["date"])

                _assert_upload_months_not_finalized(
                    session, _legacy_emp_ids, _legacy_dates
                )

                # 累積實際被異動的 (emp_id, year, month),供 commit 後批次標 stale
                _legacy_affected_months: set = set()

                legacy_attendance_cache: dict = {}
                if _legacy_emp_ids and _legacy_dates:
                    _lc = (
                        session.query(Attendance)
                        .filter(
                            Attendance.employee_id.in_(_legacy_emp_ids),
                            Attendance.attendance_date.in_(_legacy_dates),
                        )
                        .all()
                    )
                    legacy_attendance_cache = {
                        (a.employee_id, a.attendance_date): a for a in _lc
                    }

                for emp_name, result in results.items():
                    employee = emp_by_name.get(emp_name)
                    if not employee:
                        continue

                    for detail in result.details:
                        p_in = detail["punch_in"]
                        p_out = detail["punch_out"]
                        a_date = detail["date"]

                        # 取完整 datetime（跨夜班的 punch_out_dt 已是次日）
                        dt_in_full = detail.get("punch_in_dt")
                        dt_out_full = detail.get("punch_out_dt")
                        # 向後相容：若舊資料無 punch_in_dt，fallback 到 combine
                        if dt_in_full is None and p_in:
                            dt_in_full = datetime.combine(a_date, p_in)
                        if dt_out_full is None and p_out:
                            dt_out_full = datetime.combine(a_date, p_out)

                        status = detail["status"]
                        is_late = detail["is_late"]
                        is_early_leave = detail["is_early_leave"]

                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        is_driver = "司機" in employee.title_name

                        if (
                            (is_head_teacher or is_assistant)
                            and dt_in_full
                            and dt_out_full
                        ):
                            week_monday = a_date - timedelta(days=a_date.weekday())
                            shift_key = (employee.id, week_monday)
                            if shift_key in shift_schedule_map:
                                shift = shift_schedule_map[shift_key]
                                shift_start = datetime.strptime(
                                    shift["work_start"], "%H:%M"
                                ).time()
                                shift_end = datetime.strptime(
                                    shift["work_end"], "%H:%M"
                                ).time()
                                shift_start_dt = datetime.combine(a_date, shift_start)
                                shift_end_dt = datetime.combine(a_date, shift_end)
                                # 跨夜班：排班結束在隔日
                                if shift_end_dt <= shift_start_dt:
                                    shift_end_dt += timedelta(days=1)

                                is_late = dt_in_full > shift_start_dt
                                late_minutes = (
                                    max(
                                        0,
                                        int(
                                            (
                                                dt_in_full - shift_start_dt
                                            ).total_seconds()
                                            / 60
                                        ),
                                    )
                                    if is_late
                                    else 0
                                )
                                is_early_leave = dt_out_full < shift_end_dt

                                if is_late and is_early_leave:
                                    status = "late+early_leave"
                                elif is_late:
                                    status = "late"
                                elif is_early_leave:
                                    status = "early_leave"
                                else:
                                    status = "normal"

                        elif dt_in_full and dt_out_full:
                            duration_minutes = int(
                                (dt_out_full - dt_in_full).total_seconds() / 60
                            )
                            required_duration = 480 if is_driver else 540

                            if duration_minutes >= required_duration:
                                status = "normal"
                                is_late = False
                                is_early_leave = False

                        existing = legacy_attendance_cache.get((employee.id, a_date))

                        db_p_in = dt_in_full
                        db_p_out = dt_out_full

                        if existing:
                            existing.punch_in_time = db_p_in
                            existing.punch_out_time = db_p_out
                            existing.status = status
                            existing.is_late = is_late
                            existing.is_early_leave = is_early_leave
                            existing.is_missing_punch_in = detail["is_missing_punch_in"]
                            existing.is_missing_punch_out = detail[
                                "is_missing_punch_out"
                            ]
                            if status == "normal":
                                existing.late_minutes = 0
                                existing.early_leave_minutes = 0
                            else:
                                existing.late_minutes = detail["late_minutes"]
                                existing.early_leave_minutes = detail["early_minutes"]

                            existing.remark = "Legacy Upload"
                        else:
                            att = Attendance(
                                employee_id=employee.id,
                                attendance_date=a_date,
                                punch_in_time=db_p_in,
                                punch_out_time=db_p_out,
                                status=status,
                                is_late=is_late,
                                is_early_leave=is_early_leave,
                                is_missing_punch_in=detail["is_missing_punch_in"],
                                is_missing_punch_out=detail["is_missing_punch_out"],
                                late_minutes=(
                                    0 if status == "normal" else detail["late_minutes"]
                                ),
                                early_leave_minutes=(
                                    0 if status == "normal" else detail["early_minutes"]
                                ),
                                remark="Legacy Upload",
                            )
                            session.add(att)
                            legacy_attendance_cache[(employee.id, a_date)] = att

                        db_save_count += 1
                        _legacy_affected_months.add(
                            (employee.id, a_date.year, a_date.month)
                        )

                # 已異動的 (emp, year, month) 批次標 needs_recalc=True
                _mark_attendance_upload_stale(session, _legacy_affected_months)
                session.commit()

            except Exception as e:
                session.rollback()
                logger.error(f"Failed to save legacy records: {e}")
            finally:
                session.close()

            anomaly_df.to_excel("output/anomaly_report.xlsx", index=False)
            summary_df.to_excel("output/attendance_summary.xlsx", index=False)

            summary_data = summary_df.to_dict("records")
            anomaly_data = anomaly_df.to_dict("records")

            return {
                "message": f"考勤記錄解析並存檔完成 (已處理 {len(summary_data)} 人)",
                "summary": summary_data,
                "anomaly_count": len(anomaly_data),
                "anomalies": anomaly_data[:20],
            }

    except Exception as e:
        raise_safe_500(e, context="解析失敗")
    finally:
        # 處理完畢後刪除暫存檔，無論成功或失敗
        file_path.unlink(missing_ok=True)


@router.post("/upload-csv")
async def upload_attendance_csv(
    request: AttendanceUploadRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """上傳 CSV 格式考勤記錄並存入資料庫"""
    session = get_session()
    try:
        results = {
            "total": len(request.records),
            "success": 0,
            "failed": 0,
            "errors": [],
            "summary": [],
        }

        employees = session.query(Employee).filter(Employee.is_active == True).all()
        emp_by_id = {emp.employee_id: emp for emp in employees}
        emp_by_name = {emp.name: emp for emp in employees}

        # 預先批量查詢，避免迴圈內 N+1
        _csv_emp_ids: set = set()
        _csv_dates: set = set()
        for _row in request.records:
            _e = emp_by_id.get(_row.employee_number) or emp_by_name.get(_row.name)
            if _e:
                _csv_emp_ids.add(_e.id)
            try:
                try:
                    _csv_dates.add(datetime.strptime(_row.date, "%Y/%m/%d").date())
                except ValueError:
                    _csv_dates.add(datetime.strptime(_row.date, "%Y-%m-%d").date())
            except ValueError:
                pass

        _assert_upload_months_not_finalized(session, _csv_emp_ids, _csv_dates)

        # 累積實際被異動的 (emp_id, year, month),供 commit 後批次標 stale
        _csv_affected_months: set = set()

        csv_attendance_cache: dict = {}
        if _csv_emp_ids and _csv_dates:
            _cc = (
                session.query(Attendance)
                .filter(
                    Attendance.employee_id.in_(_csv_emp_ids),
                    Attendance.attendance_date.in_(_csv_dates),
                )
                .all()
            )
            csv_attendance_cache = {(a.employee_id, a.attendance_date): a for a in _cc}

        employee_stats = {}

        for row in request.records:
            try:
                employee = emp_by_id.get(row.employee_number) or emp_by_name.get(
                    row.name
                )

                if not employee:
                    results["failed"] += 1
                    results["errors"].append(
                        f"找不到員工: {row.name} (編號: {row.employee_number})"
                    )
                    continue

                try:
                    attendance_date = datetime.strptime(row.date, "%Y/%m/%d").date()
                except ValueError:
                    try:
                        attendance_date = datetime.strptime(row.date, "%Y-%m-%d").date()
                    except ValueError:
                        results["failed"] += 1
                        results["errors"].append(f"日期格式錯誤: {row.date}")
                        continue

                punch_in_time = None
                if row.punch_in and row.punch_in.strip():
                    try:
                        punch_in_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_in.strip(), "%H:%M").time(),
                        )
                    except ValueError:
                        pass

                punch_out_time = None
                if row.punch_out and row.punch_out.strip():
                    try:
                        punch_out_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_out.strip(), "%H:%M").time(),
                        )
                    except ValueError:
                        pass

                # 跨夜班修正：下班時間早於上班時間表示隔日下班（如 18:00→02:00）
                if punch_in_time and punch_out_time and punch_out_time < punch_in_time:
                    punch_out_time += timedelta(days=1)
                elif (
                    punch_in_time and punch_out_time and punch_out_time == punch_in_time
                ):
                    results["failed"] += 1
                    results["errors"].append(
                        f"{row.name} {row.date}: 上下班時間相同 {row.punch_in}，請確認資料"
                    )
                    continue

                work_start = datetime.strptime(
                    employee.work_start_time or "08:00", "%H:%M"
                ).time()
                work_end = datetime.strptime(
                    employee.work_end_time or "17:00", "%H:%M"
                ).time()

                is_late = False
                is_early_leave = False
                is_missing_punch_in = punch_in_time is None
                is_missing_punch_out = punch_out_time is None
                late_minutes = 0
                early_leave_minutes = 0
                status = "normal"

                if punch_in_time:
                    work_start_dt = datetime.combine(attendance_date, work_start)
                    if punch_in_time > work_start_dt:
                        is_late = True
                        late_minutes = int(
                            (punch_in_time - work_start_dt).total_seconds() / 60
                        )
                        status = "late"

                if punch_out_time:
                    work_end_dt = datetime.combine(attendance_date, work_end)
                    if punch_out_time < work_end_dt:
                        is_early_leave = True
                        early_leave_minutes = int(
                            (work_end_dt - punch_out_time).total_seconds() / 60
                        )
                        if status == "normal":
                            status = "early_leave"
                        else:
                            status += "+early_leave"

                if is_missing_punch_in:
                    status = "missing" if status == "normal" else status + "+missing_in"
                if is_missing_punch_out:
                    status = (
                        "missing" if status == "normal" else status + "+missing_out"
                    )

                existing = csv_attendance_cache.get((employee.id, attendance_date))

                if existing:
                    existing.punch_in_time = punch_in_time
                    existing.punch_out_time = punch_out_time
                    existing.status = status
                    existing.is_late = is_late
                    existing.is_early_leave = is_early_leave
                    existing.is_missing_punch_in = is_missing_punch_in
                    existing.is_missing_punch_out = is_missing_punch_out
                    existing.late_minutes = late_minutes
                    existing.early_leave_minutes = early_leave_minutes
                    existing.remark = f"部門: {row.department}"
                else:
                    attendance = Attendance(
                        employee_id=employee.id,
                        attendance_date=attendance_date,
                        punch_in_time=punch_in_time,
                        punch_out_time=punch_out_time,
                        status=status,
                        is_late=is_late,
                        is_early_leave=is_early_leave,
                        is_missing_punch_in=is_missing_punch_in,
                        is_missing_punch_out=is_missing_punch_out,
                        late_minutes=late_minutes,
                        early_leave_minutes=early_leave_minutes,
                        remark=f"部門: {row.department}",
                    )
                    session.add(attendance)
                    csv_attendance_cache[(employee.id, attendance_date)] = attendance

                results["success"] += 1
                _csv_affected_months.add(
                    (employee.id, attendance_date.year, attendance_date.month)
                )

                if employee.name not in employee_stats:
                    employee_stats[employee.name] = {
                        "name": employee.name,
                        "total_days": 0,
                        "normal_days": 0,
                        "late_count": 0,
                        "early_leave_count": 0,
                        "missing_punch_in": 0,
                        "missing_punch_out": 0,
                        "total_late_minutes": 0,
                    }

                stats = employee_stats[employee.name]
                stats["total_days"] += 1
                if status == "normal":
                    stats["normal_days"] += 1
                if is_late:
                    stats["late_count"] += 1
                    stats["total_late_minutes"] += late_minutes
                if is_early_leave:
                    stats["early_leave_count"] += 1
                if is_missing_punch_in:
                    stats["missing_punch_in"] += 1
                if is_missing_punch_out:
                    stats["missing_punch_out"] += 1

            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"處理記錄時發生錯誤: {str(e)}")

        # 已異動的 (emp, year, month) 批次標 needs_recalc=True
        _mark_attendance_upload_stale(session, _csv_affected_months)
        session.commit()

        results["summary"] = list(employee_stats.values())

        return {
            "message": f"考勤記錄匯入完成，成功 {results['success']} 筆，失敗 {results['failed']} 筆",
            "results": results,
        }

    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="匯入失敗")
    finally:
        session.close()
