import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from models.database import DEFAULT_DATABASE_URL
from urllib.parse import urlparse

def update_schema():
    """更新資料庫 Schema"""
    url = urlparse(DEFAULT_DATABASE_URL)
    conn_params = {
        'host': url.hostname,
        'port': url.port,
        'user': url.username,
        'password': url.password,
        'dbname': url.path[1:]
    }
    
    conn = psycopg2.connect(**conn_params)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    
    try:
        # 1. 新增 Student.status_tag
        print("正在新增 Student.status_tag...")
        cur.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='students' AND column_name='status_tag') THEN 
                    ALTER TABLE students ADD COLUMN status_tag VARCHAR(50);
                    COMMENT ON COLUMN students.status_tag IS '狀態標籤 (新生/不足齡/特殊生等)';
                END IF; 
            END $$;
        """)
        
        # 2. 新增 Classroom.art_teacher_id
        print("正在新增 Classroom.art_teacher_id...")
        cur.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='classrooms' AND column_name='art_teacher_id') THEN 
                    ALTER TABLE classrooms ADD COLUMN art_teacher_id INTEGER;
                    ALTER TABLE classrooms ADD CONSTRAINT fk_classrooms_art_teacher FOREIGN KEY (art_teacher_id) REFERENCES employees(id);
                    COMMENT ON COLUMN classrooms.art_teacher_id IS '美師';
                END IF; 
            END $$;
        """)

        # 3. 新增 Classroom.class_code
        print("正在新增 Classroom.class_code...")
        cur.execute("""
            DO $$ 
            BEGIN 
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='classrooms' AND column_name='class_code') THEN 
                    ALTER TABLE classrooms ADD COLUMN class_code VARCHAR(20);
                    COMMENT ON COLUMN classrooms.class_code IS '班級代號 (如 114-11)';
                END IF; 
            END $$;
        """)

        print("資料庫 Schema 更新完成！")
        
    except Exception as e:
        print(f"更新失敗: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    update_schema()
