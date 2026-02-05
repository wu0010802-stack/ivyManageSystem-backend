"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import date
import pandas as pd
import os
import shutil

from services.attendance_parser import AttendanceParser, parse_attendance_file
from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine
from models.database import init_database, get_session, Employee, Attendance, SalaryRecord, Student

app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="1.0.0"
)

# CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化服務
insurance_service = InsuranceService()
salary_engine = SalaryEngine()

# 確保資料目錄存在
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)

# 初始化資料庫 (PostgreSQL)
init_database()


# ============ Pydantic Models ============

class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    id_number: Optional[str] = None
    employee_type: str = "regular"
    base_salary: float = 0
    hourly_rate: float = 0
    supervisor_allowance: float = 0
    teacher_allowance: float = 0
    meal_allowance: float = 0
    transportation_allowance: float = 0
    other_allowance: float = 0
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: float = 0
    work_start_time: str = "08:00"
    work_end_time: str = "17:00"
    hire_date: Optional[str] = None


class EmployeeUpdate(BaseModel):
    employee_id: Optional[str] = None
    name: Optional[str] = None
    id_number: Optional[str] = None
    employee_type: Optional[str] = None
    base_salary: Optional[float] = None
    hourly_rate: Optional[float] = None
    supervisor_allowance: Optional[float] = None
    teacher_allowance: Optional[float] = None
    meal_allowance: Optional[float] = None
    transportation_allowance: Optional[float] = None
    other_allowance: Optional[float] = None
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: Optional[float] = None
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None
    hire_date: Optional[str] = None


class BonusSettings(BaseModel):
    year: int
    month: int
    target_enrollment: int
    current_enrollment: int
    festival_bonus_base: float = 0
    overtime_bonus_per_student: float = 500


class InsuranceTableImport(BaseModel):
    table_type: str = "labor"
    data: List[dict]


class CalculateSalaryRequest(BaseModel):
    year: int
    month: int
    bonus_settings: Optional[BonusSettings] = None


class StudentCreate(BaseModel):
    student_id: str
    name: str
    birthday: Optional[str] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class StudentUpdate(BaseModel):
    student_id: Optional[str] = None
    name: Optional[str] = None
    birthday: Optional[str] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


# ============ API Routes ============

@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "1.0.0"}


# --- 員工管理 ---

@app.get("/api/employees")
async def get_employees():
    """取得所有在職員工列表"""
    session = get_session()
    try:
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        result = []
        for e in employees:
            result.append({
                "id": e.id,
                "employee_id": e.employee_id,
                "name": e.name,
                "id_number": e.id_number,
                "employee_type": e.employee_type,
                "base_salary": e.base_salary,
                "hourly_rate": e.hourly_rate,
                "supervisor_allowance": e.supervisor_allowance,
                "teacher_allowance": e.teacher_allowance,
                "meal_allowance": e.meal_allowance,
                "transportation_allowance": e.transportation_allowance,
                "other_allowance": e.other_allowance,
                "bank_code": e.bank_code,
                "bank_account": e.bank_account,
                "bank_account_name": e.bank_account_name,
                "insurance_salary_level": e.insurance_salary_level,
                "work_start_time": e.work_start_time,
                "work_end_time": e.work_end_time,
                "hire_date": e.hire_date.isoformat() if e.hire_date else None,
                "is_active": e.is_active
            })
        return result
    finally:
        session.close()


@app.get("/api/employees/{employee_id}")
async def get_employee(employee_id: int):
    """取得單一員工詳細資料"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")
        return {
            "id": employee.id,
            "employee_id": employee.employee_id,
            "name": employee.name,
            "id_number": employee.id_number,
            "employee_type": employee.employee_type,
            "base_salary": employee.base_salary,
            "hourly_rate": employee.hourly_rate,
            "supervisor_allowance": employee.supervisor_allowance,
            "teacher_allowance": employee.teacher_allowance,
            "meal_allowance": employee.meal_allowance,
            "transportation_allowance": employee.transportation_allowance,
            "other_allowance": employee.other_allowance,
            "bank_code": employee.bank_code,
            "bank_account": employee.bank_account,
            "bank_account_name": employee.bank_account_name,
            "insurance_salary_level": employee.insurance_salary_level,
            "work_start_time": employee.work_start_time,
            "work_end_time": employee.work_end_time,
            "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
            "is_active": employee.is_active
        }
    finally:
        session.close()


@app.post("/api/employees")
async def create_employee(emp: EmployeeCreate):
    """新增員工"""
    session = get_session()
    try:
        # 檢查工號是否重複
        existing = session.query(Employee).filter(Employee.employee_id == emp.employee_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="工號已存在")

        emp_data = emp.dict()
        # 處理日期欄位
        if emp_data.get('hire_date'):
            from datetime import datetime
            emp_data['hire_date'] = datetime.strptime(emp_data['hire_date'], '%Y-%m-%d').date()
        else:
            emp_data.pop('hire_date', None)

        employee = Employee(**emp_data)
        session.add(employee)
        session.commit()
        return {"message": "員工新增成功", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@app.put("/api/employees/{employee_id}")
async def update_employee(employee_id: int, emp: EmployeeUpdate):
    """更新員工資料"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        update_data = emp.dict(exclude_unset=True)

        # 處理日期欄位
        if 'hire_date' in update_data and update_data['hire_date']:
            from datetime import datetime
            update_data['hire_date'] = datetime.strptime(update_data['hire_date'], '%Y-%m-%d').date()

        for key, value in update_data.items():
            if value is not None:
                setattr(employee, key, value)

        session.commit()
        return {"message": "員工資料更新成功", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@app.delete("/api/employees/{employee_id}")
async def delete_employee(employee_id: int):
    """刪除員工（軟刪除，設為離職）"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        employee.is_active = False
        session.commit()
        return {"message": "員工已設為離職", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


# --- 學生管理 ---

@app.get("/api/students")
async def get_students():
    """取得所有在讀學生列表"""
    session = get_session()
    try:
        students = session.query(Student).filter(Student.is_active == True).all()
        result = []
        for s in students:
            result.append({
                "id": s.id,
                "student_id": s.student_id,
                "name": s.name,
                "birthday": s.birthday.isoformat() if s.birthday else None,
                "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
                "parent_name": s.parent_name,
                "phone": s.phone,
                "address": s.address,
                "is_active": s.is_active
            })
        return result
    finally:
        session.close()


@app.get("/api/students/{student_id}")
async def get_student(student_id: int):
    """取得單一學生詳細資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")
        return {
            "id": student.id,
            "student_id": student.student_id,
            "name": student.name,
            "birthday": student.birthday.isoformat() if student.birthday else None,
            "enrollment_date": student.enrollment_date.isoformat() if student.enrollment_date else None,
            "parent_name": student.parent_name,
            "phone": student.phone,
            "address": student.address,
            "is_active": student.is_active
        }
    finally:
        session.close()


@app.post("/api/students")
async def create_student(item: StudentCreate):
    """新增學生"""
    session = get_session()
    try:
        # 檢查學號是否重複
        existing = session.query(Student).filter(Student.student_id == item.student_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="學號已存在")

        data = item.dict()
        # 處理日期欄位
        if data.get('birthday'):
            from datetime import datetime
            data['birthday'] = datetime.strptime(data['birthday'], '%Y-%m-%d').date()
        else:
            data.pop('birthday', None)
            
        if data.get('enrollment_date'):
            from datetime import datetime
            data['enrollment_date'] = datetime.strptime(data['enrollment_date'], '%Y-%m-%d').date()
        else:
            data.pop('enrollment_date', None)

        student = Student(**data)
        session.add(student)
        session.commit()
        return {"message": "學生新增成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@app.put("/api/students/{student_id}")
async def update_student(student_id: int, item: StudentUpdate):
    """更新學生資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        update_data = item.dict(exclude_unset=True)

        # 處理日期欄位
        if 'birthday' in update_data and update_data['birthday']:
            from datetime import datetime
            update_data['birthday'] = datetime.strptime(update_data['birthday'], '%Y-%m-%d').date()
            
        if 'enrollment_date' in update_data and update_data['enrollment_date']:
            from datetime import datetime
            update_data['enrollment_date'] = datetime.strptime(update_data['enrollment_date'], '%Y-%m-%d').date()

        for key, value in update_data.items():
            if value is not None:
                setattr(student, key, value)

        session.commit()
        return {"message": "學生資料更新成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@app.delete("/api/students/{student_id}")
async def delete_student(student_id: int):
    """刪除學生（軟刪除）"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        student.is_active = False
        session.commit()
        return {"message": "學生已刪除", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


# --- 考勤處理 ---

@app.post("/api/attendance/upload")
async def upload_attendance(file: UploadFile = File(...)):
    """上傳打卡記錄 Excel"""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔案")
    
    # 儲存上傳檔案
    file_path = f"data/uploads/{file.filename}"
    os.makedirs("data/uploads", exist_ok=True)
    
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    # 解析考勤記錄
    try:
        results, anomaly_df, summary_df = parse_attendance_file(file_path)
        
        # 儲存報表
        anomaly_df.to_excel("output/anomaly_report.xlsx", index=False)
        summary_df.to_excel("output/attendance_summary.xlsx", index=False)
        
        summary_data = summary_df.to_dict('records')
        anomaly_data = anomaly_df.to_dict('records')
        
        return {
            "message": "考勤記錄解析完成",
            "summary": summary_data,
            "anomaly_count": len(anomaly_data),
            "anomalies": anomaly_data[:20]  # 只回傳前20筆
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


@app.get("/api/attendance/anomaly-report")
async def download_anomaly_report():
    """下載異常清單"""
    file_path = "output/anomaly_report.xlsx"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="報表尚未產生")
    return FileResponse(file_path, filename="考勤異常清單.xlsx")


# --- 勞健保 ---

@app.post("/api/insurance/import")
async def import_insurance_table(data: InsuranceTableImport):
    """匯入勞健保級距表"""
    success = insurance_service.import_table(data=data.data, table_type=data.table_type)
    if success:
        return {"message": f"{data.table_type} 級距表匯入成功"}
    raise HTTPException(status_code=400, detail="匯入失敗")


@app.get("/api/insurance/calculate")
async def calculate_insurance(salary: float = Query(...), dependents: int = Query(0)):
    """計算勞健保"""
    result = insurance_service.calculate(salary, dependents)
    return {
        "insured_amount": result.insured_amount,
        "labor_employee": result.labor_employee,
        "labor_employer": result.labor_employer,
        "health_employee": result.health_employee,
        "health_employer": result.health_employer,
        "pension_employer": result.pension_employer,
        "total_employee": result.total_employee,
        "total_employer": result.total_employer
    }


# --- 薪資結算 ---

@app.post("/api/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest):
    """一鍵結算薪資"""
    session = get_session()
    employees = session.query(Employee).filter(Employee.is_active == True).all()
    
    results = []
    bonus_settings = None
    
    if request.bonus_settings:
        bonus_settings = {
            "target": request.bonus_settings.target_enrollment,
            "current": request.bonus_settings.current_enrollment,
            "festival_base": request.bonus_settings.festival_bonus_base,
            "overtime_per": request.bonus_settings.overtime_bonus_per_student
        }
    
    for emp in employees:
        emp_dict = {
            "name": emp.name,
            "employee_id": emp.employee_id,
            "employee_type": emp.employee_type,
            "base_salary": emp.base_salary,
            "hourly_rate": emp.hourly_rate,
            "supervisor_allowance": emp.supervisor_allowance,
            "teacher_allowance": emp.teacher_allowance,
            "meal_allowance": emp.meal_allowance,
            "transportation_allowance": emp.transportation_allowance,
            "insurance_salary": emp.insurance_salary_level or emp.base_salary
        }
        
        breakdown = salary_engine.calculate_salary(
            emp_dict, request.year, request.month, bonus_settings=bonus_settings
        )
        results.append(breakdown.__dict__)
    
    return {"message": "薪資結算完成", "results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
