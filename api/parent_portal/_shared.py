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


_DEFAULT_PARENT_DISPLAY_NAME = "家長"


def resolve_parent_display_name(session, user: User) -> str:
    """家長 hero / 問候語顯示名解析。

    優先序：
        1. user.display_name —— LIFF 登入時寫入的 LINE displayName（個人化最強）
        2. Guardian.is_primary=True 的 Guardian.name —— 行政建檔的真實姓名
        3. 最早一筆 Guardian.name —— 至少有名字
        4. "家長" —— 全 fallback

    絕不回傳 user.username，那是 `parent_line_<line_user_id>` 內部識別碼。
    """
    if user.display_name and user.display_name.strip():
        return user.display_name.strip()

    rows = (
        session.query(Guardian.name, Guardian.is_primary, Guardian.created_at)
        .filter(
            Guardian.user_id == user.id,
            Guardian.deleted_at.is_(None),
        )
        .order_by(Guardian.is_primary.desc(), Guardian.created_at.asc())
        .all()
    )
    for name, _is_primary, _created in rows:
        if name and name.strip():
            return name.strip()
    return _DEFAULT_PARENT_DISPLAY_NAME
