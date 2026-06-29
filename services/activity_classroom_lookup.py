"""services/activity_classroom_lookup.py — 才藝報名班級反查（F2 第九階段抽出）。

從 api/activity/_shared.py 抽出 2 個 helper：
- _get_active_classroom — 依名稱取得啟用中的班級（None 不拋）
- _require_active_classroom — 取得啟用中班級，不存在則拋 HTTPException(400)

api/activity/_shared.py 保留 re-export 維持既有 import surface
（registrations.py / public.py / settings.py 等多模組共用）。
"""

from fastapi import HTTPException

from models.database import Classroom


def _get_active_classroom(
    session, classroom_name: str, school_year=None, semester=None
):
    """依名稱取得啟用中的班級。

    Classroom 有 uq(school_year, semester, name)，同名班級可跨學期各一筆且同時
    啟用（學期交接期）。帶 school_year/semester 時精確收斂到該學期，避免 `.first()`
    任意取到舊學期那筆而綁錯班級 FK（2026-06-29 才藝點名稽核 F3）。
    兩者省略則沿用 name+is_active，向後相容既有 caller。
    """
    query = session.query(Classroom).filter(
        Classroom.name == classroom_name.strip(),
        Classroom.is_active.is_(True),
    )
    if school_year is not None:
        query = query.filter(Classroom.school_year == school_year)
    if semester is not None:
        query = query.filter(Classroom.semester == semester)
    return query.first()


def _require_active_classroom(
    session, classroom_name: str, school_year=None, semester=None
):
    """取得啟用中班級，不存在則拋 HTTPException(400)。"""
    c = _get_active_classroom(session, classroom_name, school_year, semester)
    if not c:
        raise HTTPException(status_code=400, detail="班級不存在或已停用")
    return c
