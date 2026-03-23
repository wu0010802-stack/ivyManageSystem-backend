"""
models/auth.py — 用戶認證模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, BigInteger, DateTime, Boolean, ForeignKey, Index
from sqlalchemy.orm import relationship

from models.base import Base


class User(Base):
    """用戶認證表"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), unique=True, nullable=True, comment="關聯員工ID")
    username = Column(String(50), unique=True, nullable=False, comment="登入帳號")
    password_hash = Column(String(255), nullable=False, comment="密碼雜湊")
    role = Column(String(20), default="teacher", comment="角色: teacher/admin")
    permissions = Column(BigInteger, nullable=True, default=None, comment="功能模組權限位元遮罩 (-1=全部權限, NULL=使用角色預設)")
    is_active = Column(Boolean, default=True, comment="帳號是否啟用")
    must_change_password = Column(Boolean, default=False, comment="是否強制下次登入修改密碼")
    token_version = Column(Integer, default=0, nullable=False, comment="Token 版本號；帳號停用或權限變更時遞增，使所有現有 Token 無法刷新")
    last_login = Column(DateTime, comment="最後登入時間")
    line_user_id = Column(String(100), nullable=True, unique=True, index=True, comment="綁定的 LINE User ID")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_user_emp_active", "employee_id", "is_active"),
    )

    employee = relationship("Employee", backref="user_account")
