"""
回歸測試：封存月份薪資守衛 - 防止竄改已封存假單導致 DB 矛盾

Bug 描述：
    PUT /leaves/{id} 修改「已核准 + 涉及已封存月份薪資」的假單時，
    salary_engine.process_salary_calculation() 拋出 ValueError（is_finalized=True），
    被 except Exception 吃掉，回傳 200 Success。
    結果：假單修改成功，但封存薪資未更新 → DB 矛盾（帳目對不上）。

同樣問題存在於：
    DELETE /leaves/{id}  — 完全沒有封存檢查，也沒有薪資重算
    PUT /leaves/{id}/approve — 同 update，ValueError 被靜默忽略

修復方式：
    新增 _check_salary_months_not_finalized()，
    在 commit 前主動查詢 SalaryRecord.is_finalized，
    若封存則拋出 409，阻止整個操作（DB 永遠不進入矛盾狀態）。
"""
import pytest
from unittest.mock import MagicMock, call
from fastapi import HTTPException


class TestCheckSalaryMonthsNotFinalized:
    """_check_salary_months_not_finalized 輔助函式的單元測試"""

    def _make_session(self, finalized_record=None):
        """建立返回指定查詢結果的 mock session"""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = finalized_record
        return mock_session

    def test_raises_409_when_salary_is_finalized(self):
        """封存月份應拋出 409 Conflict，阻止整個操作"""
        from api.leaves import _check_salary_months_not_finalized

        mock_record = MagicMock()
        mock_record.finalized_by = "admin"
        session = self._make_session(finalized_record=mock_record)

        with pytest.raises(HTTPException) as exc_info:
            _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 3)})

        assert exc_info.value.status_code == 409
        assert "封存" in exc_info.value.detail

    def test_passes_when_no_finalized_record(self):
        """未封存（查不到記錄）時不拋例外，操作可繼續"""
        from api.leaves import _check_salary_months_not_finalized

        session = self._make_session(finalized_record=None)
        # Should not raise
        _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 3)})

    def test_passes_for_empty_months_set(self):
        """空月份集合不應發出任何 DB 查詢，也不拋例外"""
        from api.leaves import _check_salary_months_not_finalized

        session = MagicMock()
        _check_salary_months_not_finalized(session, employee_id=1, months=set())

        session.query.assert_not_called()

    def test_detail_contains_year_and_month(self):
        """409 訊息需包含年月，讓 HR 知道是哪個月份被封存"""
        from api.leaves import _check_salary_months_not_finalized

        mock_record = MagicMock()
        mock_record.finalized_by = "張主任"
        mock_record.salary_year = 2025
        mock_record.salary_month = 8
        session = self._make_session(finalized_record=mock_record)

        with pytest.raises(HTTPException) as exc_info:
            _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 8)})

        detail = exc_info.value.detail
        assert "2025" in detail
        assert "8" in detail

    def test_detail_contains_finalized_by(self):
        """409 訊息需包含結算人，方便 HR 追溯是誰封存的"""
        from api.leaves import _check_salary_months_not_finalized

        mock_record = MagicMock()
        mock_record.finalized_by = "林財務"
        session = self._make_session(finalized_record=mock_record)

        with pytest.raises(HTTPException) as exc_info:
            _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 3)})

        assert "林財務" in exc_info.value.detail

    def test_checks_all_months_in_set(self):
        """多個月份應合批為一次 DB 查詢（批次 OR 條件）"""
        from api.leaves import _check_salary_months_not_finalized

        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None

        _check_salary_months_not_finalized(session, employee_id=5, months={(2025, 1), (2025, 2)})

        # 批次查詢：多個月份只發一次 DB 查詢
        assert session.query.call_count == 1

    def test_stops_at_first_finalized_month(self):
        """找到第一個封存月份就立即拋出，不繼續查詢"""
        from api.leaves import _check_salary_months_not_finalized

        mock_record = MagicMock()
        mock_record.finalized_by = "admin"
        session = self._make_session(finalized_record=mock_record)

        with pytest.raises(HTTPException):
            # 傳入多個月份，但只要第一個查到就應停止
            _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 3)})

        # 只查了一次（找到就停）
        assert session.query.call_count == 1

    def test_fallback_when_finalized_by_is_none(self):
        """finalized_by 為 None 時，訊息應 fallback 為「系統」，不崩潰"""
        from api.leaves import _check_salary_months_not_finalized

        mock_record = MagicMock()
        mock_record.finalized_by = None
        session = self._make_session(finalized_record=mock_record)

        with pytest.raises(HTTPException) as exc_info:
            _check_salary_months_not_finalized(session, employee_id=1, months={(2025, 3)})

        assert "系統" in exc_info.value.detail
