"""Repository 基底類別。

提供所有具體 Repository 共用的 CRUD helper，讓子類別只需實作領域特化的
查詢方法（如 get_with_salary_details、list_active_by_classroom 等）。
"""

from __future__ import annotations

from typing import Any, Generic, Iterable, Optional, Type, TypeVar

from sqlalchemy.orm import Session

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """泛型 Repository 基底類別。

    子類別需設定 `model` class 變數指向 SQLAlchemy model。
    """

    model: Type[T]

    def __init__(self, session: Session):
        if not hasattr(self, "model"):
            raise NotImplementedError(
                f"{self.__class__.__name__} 未設定 class 變數 `model`"
            )
        self.session = session

    # ── 讀取 ───────────────────────────────────────────────────────────
    def get_by_id(self, entity_id: Any) -> Optional[T]:
        return self.session.query(self.model).get(entity_id)

    def exists(self, entity_id: Any) -> bool:
        return self.get_by_id(entity_id) is not None

    def list(
        self,
        *,
        skip: int = 0,
        limit: Optional[int] = None,
        order_by: Any = None,
        **filters: Any,
    ) -> list[T]:
        q = self.session.query(self.model)
        for key, value in filters.items():
            if value is None:
                continue
            col = getattr(self.model, key, None)
            if col is None:
                raise ValueError(
                    f"{self.model.__name__} 無欄位 {key!r}，無法用於 list() 篩選"
                )
            q = q.filter(col == value)
        if order_by is not None:
            q = q.order_by(order_by)
        if skip:
            q = q.offset(skip)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def count(self, **filters: Any) -> int:
        q = self.session.query(self.model)
        for key, value in filters.items():
            if value is None:
                continue
            col = getattr(self.model, key, None)
            if col is None:
                raise ValueError(
                    f"{self.model.__name__} 無欄位 {key!r}，無法用於 count() 篩選"
                )
            q = q.filter(col == value)
        return q.count()

    # ── 寫入（不 commit；交易邊界由 caller 以 session_scope 管理）─────
    def add(self, entity: T) -> T:
        self.session.add(entity)
        return entity

    def add_all(self, entities: Iterable[T]) -> list[T]:
        objs = list(entities)
        self.session.add_all(objs)
        return objs

    def delete(self, entity: T) -> None:
        self.session.delete(entity)

    def flush(self) -> None:
        """部分情境需要在 commit 前取得 autoincrement id 時使用。"""
        self.session.flush()
