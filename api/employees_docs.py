"""
Employee ancillary docs (educations / certificates / contracts) CRUD.

沿用 EMPLOYEES_READ / EMPLOYEES_WRITE 權限，不新增 Permission bit。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models.database import (
    session_scope,
    Employee,
    EmployeeEducation,
    EmployeeCertificate,
    EmployeeContract,
)
from utils.auth import require_staff_permission
from utils.error_messages import EMPLOYEE_NOT_FOUND
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.validators import parse_optional_date

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["employee-docs"])

# enum 值必須與前端 constants/employee.js 的 DEGREE_OPTIONS / CONTRACT_TYPE_OPTIONS 完全一致
DEGREE_VALUES = {"高中職", "學士", "碩士", "博士", "其他"}
CONTRACT_TYPE_VALUES = {"正式", "兼職", "試用", "臨時", "續約"}


# ============ Schemas ============


class EducationCreate(BaseModel):
    school_name: str = Field(..., min_length=1, max_length=100)
    major: Optional[str] = Field(None, max_length=100)
    degree: str
    graduation_date: Optional[str] = None
    is_highest: bool = False
    remark: Optional[str] = None


class EducationUpdate(BaseModel):
    school_name: Optional[str] = Field(None, min_length=1, max_length=100)
    major: Optional[str] = None
    degree: Optional[str] = None
    graduation_date: Optional[str] = None
    is_highest: Optional[bool] = None
    remark: Optional[str] = None


class CertificateCreate(BaseModel):
    certificate_name: str = Field(..., min_length=1, max_length=100)
    issuer: Optional[str] = None
    certificate_number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None
    remark: Optional[str] = None


class CertificateUpdate(BaseModel):
    certificate_name: Optional[str] = Field(None, min_length=1, max_length=100)
    issuer: Optional[str] = None
    certificate_number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None
    remark: Optional[str] = None


class ContractCreate(BaseModel):
    contract_type: str
    start_date: str
    end_date: Optional[str] = None
    salary_at_contract: Optional[float] = Field(None, ge=0)
    remark: Optional[str] = None


class ContractUpdate(BaseModel):
    contract_type: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    salary_at_contract: Optional[float] = Field(None, ge=0)
    remark: Optional[str] = None


# ============ Helpers ============


def _edu_to_dict(r: EmployeeEducation) -> dict:
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "school_name": r.school_name,
        "major": r.major,
        "degree": r.degree,
        "graduation_date": r.graduation_date.isoformat() if r.graduation_date else None,
        "is_highest": r.is_highest,
        "remark": r.remark,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _cert_to_dict(r: EmployeeCertificate) -> dict:
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "certificate_name": r.certificate_name,
        "issuer": r.issuer,
        "certificate_number": r.certificate_number,
        "issued_date": r.issued_date.isoformat() if r.issued_date else None,
        "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None,
        "remark": r.remark,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _contract_to_dict(r: EmployeeContract, *, mask_salary: bool = False) -> dict:
    """合約序列化；mask_salary=True 時 salary_at_contract 隱藏為 None。

    F-014：合約金額屬薪資範疇敏感資料，需與 salary.py 同等門檻
    （admin/hr 可看全員，其他角色僅可看自己）。
    """
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "contract_type": r.contract_type,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "salary_at_contract": (None if mask_salary else r.salary_at_contract),
        "remark": r.remark,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _ensure_employee(session, employee_id: int) -> Employee:
    emp = session.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)
    return emp


# ============ Educations ============


@router.get("/employees/{employee_id}/educations")
def list_educations(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    with session_scope() as session:
        _ensure_employee(session, employee_id)
        rows = (
            session.query(EmployeeEducation)
            .filter(EmployeeEducation.employee_id == employee_id)
            .order_by(
                EmployeeEducation.is_highest.desc(),
                EmployeeEducation.graduation_date.desc().nullslast(),
            )
            .all()
        )
        return [_edu_to_dict(r) for r in rows]


@router.post("/employees/{employee_id}/educations", status_code=201)
def create_education(
    employee_id: int,
    payload: EducationCreate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    if payload.degree not in DEGREE_VALUES:
        raise HTTPException(
            status_code=400, detail=f"degree 值不合法，允許：{sorted(DEGREE_VALUES)}"
        )
    try:
        with session_scope() as session:
            _ensure_employee(session, employee_id)
            data = payload.model_dump()
            data["graduation_date"] = parse_optional_date(data.get("graduation_date"))
            # 若標記為最高學歷，清除該員工其他紀錄的 is_highest
            if data.get("is_highest"):
                session.query(EmployeeEducation).filter(
                    EmployeeEducation.employee_id == employee_id
                ).update({"is_highest": False})
            row = EmployeeEducation(employee_id=employee_id, **data)
            session.add(row)
            session.flush()
            return _edu_to_dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增學歷失敗")


@router.put("/employees/{employee_id}/educations/{edu_id}")
def update_education(
    employee_id: int,
    edu_id: int,
    payload: EducationUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeEducation)
            .filter(
                EmployeeEducation.id == edu_id,
                EmployeeEducation.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="學歷紀錄不存在")
        data = payload.model_dump(exclude_unset=True)
        if "degree" in data and data["degree"] not in DEGREE_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"degree 值不合法，允許：{sorted(DEGREE_VALUES)}",
            )
        if "graduation_date" in data:
            data["graduation_date"] = parse_optional_date(data["graduation_date"])
        if data.get("is_highest"):
            session.query(EmployeeEducation).filter(
                EmployeeEducation.employee_id == employee_id,
                EmployeeEducation.id != edu_id,
            ).update({"is_highest": False})
        for k, v in data.items():
            setattr(row, k, v)
        session.flush()
        return _edu_to_dict(row)


@router.delete("/employees/{employee_id}/educations/{edu_id}")
def delete_education(
    employee_id: int,
    edu_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeEducation)
            .filter(
                EmployeeEducation.id == edu_id,
                EmployeeEducation.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="學歷紀錄不存在")
        session.delete(row)
        return {"message": "已刪除"}


# ============ Certificates ============


@router.get("/employees/{employee_id}/certificates")
def list_certificates(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    with session_scope() as session:
        _ensure_employee(session, employee_id)
        rows = (
            session.query(EmployeeCertificate)
            .filter(EmployeeCertificate.employee_id == employee_id)
            .order_by(EmployeeCertificate.issued_date.desc().nullslast())
            .all()
        )
        return [_cert_to_dict(r) for r in rows]


@router.post("/employees/{employee_id}/certificates", status_code=201)
def create_certificate(
    employee_id: int,
    payload: CertificateCreate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    try:
        with session_scope() as session:
            _ensure_employee(session, employee_id)
            data = payload.model_dump()
            data["issued_date"] = parse_optional_date(data.get("issued_date"))
            data["expiry_date"] = parse_optional_date(data.get("expiry_date"))
            row = EmployeeCertificate(employee_id=employee_id, **data)
            session.add(row)
            session.flush()
            return _cert_to_dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增證照失敗")


@router.put("/employees/{employee_id}/certificates/{cert_id}")
def update_certificate(
    employee_id: int,
    cert_id: int,
    payload: CertificateUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeCertificate)
            .filter(
                EmployeeCertificate.id == cert_id,
                EmployeeCertificate.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="證照紀錄不存在")
        data = payload.model_dump(exclude_unset=True)
        if "issued_date" in data:
            data["issued_date"] = parse_optional_date(data["issued_date"])
        if "expiry_date" in data:
            data["expiry_date"] = parse_optional_date(data["expiry_date"])
        for k, v in data.items():
            setattr(row, k, v)
        session.flush()
        return _cert_to_dict(row)


@router.delete("/employees/{employee_id}/certificates/{cert_id}")
def delete_certificate(
    employee_id: int,
    cert_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeCertificate)
            .filter(
                EmployeeCertificate.id == cert_id,
                EmployeeCertificate.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="證照紀錄不存在")
        session.delete(row)
        return {"message": "已刪除"}


# ============ Contracts ============


@router.get("/employees/{employee_id}/contracts")
def list_contracts(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    # F-014：非 admin/hr 且非本人時遮罩 salary_at_contract（合約簽訂月薪屬薪資敏感欄位）。
    # 寫入端點（create/update/delete）走 EMPLOYEES_WRITE，僅 admin/hr/supervisor 持有；
    # 此處讀取面用 can_view_salary_of 與 salary.py 維持同一致性。
    from utils.salary_access import can_view_salary_of

    mask_salary = not can_view_salary_of(current_user, employee_id)
    with session_scope() as session:
        _ensure_employee(session, employee_id)
        rows = (
            session.query(EmployeeContract)
            .filter(EmployeeContract.employee_id == employee_id)
            .order_by(EmployeeContract.start_date.desc())
            .all()
        )
        return [_contract_to_dict(r, mask_salary=mask_salary) for r in rows]


@router.post("/employees/{employee_id}/contracts", status_code=201)
def create_contract(
    employee_id: int,
    payload: ContractCreate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    if payload.contract_type not in CONTRACT_TYPE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"contract_type 值不合法，允許：{sorted(CONTRACT_TYPE_VALUES)}",
        )
    start_d = parse_optional_date(payload.start_date)
    if not start_d:
        raise HTTPException(status_code=400, detail="start_date 為必填")
    end_d = parse_optional_date(payload.end_date)
    if end_d and end_d < start_d:
        raise HTTPException(status_code=400, detail="end_date 不可早於 start_date")
    try:
        with session_scope() as session:
            _ensure_employee(session, employee_id)
            data = payload.model_dump()
            data["start_date"] = start_d
            data["end_date"] = end_d
            row = EmployeeContract(employee_id=employee_id, **data)
            session.add(row)
            session.flush()
            return _contract_to_dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增合約失敗")


@router.put("/employees/{employee_id}/contracts/{contract_id}")
def update_contract(
    employee_id: int,
    contract_id: int,
    payload: ContractUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeContract)
            .filter(
                EmployeeContract.id == contract_id,
                EmployeeContract.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="合約紀錄不存在")
        data = payload.model_dump(exclude_unset=True)
        if (
            "contract_type" in data
            and data["contract_type"] not in CONTRACT_TYPE_VALUES
        ):
            raise HTTPException(
                status_code=400,
                detail=f"contract_type 值不合法，允許：{sorted(CONTRACT_TYPE_VALUES)}",
            )
        if "start_date" in data:
            parsed = parse_optional_date(data["start_date"])
            if not parsed:
                raise HTTPException(status_code=400, detail="start_date 不可為空")
            data["start_date"] = parsed
        if "end_date" in data:
            data["end_date"] = parse_optional_date(data["end_date"])
        # 驗證 end_date >= start_date（以最新值為準）
        new_start = data.get("start_date", row.start_date)
        new_end = data.get("end_date", row.end_date)
        if new_end and new_start and new_end < new_start:
            raise HTTPException(status_code=400, detail="end_date 不可早於 start_date")
        for k, v in data.items():
            setattr(row, k, v)
        session.flush()
        return _contract_to_dict(row)


@router.delete("/employees/{employee_id}/contracts/{contract_id}")
def delete_contract(
    employee_id: int,
    contract_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    with session_scope() as session:
        row = (
            session.query(EmployeeContract)
            .filter(
                EmployeeContract.id == contract_id,
                EmployeeContract.employee_id == employee_id,
            )
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="合約紀錄不存在")
        session.delete(row)
        return {"message": "已刪除"}
