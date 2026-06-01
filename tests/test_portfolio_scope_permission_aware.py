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


def test_auto_milestone_endpoint_uses_portfolio_write_code():
    """api/portfolio/auto_milestone.py POST auto-detect 端點為 PORTFOLIO_WRITE。"""
    import inspect

    import api.portfolio.auto_milestone as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_WRITE" in source
    ), "auto_milestone.py 應傳 code=PORTFOLIO_WRITE 至 portfolio_access wrapper"


def test_student_attachments_endpoint_uses_portfolio_read_code():
    """api/portfolio/student_attachments.py GET attachments 端點為 PORTFOLIO_READ。"""
    import inspect

    import api.portfolio.student_attachments as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "student_attachments.py 應傳 code=PORTFOLIO_READ 至 portfolio_access wrapper"


def test_milestones_endpoints_use_both_read_and_write_codes():
    """api/portfolio/milestones.py 同時含 GET (READ) 與 POST/PATCH/DELETE (WRITE) 端點。"""
    import inspect

    import api.portfolio.milestones as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "milestones.py GET 端點應傳 code=PORTFOLIO_READ"
    assert (
        "code=Permission.PORTFOLIO_WRITE" in source
    ), "milestones.py POST/PATCH/DELETE 端點應傳 code=PORTFOLIO_WRITE"
    # 確保至少 1 READ + 3 WRITE wrapper call
    assert source.count("code=Permission.PORTFOLIO_READ") >= 1
    assert source.count("code=Permission.PORTFOLIO_WRITE") >= 3


def test_observations_endpoints_use_both_read_and_write_codes():
    """api/portfolio/observations.py 同時含 GET (READ) 與 POST/PATCH/DELETE (WRITE) 端點。"""
    import inspect

    import api.portfolio.observations as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "observations.py GET 端點應傳 code=PORTFOLIO_READ"
    assert (
        "code=Permission.PORTFOLIO_WRITE" in source
    ), "observations.py POST/PATCH/DELETE 端點應傳 code=PORTFOLIO_WRITE"
    assert source.count("code=Permission.PORTFOLIO_READ") >= 1
    assert source.count("code=Permission.PORTFOLIO_WRITE") >= 3
