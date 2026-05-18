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
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
        "emergency_contact",
        "birthday",
        "birth_date",
        "medication",
        "dosage",
        "allergy",
        "disability",
        "iep",
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
    }
)

_FILTERED = "[Filtered]"

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
                    crumb["message"] = _sanitize_url(crumb["message"])
    return event


def _scrub_breadcrumb(crumb: dict, _hint: dict | None = None) -> dict | None:
    if not isinstance(crumb, dict):
        return crumb
    if "data" in crumb:
        crumb["data"] = _scrub_mapping(crumb["data"])
    if isinstance(crumb.get("message"), str):
        crumb["message"] = _sanitize_url(crumb["message"])
    return crumb


def init_sentry() -> bool:
    """依環境變數 init Sentry SDK。缺 SENTRY_DSN 直接 return False（no-op）。

    成功 init 回 True；DSN 缺、空白、或 sentry-sdk 未安裝皆回 False。
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
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

    env = (
        os.environ.get("SENTRY_ENVIRONMENT")
        or os.environ.get("ENV", "development").lower()
    )
    release = os.environ.get("SENTRY_RELEASE") or None
    try:
        traces_rate = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    except ValueError:
        traces_rate = 0.1

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
