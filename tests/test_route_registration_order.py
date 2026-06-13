"""路由註冊順序回歸測試（route shadowing）。

FastAPI 依「註冊順序」匹配路由：`/api/students/{student_id}`（int path param）
若先於同 method 的靜態子路由（如 GET `/api/students/communications`、
GET `/api/students/change-logs`）註冊，請求會被動態路由先攔下，把字串
"communications" 當 student_id 解析 → 422 int_parsing。

實害（2026-06-13 用戶回報）：學生詳細資料的「溝通紀錄」「異動紀錄」tab
整個失效。既有單元測試只把單一 router 掛進迷你 app，照不到跨 router 的
遮蔽，故本測試直接檢查 main.app 的完整路由表順序。

注意：遮蔽必須「path 先匹配 + method 重疊」才成立——例如 POST
/api/students/bulk-transfer 不會被 GET/PUT/DELETE /{student_id} 攔下，
所以比對必須 method-aware，否則誤報。
"""

import os
import sys

from fastapi.routing import APIRoute

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main

_DYNAMIC = "/api/students/{student_id}"


def _api_routes() -> list[APIRoute]:
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def test_static_student_subroutes_not_shadowed_by_student_id():
    """/api/students/<靜態單段> 的每個 method 不得被先註冊的 {student_id} 同 method 路由遮蔽。"""
    routes = _api_routes()
    dynamic_first_index: dict[str, int] = {}
    for i, r in enumerate(routes):
        if r.path == _DYNAMIC:
            for m in r.methods or set():
                dynamic_first_index.setdefault(m, i)
    assert dynamic_first_index, f"找不到 {_DYNAMIC} 路由"

    static_routes = [
        (i, r)
        for i, r in enumerate(routes)
        if r.path.startswith("/api/students/")
        and "{" not in r.path
        and "/" not in r.path[len("/api/students/") :]
    ]
    static_paths = {r.path for _, r in static_routes}
    # 至少要涵蓋已知的兩個受害者，避免 prefix 改名後測試靜默變空集合
    assert "/api/students/communications" in static_paths
    assert "/api/students/change-logs" in static_paths

    shadowed = [
        f"{m} {r.path}"
        for i, r in static_routes
        for m in r.methods or set()
        if m in dynamic_first_index and dynamic_first_index[m] < i
    ]
    assert not shadowed, (
        f"以下靜態路由註冊在 {_DYNAMIC} 之後且 method 重疊，會被遮蔽成 422："
        f"{shadowed}（修法：main.py 中將其 include_router 移到 students_router 之前）"
    )
