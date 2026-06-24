"""崩潰防護 P2：公開查詢端點的 timing 延遲不可佔住 threadpool token。

public_query_registration / public_query_by_token 為 sync def，開頭 time.sleep(0.2~0.5)
做 timing-attack 緩解。sync def 跑在 AnyIO threadpool（token 上限 = pool 容量 + headroom
≈ 28），sleep 期間佔住一個 token；併發公開查詢 + IP 共桶（TRUSTED_PROXY_IPS 未設）下
可放大成全站 sync 路由排隊延遲。

修法：改 async def + await asyncio.sleep（延遲不佔 token），DB 工作經 await
asyncio.to_thread 丟 threadpool（仍不在 event loop 上跑同步 DB）。
"""

import inspect

from api.activity import public as public_module


def test_public_query_is_async_and_nonblocking():
    fn = public_module.public_query_registration
    assert inspect.iscoroutinefunction(fn), "public_query_registration 應為 async def"
    src = inspect.getsource(fn)
    assert "asyncio.sleep" in src, "timing 延遲應改用 await asyncio.sleep（不佔 token）"
    assert (
        "asyncio.to_thread" in src
    ), "同步 DB 工作應經 asyncio.to_thread 丟 threadpool"
    assert "time.sleep" not in src, "不可再用阻塞 time.sleep（佔住 threadpool token）"


def test_public_query_by_token_is_async_and_nonblocking():
    fn = public_module.public_query_by_token
    assert inspect.iscoroutinefunction(fn), "public_query_by_token 應為 async def"
    src = inspect.getsource(fn)
    assert "asyncio.sleep" in src, "timing 延遲應改用 await asyncio.sleep（不佔 token）"
    assert (
        "asyncio.to_thread" in src
    ), "同步 DB 工作應經 asyncio.to_thread 丟 threadpool"
    assert "time.sleep" not in src, "不可再用阻塞 time.sleep（佔住 threadpool token）"
