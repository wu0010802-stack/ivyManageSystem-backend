"""R6-9：magic-link download token 從 access log 遮罩 middleware。"""

import asyncio

from middleware.magic_link_log_scrub import MagicLinkLogScrubMiddleware


def _run(scope):
    async def app(scope, receive, send):
        return None

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        return None

    asyncio.run(MagicLinkLogScrubMiddleware(app)(scope, receive, send))


def test_masks_token_and_preserves_in_scope():
    scope = {
        "type": "http",
        "path": "/api/offboarding/download",
        "query_string": b"token=secret123&x=1",
    }
    _run(scope)
    assert b"secret123" not in scope["query_string"]
    assert b"__redacted__" in scope["query_string"]
    assert scope["magic_link_token"] == "secret123"
    assert b"x=1" in scope["query_string"]


def test_ignores_other_paths():
    scope = {
        "type": "http",
        "path": "/api/employees",
        "query_string": b"token=abc",
    }
    _run(scope)
    assert scope["query_string"] == b"token=abc"
    assert "magic_link_token" not in scope
