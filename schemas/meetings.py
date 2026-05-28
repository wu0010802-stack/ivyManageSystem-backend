"""Meetings router (api/meetings.py) Out schemas。

Phase 3 範圍：
- POST /meetings → MutationResultOut (re-use)
- POST /meetings/batch → MeetingBatchCreateOut (message + count)
- PUT /meetings/{id} → DeleteResultOut (純 message)
- DELETE /meetings/{id} → DeleteResultOut

Out of scope (Phase 3.5)：
- GET /meetings (list with date/employee filters)
- GET /meetings/summary (彙總統計)
"""

from __future__ import annotations

from schemas._base import IvyBaseModel


class MeetingBatchCreateOut(IvyBaseModel):
    """POST /meetings/batch — 批次建立會議紀錄回傳。"""

    message: str
    count: int
