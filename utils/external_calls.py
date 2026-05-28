"""共用外呼 helper：retry_with_backoff（Phase 4 用）+ tagged_capture（Phase 1 起用）。

對應 spec docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md §4.1。
無新 dependency；retry/breaker 套件由 utils/circuit_breaker.py 在 Phase 3 提供。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

TagType = Literal["line", "supabase", "external_http"]
_VALID_TAGS: frozenset[str] = frozenset({"line", "supabase", "external_http"})


def tagged_capture(
    exc: BaseException,
    tag: TagType,
    *,
    level: Literal["error", "warning"] = "error",
) -> None:
    """上報 exception 到 Sentry，scope 帶 tag='external' + tag=<tag>。

    Args:
        exc: 要上報的 exception
        tag: 'line' / 'supabase' / 'external_http' 三選一
        level: Sentry event level

    行為：
    - settings.sentry.tag_external_failures=False → no-op（test 友善）
    - sentry_sdk 未 init → 內部 capture_exception 自動 no-op（utils.sentry_init 既有保護）
    - 任何 sentry 錯誤都吞掉（不能傳染回主邏輯）

    Phase 1 內 line_service 4xx 分流由 caller 自行決定 level / tag；本 helper 不分流。
    """
    if tag not in _VALID_TAGS:
        raise ValueError(f"tag must be one of {_VALID_TAGS!r}, got {tag!r}")

    from config import settings as _settings

    if not getattr(_settings.sentry, "tag_external_failures", True):
        return

    try:
        import sentry_sdk
        from utils.sentry_init import capture_exception as _capture

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("external", tag)
            _capture(exc, level=level)
    except Exception:  # noqa: BLE001 — Sentry 錯誤不能往上傳
        logger.debug("tagged_capture failed (silenced)", exc_info=True)


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_seconds: float = 1.0,
    cap_seconds: float = 10.0,
    jitter: float = 0.2,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Exponential backoff with ±jitter%. 拋出最後一次 exception。

    Args:
        fn: 無參數可呼叫；用 lambda 包裝有參數的 caller
        attempts: 總嘗試次數（不是「retry 次數」；attempts=1 表示不重試）
        base_seconds: 第 1 次 retry 前 sleep 秒數，之後 2x 指數成長
        cap_seconds: sleep 上限（避免極端情況一次睡幾分鐘）
        jitter: ±jitter 比例隨機抖動，避免 thundering herd
        retry_on: 只有屬於這些 type 的 exception 才重試；其他直接拋

    Phase 1 不被任何 caller 呼叫；Phase 4 SupabaseStorage.save 包此 helper。
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            delay = min(base_seconds * (2**i), cap_seconds)
            factor = random.uniform(1.0 - jitter, 1.0 + jitter)
            time.sleep(delay * factor)
    assert last_exc is not None  # for type checker
    raise last_exc
