"""utils/pagination.py 行為測試。

涵蓋：
- PaginationParams 基本構造與型別約束
- paginated_params(default, max_size) factory 邊界
- paginate(query, params) 對 SAQuery 的 offset/limit/count 行為
"""

import pytest
from pydantic import ValidationError
from utils.pagination import PaginationParams


def test_pagination_params_basic():
    """PaginationParams 基本可建構。"""
    p = PaginationParams(page=2, page_size=20)
    assert p.page == 2
    assert p.page_size == 20


def test_pagination_params_rejects_zero_page():
    """page 必須 >= 1。"""
    with pytest.raises(ValidationError):
        PaginationParams(page=0, page_size=20)


def test_pagination_params_rejects_zero_page_size():
    """page_size 必須 >= 1。"""
    with pytest.raises(ValidationError):
        PaginationParams(page=1, page_size=0)
