"""學生在籍/班籍異動（轉班 / 退學 / 畢業）→ 才藝儀表板快取主動失效。

activity_dashboard_table 招生達成率分母用各班在籍人數；學生離校/轉班後若不主動失效，
最長要等 30 分 TTL 才反映新分母。api.students 4 個在籍變動端點（PUT 改班/在籍日、
DELETE 軟刪、graduate、bulk-transfer）在 commit 後呼叫
_invalidate_activity_dashboard_after_enrollment_change。此處測 helper 清掉三個 activity
dashboard category（且不誤清無關 category）。
"""

from datetime import datetime, timedelta

from api.students import _invalidate_activity_dashboard_after_enrollment_change


def _seed_cache(session, category, key):
    from models.database import ReportSnapshot

    now = datetime(2026, 6, 17, 12, 0, 0)
    session.add(
        ReportSnapshot(
            cache_key=key,
            category=category,
            payload="{}",
            computed_at=now,
            expires_at=now + timedelta(seconds=1800),
        )
    )
    session.commit()


def test_helper_clears_activity_dashboard_categories_only(test_db_session):
    from models.database import ReportSnapshot
    from services.activity_service import ACTIVITY_DASHBOARD_CACHE_CATEGORIES

    for i, cat in enumerate(ACTIVITY_DASHBOARD_CACHE_CATEGORIES):
        _seed_cache(test_db_session, cat, key=f"act{i}")
    # 一筆不相關 category，確認不被誤清
    _seed_cache(test_db_session, "reports_finance_summary", key="fin")

    _invalidate_activity_dashboard_after_enrollment_change(test_db_session)

    test_db_session.expire_all()
    remaining = {r.category for r in test_db_session.query(ReportSnapshot).all()}
    assert remaining == {"reports_finance_summary"}


def test_helper_is_best_effort_and_swallows_errors(test_db_session, monkeypatch):
    """失效失敗只 log、不外拋（不可阻擋學生主流程）。"""
    import api.students as students_mod

    def _boom(_session):
        raise RuntimeError("cache backend down")

    monkeypatch.setattr(
        "api.activity._shared._invalidate_activity_dashboard_caches", _boom
    )
    # 不應拋出
    students_mod._invalidate_activity_dashboard_after_enrollment_change(test_db_session)
