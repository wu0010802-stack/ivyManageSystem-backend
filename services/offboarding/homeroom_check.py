"""離職時偵測仍掛該員工的 active 班級導師綁定（head/assistant/art）。

對稱於 api/classrooms.delete_classroom（班級停用會解綁三類導師）：員工終態這側
補上偵測與提示。**不清空欄位**——若離職時把 active 班級導師直接設 NULL，班級會
瞬間無導師；正解是標記「需改派」交由 HR 處理。dangling 綁定若不處理，salary
breakdown_enrollment 以 head_teacher_id 配對班級在學人數時會仍歸屬給離職教師。
"""

from __future__ import annotations

from sqlalchemy import or_

from models.classroom import Classroom


def detect_dangling_homeroom_assignments(session, employee_id: int) -> list[dict]:
    """回傳仍將 employee_id 列為導師的 active 班級清單（不修改任何資料）。

    每筆：{classroom_id, classroom_name, roles: [head|assistant|art]}。
    """
    rows = (
        session.query(Classroom)
        .filter(
            Classroom.is_active.is_(True),
            or_(
                Classroom.head_teacher_id == employee_id,
                Classroom.assistant_teacher_id == employee_id,
                Classroom.art_teacher_id == employee_id,
            ),
        )
        .order_by(Classroom.id.asc())
        .all()
    )
    result: list[dict] = []
    for c in rows:
        roles = []
        if c.head_teacher_id == employee_id:
            roles.append("head")
        if c.assistant_teacher_id == employee_id:
            roles.append("assistant")
        if c.art_teacher_id == employee_id:
            roles.append("art")
        result.append({"classroom_id": c.id, "classroom_name": c.name, "roles": roles})
    return result
