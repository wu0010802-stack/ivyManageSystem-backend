import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from models.database import init_database, DEFAULT_DATABASE_URL
from urllib.parse import urlparse

def create_database():
    """建立資料庫（如果不存在）"""
    url = urlparse(DEFAULT_DATABASE_URL)
    db_name = url.path[1:]  # 移除斜線取得資料庫名稱
    
    # 連線到預設的 'postgres' 資料庫以執行 CREATE DATABASE
    conn_params = {
        'host': url.hostname,
        'port': url.port,
        'user': url.username,
        'password': url.password,
        'dbname': 'postgres'
    }
    
    try:
        conn = psycopg2.connect(**conn_params)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        
        # 檢查資料庫是否存在
        cur.execute(f"SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{db_name}'")
        exists = cur.fetchone()
        
        if not exists:
            print(f"正在建立資料庫 '{db_name}'...")
            cur.execute(f"CREATE DATABASE {db_name}")
            print(f"資料庫 '{db_name}' 建立成功！")
        else:
            print(f"資料庫 '{db_name}' 已存在。")
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"建立資料庫時發生錯誤: {e}")
        return False
    return True

def init_tables():
    """初始化資料表"""
    try:
        print("正在初始化資料表...")
        engine, Session = init_database()
        print("資料表初始化完成！")
    except Exception as e:
        print(f"初始化資料表時發生錯誤: {e}")

if __name__ == "__main__":
    if create_database():
        init_tables()
