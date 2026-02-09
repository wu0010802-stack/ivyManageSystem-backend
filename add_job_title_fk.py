from sqlalchemy import create_engine, text
from models.database import DEFAULT_DATABASE_URL

def add_job_title_fk():
    engine = create_engine(DEFAULT_DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE employees ADD COLUMN IF NOT EXISTS job_title_id INTEGER REFERENCES job_titles(id);"))
        conn.commit()
    print("Added job_title_id column to employees table.")

if __name__ == "__main__":
    add_job_title_fk()
