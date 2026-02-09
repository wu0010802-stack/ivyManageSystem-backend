from models.database import get_session, Employee, JobTitle
from sqlalchemy import func

def migrate_titles():
    session = get_session()
    try:
        # Get all employees
        employees = session.query(Employee).all()
        print(f"Found {len(employees)} employees.")
        
        migrated_count = 0
        
        for emp in employees:
            if not emp.title:
                continue
                
            # Find or create job title
            title_name = emp.title.strip()
            if not title_name:
                continue
                
            job_title = session.query(JobTitle).filter(JobTitle.name == title_name).first()
            
            if not job_title:
                print(f"Creating new job title: {title_name}")
                job_title = JobTitle(name=title_name, is_active=True)
                session.add(job_title)
                session.flush() # Get ID
            
            if emp.job_title_id != job_title.id:
                old_id = emp.job_title_id
                emp.job_title_id = job_title.id
                print(f"Migrated employee {emp.name}: {old_id} -> {job_title.id} ({title_name})")
                migrated_count += 1
                
        session.commit()
        print(f"Migration completed. Updated {migrated_count} employees.")
        
    except Exception as e:
        session.rollback()
        print(f"Migration failed: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    migrate_titles()
