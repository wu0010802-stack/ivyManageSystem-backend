"""scripts/classify_async_handlers.py 的單元測試。

驗證 AST 分類函式對各種 route handler 形態的判定：
- async def 無 await → convert
- async def 唯一 await=asyncio.sleep → convert
- async def 含真 await（ws/檔案/broadcast）→ keep
- async def 含 async with / async for → keep（即使無 Await 節點）
- @websocket 裝飾器 → 一律 keep
- 已是 def 的 handler → skip
- 非 router-decorated 的 async def helper → 不列入轉換（non_handler_skip）
- 巢狀 async def 內的 await 不計入外層 handler
"""

from scripts.classify_async_handlers import classify_source


def _by_func(results):
    return {r["func"]: r for r in results}


def test_async_get_no_await_is_convert():
    src = """
@router.get("/x")
async def h_no_await():
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_no_await"]["decision"] == "convert"
    assert res["h_no_await"]["decorator"] == "get"
    assert res["h_no_await"]["await_names"] == []


def test_async_post_only_asyncio_sleep_is_convert():
    src = """
@router.post("/x")
async def h_sleep_only():
    await asyncio.sleep(0.3)
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_sleep_only"]["decision"] == "convert"
    assert res["h_sleep_only"]["await_names"] == ["asyncio.sleep"]


def test_async_post_real_await_is_keep():
    src = """
@router.post("/x")
async def h_real_await():
    await ws.close()
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_real_await"]["decision"] == "keep"
    assert "ws.close" in res["h_real_await"]["await_names"]


def test_async_mixed_sleep_and_real_await_is_keep():
    """同時有 asyncio.sleep 與真 await → keep（驗證蒐集所有 await 而非第一個）。"""
    src = """
@router.post("/x")
async def h_mixed():
    await asyncio.sleep(0.2)
    data = await request.json()
    return data
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_mixed"]["decision"] == "keep"
    assert "asyncio.sleep" in res["h_mixed"]["await_names"]
    assert "request.json" in res["h_mixed"]["await_names"]


def test_websocket_decorator_no_await_is_keep():
    src = """
@router.websocket("/ws")
async def h_ws():
    return None
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_ws"]["decision"] == "keep"
    assert res["h_ws"]["decorator"] == "websocket"


def test_sync_def_handler_is_skip():
    src = """
@router.get("/x")
def h_sync():
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_sync"]["decision"] == "skip"


def test_non_router_async_helper_not_converted():
    """非 router-decorated 的 async def helper（會被別處 await）一律不列入 convert。"""
    src = """
async def helper_dep():
    return 1

@router.get("/x")
async def h():
    val = await helper_dep()
    return val
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    # helper 不可被標成 convert
    assert res["helper_dep"]["decision"] == "non_handler_skip"
    # 真 handler 因為 await helper_dep() 是真 await → keep
    assert res["h"]["decision"] == "keep"


def test_async_with_makes_handler_keep_even_without_await_node():
    """async with 即真 async，即使 body 內無 Await 節點 → keep。"""
    src = """
@router.post("/x")
async def h_async_with():
    async with some_lock:
        pass
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_async_with"]["decision"] == "keep"


def test_async_for_makes_handler_keep():
    src = """
@router.get("/x")
async def h_async_for():
    async for item in stream():
        process(item)
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_async_for"]["decision"] == "keep"


def test_nested_async_def_await_not_counted():
    """巢狀 async def 內的 await 不計入外層 handler；外層無自身 await → convert。"""
    src = """
@router.get("/x")
async def h_outer():
    async def inner():
        await something()
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    # inner 的 await 不計入 h_outer → h_outer 無真 await → convert
    assert res["h_outer"]["decision"] == "convert"
    assert res["h_outer"]["await_names"] == []


def test_multiple_decorators_router_method_wins():
    """疊多個 decorator，只要其一是 router HTTP-method 即視為 handler。"""
    src = """
@some_wrapper
@router.delete("/x")
async def h_multi():
    return 1
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_multi"]["decision"] == "convert"
    assert res["h_multi"]["decorator"] == "delete"


def test_non_call_await_does_not_crash():
    """await some_var（非 Call）不可讓 scan crash，且視為真 await → keep。"""
    src = """
@router.get("/x")
async def h_await_var():
    coro = something()
    result = await coro
    return result
"""
    res = _by_func(classify_source(src, "api/sample.py"))
    assert res["h_await_var"]["decision"] == "keep"
