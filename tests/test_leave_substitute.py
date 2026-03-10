"""
職務代理人功能回歸測試

測試範圍（不依賴 DB）：
- _check_substitute_guard：核准假單前的代理人狀態守衛
- _validate_substitute：建立假單時的代理人合法性驗證
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unittest.mock import MagicMock
from fastapi import HTTPException


# ============================================================
# _check_substitute_guard：核准守衛
# ============================================================

class TestSubstituteGuard:
    """_check_substitute_guard 輔助函式的單元測試

    序列式流程守衛：
    - pending  → 拋 409（代理人尚未回應）
    - rejected → 拋 409（代理人已拒絕，需重新指定）
    - accepted / not_required → 放行
    """

    def test_approve_blocked_when_pending(self):
        """代理人未回應（pending）時，主管核准應拋 409"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'pending'

        with pytest.raises(HTTPException) as exc_info:
            _check_substitute_guard(leave)

        assert exc_info.value.status_code == 409
        assert '代理人' in exc_info.value.detail

    def test_approve_blocked_when_rejected(self):
        """代理人已拒絕（rejected）時，主管核准應拋 409"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'rejected'

        with pytest.raises(HTTPException) as exc_info:
            _check_substitute_guard(leave)

        assert exc_info.value.status_code == 409
        assert '拒絕' in exc_info.value.detail

    def test_approve_succeeds_when_accepted(self):
        """代理人已接受（accepted）時，守衛應放行（不拋例外）"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'accepted'

        # Should not raise
        _check_substitute_guard(leave)

    def test_approve_succeeds_not_required(self):
        """無代理人（not_required）時，守衛應放行（回歸測試：原有假單不受影響）"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'not_required'

        # Should not raise
        _check_substitute_guard(leave)

    def test_pending_detail_guides_approver(self):
        """pending 的 409 訊息應引導主管等待代理人回應"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'pending'

        with pytest.raises(HTTPException) as exc_info:
            _check_substitute_guard(leave)

        detail = exc_info.value.detail
        # 訊息需提示主管等待
        assert '尚未' in detail or '等待' in detail

    def test_rejected_detail_guides_approver(self):
        """rejected 的 409 訊息應引導主管要求員工重新指定代理人"""
        from api.leaves import _check_substitute_guard

        leave = MagicMock()
        leave.substitute_status = 'rejected'

        with pytest.raises(HTTPException) as exc_info:
            _check_substitute_guard(leave)

        detail = exc_info.value.detail
        assert '重新' in detail or '代理人' in detail


# ============================================================
# _validate_substitute：建立假單時的代理人驗證
# ============================================================

class TestValidateSubstitute:
    """_validate_substitute 輔助函式的單元測試

    驗證規則：
    - 不能指定自己為代理人（400）
    - 代理人員工必須存在且在職（404）
    - 合法代理人應正常返回員工物件
    """

    def _make_session(self, substitute_emp=None):
        """建立返回指定查詢結果的 mock session"""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = substitute_emp
        return mock_session

    def test_cannot_set_self_as_substitute(self):
        """指定自己為代理人應拋 400，不應查詢 DB"""
        from api.portal.leaves import _validate_substitute

        session = MagicMock()  # 不應被呼叫

        with pytest.raises(HTTPException) as exc_info:
            _validate_substitute(session, emp_id=42, substitute_id=42)

        assert exc_info.value.status_code == 400
        # 確認 DB 查詢未被觸發（自己不能是代理人，在 DB 查詢前就應拋出）
        session.query.assert_not_called()

    def test_invalid_substitute_employee_raises_404(self):
        """不存在（或已離職）的員工 ID 應拋 404"""
        from api.portal.leaves import _validate_substitute

        session = self._make_session(substitute_emp=None)

        with pytest.raises(HTTPException) as exc_info:
            _validate_substitute(session, emp_id=1, substitute_id=999)

        assert exc_info.value.status_code == 404

    def test_valid_substitute_returns_employee(self):
        """有效代理人應返回員工物件，不拋例外"""
        from api.portal.leaves import _validate_substitute

        mock_emp = MagicMock()
        mock_emp.id = 99
        session = self._make_session(substitute_emp=mock_emp)

        result = _validate_substitute(session, emp_id=1, substitute_id=99)
        assert result is mock_emp

    def test_different_emp_ids_allowed(self):
        """不同的 emp_id 與 substitute_id 應允許查詢（不觸發自我指定守衛）"""
        from api.portal.leaves import _validate_substitute

        mock_emp = MagicMock()
        session = self._make_session(substitute_emp=mock_emp)

        # emp_id=1, substitute_id=2，兩者不同，應正常查詢 DB
        result = _validate_substitute(session, emp_id=1, substitute_id=2)
        session.query.assert_called()
