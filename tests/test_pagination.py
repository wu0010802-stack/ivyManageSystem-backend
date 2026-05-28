"""utils/pagination.py 行為測試。

涵蓋：
- PaginationParams 基本構造與型別約束
- paginated_params(default, max_size) factory 邊界
- paginate(query, params) 對 SAQuery 的 offset/limit/count 行為
"""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from pydantic import ValidationError
from utils.pagination import PaginationParams, paginated_params


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


def _make_test_app(default: int = 20, max_size: int = 200):
    """Helper：建一個小 FastAPI app 注入 paginated_params 拿來測 factory。"""
    app = FastAPI()

    @app.get("/test")
    def _endpoint(
        p: PaginationParams = Depends(
            paginated_params(default=default, max_size=max_size)
        )
    ):
        return {"page": p.page, "page_size": p.page_size}

    return TestClient(app)


def test_paginated_params_default_values():
    """不傳參數時，使用 factory default。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test")
    assert r.status_code == 200
    assert r.json() == {"page": 1, "page_size": 50}


def test_paginated_params_custom_values():
    """傳 query string 覆蓋 default。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test?page=3&page_size=10")
    assert r.status_code == 200
    assert r.json() == {"page": 3, "page_size": 10}


def test_paginated_params_rejects_page_zero():
    """page < 1 → 422（FastAPI Query ge 驗證）。"""
    client = _make_test_app(default=50, max_size=200)
    r = client.get("/test?page=0")
    assert r.status_code == 422


def test_paginated_params_rejects_page_size_over_max():
    """page_size > max_size → 422。"""
    client = _make_test_app(default=20, max_size=100)
    r = client.get("/test?page_size=500")
    assert r.status_code == 422


def test_paginated_params_max_size_override():
    """factory max_size 高於 default 時，page_size 上限放寬。"""
    client = _make_test_app(default=100, max_size=500)
    r = client.get("/test?page_size=300")
    assert r.status_code == 200
    assert r.json()["page_size"] == 300
