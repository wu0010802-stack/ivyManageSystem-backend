import sys
import os

# Ensure backend directory is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models.database import get_session, Employee

def list_employees():
    session = get_session()
    try:
        employees = session.query(Employee).order_by(Employee.id).all()
        print(f"{'ID':<5} {'EmpID':<10} {'Name':<10} {'Title':<15} {'Status':<10}")
        print("-" * 60)
        for emp in employees:
            title = emp.title
            if not title and emp.job_title_rel:
                title = emp.job_title_rel.name
            
            # Filter: Exclude '美師' and empty titles
            if not title or title.strip() == "" or "美師" in title:
                continue

            status = "Active" if emp.is_active else "Inactive"
            print(f"{emp.id:<5} {emp.employee_id or '-':<10} {emp.name:<10} {title or '-':<15} {status:<10}")
    finally:
        session.close()

if __name__ == "__main__":
    list_employees()
