"""
新增 is_office_staff 欄位到 employees 表
"""

from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"

def migrate():
    engine = create_engine(DATABASE_URL)

    with engine.connect() as conn:
        # 檢查欄位是否已存在
        result = conn.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'employees' AND column_name = 'is_office_staff'
        """))

        if result.fetchone() is None:
            # 新增欄位
            conn.execute(text("""
                ALTER TABLE employees
                ADD COLUMN is_office_staff BOOLEAN DEFAULT FALSE
            """))
            conn.commit()
            print("已新增 is_office_staff 欄位")
        else:
            print("is_office_staff 欄位已存在")

if __name__ == "__main__":
    migrate()
