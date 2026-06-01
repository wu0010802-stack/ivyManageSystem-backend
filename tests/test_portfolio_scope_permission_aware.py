"""Phase 2.1 router migration tests：each migrated router 確認 code= 傳入正確 perm。"""


def test_timeline_endpoint_uses_portfolio_read_code():
    """確保 api/portfolio/timeline.py 將 code=PORTFOLIO_READ 傳入 portfolio_access wrapper。"""
    import inspect

    import api.portfolio.timeline as mod

    source = inspect.getsource(mod)
    # 必含 code= 傳 PORTFOLIO_READ
    assert (
        "code=Permission.PORTFOLIO_READ" in source or 'code="PORTFOLIO_READ"' in source
    ), "api/portfolio/timeline.py 應傳 code=PORTFOLIO_READ 至 portfolio_access wrapper"
