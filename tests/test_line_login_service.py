"""驗證 LineLoginService aud 校驗的 defense-in-depth：

- 啟動時 channel_id 為空 → log warning（SRE 早知道）
- aud 必須為非空字串 + 嚴格相等 channel_id
  - 防 LINE 異常 response 把 None / "" / 非字串 aud 吃進通過
  - 防 cross-channel id_token 重放

Refs: 資安掃描 2026-05-07 P2。
"""

import logging
import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.line_login_service import LineLoginService


class TestStartupWarning:
    def test_empty_channel_id_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            LineLoginService(channel_id="")
        assert any("LINE_LOGIN_CHANNEL_ID 未設定" in r.message for r in caplog.records)

    def test_none_channel_id_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            LineLoginService(channel_id=None)
        assert any("LINE_LOGIN_CHANNEL_ID 未設定" in r.message for r in caplog.records)

    def test_configured_channel_id_logs_info_with_suffix(self, caplog):
        with caplog.at_level(logging.INFO):
            LineLoginService(channel_id="2001234567")
        info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
        # 後 4 碼揭露但不揭露完整 channel_id（資訊安全 trade-off）
        assert any("4567" in m for m in info_msgs)
        # 不應該出現完整 channel_id
        assert not any("2001234567" in m for m in info_msgs)


class TestAudValidation:
    """資安掃描 2026-05-07 P2：aud 必須為非空字串 + 嚴格相等 channel_id。"""

    def _mock_verify_response(self, aud_value, sub_value="U_test_sub"):
        """產生 mock httpx response payload。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"aud": aud_value, "sub": sub_value}
        return mock_resp

    def test_aud_match_passes(self):
        svc = LineLoginService(channel_id="2001234567")
        with patch("services.line_login_service.httpx.post") as mock_post:
            mock_post.return_value = self._mock_verify_response("2001234567")
            payload = svc.verify_id_token("fake_id_token")
        assert payload["aud"] == "2001234567"

    def test_aud_mismatch_rejected(self):
        svc = LineLoginService(channel_id="2001234567")
        with patch("services.line_login_service.httpx.post") as mock_post:
            mock_post.return_value = self._mock_verify_response("9999999999")
            with pytest.raises(HTTPException) as exc:
                svc.verify_id_token("fake_id_token")
        assert exc.value.status_code == 401
        assert "aud" in exc.value.detail

    def test_aud_none_rejected(self):
        """LINE 回應 aud=None → 拒絕（defense in depth）"""
        svc = LineLoginService(channel_id="2001234567")
        with patch("services.line_login_service.httpx.post") as mock_post:
            mock_post.return_value = self._mock_verify_response(None)
            with pytest.raises(HTTPException) as exc:
                svc.verify_id_token("fake_id_token")
        assert exc.value.status_code == 401

    def test_aud_empty_string_rejected(self):
        """空字串 aud → 拒絕（即使 channel_id 也是空也不能通過，is_configured 會先擋）"""
        svc = LineLoginService(channel_id="2001234567")
        with patch("services.line_login_service.httpx.post") as mock_post:
            mock_post.return_value = self._mock_verify_response("")
            with pytest.raises(HTTPException) as exc:
                svc.verify_id_token("fake_id_token")
        assert exc.value.status_code == 401

    def test_aud_non_string_type_rejected(self):
        """整數 aud → 拒絕（避免 LINE 異常 response 帶非字串型別）"""
        svc = LineLoginService(channel_id="2001234567")
        with patch("services.line_login_service.httpx.post") as mock_post:
            mock_post.return_value = self._mock_verify_response(2001234567)
            with pytest.raises(HTTPException) as exc:
                svc.verify_id_token("fake_id_token")
        assert exc.value.status_code == 401

    def test_unconfigured_service_rejects_with_503(self):
        """channel_id 為空 → 503 不打 LINE（避免 cost / DDoS 放大）"""
        svc = LineLoginService(channel_id="")
        with pytest.raises(HTTPException) as exc:
            svc.verify_id_token("fake_id_token")
        assert exc.value.status_code == 503
