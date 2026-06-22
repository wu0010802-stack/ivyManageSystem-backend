"""Quick Win B（2026-06-22）：才藝 admin 裸 dict 端點補 response_model。

原本 13 個 admin 端點（家長提問 / 報名項目增刪 / 繳退費 / 批次付款）回裸 dict，
OpenAPI 無具名 schema → 前端 codegen 只能拿到 unknown。本批宣告 response_model
後 OpenAPI 有具名型別。此測試鎖定「這些端點確有宣告 response_model」，避免日後
新端點又回裸 dict 漂移。

shape 正確性由各模組既有測試（test_activity_api / pos / fee / payment 等）守護：
若 response_model 漏欄位 / 型別不符，既有斷言會 fail。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.activity import router as activity_router

# (path, method) → 這些端點必須有具名 response_model（非裸 dict）。
# path 為 activity 子 router 內路徑（main.py 掛 /activity prefix，這裡不含）。
EXPECTED = [
    ("/inquiries", "GET"),
    ("/inquiries/{inquiry_id}/read", "PUT"),
    ("/inquiries/{inquiry_id}/reply", "PUT"),
    ("/inquiries/{inquiry_id}", "DELETE"),
    ("/registrations/{registration_id}/courses", "POST"),
    ("/registrations/{registration_id}/supplies", "POST"),
    ("/registrations/{registration_id}/supplies/{supply_record_id}", "DELETE"),
    ("/registrations/{registration_id}/courses/{course_id}", "DELETE"),
    ("/registrations/{registration_id}/payment", "PUT"),
    ("/registrations/{registration_id}/payments", "GET"),
    ("/registrations/{registration_id}/payments", "POST"),
    ("/registrations/{registration_id}/payments/{payment_id}", "DELETE"),
    ("/registrations/batch-payment", "PUT"),
]


# 聚合 router 的 route.path 帶 /api/activity 前綴（main.py include 時加），
# EXPECTED 用子 router 內路徑，比對時補前綴做精確 match（避免 /public/inquiries 撞）。
_PREFIX = "/api/activity"


def _find_route(path: str, method: str):
    full = _PREFIX + path
    for r in activity_router.routes:
        if getattr(r, "path", None) == full and method in getattr(r, "methods", set()):
            return r
    return None


@pytest.mark.parametrize("path,method", EXPECTED)
def test_endpoint_declares_response_model(path, method):
    route = _find_route(path, method)
    assert route is not None, f"找不到端點 {method} {path}"
    assert (
        route.response_model is not None
    ), f"{method} {path} 應宣告 response_model（不可回裸 dict 造成 OpenAPI unknown）"
