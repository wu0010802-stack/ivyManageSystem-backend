"""capture_fail_open helper 單元測試。"""

from unittest.mock import MagicMock, patch

from utils.fail_open import capture_fail_open


def test_capture_fail_open_logs_warning(caplog):
    """應 log warning 含 operation 名稱與 error message。"""
    err = RuntimeError("DB down")
    with caplog.at_level("WARNING"):
        capture_fail_open("is_token_revoked", err)
    assert "is_token_revoked" in caplog.text
    assert "DB down" in caplog.text


def test_capture_fail_open_sets_sentry_tag_and_captures():
    """應 push_scope + set_tag('fail_open', operation) + capture_exception。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch(
            "utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm
        ) as mock_push,
        patch("utils.fail_open.sentry_sdk.capture_exception") as mock_capture,
    ):
        err = RuntimeError("DB down")
        capture_fail_open("is_token_revoked", err)
        mock_push.assert_called_once()
        fake_scope.set_tag.assert_any_call("fail_open", "is_token_revoked")
        mock_capture.assert_called_once_with(err)


def test_capture_fail_open_extra_tags_prefixed_and_stringified():
    """extra kwargs 應以 fail_open.{key} 設 tag + str() 處理 value。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch("utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm),
        patch("utils.fail_open.sentry_sdk.capture_exception"),
    ):
        capture_fail_open(
            "rate_limit.check",
            RuntimeError("x"),
            name="login",
            key="ip:1.2.3.4",
            count=42,
        )
        fake_scope.set_tag.assert_any_call("fail_open.name", "login")
        fake_scope.set_tag.assert_any_call("fail_open.key", "ip:1.2.3.4")
        fake_scope.set_tag.assert_any_call("fail_open.count", "42")


def test_capture_fail_open_no_extra_works():
    """無 extra kwargs 也應正常 capture。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch("utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm),
        patch("utils.fail_open.sentry_sdk.capture_exception") as mock_capture,
    ):
        capture_fail_open("op", RuntimeError("x"))
        mock_capture.assert_called_once()
