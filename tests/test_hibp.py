"""HIBP k-anonymity assertion tests。"""

from unittest.mock import patch, MagicMock

import pytest

from utils.hibp import PasswordPwnedError, assert_not_pwned


def _fake_response(text: str, status: int = 200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


def test_assert_not_pwned_raises_when_suffix_in_response():
    """SHA1('password') = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
    prefix=5BAA6 / suffix=1E4C9B93F3F0682250B6CF8331B7EE68FD8"""
    response_body = (
        "1E4C9B93F3F0682250B6CF8331B7EE68FD8:9659365\r\n"
        "1E4D7E893E80B79F18CDA72FF6D9CDD68B9:1\r\n"
    )
    with patch("utils.hibp.requests.get", return_value=_fake_response(response_body)):
        with pytest.raises(PasswordPwnedError) as ei:
            assert_not_pwned("password")
        assert ei.value.occurrences == 9659365


def test_assert_not_pwned_passes_when_suffix_not_in_response():
    """random password 不在 HIBP DB。"""
    response_body = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:1\r\n"
    with patch("utils.hibp.requests.get", return_value=_fake_response(response_body)):
        assert_not_pwned("password")


def test_assert_not_pwned_fail_open_on_network_error():
    """network error → fail-open（不 raise）。"""
    import requests as req_mod

    with patch(
        "utils.hibp.requests.get",
        side_effect=req_mod.ConnectionError("network down"),
    ):
        assert_not_pwned("password")  # no raise


def test_assert_not_pwned_fail_open_on_timeout():
    import requests as req_mod

    with patch("utils.hibp.requests.get", side_effect=req_mod.Timeout("slow")):
        assert_not_pwned("password")
