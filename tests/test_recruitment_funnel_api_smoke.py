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
