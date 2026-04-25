"""api/parent_portal/_shared.py — 家長端共享 helper 與 schema

關鍵 IDOR 防護函式：所有家長端 endpoint 接受 student_id / leave_request_id
/ fee_record_id 等資源 id 時，必經 _assert_student_owned，確保家長只能
看自己小孩的資料。

JWT 不快取 guardian_ids / student_ids（見 plan B.4），每個 request 即時
從 DB 撈，避免 add/remove 小孩產生 stale token 問題。
"""

from fastapi import HTTPException

from models.database import Guardian, User


def _get_parent_user(session, current_user: dict) -> User:
    """從 JWT payload 取出家長 User；非 parent role 一律 403。"""
    if current_user.get("role") != "parent":
        raise HTTPException(status_code=403, detail="此 API 僅限家長端使用")
    user_id = current_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=403, detail="缺少 user_id")
    user = session.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="使用者不存在")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="使用者已停用")
    return user


def _get_parent_student_ids(session, user_id: int) -> tuple[list[int], list[int]]:
    """回傳 (guardian_ids, student_ids)。

    僅算 deleted_at IS NULL 的活的監護人關係；同一 student_id 可能對應多筆
    Guardian（罕見但允許），用 set 去重後轉 list。
    """
    rows = (
        session.query(Guardian.id, Guardian.student_id)
        .filter(Guardian.user_id == user_id, Guardian.deleted_at.is_(None))
        .all()
    )
    guardian_ids = [r[0] for r in rows]
    student_ids = list({r[1] for r in rows})
    return guardian_ids, student_ids


def _assert_student_owned(session, user_id: int, student_id: int) -> None:
    """非自己小孩 → 403。所有接受 student_id 的家長 endpoint 必經此檢查。"""
    _, student_ids = _get_parent_student_ids(session, user_id)
    if student_id not in student_ids:
        raise HTTPException(status_code=403, detail="此學生不屬於您")
