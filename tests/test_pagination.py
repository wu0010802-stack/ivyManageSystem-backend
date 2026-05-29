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
from utils.pagination import PaginationParams, paginated_params, paginate
from models.database import Employee


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


def _seed_employees(session, n: int):
    """Helper：建 n 個 employee 給 paginate 測試（順序由 id 決定）。"""
    for i in range(n):
        session.add(
            Employee(
                employee_id=f"P{i:03d}",
                name=f"員工{i}",
                position="教師",
            )
        )
    session.commit()


def test_paginate_empty_query(test_db_session):
    """空 query → ([], 0)。"""
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert items == []
    assert total == 0


def test_paginate_first_page(test_db_session):
    """25 列、page=1 size=10 → 回 10 列 + total=25。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert len(items) == 10
    assert total == 25
    assert items[0].employee_id == "P000"
    assert items[-1].employee_id == "P009"


def test_paginate_last_partial_page(test_db_session):
    """25 列、page=3 size=10 → 回剩餘 5 列 + total=25。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=3, page_size=10))
    assert len(items) == 5
    assert total == 25
    assert items[0].employee_id == "P020"
    assert items[-1].employee_id == "P024"


def test_paginate_overshoot_returns_empty(test_db_session):
    """25 列、page=99 → 回空 list 不 raise，total 仍正確。"""
    _seed_employees(test_db_session, 25)
    q = test_db_session.query(Employee).order_by(Employee.id)
    items, total = paginate(q, PaginationParams(page=99, page_size=10))
    assert items == []
    assert total == 25


def test_paginate_preserves_order_by(test_db_session):
    """order_by 由呼叫端設定，paginate 不改動。"""
    _seed_employees(test_db_session, 5)
    q = test_db_session.query(Employee).order_by(Employee.id.desc())
    items, total = paginate(q, PaginationParams(page=1, page_size=10))
    assert [e.employee_id for e in items] == ["P004", "P003", "P002", "P001", "P000"]
