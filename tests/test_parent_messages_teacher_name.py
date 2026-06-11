"""驗證家長端訊息列表的 teacher_name 不外洩 username。

威脅：原 _thread_summary_from_maps 用 `teacher.username`（內部登入帳號 emp_xxx）
回給家長端，屬於資料分類錯誤；攻擊者在橫向滲透時可拿到 username 字典做
brute-force / phishing。

修法：name 解析優先序 employee.name → user.display_name → "老師"，
username 任何情況下不外洩。

Refs: 資安掃描 2026-05-07 P1。
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.parent_portal.messages import _thread_summary_from_maps
from models.database import Employee, ParentMessageThread, Student, User


def _make_thread() -> ParentMessageThread:
    t = ParentMessageThread(
        id=1,
        student_id=10,
        teacher_user_id=99,
        last_message_at=datetime(2026, 5, 7, 10, 0, 0),
        parent_last_read_at=None,
    )
    return t


def _make_student() -> Student:
    return Student(id=10, name="王小明")


def _make_user(
    *, username="t_internal_001", display_name=None, employee_id=None
) -> User:
    u = User(
        id=99,
        username=username,
        password_hash="!",
        role="teacher",
        permission_names=[],
        is_active=True,
        display_name=display_name,
        employee_id=employee_id,
    )
    return u


def _make_emp(name="王老師") -> Employee:
    return Employee(id=42, employee_id="E001", name=name, is_active=True, base_salary=0)


class TestTeacherNameResolution:
    def test_uses_employee_name_when_available(self):
        """有 employee.name 時優先使用（員工正式姓名）"""
        result = _thread_summary_from_maps(
            thread=_make_thread(),
            student=_make_student(),
            teacher=_make_user(username="t_secret_001", employee_id=42),
            teacher_employee=_make_emp("陳老師"),
            last_message=None,
            unread_count=0,
        )
        assert result["teacher_name"] == "陳老師"
        # 絕對不能漏出 username
        assert "t_secret_001" not in str(result.values())

    def test_falls_back_to_display_name_when_no_employee(self):
        """沒接 employee 時用 user.display_name（家長端 LIFF 設定的暱稱）"""
        result = _thread_summary_from_maps(
            thread=_make_thread(),
            student=_make_student(),
            teacher=_make_user(username="t_secret_002", display_name="李老師"),
            teacher_employee=None,
            last_message=None,
            unread_count=0,
        )
        assert result["teacher_name"] == "李老師"
        assert "t_secret_002" not in str(result.values())

    def test_falls_back_to_generic_label_when_no_name_available(self):
        """employee 與 display_name 都缺時用「老師」通用標籤；不洩漏 username"""
        result = _thread_summary_from_maps(
            thread=_make_thread(),
            student=_make_student(),
            teacher=_make_user(username="t_internal_emp_007"),
            teacher_employee=None,
            last_message=None,
            unread_count=0,
        )
        assert result["teacher_name"] == "老師"
        assert "t_internal_emp_007" not in str(result.values())

    def test_no_teacher_returns_none(self):
        """thread 無對應 teacher（資料異常）→ None，不丟例外"""
        result = _thread_summary_from_maps(
            thread=_make_thread(),
            student=_make_student(),
            teacher=None,
            teacher_employee=None,
            last_message=None,
            unread_count=0,
        )
        assert result["teacher_name"] is None

    def test_empty_employee_name_falls_through(self):
        """employee 存在但 name 為空字串 → 退到 display_name"""
        result = _thread_summary_from_maps(
            thread=_make_thread(),
            student=_make_student(),
            teacher=_make_user(username="t_x", display_name="王老師"),
            teacher_employee=_make_emp(""),
            last_message=None,
            unread_count=0,
        )
        assert result["teacher_name"] == "王老師"


def test_portal_resolve_teacher_display_name_uses_employee_name():
    """R6-2（教師端對稱修補）：api/portal/parent_messages._resolve_teacher_display_name
    用 Employee.name，絕不回 User.username（=員工工號/登入帳號，會經 renderer 送家長）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from models.base import Base
    from models.database import Employee, User
    from api.portal.parent_messages import _resolve_teacher_display_name

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        emp = Employee(employee_id="E001", name="陳老師", is_active=True, base_salary=0)
        s.add(emp)
        s.flush()
        u = User(
            username="t_secret_001",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            is_active=True,
        )
        s.add(u)
        s.flush()
        name = _resolve_teacher_display_name(s, u.id)
        assert name == "陳老師"
        assert "t_secret_001" not in name
    finally:
        s.close()
        engine.dispose()
