"""
Attendance - upload endpoints (Excel and CSV)
"""

import logging
import os
import shutil
from datetime import datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from models.database import (
    get_session, Employee, Attendance, Classroom,
    ShiftAssignment, ShiftType, DailyShift,
)
from utils.auth import require_admin
from ._shared import AttendanceUploadRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/upload")
async def upload_attendance(file: UploadFile = File(...), current_user: dict = Depends(require_admin)):
    """上傳打卡記錄 Excel（支持分開的上班/下班時間欄位）"""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔案")

    file_path = f"data/uploads/{file.filename}"
    os.makedirs("data/uploads", exist_ok=True)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        df = pd.read_excel(file_path)
        columns = df.columns.tolist()

        # 新格式：部門, 編號, 姓名, 日期, 星期, 上班時間, 下班時間
        if '上班時間' in columns and '下班時間' in columns:
            session = get_session()
            try:
                employees = session.query(Employee).filter(Employee.is_active == True).all()
                emp_by_id = {str(emp.employee_id): emp for emp in employees}
                emp_by_name = {emp.name: emp for emp in employees}

                all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
                head_teacher_map = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

                shift_assignments = session.query(ShiftAssignment).all()
                shift_types = {st.id: st for st in session.query(ShiftType).all()}
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

                if '日期' in df.columns:
                    temp_dates = pd.to_datetime(df['日期'], errors='coerce').dt.date.dropna()
                    if not temp_dates.empty:
                        min_date, max_date = temp_dates.min(), temp_dates.max()
                        daily_shifts_query = session.query(DailyShift).filter(
                            DailyShift.date >= min_date,
                            DailyShift.date <= max_date
                        ).all()

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
                    "summary": []
                }

                employee_stats = {}

                for idx, row in df.iterrows():
                    try:
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

                        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                        grace_minutes = 0

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

                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        title_str = (employee.title or "") + (employee.job_title_rel.name if employee.job_title_rel else "")
                        is_driver = "司機" in title_str

                        daily_key = (employee.id, attendance_date)
                        week_monday = attendance_date - timedelta(days=attendance_date.weekday())
                        shift_key = (employee.id, week_monday)

                        shift_data = None

                        if daily_key in daily_shift_map:
                             shift_data = daily_shift_map[daily_key]
                        elif (is_head_teacher or is_assistant) and shift_key in shift_schedule_map:
                             shift_data = shift_schedule_map[shift_key]

                        if shift_data and punch_in_time and punch_out_time:
                             shift_start = datetime.strptime(shift_data["work_start"], "%H:%M").time()
                             shift_end = datetime.strptime(shift_data["work_end"], "%H:%M").time()

                             shift_start_dt = datetime.combine(attendance_date, shift_start)
                             shift_end_dt = datetime.combine(attendance_date, shift_end)

                             is_late = punch_in_time > shift_start_dt
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
                            duration_minutes = int((punch_out_time - punch_in_time).total_seconds() / 60)
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

            session = get_session()
            try:
                employees = session.query(Employee).filter(Employee.is_active == True).all()
                emp_by_name = {emp.name: emp for emp in employees}

                all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
                head_teacher_map = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
                assistant_teacher_map = set()
                for c in all_classrooms:
                    if c.assistant_teacher_id:
                        assistant_teacher_map.add(c.assistant_teacher_id)

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
                        p_in = detail['punch_in']
                        p_out = detail['punch_out']
                        a_date = detail['date']

                        status = detail['status']
                        is_late = detail['is_late']
                        is_early_leave = detail['is_early_leave']

                        is_head_teacher = employee.id in head_teacher_map
                        is_assistant = employee.id in assistant_teacher_map
                        title_str = (employee.title or "") + (employee.job_title_rel.name if employee.job_title_rel else "")
                        is_driver = "司機" in title_str

                        if (is_head_teacher or is_assistant) and p_in and p_out:
                            week_monday = a_date - timedelta(days=a_date.weekday())
                            shift_key = (employee.id, week_monday)
                            if shift_key in shift_schedule_map:
                                shift = shift_schedule_map[shift_key]
                                shift_start = datetime.strptime(shift["work_start"], "%H:%M").time()
                                shift_end = datetime.strptime(shift["work_end"], "%H:%M").time()
                                dt_in = datetime.combine(a_date, p_in)
                                dt_out = datetime.combine(a_date, p_out)

                                is_late = dt_in > datetime.combine(a_date, shift_start)
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
                            dt_in = datetime.combine(a_date, p_in)
                            dt_out = datetime.combine(a_date, p_out)
                            duration_minutes = int((dt_out - dt_in).total_seconds() / 60)
                            required_duration = 480 if is_driver else 540

                            if duration_minutes >= required_duration:
                                status = "normal"
                                is_late = False
                                is_early_leave = False

                        existing = session.query(Attendance).filter(
                            Attendance.employee_id == employee.id,
                            Attendance.attendance_date == a_date
                        ).first()

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

            except Exception as e:
                session.rollback()
                logger.error(f"Failed to save legacy records: {e}")
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


@router.post("/upload-csv")
async def upload_attendance_csv(request: AttendanceUploadRequest, current_user: dict = Depends(require_admin)):
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

        employees = session.query(Employee).filter(Employee.is_active == True).all()
        emp_by_id = {emp.employee_id: emp for emp in employees}
        emp_by_name = {emp.name: emp for emp in employees}

        employee_stats = {}

        for row in request.records:
            try:
                employee = emp_by_id.get(row.employee_number) or emp_by_name.get(row.name)

                if not employee:
                    results["failed"] += 1
                    results["errors"].append(f"找不到員工: {row.name} (編號: {row.employee_number})")
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
                            datetime.strptime(row.punch_in.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                punch_out_time = None
                if row.punch_out and row.punch_out.strip():
                    try:
                        punch_out_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_out.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                grace_minutes = 0

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
                        late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                        status = "late"

                if punch_out_time:
                    work_end_dt = datetime.combine(attendance_date, work_end)
                    if punch_out_time < work_end_dt:
                        is_early_leave = True
                        early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                        if status == "normal":
                            status = "early_leave"
                        else:
                            status += "+early_leave"

                if is_missing_punch_in:
                    status = "missing" if status == "normal" else status + "+missing_in"
                if is_missing_punch_out:
                    status = "missing" if status == "normal" else status + "+missing_out"

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
                        remark=f"部門: {row.department}"
                    )
                    session.add(attendance)

                results["success"] += 1

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
