"""Repository 層 —— 資料存取層抽象。

目標：
  - 將 SQLAlchemy ORM 查詢封裝在 Repository 類別，讓 service/router 不直接
    依賴 session.query(Model)；service 改依賴 Repository 介面。
  - 集中 joinedload / selectinload 策略，避免 N+1 散落於各 endpoint。
  - 方便未來對 DB 層做 mocking / swap（例如單元測試可注入 stub）。

試點：employee、student。其他模組可依需求逐步遷移。
"""

from repositories.base import BaseRepository
from repositories.employee import EmployeeRepository
from repositories.student import StudentRepository

__all__ = ["BaseRepository", "EmployeeRepository", "StudentRepository"]
