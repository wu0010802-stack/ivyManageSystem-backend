"""
Attendance management router
"""

import logging
import os
import shutil
from datetime import date, datetime, timedelta
from calendar import monthrange
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from models.database import get_session, Employee, Attendance, Classroom, LeaveRecord, OvertimeRecord, ShiftAssignment, ShiftType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["attendance"])


# ============ Pydantic Models ============

class AttendanceCSVRow(BaseModel):
    """CSV 考勤記錄格式"""
    department: str
    employee_number: str
    name: str
    date: str
    weekday: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


class AttendanceUploadRequest(BaseModel):
    """CSV 考勤上傳請求"""
    records: List[AttendanceCSVRow]
    year: int
    month: int


class AttendanceRecordUpdate(BaseModel):
    """單筆考勤記錄更新"""
    employee_id: int
    date: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


# Leave type labels (needed for calendar endpoint)
LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
}


# ============ Routes ============

@router.post("/attendance/upload")
async def upload_attendance(file: UploadFile = File(...)):
    """上傳打卡記錄 Excel（支持分開的上班/下班時間欄位）"""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔案")

    # 儲存上傳檔案
    file_path = f"data/uploads/{file.filename}"
    os.makedirs("data/uploads", exist_ok=True)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # 解析考勤記錄
    try:
        # 讀取 Excel
        df = pd.read_excel(file_path)

        # 檢查欄位格式
        columns = df.columns.tolist()

        # 新格式：部門, 編號, 姓名, 日期, 星期, 上班時間, 下班時間
        if '上班時間' in columns and '下班時間' in columns:
            session = get_session()
            try:
                employees = session.query(Employee).filter(Employee.is_active == True).all()
                emp_by_id = {str(emp.employee_id): emp for emp in employees}
                emp_by_name = {emp.name: emp for emp in employees}

                # Pre-fetch Classrooms for Role Determination
                all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
                head_teacher_map = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

                # Pre-fetch shift assignments for shift-based checking
                shift_assignments = session.query(ShiftAssignment).all()
                shift_types = {st.id: st for st in session.query(ShiftType).all()}
                shift_schedule_map = {}  # (employee_id, week_monday) -> {work_start, work_end, name}
                for sa in shift_assignments:
                    st = shift_types.get(sa.shift_type_id)
                    if st:
                        shift_schedule_map[(sa.employee_id, sa.week_start_date)] = {
                            "work_start": st.work_start,
                            "work_end": st.work_end,
                            "name": st.name,
                        }

                results_data = {
                    "total": len(df),
                    "success": 0,
                    "failed": 0,
                    "errors": [],
                    "summary": []
                }

                employee_stats = {}

                for idx, row in df.iterrows():
                    try:
                        # 取得員工
                        raw_id = row.get('編號', '')
                        emp_number = str(raw_id).strip()
                        if emp_number.endswith('.0'):
                            emp_number = emp_number[:-2]
                            
                        emp_name = str(row.get('姓名', '')).strip()
                        employee = emp_by_id.get(emp_number) or emp_by_name.get(emp_name)

                        if not employee:
                            results_data["failed"] += 1
                            results_data["errors"].append(f"第 {idx+2} 行: 找不到員工 {emp_name}")
                            continue

                        # 解析日期
                        date_val = row.get('日期')
                        if pd.isna(date_val):
                            results_data["failed"] += 1
                            results_data["errors"].append(f"第 {idx+2} 行: 日期為空")
                            continue

                        if isinstance(date_val, str):
                            try:
                                attendance_date = datetime.strptime(date_val, "%Y/%m/%d").date()
                            except:
                                attendance_date = datetime.strptime(date_val, "%Y-%m-%d").date()
                        else:
                            attendance_date = pd.to_datetime(date_val).date()

                        # 解析上班時間
                        punch_in_time = None
                        punch_in_val = row.get('上班時間')
                        if not pd.isna(punch_in_val) and str(punch_in_val).strip():
                            try:
                                time_str = str(punch_in_val).strip()
                                if ':' in time_str:
                                    parts = time_str.split(':')
                                    hour = int(parts[0])
                                    minute = int(parts[1].split('.')[0]) if '.' in parts[1] else int(parts[1])
                                    punch_in_time = datetime.combine(attendance_date, datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time())
                            except:
                                pass

                        # 解析下班時間
                        punch_out_time = None
                        punch_out_val = row.get('下班時間')
                        if not pd.isna(punch_out_val) and str(punch_out_val).strip():
                            try:
                                time_str = str(punch_out_val).strip()
                                if ':' in time_str:
                                    parts = time_str.split(':')
                                    hour = int(parts[0])
                                    minute = int(parts[1].split('.')[0]) if '.' in parts[1] else int(parts[1])
                                    punch_out_time = datetime.combine(attendance_date, datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time())
                            except:
                                pass

                        # 計算考勤狀態
                        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                        grace_minutes = 5

                        is_late = False
                        is_early_leave = False
                        is_missing_punch_in = punch_in_time is None
                        is_missing_punch_out = punch_out_time is None
                        late_minutes = 0
                        early_leave_minutes = 0
                        status = "normal"

                        if punch_in_time:
                            work_start_dt = datetime.combine(attendance_date, work_start)
                            grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
                            if punch_in_time > grace_dt:
                                is_late = True
                                late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                                status = "late"

                        if punch_out_time:
                            work_end_dt = datetime.combine(attendance_date, work_end)
                            if punch_out_time < work_end_dt:
                                is_early_leave = True
                                early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                                status = "early_leave" if status == "normal" else status + "+early_leave"

                        if is_missing_punch_in:
                            status = "missing" if status == "normal" else status + "+missing_in"
                        if is_missing_punch_out:
                            status = "missing" if status == "normal" else status + "+missing_out"

                        # --- Attendance Check Logic ---
                        # Rules:
                        # 1. Head Teacher (班導) / Assistant (副班導): Use shift schedule times
                        # 2. Driver (司機): Duration >= 8h (480m) -> Normal (no lunch)
                        # 3. All others: Duration >= 9h (540m) -> Normal (includes 1h lunch)

                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        title_str = (employee.title or "") + (employee.job_title_rel.name if employee.job_title_rel else "")
                        is_driver = "司機" in title_str

                        if (is_head_teacher or is_assistant) and punch_in_time and punch_out_time:
                            # Look up shift assignment for this week
                            week_monday = attendance_date - timedelta(days=attendance_date.weekday())
                            shift_key = (employee.id, week_monday)
                            if shift_key in shift_schedule_map:
                                shift = shift_schedule_map[shift_key]
                                shift_start = datetime.strptime(shift["work_start"], "%H:%M").time()
                                shift_end = datetime.strptime(shift["work_end"], "%H:%M").time()
                                # Recalculate late/early based on shift times
                                shift_start_dt = datetime.combine(attendance_date, shift_start)
                                shift_end_dt = datetime.combine(attendance_date, shift_end)
                                grace_dt_shift = shift_start_dt + timedelta(minutes=grace_minutes)

                                is_late = punch_in_time > grace_dt_shift
                                late_minutes = max(0, int((punch_in_time - shift_start_dt).total_seconds() / 60)) if is_late else 0
                                is_early_leave = punch_out_time < shift_end_dt

                                if is_late and is_early_leave:
                                    status = "late+early_leave"
                                elif is_late:
                                    status = "late"
                                elif is_early_leave:
                                    status = "early_leave"
                                else:
                                    status = "normal"
                                early_leave_minutes = max(0, int((shift_end_dt - punch_out_time).total_seconds() / 60)) if is_early_leave else 0

                        elif punch_in_time and punch_out_time:
                            # Duration-based check for non-teacher roles
                            duration_minutes = int((punch_out_time - punch_in_time).total_seconds() / 60)
                            if is_driver:
                                required_duration = 480  # 8h, no lunch
                            else:
                                required_duration = 540  # 9h (8h work + 1h lunch)

                            if duration_minutes >= required_duration:
                                is_late = False
                                is_early_leave = False
                                status = "normal"
                                late_minutes = 0
                                early_leave_minutes = 0

                        # 儲存到資料庫
                        department = str(row.get('部門', '')).strip()
                        existing = session.query(Attendance).filter(
                            Attendance.employee_id == employee.id,
                            Attendance.attendance_date == attendance_date
                        ).first()

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
                                remark=f"部門: {department}"
                            )
                            session.add(attendance)

                        results_data["success"] += 1

                        # 統計
                        if emp_name not in employee_stats:
                            employee_stats[emp_name] = {
                                "員工姓名": emp_name,
                                "總出勤天數": 0,
                                "正常天數": 0,
                                "遲到次數": 0,
                                "早退次數": 0,
                                "未打卡(上班)": 0,
                                "未打卡(下班)": 0,
                                "遲到總分鐘": 0
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

                session.commit()

                summary_data = list(employee_stats.values())

                return {
                    "message": f"考勤記錄匯入完成，成功 {results_data['success']} 筆，失敗 {results_data['failed']} 筆",
                    "summary": summary_data,
                    "anomaly_count": results_data["failed"],
                    "anomalies": results_data["errors"][:20]
                }

            finally:
                session.close()

        else:
            # 舊格式：使用原有解析器
            from services.attendance_parser import parse_attendance_file

            results, anomaly_df, summary_df = parse_attendance_file(file_path)

            # SAVE TO DB Logic for Old Format
            session = get_session()
            try:
                # Need employees map
                employees = session.query(Employee).filter(Employee.is_active == True).all()
                emp_by_name = {emp.name: emp for emp in employees}
                
                # Fetch Classroom context for role-based check
                all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
                head_teacher_map = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

                # Pre-fetch shift assignments
                shift_assignments = session.query(ShiftAssignment).all()
                shift_types_map = {st.id: st for st in session.query(ShiftType).all()}
                shift_schedule_map = {}
                for sa in shift_assignments:
                    st = shift_types_map.get(sa.shift_type_id)
                    if st:
                        shift_schedule_map[(sa.employee_id, sa.week_start_date)] = {
                            "work_start": st.work_start, "work_end": st.work_end, "name": st.name,
                        }

                db_save_count = 0
                
                for emp_name, result in results.items():
                    employee = emp_by_name.get(emp_name)
                    if not employee:
                        continue
                        
                    for detail in result.details:
                        # detail: {date, punch_in, punch_out, is_late, ... status}
                        # We need to apply the Duration Override Logic here too!
                        
                        p_in = detail['punch_in'] # time obj or None
                        p_out = detail['punch_out'] # time obj or None
                        a_date = detail['date'] # date obj
                        
                        # Re-calculate or use detail?
                        # detail already has status from parser, but parser DOES NOT know about 9h/9.5h rules!
                        # So we should re-eval status or apply override.
                        
                        status = detail['status']
                        is_late = detail['is_late']
                        is_early_leave = detail['is_early_leave']
                        
                        # --- Attendance Check Logic ---
                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        title_str = (employee.title or "") + (employee.job_title_rel.name if employee.job_title_rel else "")
                        is_driver = "司機" in title_str

                        if (is_head_teacher or is_assistant) and p_in and p_out:
                            # Use shift schedule times
                            week_monday = a_date - timedelta(days=a_date.weekday())
                            shift_key = (employee.id, week_monday)
                            if shift_key in shift_schedule_map:
                                shift = shift_schedule_map[shift_key]
                                shift_start = datetime.strptime(shift["work_start"], "%H:%M").time()
                                shift_end = datetime.strptime(shift["work_end"], "%H:%M").time()
                                dt_in = datetime.combine(a_date, p_in)
                                dt_out = datetime.combine(a_date, p_out)
                                grace_dt = datetime.combine(a_date, shift_start) + timedelta(minutes=5)

                                is_late = dt_in > grace_dt
                                late_minutes = max(0, int((dt_in - datetime.combine(a_date, shift_start)).total_seconds() / 60)) if is_late else 0
                                is_early_leave = dt_out < datetime.combine(a_date, shift_end)

                                if is_late and is_early_leave:
                                    status = "late+early_leave"
                                elif is_late:
                                    status = "late"
                                elif is_early_leave:
                                    status = "early_leave"
                                else:
                                    status = "normal"

                        elif p_in and p_out:
                            # Duration-based check for non-teacher roles
                            dt_in = datetime.combine(a_date, p_in)
                            dt_out = datetime.combine(a_date, p_out)
                            duration_minutes = int((dt_out - dt_in).total_seconds() / 60)
                            required_duration = 480 if is_driver else 540

                            if duration_minutes >= required_duration:
                                status = "normal"
                                is_late = False
                                is_early_leave = False
                        
                        # Save to DB
                        # Check existing
                        existing = session.query(Attendance).filter(
                            Attendance.employee_id == employee.id,
                            Attendance.attendance_date == a_date
                        ).first()
                        
                        # Convert time objects to datetime for DB (if needed? Model defines DateTime)
                        # Actually Model defines DateTime for punch_in_time.
                        # AttendanceParser returns time objects.
                        # We need datetime.combine(date, time)
                        
                        db_p_in = datetime.combine(a_date, p_in) if p_in else None
                        db_p_out = datetime.combine(a_date, p_out) if p_out else None
                        
                        if existing:
                            existing.punch_in_time = db_p_in
                            existing.punch_out_time = db_p_out
                            existing.status = status
                            existing.is_late = is_late
                            existing.is_early_leave = is_early_leave
                            existing.is_missing_punch_in = detail['is_missing_punch_in']
                            existing.is_missing_punch_out = detail['is_missing_punch_out']
                            existing.late_minutes = detail['late_minutes'] if not is_late else detail['late_minutes'] # Logic check: if override, late_min should be 0?
                            # If overridden to normal, we should probably set late/early minutes to 0 in DB
                            if status == "normal":
                                existing.late_minutes = 0
                                existing.early_leave_minutes = 0
                            else:
                                existing.late_minutes = detail['late_minutes']
                                existing.early_leave_minutes = detail['early_minutes']
                                
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
                                is_missing_punch_in=detail['is_missing_punch_in'],
                                is_missing_punch_out=detail['is_missing_punch_out'],
                                late_minutes=0 if status == "normal" else detail['late_minutes'],
                                early_leave_minutes=0 if status == "normal" else detail['early_minutes'],
                                remark="Legacy Upload"
                            )
                            session.add(att)
                        
                        db_save_count += 1
                        
                session.commit()
                # logger.info(f"Saved {db_save_count} records from legacy format")
            
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to save legacy records: {e}")
                # We don't raise here to allow returning the analysis result, but maybe should warn?
            finally:
                session.close()

            anomaly_df.to_excel("output/anomaly_report.xlsx", index=False)
            summary_df.to_excel("output/attendance_summary.xlsx", index=False)

            summary_data = summary_df.to_dict('records')
            anomaly_data = anomaly_df.to_dict('records')

            return {
                "message": f"考勤記錄解析並存檔完成 (已處理 {len(summary_data)} 人)",
                "summary": summary_data,
                "anomaly_count": len(anomaly_data),
                "anomalies": anomaly_data[:20]
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


@router.post("/attendance/upload-csv")
async def upload_attendance_csv(request: AttendanceUploadRequest):
    """上傳 CSV 格式考勤記錄並存入資料庫"""
    session = get_session()
    try:
        results = {
            "total": len(request.records),
            "success": 0,
            "failed": 0,
            "errors": [],
            "summary": []
        }

        # 取得所有員工對照表（by employee_id 和 name）
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        emp_by_id = {emp.employee_id: emp for emp in employees}
        emp_by_name = {emp.name: emp for emp in employees}

        # 用於統計的字典
        employee_stats = {}

        for row in request.records:
            try:
                # 查找員工：先用編號，再用姓名
                employee = emp_by_id.get(row.employee_number) or emp_by_name.get(row.name)

                if not employee:
                    results["failed"] += 1
                    results["errors"].append(f"找不到員工: {row.name} (編號: {row.employee_number})")
                    continue

                # 解析日期
                try:
                    attendance_date = datetime.strptime(row.date, "%Y/%m/%d").date()
                except ValueError:
                    try:
                        attendance_date = datetime.strptime(row.date, "%Y-%m-%d").date()
                    except ValueError:
                        results["failed"] += 1
                        results["errors"].append(f"日期格式錯誤: {row.date}")
                        continue

                # 解析上班時間
                punch_in_time = None
                if row.punch_in and row.punch_in.strip():
                    try:
                        time_parts = row.punch_in.strip().split(":")
                        punch_in_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_in.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                # 解析下班時間
                punch_out_time = None
                if row.punch_out and row.punch_out.strip():
                    try:
                        punch_out_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_out.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                # 計算考勤狀態
                work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                grace_minutes = 5

                is_late = False
                is_early_leave = False
                is_missing_punch_in = punch_in_time is None
                is_missing_punch_out = punch_out_time is None
                late_minutes = 0
                early_leave_minutes = 0
                status = "normal"

                # 檢查遲到
                if punch_in_time:
                    work_start_dt = datetime.combine(attendance_date, work_start)
                    grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
                    if punch_in_time > grace_dt:
                        is_late = True
                        late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                        status = "late"

                # 檢查早退
                if punch_out_time:
                    work_end_dt = datetime.combine(attendance_date, work_end)
                    if punch_out_time < work_end_dt:
                        is_early_leave = True
                        early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                        if status == "normal":
                            status = "early_leave"
                        else:
                            status += "+early_leave"

                # 處理未打卡狀態
                if is_missing_punch_in:
                    status = "missing" if status == "normal" else status + "+missing_in"
                if is_missing_punch_out:
                    status = "missing" if status == "normal" else status + "+missing_out"

                # 檢查是否已存在該日考勤記錄
                existing = session.query(Attendance).filter(
                    Attendance.employee_id == employee.id,
                    Attendance.attendance_date == attendance_date
                ).first()

                if existing:
                    # 更新現有記錄
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
                    # 新增記錄
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
                        remark=f"部門: {row.department}"
                    )
                    session.add(attendance)

                results["success"] += 1

                # 統計
                if employee.name not in employee_stats:
                    employee_stats[employee.name] = {
                        "name": employee.name,
                        "total_days": 0,
                        "normal_days": 0,
                        "late_count": 0,
                        "early_leave_count": 0,
                        "missing_punch_in": 0,
                        "missing_punch_out": 0,
                        "total_late_minutes": 0
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

        session.commit()

        # 轉換統計結果
        results["summary"] = list(employee_stats.values())

        return {
            "message": f"考勤記錄匯入完成，成功 {results['success']} 筆，失敗 {results['failed']} 筆",
            "results": results
        }

    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"匯入失敗: {str(e)}")
    finally:
        session.close()


@router.get("/attendance/records")
async def get_attendance_records(
    year: int = Query(...),
    month: int = Query(...),
    employee_id: Optional[int] = None
):
    """查詢考勤記錄"""
    session = get_session()
    try:
        # 計算月份的起止日期
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        query = session.query(Attendance, Employee).join(Employee).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        query = query.order_by(Employee.name, Attendance.attendance_date)

        records = query.all()

        result = []
        for att, emp in records:
            result.append({
                "id": att.id,
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "date": att.attendance_date.isoformat(),
                "weekday": ["一", "二", "三", "四", "五", "六", "日"][att.attendance_date.weekday()],
                "punch_in": att.punch_in_time.strftime("%H:%M") if att.punch_in_time else None,
                "punch_out": att.punch_out_time.strftime("%H:%M") if att.punch_out_time else None,
                "status": att.status,
                "is_late": att.is_late,
                "is_early_leave": att.is_early_leave,
                "is_missing_punch_in": att.is_missing_punch_in,
                "is_missing_punch_out": att.is_missing_punch_out,
                "late_minutes": att.late_minutes,
                "early_leave_minutes": att.early_leave_minutes,
                "remark": att.remark
            })

        return result
    finally:
        session.close()


@router.post("/attendance/record")
async def create_or_update_attendance_record(record: AttendanceRecordUpdate):
    """新增或更新單筆考勤記錄"""
    session = get_session()
    try:
        # 取得員工
        employee = session.query(Employee).filter(Employee.id == record.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到員工")

        # 解析日期
        try:
            attendance_date = datetime.strptime(record.date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

        # 解析上班時間
        punch_in_time = None
        if record.punch_in and record.punch_in.strip():
            try:
                punch_in_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_in.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="上班時間格式錯誤，請使用 HH:MM")

        # 解析下班時間
        punch_out_time = None
        if record.punch_out and record.punch_out.strip():
            try:
                punch_out_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_out.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="下班時間格式錯誤，請使用 HH:MM")

        # 計算考勤狀態
        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
        grace_minutes = 5

        is_late = False
        is_early_leave = False
        is_missing_punch_in = punch_in_time is None
        is_missing_punch_out = punch_out_time is None
        late_minutes = 0
        early_leave_minutes = 0
        status = "normal"

        if punch_in_time:
            work_start_dt = datetime.combine(attendance_date, work_start)
            grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
            if punch_in_time > grace_dt:
                is_late = True
                late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                status = "late"

        if punch_out_time:
            work_end_dt = datetime.combine(attendance_date, work_end)
            if punch_out_time < work_end_dt:
                is_early_leave = True
                early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                status = "early_leave" if status == "normal" else status + "+early_leave"

        if is_missing_punch_in:
            status = "missing" if status == "normal" else status + "+missing_in"
        if is_missing_punch_out:
            status = "missing" if status == "normal" else status + "+missing_out"

        # 查找或創建記錄
        existing = session.query(Attendance).filter(
            Attendance.employee_id == employee.id,
            Attendance.attendance_date == attendance_date
        ).first()

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
            message = "考勤記錄已更新"
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
                early_leave_minutes=early_leave_minutes
            )
            session.add(attendance)
            message = "考勤記錄已新增"

        session.commit()

        return {
            "message": message,
            "status": status,
            "is_late": is_late,
            "late_minutes": late_minutes,
            "is_early_leave": is_early_leave,
            "early_leave_minutes": early_leave_minutes
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/attendance/record/{employee_id}/{date}")
async def delete_single_attendance_record(employee_id: int, date: str):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()

        deleted = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == attendance_date
        ).delete()

        session.commit()

        if deleted:
            return {"message": "考勤記錄已刪除"}
        else:
            raise HTTPException(status_code=404, detail="找不到該考勤記錄")

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/attendance/records/{employee_id}/{date_str}")
def delete_single_attendance(employee_id: int, date_str: str):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        # 嘗試解析日期
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.strptime(date_str, "%Y/%m/%d").date()

        record = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == target_date
        ).first()

        if not record:
            raise HTTPException(status_code=404, detail="找不到該筆考勤記錄")

        session.delete(record)
        session.commit()
        return {"message": "刪除成功"}

    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤")
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/attendance/records/{year}/{month}")
async def delete_attendance_records(year: int, month: int):
    """刪除指定月份的所有考勤記錄"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        deleted = session.query(Attendance).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).delete()

        session.commit()

        return {"message": f"已刪除 {deleted} 筆考勤記錄"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/attendance/summary")
async def get_attendance_summary(
    year: int = Query(...),
    month: int = Query(...)
):
    """取得考勤統計摘要"""
    session = get_session()
    try:
        # 計算月份的起止日期
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        # 取得所有員工
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        result = []
        for emp in employees:
            # 查詢該員工的考勤記錄
            attendances = session.query(Attendance).filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date
            ).all()

            if not attendances:
                continue

            total_days = len(attendances)
            normal_days = sum(1 for a in attendances if a.status == "normal")
            late_count = sum(1 for a in attendances if a.is_late)
            early_leave_count = sum(1 for a in attendances if a.is_early_leave)
            missing_punch_in = sum(1 for a in attendances if a.is_missing_punch_in)
            missing_punch_out = sum(1 for a in attendances if a.is_missing_punch_out)
            total_late_minutes = sum(a.late_minutes or 0 for a in attendances)
            total_early_minutes = sum(a.early_leave_minutes or 0 for a in attendances)

            result.append({
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "total_days": total_days,
                "normal_days": normal_days,
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_in": missing_punch_in,
                "missing_punch_out": missing_punch_out,
                "total_late_minutes": total_late_minutes,
                "total_early_minutes": total_early_minutes
            })

        return result
    finally:
        session.close()


@router.get("/attendance/anomaly-report")
async def download_anomaly_report():
    """下載異常清單"""
    file_path = "output/anomaly_report.xlsx"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="報表尚未產生")
    return FileResponse(file_path, filename="考勤異常清單.xlsx")


@router.get("/attendance/calendar")
def get_attendance_calendar(
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(...)
):
    """取得員工月出勤日曆資料"""
    import calendar as cal_module

    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        _, last_day = cal_module.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        # Fetch attendance records
        attendances = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).all()
        att_map = {a.attendance_date: a for a in attendances}

        # Fetch leave records
        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
            LeaveRecord.is_approved == True
        ).all()

        # Build leave date map
        leave_map = {}
        for lv in leaves:
            d = max(lv.start_date, start_date)
            while d <= min(lv.end_date, end_date):
                leave_map[d] = lv
                d = date.fromordinal(d.toordinal() + 1)

        # Fetch overtime records
        overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
            OvertimeRecord.is_approved == True
        ).all()
        ot_map = {o.overtime_date: o for o in overtimes}

        # Build daily data
        days = []
        work_days = 0
        late_count = 0
        leave_days = 0
        overtime_hours = 0

        for day_num in range(1, last_day + 1):
            d = date(year, month, day_num)
            att = att_map.get(d)
            lv = leave_map.get(d)
            ot = ot_map.get(d)

            day_data = {
                "date": d.isoformat(),
                "weekday": d.weekday(),  # 0=Mon, 6=Sun
                "punch_in": att.punch_in_time.strftime("%H:%M") if att and att.punch_in_time else None,
                "punch_out": att.punch_out_time.strftime("%H:%M") if att and att.punch_out_time else None,
                "status": att.status if att else None,
                "is_late": att.is_late if att else False,
                "late_minutes": att.late_minutes if att else 0,
                "is_early_leave": att.is_early_leave if att else False,
                "leave_type": lv.leave_type if lv else None,
                "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type) if lv else None,
                "leave_hours": lv.leave_hours if lv else 0,
                "overtime_hours": ot.hours if ot else 0,
                "overtime_type": ot.overtime_type if ot else None,
                "remark": att.remark if att else None,
            }
            days.append(day_data)

            # Summary stats
            if att:
                work_days += 1
                if att.is_late:
                    late_count += 1
            if lv:
                leave_days += lv.leave_hours / 8
            if ot:
                overtime_hours += ot.hours

        return {
            "employee_name": emp.name,
            "employee_id": emp.employee_id,
            "year": year,
            "month": month,
            "days": days,
            "summary": {
                "work_days": work_days,
                "late_count": late_count,
                "leave_days": round(leave_days, 1),
                "overtime_hours": round(overtime_hours, 1),
            }
        }
    finally:
        session.close()
