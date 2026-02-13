"""
Audit logging middleware - automatically records all data-modifying API requests
"""

import logging
import re
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from models.database import get_session, AuditLog

logger = logging.getLogger(__name__)

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
    (r"/api/auth/change-password", "user"),
    (r"/api/attendance", "attendance"),
    (r"/api/employees/\d+/allowances", "employee_allowance"),
    (r"/api/employees", "employee"),
    (r"/api/students", "student"),
    (r"/api/classrooms", "classroom"),
    (r"/api/leaves", "leave"),
    (r"/api/overtimes", "overtime"),
    (r"/api/salaries", "salary"),
    (r"/api/salary", "salary"),
    (r"/api/config/titles", "job_title"),
    (r"/api/config/allowance-types", "allowance_type"),
    (r"/api/config/deduction-types", "deduction_type"),
    (r"/api/config/bonus-types", "bonus_type"),
    (r"/api/config", "config"),
    (r"/api/meetings", "meeting"),
    (r"/api/announcements", "announcement"),
    (r"/api/calendar", "calendar"),
    (r"/api/schedule", "schedule"),
    (r"/api/portal/swap", "shift_swap"),
]

# Skip these paths (login should not be audited as sensitive)
SKIP_PATHS = {"/api/auth/login"}


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

    action_zh = {"CREATE": "新增", "UPDATE": "修改", "DELETE": "刪除"}.get(action, action)
    entity_zh = {
        "employee": "員工", "student": "學生", "attendance": "考勤",
        "leave": "請假", "overtime": "加班", "classroom": "班級",
        "salary": "薪資", "config": "系統設定", "user": "使用者帳號",
        "job_title": "職稱", "meeting": "會議", "announcement": "公告",
        "calendar": "行事曆", "schedule": "班表", "shift_swap": "換班",
        "employee_allowance": "員工津貼",
        "allowance_type": "津貼類型", "deduction_type": "扣款類型",
        "bonus_type": "獎金類型",
    }.get(entity_type, entity_type)

    return f"{action_zh}{entity_zh}"


def _extract_user_from_header(request: Request):
    """從 Authorization header 靜默解析 JWT，不拋錯"""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None, None

    try:
        from jose import jwt
        from utils.auth import JWT_SECRET_KEY, JWT_ALGORITHM
        token = auth.split(" ", 1)[1]
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("user_id"), payload.get("name")
    except Exception:
        return None, None


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
        if 200 <= response.status_code < 300:
            try:
                user_id, username = _extract_user_from_header(request)
                entity_id = _parse_entity_id(path)
                summary = _build_summary(method, path, entity_type)
                ip = request.client.host if request.client else None

                session = get_session()
                try:
                    log = AuditLog(
                        user_id=user_id,
                        username=username or "anonymous",
                        action=METHOD_ACTION_MAP[method],
                        entity_type=entity_type,
                        entity_id=entity_id,
                        summary=summary,
                        ip_address=ip,
                        created_at=datetime.now(),
                    )
                    session.add(log)
                    session.commit()
                finally:
                    session.close()
            except Exception as e:
                logger.warning(f"Audit log write failed: {e}")

        return response
