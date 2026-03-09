import sys
import os

# Ensure backend directory is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from models.database import init_database, get_session, Employee, AllowanceType, EmployeeAllowance, EmployeeType

def migrate_allowances():
    session = get_session()
    
    # 1. Ensure AllowanceTypes exist
    types = {
        'supervisor': {'code': 'supervisor', 'name': '主管加給'},
        'teacher': {'code': 'teacher', 'name': '導師津貼'},
        'meal': {'code': 'meal', 'name': '伙食津貼'},
        'transportation': {'code': 'transportation', 'name': '交通津貼'},
        'other': {'code': 'other', 'name': '其他津貼'},
    }
    
    type_map = {} # code -> AllowanceType object
    
    for key, data in types.items():
        at = session.query(AllowanceType).filter_by(code=data['code']).first()
        if not at:
            at = AllowanceType(code=data['code'], name=data['name'])
            session.add(at)
            session.flush() # get id
        type_map[key] = at
    
    session.commit()
    print("Allowance Types verified.")
    
    # 2. Migrate Employee Data
    employees = session.query(Employee).all()
    count = 0
    
    for emp in employees:
        # Supervisor Allowance
        if emp.supervisor_allowance and emp.supervisor_allowance > 0:
            val = emp.supervisor_allowance
            ea = EmployeeAllowance(employee_id=emp.id, allowance_type_id=type_map['supervisor'].id, amount=val)
            session.add(ea)
            emp.supervisor_allowance = 0 # Clear old column to prevent double counting
            print(f"Migrated Supervisor Allowance for {emp.name}: {val}")

        # Teacher Allowance
        if emp.teacher_allowance and emp.teacher_allowance > 0:
            val = emp.teacher_allowance
            ea = EmployeeAllowance(employee_id=emp.id, allowance_type_id=type_map['teacher'].id, amount=val)
            session.add(ea)
            emp.teacher_allowance = 0
            print(f"Migrated Teacher Allowance for {emp.name}: {val}")
            
        # Meal Allowance
        if emp.meal_allowance and emp.meal_allowance > 0:
            val = emp.meal_allowance
            ea = EmployeeAllowance(employee_id=emp.id, allowance_type_id=type_map['meal'].id, amount=val)
            session.add(ea)
            emp.meal_allowance = 0
            print(f"Migrated Meal Allowance for {emp.name}: {val}")

        # Transportation Allowance
        if emp.transportation_allowance and emp.transportation_allowance > 0:
            val = emp.transportation_allowance
            ea = EmployeeAllowance(employee_id=emp.id, allowance_type_id=type_map['transportation'].id, amount=val)
            session.add(ea)
            emp.transportation_allowance = 0
            print(f"Migrated Transportation Allowance for {emp.name}: {val}")

        # Other Allowance
        if emp.other_allowance and emp.other_allowance > 0:
            val = emp.other_allowance
            ea = EmployeeAllowance(employee_id=emp.id, allowance_type_id=type_map['other'].id, amount=val)
            session.add(ea)
            emp.other_allowance = 0
            print(f"Migrated Other Allowance for {emp.name}: {val}")

        count += 1
        
    session.commit()
    session.close()
    print(f"Migration completed for {count} employees.")

if __name__ == "__main__":
    migrate_allowances()
