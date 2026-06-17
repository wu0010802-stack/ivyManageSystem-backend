"""scripts/wipe_fees.py 的 DATABASE_URL 遮罩。

資安稽核 follow-up（2026-06-17）：腳本原本將完整 DATABASE_URL（含帳密）印到
stdout/stderr，會洩漏到終端 scrollback / CI log / 截圖。改為只印 host/port/db。
"""

from scripts.wipe_fees import _redact_db_url


def test_redacts_user_and_password():
    out = _redact_db_url("postgresql://user:secret@db.example.com:5432/ivy")
    assert "secret" not in out
    assert "user:secret" not in out
    assert "db.example.com:5432" in out
    assert out.endswith("/ivy")


def test_redacts_user_only_url():
    # dev 慣用格式：postgresql://yilunwu@localhost:5432/ivymanagement
    out = _redact_db_url("postgresql://yilunwu@localhost:5432/ivymanagement")
    assert "yilunwu" not in out
    assert "localhost:5432/ivymanagement" in out


def test_url_without_credentials_unchanged():
    url = "postgresql://localhost:5432/ivymanagement"
    assert _redact_db_url(url) == url


def test_empty_url_returns_empty():
    assert _redact_db_url("") == ""
