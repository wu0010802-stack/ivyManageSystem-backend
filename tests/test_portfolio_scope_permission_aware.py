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


def test_measurements_endpoints_use_both_read_and_write_codes():
    """api/portfolio/measurements.py 含 GET list/chart-data (READ ×2) + POST/PATCH/DELETE (WRITE ×3)。"""
    import inspect

    import api.portfolio.measurements as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "measurements.py GET 端點應傳 code=PORTFOLIO_READ"
    assert (
        "code=Permission.PORTFOLIO_WRITE" in source
    ), "measurements.py POST/PATCH/DELETE 端點應傳 code=PORTFOLIO_WRITE"
    # 至少 2 個 READ (list + chart-data) + 3 個 WRITE (POST/PATCH/DELETE)
    assert source.count("code=Permission.PORTFOLIO_READ") >= 2
    assert source.count("code=Permission.PORTFOLIO_WRITE") >= 3


def test_reports_endpoints_use_both_read_and_publish_codes():
    """api/portfolio/reports.py 含 GET list/detail/download (READ ×3) + POST/DELETE/send-line (PUBLISH ×3)。"""
    import inspect

    import api.portfolio.reports as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "reports.py GET list/detail/download 端點應傳 code=PORTFOLIO_READ"
    assert (
        "code=Permission.PORTFOLIO_PUBLISH" in source
    ), "reports.py POST create / DELETE / send-line 端點應傳 code=PORTFOLIO_PUBLISH"
    # 3 個 READ (list/detail/download) + 3 個 PUBLISH (create/delete/send-line)
    assert source.count("code=Permission.PORTFOLIO_READ") >= 3
    assert source.count("code=Permission.PORTFOLIO_PUBLISH") >= 3


def test_attachments_endpoints_use_read_and_write_codes():
    """api/attachments.py 含 POST upload (WRITE ×2 pre-check + post-IO) + DELETE (WRITE) + GET download (READ)。

    所有 wrapper call 都落在 PORTFOLIO_* family（upload/delete 守 PORTFOLIO_WRITE；
    download 走 has_permission(PORTFOLIO_READ) 後再 assert_student_access），
    並無 STUDENTS_* 或其他 perm family。
    """
    import inspect

    import api.attachments as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.PORTFOLIO_WRITE" in source
    ), "attachments.py POST upload / DELETE 端點應傳 code=PORTFOLIO_WRITE"
    assert (
        "code=Permission.PORTFOLIO_READ" in source
    ), "attachments.py GET /portfolio/{key} 下載端點應傳 code=PORTFOLIO_READ"
    # 至少 3 個 WRITE (upload pre-check + upload post-IO + delete) + 1 個 READ (download)
    assert source.count("code=Permission.PORTFOLIO_WRITE") >= 3
    assert source.count("code=Permission.PORTFOLIO_READ") >= 1
