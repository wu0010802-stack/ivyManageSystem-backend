"""學年度計算工具單元測試。"""

import logging
import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.academic import resolve_current_academic_term, resolve_academic_term_filters


class TestResolveCurrentAcademicTerm:
    def test_august_is_first_semester(self):
        """8/1 → 上學期（semester=1），民國114年"""
        assert resolve_current_academic_term(date(2025, 8, 1)) == (114, 1)

    def test_july_is_second_semester(self):
        """7/31 → 下學期（semester=2），民國113年"""
        assert resolve_current_academic_term(date(2025, 7, 31)) == (113, 2)

    def test_february_is_second_semester(self):
        """2/1 → 下學期（semester=2），民國113年"""
        assert resolve_current_academic_term(date(2025, 2, 1)) == (113, 2)

    def test_january_is_first_semester(self):
        """1/31 → 上學期（semester=1，仍屬前一學年），民國113年"""
        assert resolve_current_academic_term(date(2025, 1, 31)) == (113, 1)

    def test_december_is_first_semester(self):
        """12/1 → 上學期，民國114年"""
        assert resolve_current_academic_term(date(2025, 12, 1)) == (114, 1)

    def test_september_is_first_semester(self):
        """9/1 → 上學期，民國114年"""
        assert resolve_current_academic_term(date(2025, 9, 1)) == (114, 1)

    def test_march_is_second_semester(self):
        """3/1 → 下學期，民國113年"""
        assert resolve_current_academic_term(date(2025, 3, 1)) == (113, 2)

    def test_no_date_uses_today(self):
        """不傳 target_date 時，回傳值是有效的 (year, semester) tuple"""
        result = resolve_current_academic_term()
        assert isinstance(result, tuple)
        assert len(result) == 2
        year, semester = result
        assert isinstance(year, int) and year >= 100
        assert semester in (1, 2)


class TestResolveAcademicTermFilters:
    def test_both_none_returns_current_term(self):
        """兩個都是 None 時，返回當前學期（不拋出例外）"""
        result = resolve_academic_term_filters(None, None)
        assert isinstance(result, tuple) and len(result) == 2

    def test_both_provided_returns_them(self):
        """兩個都提供時，直接回傳（民國年）"""
        assert resolve_academic_term_filters(114, 1) == (114, 1)
        assert resolve_academic_term_filters(113, 2) == (113, 2)

    def test_only_school_year_raises_400(self):
        """只提供 school_year 時，拋出 400"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            resolve_academic_term_filters(114, None)
        assert exc_info.value.status_code == 400

    def test_only_semester_raises_400(self):
        """只提供 semester 時，拋出 400"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            resolve_academic_term_filters(None, 1)
        assert exc_info.value.status_code == 400


class TestResolveDBAware:
    """utils.academic DB-aware 測試（新增）。

    既有測試（純日期推算）保持不動；以下新增 4 個 case 驗證 DB 路徑。
    """

    def test_resolve_uses_db_is_current_when_set(self, test_db_session):
        """DB 有 is_current=true row 時回傳該 row 的 (school_year, semester)。"""
        from models.academic_term import AcademicTerm
        from utils.academic import default_current_academic_term_for_column

        term = AcademicTerm(
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
            is_current=True,
        )
        test_db_session.add(term)
        test_db_session.flush()

        assert resolve_current_academic_term(session=test_db_session) == (114, 2)

    def test_resolve_fallback_to_date_when_no_current(self, test_db_session, caplog):
        """DB 無 is_current=true row 時 fallback 到日期推算 + 寫 warning。"""
        with caplog.at_level(logging.WARNING):
            sy, sem = resolve_current_academic_term(session=test_db_session)
        # 日期推算應與 _resolve_by_date(date.today()) 一致
        from utils.academic import _resolve_by_date

        assert (sy, sem) == _resolve_by_date(date.today())
        assert any(
            "AcademicTerm.is_current 未設定" in record.message
            for record in caplog.records
        )

    def test_resolve_target_date_skips_db_query(self):
        """顯式傳 target_date 不應查 DB。"""
        mock_session = MagicMock()
        result = resolve_current_academic_term(
            target_date=date(2025, 9, 1), session=mock_session
        )
        assert result == (114, 1)
        mock_session.query.assert_not_called()

    def test_default_for_column_never_queries_db(self):
        """Column default helper 永遠不查 DB（純日期推算）。"""
        from utils.academic import default_current_academic_term_for_column

        # 無 session 參數可傳；如果內部有任何 DB query 會 raise
        result = default_current_academic_term_for_column()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], int)
