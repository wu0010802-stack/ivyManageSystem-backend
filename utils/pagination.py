"""utils/pagination.py — 共用分頁工具。

慣例：所有 list endpoint 一律：
1. Query 參數 page + page_size（透過 paginated_params() 注入）
2. Response shape {items, total, page, page_size}
3. 呼叫 paginate(query, pagination) 取得 (items, total)

範例：
    from utils.pagination import PaginationParams, paginated_params, paginate

    @router.get("/items")
    def list_items(
        pagination: PaginationParams = Depends(paginated_params(default=50, max_size=200)),
    ):
        q = session.query(Item).order_by(Item.created_at.desc())
        items, total = paginate(q, pagination)
        return {
            "items": [...],
            "total": total,
            "page": pagination.page,
            "page_size": pagination.page_size,
        }
"""

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Query as SAQuery


class PaginationParams(BaseModel):
    """FastAPI Depends() 注入的分頁參數。

    透過 paginated_params() factory 產生，default/max_size 由呼叫端決定。
    """

    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


def paginated_params(default: int = 20, max_size: int = 200):
    """產生 PaginationParams Depends factory，支援 per-endpoint default/max。

    Why factory 而非單一 dependency class：不同 endpoint 的合理 page_size default 不同
    （audit_log 50、recruitment_gov_kindergartens 100/最大 500、students 50）。
    Closure 內 default / max_size 進入 FastAPI Query 才能對應 OpenAPI 正確 default，
    避免「全 codebase 同 default」失真。

    用法：
        pagination: PaginationParams = Depends(paginated_params(default=50, max_size=500))
    """

    def _params(
        page: int = Query(1, ge=1, description="第幾頁（從 1 開始）"),
        page_size: int = Query(default, ge=1, le=max_size, description="每頁筆數"),
    ) -> PaginationParams:
        return PaginationParams(page=page, page_size=page_size)

    return _params


def paginate(query: SAQuery, params: PaginationParams) -> tuple[list, int]:
    """對 SAQuery 執行 count + offset/limit，回 (items, total)。

    呼叫端責任：
    - 先 apply 所有 filter + order_by 再傳 query 進來。
    - 自行決定如何 serialize items（不強制 Pydantic model_validate，避免 60+
      endpoint 大量 schema 改寫）。

    Why tuple 而非 dict：呼叫端要回傳的 dict 還有自家 serialize 後的 items
    與其他額外欄位（如 audit_log 加 meta、students 加 has_archived flag），
    讓呼叫端拼最後 dict 較直覺。
    """
    total = query.count()
    items = (
        query.offset((params.page - 1) * params.page_size).limit(params.page_size).all()
    )
    return items, total
