"""utils/sentry_init.py — Sentry SDK 初始化與 PII 過濾。

行為：
- 缺 SENTRY_DSN 時整個模組 no-op；DSN 是唯一啟用開關（dev/test 友善）
- send_default_pii=False，不送 IP/Cookie/request body 預設欄位
- before_send 對 request/transaction/breadcrumb/extra 全部遞迴遮罩 PII key
- URL path 中段純數字 → `:id`，避免 transaction name 把學生/員工 id 灌進 dashboard
- LoggingIntegration event_level=ERROR：logger.warning 不會被當 event 抓
  → main.py 的 scheduler 啟動 try/except 改顯式 capture_exception
"""

import hashlib
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config import settings

logger = logging.getLogger(__name__)

# 全 repo PII 欄位 denylist（小寫 substring 命中即遮）。範圍：
# - 金流: salary/insured/dependent/bonus_amount/bank/card
# - 個資: id_number/passport/phone/mobile/email/line_user_id/liff/address
# - 幼兒: child/student/parent/guardian/emergency_contact/birth
# - 醫療: medication/dosage/allergy/disability/iep/health/diagnosis/growth/measurement
# - 認證: password/token/secret/jwt/cookie/authorization/refresh
_PII_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "salary",
        "insured",
        "dependent",
        "bonus_amount",
        "bank_account",
        "bank_code",
        "card_no",
        "credit_card",
        "id_number",
        "passport",
        "phone",
        "mobile",
        "email",
        "line_user_id",
        "liff",
        "address",
        "child_name",
        "student_name",
        "parent_name",
        "guardian",
        "custody_note",
        "emergency_contact",
        "birthday",
        "birth_date",
        "medication",
        "dosage",
        "allergy",
        "allergen",
        "allergies",
        "reaction_symptom",
        "first_aid_note",
        "disability",
        "iep",
        "special_needs",
        "health",
        "diagnosis",
        "growth",
        "measurement",
        "height",
        "weight",
        "password",
        "secret",
        "token",
        "jwt",
        "cookie",
        "authorization",
        "refresh_token",
        "access_token",
        "api_key",
        "resign_reason",
        "leave_balance_snapshot",
        "certificate_pdf_path",
    }
)

_FILTERED = "[Filtered]"

# P2-2/P2-10（2026-06-23 資安掃描）：value-level 強識別子遮罩（身分證/手機/市話）。
# key-based denylist 漏掉「自由文字 value」（reason/note/summary）與「DB 例外訊息」
# （SQLAlchemy [parameters: {...}]）內嵌的識別子；此層補上。正則對齊 utils/audit_redact
# （單一語意；sentry_init 為底層模組，不反向 import audit_redact 以免循環）。
# 邊界用顯式 lookaround 而非 \b：Python \b 為 Unicode-aware，對「中文緊鄰數字無空白」
# （如 `電話0912345678請改期`，zh-TW 自由文字極常見）不視為詞邊界 → 漏遮（前端 JS \b
# 為 ASCII-only 反而較嚴）。改「不被數字（ID/uid 類為英數）包夾」的顯式邊界，既修 CJK
# 緊鄰漏遮、又保留原意（不遮夾在更長數字串中的子序列）。三份對齊：本檔 / utils/audit_redact
# / 前端 src/utils/sentry.ts（陷阱#8）。
_VALUE_TW_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z][12A-Da-d]\d{8}(?![A-Za-z0-9])"
)  # 身分證 / 居留證
_VALUE_MOBILE_RE = re.compile(r"(?<!\d)09\d{8}(?!\d)")  # 手機
_VALUE_LANDLINE_RE = re.compile(r"(?<!\d)0\d{1,2}-\d{6,8}(?!\d)")  # 市話（帶 dash）
# SEC-2026-0624-01：LINE userId（`U` + 32 小寫 hex，全球唯一、可直接對映真實
# LINE 帳號）。家長綁定 log 已改 line_user_id[:8] 截短，此正則為縱深防禦——
# 攔截任何隨自由文字漏進 breadcrumb message / exception value 的完整 userId。
_VALUE_LINE_UID_RE = re.compile(r"(?<![A-Za-z0-9])U[0-9a-f]{32}(?![A-Za-z0-9])")


def _redact_pii_value(text: Any) -> Any:
    """遮罩自由文字 / 例外訊息中的強識別子（身分證/居留證、手機、帶 dash 市話、
    LINE userId）。

    非字串原樣回傳。只遮樣式明確的識別子，避免誤遮操作 id / 數字 / 姓名。
    """
    if not isinstance(text, str) or not text:
        return text
    text = _VALUE_TW_ID_RE.sub(_FILTERED, text)
    text = _VALUE_MOBILE_RE.sub(_FILTERED, text)
    text = _VALUE_LANDLINE_RE.sub(_FILTERED, text)
    text = _VALUE_LINE_UID_RE.sub(_FILTERED, text)
    return text


# Exempt：常見被誤判的 system / metric 欄位（substring 匹配；exempt 優先於 denylist）。
# 起源：denylist 用 substring 匹配是為涵蓋 `employee_phone` / `parent_email` 等延伸欄位，
# 副作用是 `ip_address`（含 address）、`health_check`（含 health）、`email_template`（含 email）
# 等系統/分析欄位也被誤遮 — 把 prod debug 需要的 context 也刪掉。
# 新增 PII 欄位前先確認不會跟既有 exempt 衝突。
_PII_KEY_EXEMPT_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "ip_addr",  # IP 系統欄位（ip_address / request_ip_addr_v6 等）
        "healthcheck",
        "health_check",
        "health_status",  # system health；個人健康狀態用 health_record / personal_health
        "email_template",
        "email_subject",  # 系統 email 元資料；個人 email 用 email_address / user_email
        "growth_funnel",
        "growth_rate",
        "growth_count",  # business analytics；個人成長用 growth_record / growth_data
        "measurement_unit",
        "measurement_type",  # metadata；個人量測值用 measurement_value / measurement_height
    }
)

# URL path 中段「/數字」→「/:id」，e.g.
#   /api/students/123/measurements/45 → /api/students/:id/measurements/:id
_URL_ID_RE = re.compile(r"/(\d+)(?=/|$|\?)")


def _sanitize_url(url: str) -> str:
    """Sanitize URL：path 中段純數字 → `:id`；query 內 PII key 值 → `[Filtered]`。

    Query 內 PII 過去完全繞過 scrubber（search?phone=0912 / ?id_number=A1 等都會
    原樣進 Sentry）。改用 urlsplit 拆 path + query，path 跑既有 id 替換，
    query 用相同 denylist 做 key-based 遮罩，最後拼回。
    """
    if not isinstance(url, str) or not url:
        return url
    parts = urlsplit(url)
    new_path = _URL_ID_RE.sub("/:id", parts.path)
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [(k, _FILTERED if _key_is_pii(k) else v) for k, v in pairs]
        new_query = urlencode(scrubbed)
    else:
        new_query = parts.query
    return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, parts.fragment))


def _key_is_pii(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lk = key.lower()
    # Exempt 先檢查：被誤判為 PII 的系統/metric 欄位放行
    for needle in _PII_KEY_EXEMPT_SUBSTRINGS:
        if needle in lk:
            return False
    for needle in _PII_KEY_SUBSTRINGS:
        if needle in lk:
            return True
    return False


def _scrub_mapping(obj: Any) -> Any:
    """遞迴遮 dict / list 內命中 denylist 的 key；string/number 不動。"""
    if isinstance(obj, dict):
        return {
            k: (_FILTERED if _key_is_pii(k) else _scrub_mapping(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_mapping(item) for item in obj]
    # P2-10：對 string value 跑 value-level 識別子遮罩（key 非 PII 時的兜底，
    # 遮自由文字 reason/note/summary 內的手機/身分證/市話）。
    if isinstance(obj, str):
        return _redact_pii_value(obj)
    return obj


def _scrub_query_string(value: Any) -> Any:
    """request.query_string 可能是 string 或 dict；前者 parse 後跑相同 denylist。"""
    if isinstance(value, str):
        if not value:
            return value
        pairs = parse_qsl(value, keep_blank_values=True)
        scrubbed = [(k, _FILTERED if _key_is_pii(k) else v) for k, v in pairs]
        return urlencode(scrubbed)
    return _scrub_mapping(value)


def _hash_user_id(value: Any) -> Any:
    """employees.id / parents.id 對 Sentry 是擬個資（pseudonymous identifier）；
    blake2b 8-char hash 保留 issue grouping 能力但移除直連性。None / 空字串不變。
    """
    if value is None or value == "":
        return value
    return hashlib.blake2b(str(value).encode("utf-8"), digest_size=4).hexdigest()


def _scrub_event(event: dict, _hint: dict | None = None) -> dict | None:
    """Sentry before_send hook：遮 PII + sanitize URL。"""
    if not isinstance(event, dict):
        return event

    request = event.get("request")
    if isinstance(request, dict):
        if isinstance(request.get("url"), str):
            request["url"] = _sanitize_url(request["url"])
        if "query_string" in request:
            request["query_string"] = _scrub_query_string(request["query_string"])
        for sect in ("headers", "cookies", "data", "env"):
            if sect in request:
                request[sect] = _scrub_mapping(request[sect])

    if isinstance(event.get("transaction"), str):
        event["transaction"] = _sanitize_url(event["transaction"])

    for sect in ("extra", "contexts", "tags", "user"):
        if sect in event:
            event[sect] = _scrub_mapping(event[sect])

    # user.id 對映 employees.id / parents.id —— 直連個人。額外 hash 化以保留
    # Sentry issue grouping 能力但移除直連性。
    user = event.get("user")
    if isinstance(user, dict) and "id" in user:
        user["id"] = _hash_user_id(user["id"])

    crumbs = event.get("breadcrumbs")
    if isinstance(crumbs, dict) and isinstance(crumbs.get("values"), list):
        for crumb in crumbs["values"]:
            if isinstance(crumb, dict):
                if "data" in crumb:
                    crumb["data"] = _scrub_mapping(crumb["data"])
                if isinstance(crumb.get("message"), str):
                    crumb["message"] = _redact_pii_value(
                        _sanitize_url(crumb["message"])
                    )

    # P2-2：DB 例外訊息（SQLAlchemy StatementError 含 [parameters: {...}]）的 value
    # 不在 request/extra 內，需單獨對 exception.values[].value 跑識別子遮罩。
    exc = event.get("exception")
    if isinstance(exc, dict) and isinstance(exc.get("values"), list):
        for ev in exc["values"]:
            if isinstance(ev, dict) and isinstance(ev.get("value"), str):
                ev["value"] = _redact_pii_value(ev["value"])
    return event


def _scrub_breadcrumb(crumb: dict, _hint: dict | None = None) -> dict | None:
    if not isinstance(crumb, dict):
        return crumb
    if "data" in crumb:
        crumb["data"] = _scrub_mapping(crumb["data"])
    if isinstance(crumb.get("message"), str):
        crumb["message"] = _redact_pii_value(_sanitize_url(crumb["message"]))
    return crumb


def init_sentry() -> bool:
    """依環境變數 init Sentry SDK。缺 SENTRY_DSN 直接 return False（no-op）。

    成功 init 回 True；DSN 缺、空白、或 sentry-sdk 未安裝皆回 False。
    """
    dsn = (settings.sentry.dsn or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        logger.warning("sentry-sdk 未安裝；Sentry 整合略過")
        return False

    env = settings.sentry.environment or settings.core.env.lower()
    release = settings.sentry.release or None
    traces_rate = settings.sentry.traces_sample_rate

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        traces_sample_rate=traces_rate,
        send_default_pii=False,
        max_breadcrumbs=50,
        attach_stacktrace=True,
        before_send=_scrub_event,
        before_breadcrumb=_scrub_breadcrumb,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
    logger.info(
        "Sentry SDK initialised (env=%s, traces_sample_rate=%s)",
        env,
        traces_rate,
    )
    return True


def capture_exception(exc: BaseException, level: str = "error") -> None:
    """便利包裝：scheduler / WS 等 logger.warning 吞 exception 的點顯式上報。

    Args:
        exc: 要上報的 exception
        level: Sentry event level (`error` / `warning` / `info`)。scheduler
            啟動失敗業務語意是「可降級警告」，傳 `warning` 避免污染 error 看板。

    sentry-sdk 未 init 時，內部會自動 no-op；不需在呼叫端守 DSN。
    """
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.level = level
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — 上報失敗不能傳染回主邏輯
        pass


def capture_message(message: str, level: str = "warning") -> None:
    """便利包裝：非 exception 的「該被看見」事件顯式上報（與 capture_exception 對稱）。

    給沒有 exception 物件的告警用——例如啟動期偵測到 DB permission_definitions 與
    Permission enum 漂移。``logger.warning`` 本身不會被 LoggingIntegration 上報
    （event_level=ERROR），故這類「監控自己瞎掉」的訊號需顯式呼叫才會進 Sentry。

    Args:
        message: 告警訊息（勿夾帶 PII；scrubber 仍會跑但訊息應本就安全）
        level: Sentry event level（預設 `warning`）

    sentry-sdk 未 init 時內部自動 no-op；不需在呼叫端守 DSN。
    """
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.level = level
            sentry_sdk.capture_message(message, level=level)
    except Exception:  # noqa: BLE001 — 上報失敗不能傳染回主邏輯
        pass
