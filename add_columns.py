import psycopg2
from psycopg2 import sql

DB_URL = "dbname='kindergarten_payroll' user='yilunwu' host='localhost' port='5432'"

def migrate():
    try:
        conn = psycopg2.connect(DB_URL)
        conn.autocommit = True
        cur = conn.cursor()

        # Add title column
        try:
            cur.execute("ALTER TABLE employees ADD COLUMN title VARCHAR(50);")
            print("Added 'title' column.")
        except psycopg2.errors.DuplicateColumn:
            print("'title' column already exists.")

        # Add class_name column
        try:
            cur.execute("ALTER TABLE employees ADD COLUMN class_name VARCHAR(50);")
            print("Added 'class_name' column.")
        except psycopg2.errors.DuplicateColumn:
            print("'class_name' column already exists.")

        cur.close()
        conn.close()
        print("Migration complete.")
    except Exception as e:
        print(f"Migration failed: {e}")

if __name__ == "__main__":
    migrate()
