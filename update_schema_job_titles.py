from database import engine, Base, JobTitle
from sqlalchemy import inspect

def create_job_titles_table():
    inspector = inspect(engine)
    if not inspector.has_table("job_titles"):
        print("Creating job_titles table...")
        JobTitle.__table__.create(bind=engine)
        print("Table created.")
    else:
        print("Table job_titles already exists.")

if __name__ == "__main__":
    create_job_titles_table()
