"""Classrooms router (api/classrooms.py) 對應 Out schemas。

Phase 2 範圍（本檔）：
- POST /classrooms → MutationResultOut
- PUT /classrooms/{id} → ClassroomUpdateResultOut (含 name)
- DELETE /classrooms/{id} → MutationResultOut
- GET /classrooms/teacher-options → list[TeacherOptionOut]
- GET /grades → list[GradeOut]
- PATCH /grades/{id} → GradeOut

Out of scope (Phase 2.5)：
- GET /classrooms (etag_response wrap)
- GET /classrooms/{id} (_serialize_classroom_detail 巨大 nested)
- GET /classrooms/{id}/enrollment-composition (複雜統計)
- POST /classrooms/clone-term + /promote-academic-year (跨班級複製結果)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class TeacherOptionOut(IvyBaseModel):
    """GET /classrooms/teacher-options 單筆。"""

    id: int
    name: str  # pii-allow: 老師姓名（教師端必看）


class ClassroomUpdateResultOut(IvyBaseModel):
    """PUT /classrooms/{id} 回傳 — message + id + name。"""

    message: str
    id: int
    name: str


class GradeOut(IvyBaseModel):
    """年級 (GET /grades list 單筆 / PATCH /grades/{id} 回傳)。

    PATCH 不回 age_range / sort_order，這兩欄為 Optional。
    """

    id: int
    name: str
    age_range: Optional[str] = None
    sort_order: Optional[int] = None
    is_graduation_grade: bool
