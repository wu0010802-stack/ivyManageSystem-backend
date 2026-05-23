"""term.changed 事件 hooks registry。

設計原則：
- 同步、in-process、單一 transaction：caller 持 session、未 commit；
  fire 後依序串註呼叫所有 handler，handler 都在 caller session 上寫；
  任一 handler raise → caller responsibility 整 transaction rollback
- 註冊順序穩定：handler 按 register 順序執行；register 在 module import 時跑，
  startup 顯式 import 一次保證順序
- testability：reset_handlers_for_tests() 清空、register_handler() 不靠 decorator
"""

import logging
from typing import Callable, Optional, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TermLike(Protocol):
    """AcademicTerm 介面 — 用 Protocol 避開 circular import。"""

    id: int
    school_year: int
    semester: int


TermChangedHandler = Callable[..., None]  # signature: (*, old, new, session) -> None


_HANDLERS: list[tuple[str, TermChangedHandler]] = []


def on_term_changed(name: str):
    """Decorator：註冊 handler。name 用於 log、debug、duplicate check。

    用法：
        @on_term_changed("classroom_carry_over")
        def handler(*, old, new, session):
            ...
    """

    def decorator(fn: TermChangedHandler) -> TermChangedHandler:
        register_handler(name, fn)
        return fn

    return decorator


def register_handler(name: str, fn: TermChangedHandler) -> None:
    """顯式註冊（不靠 decorator）。重複 name 會 raise，避免 double-register。"""
    if any(n == name for n, _ in _HANDLERS):
        raise RuntimeError(f"term.changed handler 已註冊：{name}")
    _HANDLERS.append((name, fn))


def fire_term_changed(
    *,
    old: Optional[TermLike],
    new: TermLike,
    session: Session,
) -> None:
    """同步串註呼叫所有 handler。

    Caller contract：
    - 已持有 session 且 transaction 進行中（未 commit）
    - 已完成 AcademicTerm.is_current toggle 的 UPDATE（handler 可看到 new state）
    - handler raise propagate 到 caller 觸發 rollback
    """
    if not _HANDLERS:
        logger.info("term.changed fired but no handler registered")
        return
    for name, handler in _HANDLERS:
        logger.info(
            "term.changed handler 觸發：%s (old=%s, new=%s/%s)",
            name,
            f"{old.school_year}-{old.semester}" if old else None,
            new.school_year,
            new.semester,
        )
        handler(old=old, new=new, session=session)


def reset_handlers_for_tests() -> None:
    """測試專用：清空 handler 註冊表。"""
    _HANDLERS.clear()


def list_handler_names() -> list[str]:
    """debug / 健康檢查用：回傳已註冊 handler 名稱列表。"""
    return [n for n, _ in _HANDLERS]
