"""services/activity_classroom_lookup.py — 才藝報名班級反查（F2 第九階段抽出）。

從 api/activity/_shared.py 抽出 2 個 helper：
- _get_active_classroom — 依名稱取得啟用中的班級（None 不拋）
- _require_active_classroom — 取得啟用中班級，不存在則拋 HTTPException(400)

api/activity/_shared.py 保留 re-export 維持既有 import surface
（registrations.py / public.py / settings.py 等多模組共用）。
"""

from fastapi import HTTPException

from models.database import Classroom


def _get_active_classroom(session, classroom_name: str):
    """依名稱取得啟用中的班級。"""
    return (
        session.query(Classroom)
        .filter(
            Classroom.name == classroom_name.strip(),
            Classroom.is_active.is_(True),
        )
        .first()
    )


def _require_active_classroom(session, classroom_name: str):
    """取得啟用中班級，不存在則拋 HTTPException(400)。"""
    c = _get_active_classroom(session, classroom_name)
    if not c:
        raise HTTPException(status_code=400, detail="班級不存在或已停用")
    return c
