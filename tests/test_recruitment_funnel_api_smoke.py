"""Smoke test for /recruitment/funnel API:
- module imports cleanly
- routes registered at expected paths
- permission helper returns expected sets
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_import_router():
    from api.recruitment.funnel import router

    assert router is not None


def test_routes_registered():
    from api.recruitment.funnel import router

    paths = {r.path for r in router.routes}
    assert any(p.endswith("/board") for p in paths)
    assert any("/transition" in p for p in paths)
    assert any("/timeline" in p for p in paths)


def test_main_app_includes_routes():
    import main

    app_paths = {r.path for r in main.app.routes}
    assert any("funnel/board" in p for p in app_paths)
    assert any("funnel/visits" in p and "transition" in p for p in app_paths)


def test_required_permissions_mapping():
    from api.recruitment.funnel import _required_permissions
    from utils.permissions import Permission

    assert Permission.RECRUITMENT_WRITE in _required_permissions("visited", "deposited")
    assert Permission.RECRUITMENT_CONVERT in _required_permissions(
        "deposited", "enrolled"
    )
    perms = _required_permissions("enrolled", "deposited")
    assert Permission.RECRUITMENT_CONVERT in perms
    assert Permission.STUDENTS_WRITE in perms
    assert Permission.STUDENTS_WRITE in _required_permissions("enrolled", "active")


def test_funnel_routes_under_api_recruitment_prefix():
    """funnel 路由必須掛在 /api/recruitment/funnel（與 7 個兄弟 recruitment router 一致）。

    Bug（2026-06-15 運作探測 P1-3）：funnel.py router prefix 誤為 "/funnel"，
    使三條路由落在 /api 命名空間外 → 前端 axios baseURL=/api 送 /api/funnel/*
    → 404；即使打對 /funnel/*，httpOnly cookie path=/api 不送 → 401，
    「招生入學」預設漏斗看板分頁真實瀏覽器完全載不出來。
    """
    import main

    funnel_paths = [
        r.path for r in main.app.routes if hasattr(r, "path") and "funnel" in r.path
    ]
    assert funnel_paths, "funnel 路由未註冊"
    for p in funnel_paths:
        assert p.startswith(
            "/api/recruitment/funnel"
        ), f"funnel 路由 {p} 未掛在 /api/recruitment 命名空間下（前端會 404/401）"
