"""新生名額規劃 API smoke：router 匯入、路徑註冊、聚合進 main.app。
行為（守衛/彙總/upsert）由 service 層測試覆蓋（test_recruitment_intake_plan.py）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_intake_router_imports_and_paths():
    from api.recruitment.intake import router

    paths = {r.path for r in router.routes}
    assert any(p.endswith("/reserve-seat") for p in paths)
    assert any(p.endswith("/intake-plan") for p in paths)
    assert any(p.endswith("/intake-targets") for p in paths)


def test_main_app_includes_intake_routes():
    import main

    app_paths = {r.path for r in main.app.routes}
    assert any("reserve-seat" in p for p in app_paths)
    assert any("intake-plan" in p for p in app_paths)
    assert any("intake-targets" in p for p in app_paths)
