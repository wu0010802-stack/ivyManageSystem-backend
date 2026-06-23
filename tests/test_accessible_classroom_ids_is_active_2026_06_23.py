"""accessible_classroom_ids 須過濾 Classroom.is_active=False，與 portal 對齊。

qa-loop #12（2026-06-23）：accessible_classroom_ids 以 head/assistant/art_teacher_id
比對 Classroom 但未加 Classroom.is_active==True 過濾；portal 端 _get_teacher_classroom_ids
（api/portal/_shared.py）則有此過濾。持 STUDENTS_READ:own_class 的教師可能透過 admin
側 scoped 端點看到已停用班級的學生。屬前後端 scope 來源口徑漂移的縱深防禦缺口，補齊
使兩端一致。
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom
from models.employee import Employee
from utils.portfolio_access import accessible_classroom_ids


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "acc_classroom_is_active.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    s = sf()
    yield s
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_sf
    engine.dispose()


def test_accessible_classroom_ids_excludes_inactive(db_session):
    s = db_session
    teacher = Employee(employee_id="T1", name="王老師")
    s.add(teacher)
    s.flush()
    active = Classroom(
        name="星星班",
        school_year=113,
        semester=2,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    inactive = Classroom(
        name="停用班",
        school_year=113,
        semester=2,
        head_teacher_id=teacher.id,
        is_active=False,
    )
    s.add_all([active, inactive])
    s.commit()

    user = {
        "role": "teacher",
        "employee_id": teacher.id,
        "permission_names": ["STUDENTS_READ:own_class"],
    }
    ids = accessible_classroom_ids(s, user)
    assert active.id in ids
    assert (
        inactive.id not in ids
    ), "已停用班級不應出現在可存取班級清單（與 portal 對齊）"
