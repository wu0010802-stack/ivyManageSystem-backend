"""utils/external_calls.py helper unit tests — Phase 1 P1 resilience.

策略：mock sentry_sdk，不打真 DSN；驗證 scope tag 設定 + flag 控制 + no-op fallback。
"""

from unittest.mock import MagicMock, patch
import pytest


class TestTaggedCapture:
    def test_sets_scope_tag(self, monkeypatch):
        """tagged_capture 應透過 sentry_sdk.new_scope 設定 tag='external'."""
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "true")
        from config import settings

        settings.sentry.__class__.model_config["env_file"] = None  # bypass cache
        with patch("utils.sentry_init.capture_exception") as mock_capture:
            from utils.external_calls import tagged_capture

            tagged_capture(RuntimeError("boom"), tag="line")
            mock_capture.assert_called_once()
            args, kwargs = mock_capture.call_args
            assert isinstance(args[0], RuntimeError)

    def test_respects_disabled_env_flag(self, monkeypatch):
        """flag=False 時不呼叫 sentry — 完全 no-op."""
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "false")
        from config import reset_for_tests

        # 強制重 init settings
        reset_for_tests()
        with patch("utils.sentry_init.capture_exception") as mock_capture:
            from utils.external_calls import tagged_capture

            tagged_capture(RuntimeError("boom"), tag="line")
            mock_capture.assert_not_called()

    def test_invalid_tag_raises(self):
        from utils.external_calls import tagged_capture

        with pytest.raises(ValueError, match="tag"):
            tagged_capture(RuntimeError("x"), tag="invalid_tag")  # type: ignore[arg-type]

    def test_no_op_when_sentry_uninitialised(self, monkeypatch):
        """sentry_sdk 未 init 時 capture_exception 內部已 no-op；外層不應拋."""
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "true")
        from config import reset_for_tests

        reset_for_tests()
        # 不 mock sentry — 真實呼叫 utils.sentry_init.capture_exception (no DSN → no-op)
        from utils.external_calls import tagged_capture

        # 不應拋
        tagged_capture(ValueError("safe"), tag="supabase")

    def test_level_passed_through(self, monkeypatch):
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "true")
        from config import reset_for_tests

        reset_for_tests()
        with patch("utils.sentry_init.capture_exception") as mock_capture:
            from utils.external_calls import tagged_capture

            tagged_capture(RuntimeError("boom"), tag="line", level="warning")
            mock_capture.assert_called_once()
            # 第二個 positional 或 level kwarg = "warning"
            kwargs = mock_capture.call_args.kwargs
            assert kwargs.get("level") == "warning"


class TestRetryWithBackoff:
    def test_returns_first_success_without_retry(self):
        from utils.external_calls import retry_with_backoff

        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert retry_with_backoff(fn) == "ok"
        assert len(calls) == 1

    def test_retries_until_success(self, monkeypatch):
        from utils.external_calls import retry_with_backoff

        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        assert retry_with_backoff(fn, attempts=3) == "recovered"
        assert attempts["n"] == 3

    def test_raises_last_exception_after_exhausted(self, monkeypatch):
        from utils.external_calls import retry_with_backoff

        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        def fn():
            raise ConnectionError("always fail")

        with pytest.raises(ConnectionError, match="always fail"):
            retry_with_backoff(fn, attempts=3)

    def test_retry_on_filter(self, monkeypatch):
        """非 retry_on type 的 exception 直接拋，不重試."""
        from utils.external_calls import retry_with_backoff

        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            raise ValueError("bug")  # 非 ConnectionError

        with pytest.raises(ValueError):
            retry_with_backoff(fn, attempts=3, retry_on=(ConnectionError,))
        assert attempts["n"] == 1  # 不重試

    def test_backoff_grows_exponentially(self, monkeypatch):
        """每次 sleep 時間至少是上次的 base 倍（jitter 容忍 ±20%）."""
        from utils.external_calls import retry_with_backoff

        sleeps = []
        monkeypatch.setattr(
            "utils.external_calls.time.sleep", lambda s: sleeps.append(s)
        )
        # jitter 用固定 seed 讓 test 可重現
        monkeypatch.setattr("utils.external_calls.random.uniform", lambda a, b: 1.0)

        def fn():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            retry_with_backoff(fn, attempts=4, base_seconds=1.0, cap_seconds=100.0)
        # attempts=4 → sleep 3 次（最後一次失敗不 sleep）
        assert len(sleeps) == 3
        assert sleeps[0] == pytest.approx(1.0)
        assert sleeps[1] == pytest.approx(2.0)
        assert sleeps[2] == pytest.approx(4.0)

    def test_backoff_caps(self, monkeypatch):
        from utils.external_calls import retry_with_backoff

        sleeps = []
        monkeypatch.setattr(
            "utils.external_calls.time.sleep", lambda s: sleeps.append(s)
        )
        monkeypatch.setattr("utils.external_calls.random.uniform", lambda a, b: 1.0)

        def fn():
            raise ConnectionError("fail")

        with pytest.raises(ConnectionError):
            retry_with_backoff(fn, attempts=5, base_seconds=10.0, cap_seconds=15.0)
        # 10, 15(capped), 15, 15
        assert sleeps[0] == pytest.approx(10.0)
        for s in sleeps[1:]:
            assert s == pytest.approx(15.0)

    def test_jitter_within_bounds(self, monkeypatch):
        """jitter ±20% 範圍內 — 多次跑統計分布."""
        from utils.external_calls import retry_with_backoff

        sleeps = []
        monkeypatch.setattr(
            "utils.external_calls.time.sleep", lambda s: sleeps.append(s)
        )

        def fn():
            raise ConnectionError("fail")

        # 跑 50 次，base=1.0 第一次 sleep 應在 [0.8, 1.2]
        for _ in range(50):
            sleeps.clear()
            try:
                retry_with_backoff(fn, attempts=2, base_seconds=1.0, jitter=0.2)
            except ConnectionError:
                pass
            assert 0.8 <= sleeps[0] <= 1.2, f"jitter out of bounds: {sleeps[0]}"


class TestSettingsField:
    def test_tag_external_failures_default_true(self, monkeypatch):
        monkeypatch.delenv("SENTRY_TAG_EXTERNAL_FAILURES", raising=False)
        from config.sentry import SentrySettings

        s = SentrySettings()
        assert s.tag_external_failures is True

    def test_tag_external_failures_env_override(self, monkeypatch):
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "false")
        from config.sentry import SentrySettings

        s = SentrySettings()
        assert s.tag_external_failures is False
