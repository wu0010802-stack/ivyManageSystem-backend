"""services/activity_query_token.py — 公開報名查詢碼（F2 第六階段抽出）。

從 api/activity/_shared.py 抽出公開查詢碼相關 helper：
- _query_token_ttl_days — TTL 環境變數讀取（預設 180 天）
- is_query_token_expired — 過期判斷（含舊資料 None 視為過期）
- _generate_query_token — 產生 32-char URL-safe 明文
- _hash_query_token — HMAC-SHA256 with domain salt

domain salt 與 silent-success 設計：
- HMAC key 借用 JWT_SECRET_KEY，但用 ACTIVITY_TOKEN_DOMAIN 做用途隔離，
  避免不同模組借用 JWT_SECRET_KEY 時 hash 撞號。
- silent-success path（register 失敗時不洩漏）用同 _generate_query_token 產假 token，
  維持 response shape 一致避免 F-030 enumeration oracle。

api/activity/_shared.py 保留 re-export 維持既有 import surface
（api/activity/public.py / registrations.py 等模組仍可從 _shared 取）。

DEPRECATION（2026-05-21）：本模組借用 JWT_SECRET_KEY 做 HMAC，未支援
multi-key rotation。JWT secret rotation 後（JWT_SECRET_KEY 變值），
既有外發 activity query token 會失效。

Follow-up：解耦到專屬 env ACTIVITY_TOKEN_HMAC_KEY 並支援 olds list 容忍
rotation。spec 連結：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from utils.auth import JWT_SECRET_KEY

_ACTIVITY_TOKEN_DOMAIN = b"activity_query_token:v1"


def _query_token_ttl_days() -> int:
    """讀 ACTIVITY_QUERY_TOKEN_TTL_DAYS 設定（預設 180 天）。

    180 天涵蓋一個學期完整活動期 + 部分緩衝；業主可調為更短（例 90）強化。
    """
    from config import get_settings

    return get_settings().misc.activity_query_token_ttl_days


def is_query_token_expired(issued_at) -> bool:
    """判斷查詢碼是否已過期。

    issued_at 為 None（舊資料未發 token / backfill 期）一律視為過期。
    這樣攻擊者拿到舊 reg 的偽造 token 也無法用，必須走 /public/query 三欄比對。
    """
    if issued_at is None:
        return True
    ttl = timedelta(days=_query_token_ttl_days())
    return datetime.now() - issued_at > ttl


def _generate_query_token() -> str:
    """產生公開查詢碼明文（32-char URL-safe）。

    僅在 register 真實成功 / reject rotate 當下回給呼叫端。
    silent-success path 用同函式產一個「假」token（不寫 DB），維持 response shape
    一致避免 F-030 enumeration oracle。
    """
    return secrets.token_urlsafe(24)


def _hash_query_token(token: str) -> str:
    """HMAC-SHA256(JWT_SECRET_KEY, domain || token) → hex digest（64 chars）。

    domain salt（_ACTIVITY_TOKEN_DOMAIN）做用途隔離 — 即使 JWT_SECRET_KEY 被
    其他模組借用，產生的 hash 不會撞號。
    """
    msg = _ACTIVITY_TOKEN_DOMAIN + token.encode("utf-8")
    key = (JWT_SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()
