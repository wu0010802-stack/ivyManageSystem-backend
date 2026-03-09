from models.database import get_session, Employee, JobTitle

def migrate_title_data():
    session = get_session()
    employees = session.query(Employee).all()
    
    count = 0
    for emp in employees:
        if emp.title:
            # Find or create JobTitle
            job_title = session.query(JobTitle).filter(JobTitle.name == emp.title).first()
            if not job_title:
                print(f"Creating missing job title: {emp.title}")
                job_title = JobTitle(name=emp.title, is_active=True)
                session.add(job_title)
                session.flush() # Get ID
            
            emp.job_title_id = job_title.id
            count += 1
            
    session.commit()
    print(f"Migrated title data for {count} employees.")

if __name__ == "__main__":
    migrate_title_data()
