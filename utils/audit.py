"""
Audit logging middleware - automatically records all data-modifying API requests
"""

import asyncio
import json
import logging
import re
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from models.database import get_session, AuditLog

logger = logging.getLogger(__name__)

# Hold strong refs to fire-and-forget audit tasks so the event loop
# does not drop them before they finish (asyncio gotcha).
_background_tasks: "set[asyncio.Task]" = set()

# HTTP method → action mapping
METHOD_ACTION_MAP = {
    "POST": "CREATE",
    "PUT": "UPDATE",
    "PATCH": "UPDATE",
    "DELETE": "DELETE",
}

# URL path → entity_type mapping (order matters, first match wins)
ENTITY_PATTERNS = [
    (r"/api/auth/users", "user"),
    (r"/api/auth/impersonate", "user"),
    (r"/api/auth/end-impersonate", "user"),
    (r"/api/auth/change-password", "user"),
    (r"/api/attendance", "attendance"),
    (r"/api/employees", "employee"),
    (r"/api/students", "student"),
    (r"/api/classrooms", "classroom"),
    (r"/api/leaves", "leave"),
    (r"/api/overtimes", "overtime"),
    (r"/api/salaries", "salary"),
    (r"/api/salary", "salary"),
    (r"/api/config/titles", "job_title"),
    (r"/api/config/deduction-types", "deduction_type"),
    (r"/api/config/bonus-types", "bonus_type"),
    (r"/api/config", "config"),
    (r"/api/meetings", "meeting"),
    (r"/api/announcements", "announcement"),
    (r"/api/calendar", "calendar"),
    (r"/api/schedule", "schedule"),
    (r"/api/portal/swap", "shift_swap"),
    # 才藝系統：細粒度分類，POS 日結必須排在 /api/activity/pos 之前（first match wins）
    (r"/api/activity/pos/daily-close", "activity_daily_close"),
    (r"/api/activity/pos", "activity_pos"),
    (r"/api/activity/registrations", "activity_registration"),
    (r"/api/activity/waitlist", "activity_registration"),
    (r"/api/activity/courses", "activity_course"),
    (r"/api/activity/supplies", "activity_supply"),
    (r"/api/activity/inquiries", "activity_inquiry"),
    (r"/api/activity/sessions", "activity_session"),
    (r"/api/activity/settings", "activity_settings"),
]

# Skip these paths (login should not be audited as sensitive)
SKIP_PATHS = {"/api/auth/login"}

# entity_type → 中文 label。同時作為 /audit-logs/meta 的 source of truth
# 與前端下拉選項同步。新增 entity_type 請只在此處增補一次。
ENTITY_LABELS = {
    "employee": "員工",
    "student": "學生",
    "attendance": "考勤",
    "leave": "請假",
    "overtime": "加班",
    "classroom": "班級",
    "salary": "薪資",
    "config": "系統設定",
    "user": "使用者帳號",
    "job_title": "職稱",
    "meeting": "會議",
    "announcement": "公告",
    "calendar": "行事曆",
    "schedule": "班表",
    "shift_swap": "換班",
    "deduction_type": "扣款類型",
    "bonus_type": "獎金類型",
    "activity_registration": "才藝報名",
    "activity_course": "才藝課程",
    "activity_supply": "才藝教具",
    "activity_inquiry": "才藝詢問",
    "activity_session": "才藝點名",
    "activity_pos": "才藝 POS",
    "activity_daily_close": "POS 日結",
    "activity_settings": "才藝設定",
}

ACTION_LABELS = {
    "CREATE": "新增",
    "UPDATE": "修改",
    "DELETE": "刪除",
    "EXPORT": "匯出",
}


def _parse_entity_type(path):
    """從 URL path 解析 entity_type"""
    for pattern, entity_type in ENTITY_PATTERNS:
        if re.match(pattern, path):
            return entity_type
    return None


def _parse_entity_id(path):
    """從 URL path 尾部提取數字 ID"""
    # Match patterns like /api/employees/5 or /api/leaves/12/approve
    match = re.search(r"/(\d+)(?:/[a-z-]+)?$", path)
    return match.group(1) if match else None


def _build_summary(method, path, entity_type):
    """產生人類可讀的操作摘要"""
    action = METHOD_ACTION_MAP.get(method, method)

    # Special cases
    if "/approve" in path:
        return f"審核{entity_type}記錄"
    if "/reset-password" in path:
        return "重設使用者密碼"
    if "/change-password" in path:
        return "修改密碼"
    if "/impersonate" in path:
        return "切換使用者身份"
    if "/upload" in path:
        return "上傳考勤資料"
    if "/calculate" in path:
        return "計算薪資"

    action_zh = ACTION_LABELS.get(action, action)
    entity_zh = ENTITY_LABELS.get(entity_type, entity_type)

    return f"{action_zh}{entity_zh}"


def _extract_user_from_header(request: Request):
    """從 Cookie 或 Authorization header 靜默解析 JWT，不拋錯"""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]

    if not token:
        return None, None

    try:
        from jose import jwt
        from utils.auth import JWT_SECRET_KEY, JWT_ALGORITHM

        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("user_id"), payload.get("name")
    except Exception:
        return None, None


def _write_audit_sync(payload: dict) -> None:
    """在 threadpool 中執行的同步寫入，不可拋出例外到上層。"""
    try:
        session = get_session()
        try:
            session.add(AuditLog(**payload))
            session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Audit log write failed: {e}")


def _schedule_audit_write(payload: dict) -> None:
    """把 audit 寫入推到背景 threadpool,不阻塞 request 週期。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 事件迴圈不可用時（例如測試直接呼叫），退回同步寫入保底。
        _write_audit_sync(payload)
        return
    task = loop.create_task(asyncio.to_thread(_write_audit_sync, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def write_explicit_audit(
    request: Request,
    *,
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | None = None,
    changes: dict | None = None,
) -> None:
    """為 GET 匯出 / 敏感讀取顯式寫 AuditLog。

    Why: AuditMiddleware 只審計 POST/PUT/PATCH/DELETE,但匯出端點通常是 GET,
    且會輸出 PII / 銀行帳號等敏感資料。此 helper 讓這類路徑留下不可推卸的
    稽核痕跡(操作人、IP、筆數、是否含敏感欄位等)。

    與 AuditMiddleware 同樣採 fire-and-forget 背景寫入,失敗只記 logger,
    不會阻斷或影響原請求回應。
    """
    try:
        user_id, username = _extract_user_from_header(request)
        ip = request.client.host if request.client else None
        changes_json = None
        if changes is not None:
            try:
                changes_json = json.dumps(changes, ensure_ascii=False, default=str)
                if len(changes_json) > 64 * 1024:
                    changes_json = json.dumps(
                        {"_truncated": True, "size": len(changes_json)}
                    )
            except (TypeError, ValueError) as e:
                logger.warning(f"Explicit audit changes serialize failed: {e}")

        payload = dict(
            user_id=user_id,
            username=username or "anonymous",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            changes=changes_json,
            ip_address=ip,
            created_at=datetime.now(),
        )
        _schedule_audit_write(payload)
    except Exception as e:
        logger.warning(f"Explicit audit write failed: {e}")


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()

        # Only audit data-modifying requests
        if method not in METHOD_ACTION_MAP:
            return await call_next(request)

        path = request.url.path

        # Skip certain paths
        if path in SKIP_PATHS:
            return await call_next(request)

        # Parse entity info before calling next
        entity_type = _parse_entity_type(path)
        if not entity_type:
            return await call_next(request)

        # Execute the actual request
        response = await call_next(request)

        # Only log successful requests
        if not (200 <= response.status_code < 300):
            return response

        # 若 endpoint 已設定跳過標記，直接略過
        if getattr(request.state, "audit_skip", False):
            return response

        try:
            user_id, username = _extract_user_from_header(request)
            # endpoint 可透過 request.state 覆寫摘要與 entity_id
            entity_id = getattr(
                request.state, "audit_entity_id", None
            ) or _parse_entity_id(path)
            summary = getattr(request.state, "audit_summary", None) or _build_summary(
                method, path, entity_type
            )
            ip = request.client.host if request.client else None

            changes_raw = getattr(request.state, "audit_changes", None)
            changes_json = None
            if changes_raw is not None:
                try:
                    changes_json = json.dumps(
                        changes_raw, ensure_ascii=False, default=str
                    )
                    # 單筆 diff 上限 64KB，避免撐爆 DB
                    if len(changes_json) > 64 * 1024:
                        changes_json = json.dumps(
                            {"_truncated": True, "size": len(changes_json)}
                        )
                except (TypeError, ValueError) as e:
                    logger.warning(f"Audit changes serialize failed: {e}")

            payload = dict(
                user_id=user_id,
                username=username or "anonymous",
                action=METHOD_ACTION_MAP[method],
                entity_type=entity_type,
                entity_id=entity_id,
                summary=summary,
                changes=changes_json,
                ip_address=ip,
                created_at=datetime.now(),
            )
            _schedule_audit_write(payload)
        except Exception as e:
            logger.warning(f"Audit log enqueue failed: {e}")

        return response
