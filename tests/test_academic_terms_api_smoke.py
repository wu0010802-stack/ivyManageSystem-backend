"""Smoke tests for academic_terms API:
- module imports work
- router has expected routes
- main.py includes the router with correct paths
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_import_router() -> None:
    """api.academic_terms 能正常匯入，router 物件存在。"""
    from api.academic_terms import router

    assert router is not None


def test_routes_registered() -> None:
    """router 的路由清單包含 list、/current、/{term_id} 三種路徑。"""
    from api.academic_terms import router

    paths = {r.path for r in router.routes}  # type: ignore[union-attr]
    # APIRouter prefix="/api/academic-terms" → routes show full path
    assert any(
        p.endswith("/api/academic-terms") for p in paths
    ), f"list path missing: {paths}"
    assert any("/current" in p for p in paths), f"/current path missing: {paths}"
    assert any("{term_id}" in p for p in paths), f"/{{term_id}} path missing: {paths}"


def test_main_app_includes_router() -> None:
    """main.app 已包含 /api/academic-terms 路由。"""
    import main

    app_paths = {r.path for r in main.app.routes}  # type: ignore[union-attr]
    assert any(
        "/api/academic-terms" in p for p in app_paths
    ), f"/api/academic-terms not found in app routes: {app_paths}"
