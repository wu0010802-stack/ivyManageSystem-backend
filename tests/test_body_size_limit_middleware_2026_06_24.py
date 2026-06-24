"""崩潰防護 P2：全域 request body 大小上限 middleware，防超大 body 撐爆單 worker 記憶體。

Starlette/uvicorn 預設不限 body 大小；數百 MB 的單一 body 會在驗證前被收進記憶體。
BodySizeLimitMiddleware：Content-Length 預檢超限 → 413；streaming 超限 → 截斷 body。
"""

import os
import sys

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from middleware.body_size_limit import BodySizeLimitMiddleware

_LIMIT = 1000


def _make_client(
    limit: int = _LIMIT, *, raise_server_exceptions: bool = True
) -> TestClient:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=limit)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_small_body_passes():
    client = _make_client()
    res = client.post("/echo", content=b"x" * 100)
    assert res.status_code == 200
    assert res.json()["len"] == 100


def test_oversized_content_length_rejected_with_413():
    client = _make_client()
    res = client.post("/echo", content=b"x" * (_LIMIT + 500))
    assert res.status_code == 413, f"超大 body 應 413，實得 {res.status_code}"


def test_streaming_body_truncated_when_over_limit():
    """無 Content-Length 的 chunked：超限應截斷 body（記憶體上限收斂），不得完整收進。"""
    # 截斷 body 會讓下游 await request.body() 觸發 ClientDisconnect → 非 200；
    # 用 raise_server_exceptions=False 把它視為「已拒絕」回應而非 re-raise。
    client = _make_client(raise_server_exceptions=False)

    def gen():
        for _ in range(40):
            yield b"x" * 100  # 共 4000 bytes > limit

    res = client.post("/echo", content=gen())
    # 記憶體上限收斂：要嘛被截斷後下游讀到 ≤ 上限的 body（200），要嘛回 4xx/5xx。
    if res.status_code == 200:
        assert res.json()["len"] <= _LIMIT + 100, "streaming 未在超限後截斷 body"
    else:
        assert res.status_code >= 400
