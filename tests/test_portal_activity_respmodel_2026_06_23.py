"""Portal 才藝自包含端點補 response_model（雜項，2026-06-23）。

registrations / batch-records 原回裸 dict → OpenAPI 無具名 schema → 前端 codegen
unknown（C 波在 PortalActivityView 留 TODO cast）。本批為兩個**自包含**端點宣告
response_model（sessions list/detail 走與 admin 共用的 helper，留待共用建模）。

shape 正確性由既有 portal 測試守護；此檔僅鎖定 response_model 已宣告，防再漂移成裸 dict。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.portal.activity import router

EXPECTED = [
    ("/activity/registrations", "GET"),
    ("/activity/attendance/sessions/{session_id}/records", "PUT"),
]


@pytest.mark.parametrize("path,method", EXPECTED)
def test_portal_endpoint_declares_response_model(path, method):
    route = next(
        (
            r
            for r in router.routes
            if getattr(r, "path", None) == path
            and method in getattr(r, "methods", set())
        ),
        None,
    )
    assert route is not None, f"找不到端點 {method} {path}"
    assert route.response_model is not None, f"{method} {path} 應宣告 response_model"


def test_registrations_summary_optional_for_empty_branch():
    # 早返回 {classrooms:[],registrations:[]} 不帶 summary → 模型 summary 須 Optional，
    # 否則 ResponseValidationError 500。直接驗模型可接受無 summary。
    from api.portal.activity import PortalRegistrationsOut

    obj = PortalRegistrationsOut.model_validate({"classrooms": [], "registrations": []})
    assert obj.summary is None
