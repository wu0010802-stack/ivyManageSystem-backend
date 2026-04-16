"""
startup/ — 應用程式啟動邏輯模組

拆分自 main.py，包含：
  - seed.py       預設資料 seed（年級、職稱、設定、班別、管理員、審核、才藝）
  - migrations.py Alembic migration + 資料遷移（學年度、權限位元）
  - bootstrap.py  啟動編排（呼叫 seed + migration + 服務初始化）
"""
