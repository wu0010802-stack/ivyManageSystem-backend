"""
Audit logging middleware - automatically records all data-modifying API requests
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from models.database import get_session, AuditLog

logger = logging.getLogger(__name__)

# Hold strong refs to fire-and-forget audit tasks so the event loop
# does not drop them before they finish (asyncio gotcha).
_background_tasks: "set[asyncio.Task]" = set()

# 登入路徑 — 同時用於 SKIP_PATHS（讓 AuditMiddleware 不對成功登入記預設 audit）
# 與 _should_audit_block（讓 login 失敗的 BLOCKED 計數不被 60s dedup 壓平）。
_LOGIN_PATH = "/api/auth/login"

# 資安掃描 2026-05-07 P1：401/403 失敗寫入嘗試的 audit 防灌爆。
# 同 (ip, method, path) 在 dedup window 內只記第一筆，避免攻擊者猛轟受保護端點
# 把 audit_logs 灌爆。Trade-off：失去「攻擊次數」訊號，但 server log 仍有完整記錄
# 可供 SIEM 分析。多 worker 部署時每 worker 各自有 cache（最多 N× 通過率，仍有限）。
_AUDIT_BLOCK_DEDUP_WINDOW_SEC = 60
_AUDIT_BLOCK_CACHE_MAX = 1000
_audit_block_cache: dict[tuple[str, str, str], float] = {}


def _should_audit_block(ip: str | None, method: str, path: str) -> bool:
    """同 (ip, method, path) 在 60 秒內只 audit 一次 401/403。

    例外：/api/auth/login 路徑跳過 dedup —— 登入失敗的密集計數是 C 階段
    告警的訊號來源，dedup 會把 brute-force 壓成 1 筆，失去判斷依據。
    既有 _check_ip_rate_limit 本身會在 N 次後 raise 429 自然封頂。
    Refs: spec 2026-05-11-audit-coverage-gap-design §3.2。
    """
    if path == _LOGIN_PATH:
        return True
    key = (ip or "anon", method, path)
    now = time.monotonic()
    last = _audit_block_cache.get(key)
    if last is not None and now - last < _AUDIT_BLOCK_DEDUP_WINDOW_SEC:
        return False
    _audit_block_cache[key] = now
    # Opportunistic cleanup：cache 超過 1000 條時掃過去清掉超出視窗的舊條目
    if len(_audit_block_cache) > _AUDIT_BLOCK_CACHE_MAX:
        cutoff = now - _AUDIT_BLOCK_DEDUP_WINDOW_SEC
        for k in list(_audit_block_cache.keys()):
            if _audit_block_cache[k] < cutoff:
                del _audit_block_cache[k]
    return True


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
    # 學費 / 退款。endpoint 已寫 audit_summary，但缺此規則整段被 middleware 略過。
    # 與 /api/activity 拆 entity_type 不同，學費業務只有單一 fee 類型，所以 /api/fees/items
    # 與 /api/fees/records/.../pay|refund 全部映射為 fee；前端可在 changes 細分動作。
    (r"/api/fees", "fee"),
    (r"/api/config/titles", "job_title"),
    (r"/api/config/deduction-types", "deduction_type"),
    (r"/api/config/bonus-types", "bonus_type"),
    (r"/api/config", "config"),
    # 勞健保級距表（DB 化後 admin CRUD）。改級距金額會牽動全員保費；
    # 端點本身有 has_finance_approve + reason ≥10 字守衛，但若不在 ENTITY_PATTERNS
    # AuditMiddleware 不會落 audit_logs，等於只在 logger 留下 warning，事後溯源
    # 無法用 audit-logs 篩選。Refs: 資安掃描 2026-05-07 P0。
    # 範圍嚴格限定 /brackets — 不擴張到 /import / /calculate 等其他 insurance 端點
    # （那些另有自身語意，不適合共用 insurance_bracket entity_type）。
    (r"/api/insurance/brackets", "insurance_bracket"),
    # 審核流程設定（多層 ApprovalPolicy）。policy 自身 INSERT/UPDATE/DELETE
    # 必須留 audit，否則 admin 可「改規則 → 自批 → 改回」全程零稽核。
    # Refs: 邏輯漏洞 audit 2026-05-07 P0 (#13)。
    (r"/api/approval-settings", "approval_policy"),
    (r"/api/meetings", "meeting"),
    (r"/api/announcements", "announcement"),
    (r"/api/calendar", "calendar"),
    (r"/api/schedule", "schedule"),
    (r"/api/portal/swap", "shift_swap"),
    # 教師入口：請假/加班/附件/代理人回覆。映射到與管理端一致的 leave/overtime
    # entity_type，前端篩 leave 時可同時看到「教師送出/管理員核准」整條軌跡。
    # 排在 /api/portal/swap 之後是因為 swap 規則更具體；放這裡與其他 portal 子路由共置。
    (r"/api/portal/my-leaves", "leave"),
    (r"/api/portal/my-overtimes", "overtime"),
    (r"/api/portal/my-leave-attachments", "leave"),
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
    # 家長公開頁修改：歸到 activity_registration 與管理端同類，後台「修改」軌跡可一起篩。
    # endpoint 內透過 request.state.audit_entity_id/audit_changes 帶出 reg.id 與 diff。
    (r"/api/activity/public/update", "activity_registration"),
    # 家長入口 2.0 — 家園溝通平台
    # first-match：events/.+/ack 必須排在 messages / medication-orders 之前不衝突，但都不衝突，順序自由
    (r"/api/parent/messages", "parent_message"),
    (r"/api/parent/medication-orders", "parent_medication_order"),
    (r"/api/parent/events/.+/ack", "parent_event_ack"),
    (r"/api/parent/notifications", "parent_notification_pref"),
    (r"/api/parent/student-leaves", "parent_leave"),
    (r"/api/parent/contact-book", "contact_book_entry"),
    (r"/api/portal/parent-messages", "parent_message"),
    (r"/api/portal/contact-book", "contact_book_entry"),
    # 教育部申報模組 Phase 1 — 身障/特教文件 CRUD 必須留 audit，
    # 否則鑑定證明異動（影響補助/IEP）會無稽核痕跡。
    (r"/api/gov-moe/disability-documents", "disability_document"),
    # 政府資料同步：promote/dismiss staging 寫入級距/基本工資（影響全員保費）；
    # sync-now 觸發 fetch。原 ENTITY_PATTERNS 漏，middleware 視為 entity_type=None
    # 整批跳過 audit。Refs: bug sweep round 4 (2026-05-12) DB 完整性檢查發現。
    (r"/api/gov-data", "gov_data_sync"),
    # 學生輔導：發展評估 / 事件紀錄 / 班級點名。原 /api/students 不會匹配子路徑，
    # middleware 跳過 audit。學生事件紀錄涉及衝突/受傷等敏感資訊，必留稽核。
    # 注意：student-attendance 是教師日常 batch 點名，量大；若上 prod 後 audit_logs
    # 量爆，可考慮把 /api/student-attendance 從此處移除（middleware 自然不審），
    # 改在 controversial action（如手動覆寫他人考勤）走 write_audit_in_session。
    (r"/api/student-assessments", "student_assessment"),
    (r"/api/student-incidents", "student_incident"),
    (r"/api/student-attendance", "student_attendance"),
    # 招生紀錄：records / market / hotspots / periods / competitors 共用一個
    # entity_type，前端可在 changes 細分；convert 會建學生，必留稽核痕跡。
    (r"/api/recruitment", "recruitment"),
    # 考核系統（2026-05-11）。penalty_catalog / bonus_rates 排在 cycles / participants / events /
    # summaries 之前，確保更具體路徑優先匹配。
    # /api/appraisal/cycles/{id}/summaries:recompute 歸 appraisal_cycle，
    # 因為 recompute 由 cycle 觸發；個別 summary sign/finalize/reject 由 /summaries/{id} 端點產生。
    (r"/api/appraisal/penalty_catalog", "appraisal_catalog"),
    (r"/api/appraisal/bonus_rates", "appraisal_bonus_rate"),
    (r"/api/appraisal/cycles", "appraisal_cycle"),
    (r"/api/appraisal/participants", "appraisal_participant"),
    (r"/api/appraisal/events", "appraisal_event"),
    (r"/api/appraisal/summaries", "appraisal_summary"),
]

# Skip these paths (login should not be audited as sensitive)
SKIP_PATHS = {_LOGIN_PATH}

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
    "fee": "學費",
    "activity_registration": "才藝報名",
    "activity_course": "才藝課程",
    "activity_supply": "才藝教具",
    "activity_inquiry": "才藝詢問",
    "activity_session": "才藝點名",
    "activity_pos": "才藝 POS",
    "activity_daily_close": "POS 日結",
    "activity_settings": "才藝設定",
    # 家園溝通平台
    "parent_message": "家長訊息",
    "parent_medication_order": "家長用藥單",
    "parent_event_ack": "事件簽收",
    "parent_notification_pref": "家長通知偏好",
    "parent_leave": "家長學生請假",
    "contact_book_entry": "聯絡簿",
    # F-033：匯出端點顯式 audit 用
    "shift_assignment": "排班",
    "holiday": "國定假日",
    "gov_report": "政府申報",
    # F-035：audit-logs 自身匯出（meta-audit）
    "audit_log": "操作紀錄",
    # 審核流程設定（policy 自身異動稽核）
    "approval_policy": "審核流程設定",
    "insurance_bracket": "勞健保級距",
    "auth": "登入活動",
    # 教育部申報 Phase 1
    "disability_document": "身障鑑定文件",
    # 政府資料同步（bug sweep round 4 2026-05-12 補）
    "gov_data_sync": "政府資料同步",
    # 學生輔導/招生（bug sweep round 4 2026-05-12 補）
    "student_assessment": "學生發展評估",
    "student_incident": "學生事件紀錄",
    "student_attendance": "學生點名",
    "recruitment": "招生紀錄",
    # 考核系統
    "appraisal_cycle": "考核週期",
    "appraisal_participant": "考核參與者",
    "appraisal_event": "考核事件",
    "appraisal_summary": "考核結算",
    "appraisal_bonus_rate": "考核獎金率",
    "appraisal_catalog": "懲處目錄",
}

ACTION_LABELS = {
    "CREATE": "新增",
    "UPDATE": "修改",
    "DELETE": "刪除",
    "EXPORT": "匯出",
    "READ": "查看",
    # 失敗的寫入嘗試（401/403）— audit P1 補登攻擊偵測
    "BLOCKED_CREATE": "拒絕新增",
    "BLOCKED_UPDATE": "拒絕修改",
    "BLOCKED_DELETE": "拒絕刪除",
    # 登入事件（A 階段）— write_login_audit 顯式呼叫
    "LOGIN_SUCCESS": "登入成功",
    "LOGIN_FAILED": "登入失敗",
    "LOGIN_RATE_LIMITED": "登入被限流",
    "LOGIN_LOCKED": "帳號鎖定中",
    "LOGOUT": "登出",
    "TOKEN_REFRESH": "刷新 Token",
    "TOKEN_REFRESH_FAILED": "Token 刷新失敗",
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


def write_audit_in_session(
    session,
    request: Request,
    *,
    action: str,
    entity_type: str,
    summary: str,
    entity_id: str | int | None = None,
    changes: dict | None = None,
) -> None:
    """同交易內寫入 AuditLog，避免金流類操作的 audit 丟失。

    Why: AuditMiddleware 是 fire-and-forget 背景寫入，threadpool 故障/DB 連線中斷時
    AuditLog 會丟失，但主資料已 commit。金流（學費 pay/refund、薪資手動調整等）
    要求「主交易成功 ⇔ 必有稽核軌跡」，故改在同交易寫入：audit 失敗整個 rollback。

    使用方式：
        with session_scope() as session:
            ...金流操作...
            write_audit_in_session(
                session, request,
                action="UPDATE", entity_type="fee", summary="...",
                entity_id=str(record_id), changes={...},
            )
        # session_scope 一次 commit；audit 與金流變動共生死

    呼叫此 helper 後會設置 request.state.audit_skip = True，避免 middleware 二次寫入。
    """
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
            logger.warning(f"In-session audit changes serialize failed: {e}")

    log = AuditLog(
        user_id=user_id,
        username=username or "anonymous",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        summary=summary,
        changes=changes_json,
        ip_address=ip,
        created_at=datetime.now(),
    )
    session.add(log)
    # 防止 AuditMiddleware 在 response 階段重複寫入同一筆稽核
    request.state.audit_skip = True


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


def _build_login_summary(action: str, username: str | None) -> str:
    """登入事件的摘要文案（中文）"""
    label = ACTION_LABELS.get(action, action)
    if username:
        return f"{label}：{username}"
    return label


def write_login_audit(
    request: Request,
    *,
    action: str,
    username: str | None,
    user_id: int | None = None,
    extras: dict | None = None,
) -> None:
    """登入相關事件 audit 寫入。entity_type 固定 'auth'。

    Why: AuditMiddleware 對 /api/auth/login 在 SKIP_PATHS 中跳過；登入事件
    含成功/失敗/限流/鎖定/登出/refresh 都要顯式從 endpoint 內寫入。
    與 write_explicit_audit 同樣採 fire-and-forget 背景寫入，失敗只記 logger.warning。

    安全注意：失敗事件不寫 user_id（防 audit 本身洩漏帳號存在性）；
    extras 中絕不可放密碼或密碼 hash（由 caller 自行確保）。

    Why not write_explicit_audit: 登入時尚無有效 JWT，_extract_user_from_header 無法
    取得 username；此處直接使用呼叫方傳入的 username，以保證 audit 行中有正確帳號名稱。

    Refs: spec 2026-05-11-audit-coverage-gap-design §3.2 / §3.3。
    """
    try:
        ip = request.client.host if request.client else None
        changes: dict = {}
        if extras:
            changes.update(extras)
        if username:
            changes["username"] = username
        changes_json = None
        if changes:
            try:
                changes_json = json.dumps(changes, ensure_ascii=False, default=str)
                if len(changes_json) > 64 * 1024:
                    changes_json = json.dumps(
                        {"_truncated": True, "size": len(changes_json)}
                    )
            except (TypeError, ValueError) as e:
                logger.warning(f"Login audit changes serialize failed: {e}")

        payload = dict(
            user_id=user_id,
            username=username or "anonymous",
            action=action,
            entity_type="auth",
            entity_id=str(user_id) if user_id is not None else None,
            summary=_build_login_summary(action, username),
            changes=changes_json,
            ip_address=ip,
            created_at=datetime.now(),
        )
        _schedule_audit_write(payload)
    except Exception as e:
        logger.warning(f"Login audit write failed: {e}")


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

        status = response.status_code
        # 2xx：原本就 audit 的成功路徑（CREATE/UPDATE/DELETE）
        # 401/403：失敗的寫入嘗試（攻擊偵測：未登入越權、越權嘗試）→ audit
        #          以 BLOCKED_<METHOD> 標記，AuditLogView 可篩
        # 其他 4xx/5xx（400/404/409/422/5xx）：通常使用者輸入錯誤或 internal
        #          錯誤（自有 log），不 audit 避免量爆
        # Refs: 邏輯漏洞 audit 2026-05-07 P1。
        is_success = 200 <= status < 300
        is_auth_block = status in (401, 403)
        if not (is_success or is_auth_block):
            return response

        # 若 endpoint 已設定跳過標記，直接略過
        if getattr(request.state, "audit_skip", False):
            return response

        # 資安 P1 (2026-05-07)：401/403 dedup 防灌爆。同 (ip, method, path) 60s 內只記一筆
        if is_auth_block:
            ip_for_dedup = request.client.host if request.client else None
            if not _should_audit_block(ip_for_dedup, method, path):
                return response

        try:
            user_id, username = _extract_user_from_header(request)
            # endpoint 可透過 request.state 覆寫摘要與 entity_id
            entity_id = getattr(
                request.state, "audit_entity_id", None
            ) or _parse_entity_id(path)
            base_action = METHOD_ACTION_MAP[method]
            action = base_action if is_success else f"BLOCKED_{base_action}"
            if is_auth_block:
                summary = (
                    getattr(request.state, "audit_summary", None)
                    or f"⚠ 拒絕 {method} {path} → {status}"
                )
            else:
                summary = getattr(
                    request.state, "audit_summary", None
                ) or _build_summary(method, path, entity_type)
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
            logger.warning(f"Audit log enqueue failed: {e}")

        return response
