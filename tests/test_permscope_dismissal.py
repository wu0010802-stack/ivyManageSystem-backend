"""Phase 2.3 DISMISSAL router migration regression tests.

驗證 api/portal/dismissal_calls.py 4 個 endpoint 已正確接上
`is_unrestricted(code=Permission.DISMISSAL_CALLS_*.value)` gate：
- True：classroom_ids = None 跳過班級限制（資深老師 :all scope 跨班看／處理全校）
- False：保留既有 _get_teacher_classroom_ids 行為（teacher :own_class 限自班）

是 source-level guard 測試（與 permscope01/02/03 router 端 surface-level test 同模式），
runtime 行為由 utils/portfolio_access.is_unrestricted 與 permscope04 migration backfill
共同保證。
"""

import inspect


def test_dismissal_calls_router_uses_is_unrestricted_with_code():
    """api/portal/dismissal_calls.py 4 endpoint 都應有 is_unrestricted(code=) gate。"""
    import api.portal.dismissal_calls as mod

    source = inspect.getsource(mod)

    # 必匯入 is_unrestricted
    assert (
        "is_unrestricted" in source
    ), "api/portal/dismissal_calls.py 應 import is_unrestricted"

    # READ endpoint（list / pending-count）必呼叫 is_unrestricted(code=Permission.DISMISSAL_CALLS_READ.value)
    assert (
        "code=Permission.DISMISSAL_CALLS_READ.value" in source
    ), "READ endpoint 必傳 code=Permission.DISMISSAL_CALLS_READ.value 啟用 :all scope"

    # WRITE endpoint（acknowledge / complete）必呼叫 is_unrestricted(code=Permission.DISMISSAL_CALLS_WRITE.value)
    assert (
        "code=Permission.DISMISSAL_CALLS_WRITE.value" in source
    ), "WRITE endpoint 必傳 code=Permission.DISMISSAL_CALLS_WRITE.value 啟用 :all scope"


def test_dismissal_calls_router_unrestricted_skips_classroom_filter():
    """is_unrestricted=True 時必須跳過 _get_teacher_classroom_ids 班級限制。

    確認 source 含 unrestricted 邏輯分支（不單純把結果丟掉用）：
    list / pending-count 端點需在 unrestricted=True 時不 filter classroom_id；
    transition helper 在 unrestricted=True 時不檢查 call.classroom_id in classroom_ids。
    """
    import api.portal.dismissal_calls as mod

    source = inspect.getsource(mod)

    # classroom_ids = None sentinel pattern（unrestricted 跳班級限制）
    assert (
        "classroom_ids = None" in source
    ), "unrestricted=True 需設 classroom_ids = None 作為 'no filter' sentinel"

    # SQL filter / 403 check 需 guard `if classroom_ids is not None`
    # （兩端點 + transition helper 共三處 guard）
    assert (
        source.count("if classroom_ids is not None") >= 2
    ), "list / pending-count 兩端點需 guard SQL filter"
