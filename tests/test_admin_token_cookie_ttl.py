"""admin_token cookie 壽命須對齊 JWT 15min，不可沿用 access_token 的 24h grace。

P2/#8（qa-loop 全掃 2026-06-17，業主裁示「降級不修只縮 cookie 壽命」）：
admin_token cookie max_age 原為 86400（24h），但內含 JWT 僅 15min 過期且模擬 token
不可刷新；24h 的 cookie 壽命讓「未正常 end-impersonate 的舊 admin_token」可殘留至多
24h，被下一次 impersonate 備份/還原而張冠李戴。縮短至對齊 JWT_EXPIRE_MINUTES 降低殘留窗口。
（access_token cookie 的 24h 是 refresh grace，刻意保留、不受本修補影響。）
"""

from __future__ import annotations

from starlette.responses import Response

from utils.auth import JWT_EXPIRE_MINUTES
from utils.cookie import set_access_token_cookie, set_admin_token_cookie


def test_admin_token_cookie_max_age_aligns_jwt_not_24h():
    resp = Response()
    set_admin_token_cookie(resp, "dummy-token")
    header = resp.headers.get("set-cookie") or ""
    assert "admin_token=" in header
    assert f"Max-Age={JWT_EXPIRE_MINUTES * 60}" in header, (
        f"admin_token cookie max_age 應對齊 JWT {JWT_EXPIRE_MINUTES}min "
        f"(={JWT_EXPIRE_MINUTES * 60}s)，實際 header：{header}"
    )
    assert (
        "Max-Age=86400" not in header
    ), "admin_token 不應沿用 24h grace（殘留窗口過長）"


def test_access_token_cookie_keeps_24h_grace_unchanged():
    """守衛：access_token cookie 的 24h refresh grace 不受 admin cookie 修補影響。"""
    resp = Response()
    set_access_token_cookie(resp, "dummy-token")
    header = resp.headers.get("set-cookie") or ""
    assert "access_token=" in header
    assert "Max-Age=86400" in header, f"access_token cookie 仍應為 24h，實際：{header}"
