"""AUTHZ-TEST-2：mutation 端點授權守衛的結構性覆蓋 sweep。

斷言 main.app 每個 mutation 端點（POST/PUT/PATCH/DELETE）的 dependant 樹都掛了
已知授權守衛 dependency（require_permission / require_staff_permission /
require_admin / require_*_role / require_current_consent），
否則必須明確列入 KNOWN_UNGUARDED 白名單並附理由。

Why（設計稽核 2026-06-17, AUTHZ-TEST-2）：授權守衛散落在各 handler 與前端，缺乏
結構性兜底；新增端點若忘了掛守衛，只能靠人記得手寫該端點的 403 測試，否則靜默
fail-open（2026-06-04 滲透測試 #1 教師撞考核管理端即此類根因）。本 sweep 把「漏掛
守衛」從靜默放行變成 CI 擋線。

baseline 模式：白名單記錄當前刻意「無 dependency 守衛」者——公開報名/查詢、
webhook、認證端點、登入者自助、以及少數 in-body 動態守衛端點。新增任何不在白名單
的未守衛 mutation 端點即 fail，迫使開發者「掛守衛」或「有意識地加白名單並寫理由」。

注意：本 sweep 只檢查「是否存在某個授權守衛」，不檢查「守衛是否為正確/足夠的權限」
（後者屬各端點 per-feature 403 測試與 scope 測試的範疇）。
"""

import os
import sys

from fastapi.routing import APIRoute

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main

# dependant 樹中，授權守衛 dependency 的 __qualname__ 標記。
# 守衛工廠回傳的 closure，其 __qualname__ 帶外層工廠名（如
# 'require_permission.<locals>.check_permission'），故以子字串比對即可辨識且不誤判。
_GUARD_MARKERS = (
    "require_permission",
    "require_staff_permission",
    "require_admin",
    "require_parent_role",
    "require_non_parent_role",
    "require_current_consent",
)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


# 刻意「無 dependency 授權守衛」的 mutation 端點（"METHOD path" → 理由）。
# 新增端點預設應掛守衛；確需無守衛者（公開/認證/自助/in-body 守衛）才加進此白名單。
KNOWN_UNGUARDED: dict[str, str] = {
    # ── 公開端點（匿名可達：公開報名 / 查詢 / webhook）──
    "POST /api/activity/public/inquiries": "公開才藝諮詢（匿名）",
    "POST /api/activity/public/query": "公開報名查詢（匿名）",
    "POST /api/activity/public/query-by-token": "公開報名查詢（query token）",
    "POST /api/activity/public/register": "公開才藝報名（匿名）",
    "POST /api/activity/public/registrations/{registration_id}/courses/{course_id}/confirm-promotion": "家長憑 query token 確認候補轉正（in-body token 守衛）",
    "POST /api/activity/public/registrations/{registration_id}/courses/{course_id}/decline-promotion": "家長憑 query token 婉拒候補（in-body token 守衛）",
    "POST /api/activity/public/update": "公開報名修改（in-body query_token 守衛，資安 #5）",
    "POST /api/internal/uptime-webhook": "uptime 監控 webhook（無 JWT）",
    "POST /api/line/webhook": "LINE webhook（簽章驗證，非 JWT）",
    # ── 認證端點（認證本身 / 登入者自助）──
    "POST /api/auth/login": "登入（認證端點）",
    "POST /api/auth/refresh": "access token 刷新（refresh 機制）",
    "POST /api/auth/logout": "登出（自助）",
    "POST /api/auth/change-password": "改自己密碼（get_current_user 自助）",
    "POST /api/auth/sessions/logout-all": "登出自己所有 session（自助）",
    "DELETE /api/auth/sessions/{family_id}": "撤銷自己某 session（自助）",
    "POST /api/auth/impersonate": "冒充（in-body PORTAL_PREVIEW/IMPERSONATE 守衛）",
    "POST /api/auth/end-impersonate": "結束冒充（特殊 impersonation token 處理）",
    # ── 家長端認證 ──
    "POST /api/parent/auth/liff-login": "家長 LIFF 登入（認證端點）",
    "POST /api/parent/auth/bind": "家長綁定（特殊 bind token）",
    "POST /api/parent/auth/device-setup": "家長裝置設定（特殊 token）",
    "POST /api/parent/auth/refresh": "家長 token 刷新",
    # ── in-body 動態守衛 ──
    "POST /api/recruitment/funnel/visits/{visit_id}/transition": "funnel 階段轉移（in-body 擋 teacher/parent + per-stage 權限，GUARD-2）",
}


def _collect_dependency_qualnames(dependant, acc: list[str]) -> None:
    """遞迴收集 dependant 樹（含 router-level 與 path-level dependencies）所有
    dependency callable 的 __qualname__。"""
    for sub in dependant.dependencies:
        call = getattr(sub, "call", None)
        if call is not None:
            acc.append(getattr(call, "__qualname__", str(call)))
        _collect_dependency_qualnames(sub, acc)


def _route_has_guard(route: APIRoute) -> bool:
    quals: list[str] = []
    if route.dependant is not None:
        _collect_dependency_qualnames(route.dependant, quals)
    return any(any(m in q for m in _GUARD_MARKERS) for q in quals)


def _unguarded_mutation_keys() -> set[str]:
    keys: set[str] = set()
    for r in main.app.routes:
        if not isinstance(r, APIRoute):
            continue
        methods = (r.methods or set()) & _MUTATING
        if not methods or _route_has_guard(r):
            continue
        for m in methods:
            keys.add(f"{m} {r.path}")
    return keys


def test_no_new_unguarded_mutation_endpoint():
    """每個 mutation 端點都須掛授權守衛，或明確列入 KNOWN_UNGUARDED 白名單。"""
    unguarded = _unguarded_mutation_keys()
    new_unguarded = sorted(unguarded - set(KNOWN_UNGUARDED))
    assert not new_unguarded, (
        "以下 mutation 端點未掛任何授權守衛 dependency 且不在 KNOWN_UNGUARDED 白名單：\n"
        + "\n".join(f"  - {k}" for k in new_unguarded)
        + "\n修法：掛 require_staff_permission / require_permission 等守衛；"
        "若刻意無 dependency 守衛（公開/認證/自助/in-body 守衛），請加進"
        " tests/test_mutation_guard_coverage.py 的 KNOWN_UNGUARDED 並寫理由。"
    )


def test_allowlist_has_no_stale_entries():
    """白名單不得有過時項：每個白名單 key 都應仍是一個『實際存在且未守衛』的
    mutation 端點（端點已加守衛或被刪除時，提醒清掉白名單，避免它默默放行未來新洞）。"""
    unguarded = _unguarded_mutation_keys()
    stale = sorted(set(KNOWN_UNGUARDED) - unguarded)
    assert not stale, (
        "以下 KNOWN_UNGUARDED 白名單項已不再是『未守衛的 mutation 端點』"
        "（端點已加守衛或已移除），請從白名單刪除：\n"
        + "\n".join(f"  - {k}" for k in stale)
    )
