"""離職時偵測仍掛該員工的 active 班級導師綁定（#5 標記待改派）。

對稱於 api/classrooms.delete_classroom 會解綁三類導師；員工終態這側補偵測。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.classroom import Classroom
from services.offboarding.homeroom_check import detect_dangling_homeroom_assignments


def test_detects_active_classrooms_with_employee_as_teacher(test_db_session):
    s = test_db_session
    s.add(Classroom(id=1, name="兔兔班", is_active=True, head_teacher_id=7))
    s.add(
        Classroom(
            id=2,
            name="貓貓班",
            is_active=True,
            assistant_teacher_id=7,
            art_teacher_id=7,
        )
    )
    s.add(Classroom(id=3, name="狗狗班", is_active=True, head_teacher_id=99))  # 他人
    s.add(
        Classroom(id=4, name="停用班", is_active=False, head_teacher_id=7)
    )  # 停用不算
    s.commit()

    result = detect_dangling_homeroom_assignments(s, 7)

    assert len(result) == 2
    assert result[0] == {
        "classroom_id": 1,
        "classroom_name": "兔兔班",
        "roles": ["head"],
    }
    assert result[1]["classroom_id"] == 2
    assert result[1]["roles"] == ["assistant", "art"]


def test_no_assignments_returns_empty(test_db_session):
    s = test_db_session
    s.add(Classroom(id=1, name="兔兔班", is_active=True, head_teacher_id=99))
    s.commit()
    assert detect_dangling_homeroom_assignments(s, 7) == []
