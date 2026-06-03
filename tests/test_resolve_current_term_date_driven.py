"""resolve_current_academic_term() 改為日期推導：不再讀 is_current。"""

from datetime import date
from unittest.mock import patch

from utils.academic import resolve_current_academic_term


def test_no_target_date_uses_today_not_db():
    """無 target_date 時走 today_taipei 日期推導，完全不碰 DB。"""
    with patch("utils.academic.today_taipei", return_value=date(2026, 3, 15)):
        sy, sem = resolve_current_academic_term()
    assert (sy, sem) == (114, 2)


def test_explicit_target_date_still_pure():
    assert resolve_current_academic_term(target_date=date(2025, 9, 1)) == (114, 1)


def test_does_not_query_db_session():
    """傳入一個會在被 query 時爆炸的假 session，確認根本沒被 query。"""

    class BoomSession:
        def query(self, *a, **k):
            raise AssertionError("不應再查 DB")

    with patch("utils.academic.today_taipei", return_value=date(2026, 1, 10)):
        # 1 月 → 前一年上學期：2025 → 114, sem 1
        assert resolve_current_academic_term(session=BoomSession()) == (114, 1)
