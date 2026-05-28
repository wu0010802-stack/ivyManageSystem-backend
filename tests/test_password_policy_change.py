"""驗證 _PASSWORD_MIN_LENGTH 改為 12 + HIBP 整合。"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from utils.auth import validate_password_strength


def _stub_hibp_no_match():
    """HIBP response 不命中（空 suffix list）。"""
    m = patch("utils.hibp.requests.get")
    gh = m.start()
    gh.return_value.text = ""
    gh.return_value.status_code = 200
    gh.return_value.raise_for_status = lambda: None
    return m


def test_validates_12_char_minimum():
    """11 字元應該擋。"""
    m = _stub_hibp_no_match()
    try:
        with pytest.raises(HTTPException) as ei:
            validate_password_strength("ShortPwd123")  # 11 chars
        assert ei.value.status_code == 400
        assert "12" in ei.value.detail
    finally:
        m.stop()


def test_passes_at_12_chars_with_complexity():
    m = _stub_hibp_no_match()
    try:
        validate_password_strength("LongerPwd123")  # 12 chars, ABC abc 123
    finally:
        m.stop()


def test_rejects_pwned_password():
    """HIBP 命中應拒。"""
    import hashlib

    sha1 = hashlib.sha1(b"Test-Pwned-Password-1").hexdigest().upper()
    suffix = sha1[5:]
    response_body = f"{suffix}:42\r\n"
    with patch("utils.hibp.requests.get") as gh:
        gh.return_value.text = response_body
        gh.return_value.status_code = 200
        gh.return_value.raise_for_status = lambda: None
        with pytest.raises(HTTPException) as ei:
            validate_password_strength("Test-Pwned-Password-1")
        assert "外洩" in ei.value.detail
