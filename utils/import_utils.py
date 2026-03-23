"""
批量匯入共用工具函式，供 leaves、overtimes 等批次匯入端點使用。
"""

from models.database import Employee


def build_employee_lookup(session) -> tuple[dict, dict]:
    """載入所有在職員工，回傳 (by_id, by_name) 兩個查詢字典。"""
    employees = session.query(Employee).filter(Employee.is_active == True).all()
    emp_by_id = {str(e.employee_id): e for e in employees}
    emp_by_name = {e.name: e for e in employees}
    return emp_by_id, emp_by_name


def resolve_employee_from_row(row, emp_by_id: dict, emp_by_name: dict):
    """從 Excel 列中解析員工物件，優先使用編號，不符時以姓名查詢。

    找不到員工時拋出 ValueError（呼叫端應在 try/except 中捕捉以記入 errors 清單）。
    """
    emp_id_str = str(row.get("員工編號", "")).strip()
    emp_name_str = str(row.get("員工姓名", "")).strip()
    emp = None
    if emp_id_str and emp_id_str not in ("nan", ""):
        emp = emp_by_id.get(emp_id_str)
    if emp is None and emp_name_str and emp_name_str not in ("nan", ""):
        emp = emp_by_name.get(emp_name_str)
    if emp is None:
        raise ValueError(f"找不到員工（編號:{emp_id_str}，姓名:{emp_name_str}）")
    return emp
