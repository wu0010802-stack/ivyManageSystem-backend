"""services/consent/checker.py — consent_check 純函式。

個資法 §8 / GDPR Art. 7 單一數據源：所有 consent 咽喉的核心判定。
查 ParentConsentLog 最新一筆（consented_at desc）即為現況；
撤回一樣寫入 log（consented=False），無需軟刪除。

公開 API：
  consent_check(session, user_id, scope) -> bool
      查個別 user 對指定 scope 的最新同意狀態。
      - 有記錄：回最新一筆 consented 值
      - 無記錄：回 False（未曾表態 = 未同意）

  consent_check_student_scope(session, student_id, scope) -> bool
      per-student 家庭層判定（D2 業主策略）：
      - 有主要聯絡人（is_primary=True）且已綁 user → 以主要聯絡人 consent 為準
      - 無主要聯絡人 → 任一已綁 guardian 同意即可（OR 語意）
      - 無任何已綁 guardian → False

      ⚠️ 策略集中在此函式一處。
      如業主改為「所有 guardian 皆須同意」只需把 OR 改為 ALL/any→all。
      如改為「主要聯絡人 AND 次要聯絡人皆同意」，同樣只改此函式即可。

  invalidate_consent_cache(user_id, scope)
      撤回 / opt-out task 寫入新 log 後呼叫，即時清掉 TTL cache entry，
      避免撤回後 60 秒內仍回 True。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from config import get_settings
from models.consent import (
    CONSENT_SCOPE_CROSS_BORDER_TRANSFER,
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.guardian import Guardian
from utils.taipei_time import now_taipei_naive
from utils.cache_layer import get_cache

logger = logging.getLogger(__name__)

_CACHE_NS = "consent"
_CACHE_TTL = 60  # 秒；撤回時由 invalidate_consent_cache 立即清除


# ── per-user ──────────────────────────────────────────────────────────────────


def consent_check(session: Session, user_id: int, scope: str) -> bool:
    """查 user_id 對 scope 的最新同意狀態（含快取）。

    Returns:
        True  — 最新一筆 consented=True
        False — 最新一筆 consented=False，或無任何記錄
    """
    cache_key = f"{user_id}:{scope}"
    cached = get_cache().get(_CACHE_NS, cache_key)
    if cached is not None:
        return bool(cached)

    row = (
        session.query(ParentConsentLog.consented)
        .filter(
            ParentConsentLog.user_id == user_id,
            ParentConsentLog.scope == scope,
        )
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    result = bool(row[0]) if row is not None else False

    # 快取兩種結果（True / False）；撤回後由 invalidate_consent_cache 清除
    get_cache().set(_CACHE_NS, cache_key, result, ttl=_CACHE_TTL)
    return result


# ── policy-bump 重簽判定 ─────────────────────────────────────────────────────


def has_signed_current_policy(session: Session, user_id: int) -> bool:
    """判定家長是否已對當期生效政策簽署 service_essential 同意。

    「當期政策」= effective_at <= now 且最新（effective_at desc 取第一筆）。
    若 DB 尚未 seed 任何 PolicyVersion → dark 期，一律回 True（不擋）。

    Returns:
        True  — 已簽署當期政策版本（或尚無任何政策 seed）
        False — 未簽署、或簽的是舊版（policy_version_id 不符當期）
    """
    current = (
        session.query(PolicyVersion.id)
        .filter(PolicyVersion.effective_at <= now_taipei_naive())
        .order_by(PolicyVersion.effective_at.desc())
        .first()
    )
    if current is None:
        # 尚未 seed 任何 policy → dark 期，不擋
        return True

    latest = (
        session.query(ParentConsentLog)
        .filter(
            ParentConsentLog.user_id == user_id,
            ParentConsentLog.scope == CONSENT_SCOPE_SERVICE_ESSENTIAL,
        )
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    return bool(latest and latest.consented and latest.policy_version_id == current[0])


# ── per-student（家庭層）──────────────────────────────────────────────────────


def consent_check_student_scope(session: Session, student_id: int, scope: str) -> bool:
    """查學生 student_id 對 scope 的家庭層同意狀態（D2 策略：主要聯絡人優先）。

    業主決策（2026-06-02）：以主要聯絡人（is_primary=True）的 consent 為準；
    無主要聯絡人時，任一已綁 guardian 同意即可。

    Returns:
        True  — 依策略判定家庭層已同意
        False — 未同意，或無已綁 guardian
    """
    guardians = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student_id,
            Guardian.user_id.isnot(None),
            Guardian.deleted_at.is_(None),
        )
        .all()
    )

    if not guardians:
        return False

    primary = next((g for g in guardians if g.is_primary), None)
    if primary is not None:
        # 策略：以主要聯絡人 consent 為準（業主已確認）
        # 若改為「主要 AND 次要皆同意」，在此擴充即可
        return consent_check(session, primary.user_id, scope)

    # 無主要聯絡人：任一已綁 guardian 同意即可
    # 若改為「所有 guardian 皆同意」，把 any(...) 改成 all(...) 即可
    return any(consent_check(session, g.user_id, scope) for g in guardians)


# ── 上傳咽喉（cross_border）──────────────────────────────────────────────────


def enforce_student_cross_border(session: Session, student_id: int) -> None:
    """上傳含學生 PII 前呼叫的 cross_border_transfer consent 守門員。

    - flag off（get_settings().consent.enforcement_enabled is False）→ no-op，直接 return。
    - flag on：
        - 查詢出錯 → fail-closed（記 warning，raise ConsentRequired）
        - 未同意 → raise ConsentRequired
        - 已同意 → return（允許上傳）

    設計原則：
    - 此函式只做 consent 判定，不觸碰 storage 邏輯。
    - fail-closed：查詢異常一律視為「未同意」，防止異常被繞過成為後門。
    - 例外：寫入 warning log 供 Sentry / ops 告警追蹤。

    caller 在「取得 entry.student_id 之後、呼叫 storage.put_attachment 之前」插入：
        enforce_student_cross_border(session, entry.student_id)
    """
    if not get_settings().consent.enforcement_enabled:
        return

    from services.business_errors.parent import ConsentRequired

    try:
        ok = consent_check_student_scope(
            session, student_id, CONSENT_SCOPE_CROSS_BORDER_TRANSFER
        )
    except Exception as exc:
        logger.warning(
            "enforce_student_cross_border 查詢異常，fail-closed；student_id=%s exc=%s",
            student_id,
            exc,
        )
        raise ConsentRequired("家長尚未同意學生資料跨境傳輸，無法上傳含個資的檔案")

    if not ok:
        raise ConsentRequired("家長尚未同意學生資料跨境傳輸，無法上傳含個資的檔案")


# ── 快取失效（撤回時立即清除）────────────────────────────────────────────────


def invalidate_consent_cache(user_id: int, scope: str) -> None:
    """撤回 / opt-out 寫入新 log 後立即呼叫，確保下次查詢重新從 DB 讀取。

    供 Task 3（opt-out endpoint）及後續撤回流程呼叫。
    """
    get_cache().delete(_CACHE_NS, f"{user_id}:{scope}")
    logger.debug("consent cache invalidated: user_id=%s scope=%s", user_id, scope)
