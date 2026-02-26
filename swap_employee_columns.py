from models.database import get_session, Employee

def swap_columns():
    session = get_session()
    employees = session.query(Employee).all()
    
    count = 0
    for emp in employees:
        # Swap values
        original_title = emp.title
        original_position = emp.position
        
        emp.title = original_position
        emp.position = original_title
        count += 1
        
    session.commit()
    print(f"Swapped title and position for {count} employees.")

if __name__ == "__main__":
    swap_columns()
