# P1 韌性 Phase 1：Sentry tagging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 18 個外部整合站點（LINE 7 / Supabase 5 / 外部 HTTP 6）加 Sentry tagged_capture，將 `logger.warning` 升級為 `logger.exception` + tagged scope，並建立 retry/breaker 後續 phase 共用的 helper `utils/external_calls.py`。Phase 1 完成後 prod 立即可量化故障率，為 Phase 2/3/4 是否需要實施提供數據基礎。

**Architecture:** 純函式共用 helper（`tagged_capture` 包既有 `utils.sentry_init.capture_exception` 加 scope tag、`retry_with_backoff` 為 Phase 4 預備但 Phase 1 不調用）放 `utils/external_calls.py`。18 個外呼站點各自呼叫 `tagged_capture(exc, tag='line'/'supabase'/'external_http')`，4xx vs 5xx/network 分流規則寫成 line_service 內部 `_record_line_response` helper。新 env `SENTRY_TAG_EXTERNAL_FAILURES` 預設 on，test 可關。

**Tech Stack:** Python 3.9 (prod) / 3.14 (test)、FastAPI、`requests`、`sentry-sdk[fastapi]`、`supabase-py`、pytest + monkeypatch + unittest.mock；無新增 dependency。

**Spec:** `docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md`（§4.1 / §5）

---

## File Structure

| 動作 | 路徑 | 責任 |
|------|------|------|
| Create | `utils/external_calls.py` | `tagged_capture` + `retry_with_backoff` 純函式 helper（~80 行） |
| Create | `tests/test_external_calls.py` | helper unit test，mock sentry_sdk 與 sleep |
| Modify | `config/sentry.py` | 加 `tag_external_failures: bool = True` field |
| Modify | `services/line_service.py` | 加 module-level `_record_line_response` helper；7 處 `_push*/_reply*` `logger.warning` → `logger.exception` + `tagged_capture` + 4xx 分流 |
| Modify | `utils/supabase_storage.py` | 5 個 method (`save/read/delete/exists/signed_url`) 包 try/except + `tagged_capture(exc, "supabase")`；既有 idempotent delete 行為保留 |
| Modify | `services/recruitment_market_intelligence.py` | 3 處 `requests.get/post` 包 try/except + `tagged_capture(exc, "external_http")` |
| Modify | `services/geocoding_service.py` | 2 處 `requests.get` 包 try/except + `tagged_capture` |
| Modify | `services/official_calendar.py` | 1 處 `requests.get` 包 try/except + `tagged_capture` |
| Modify | `tests/test_line_service.py` | 補 401/403/網路錯誤的 sentry capture 驗證 |
| Modify | `tests/test_supabase_storage.py` | 補 upload exception → sentry capture 驗證 |

---

## Conventions

- 既有 `logger = logging.getLogger(__name__)` 不動；改的是 `logger.warning` → `logger.exception` 並加 `tagged_capture(exc, tag)`。
- `tagged_capture` 內部 check `settings.sentry.tag_external_failures` flag；False 時直接 no-op。
- 4xx 分流規則（line_service 專用）：
  - 401/403 → `tagged_capture(exc, "line", level="error")` + return False；Phase 1 process-local in-memory dedup（1 小時內同 status code 只發一次）
  - 404/400 → `tagged_capture(exc, "line", level="warning")` + return False
  - 429 → `tagged_capture(exc, "line", level="warning")` + return False
  - 5xx/timeout/network → `tagged_capture(exc, "line", level="error")` + return False
- Supabase / external_http 不分流（一律 level="error"）；Phase 3 加 breaker 時才細分。
- TDD 順序：每 task 都是 **failing test → impl → passing test → commit**。

---

## Task 1: `utils/external_calls.py` — `tagged_capture` (TDD)

**Files:**
- Create: `utils/external_calls.py`
- Create: `tests/test_external_calls.py`

- [ ] **Step 1.1: Write failing tests for `tagged_capture`**

```python
# tests/test_external_calls.py
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
        from config import settings
        # 強制重 init settings
        settings.reset_for_tests()
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
        from config import settings
        settings.reset_for_tests()
        # 不 mock sentry — 真實呼叫 utils.sentry_init.capture_exception (no DSN → no-op)
        from utils.external_calls import tagged_capture
        # 不應拋
        tagged_capture(ValueError("safe"), tag="supabase")

    def test_level_passed_through(self, monkeypatch):
        monkeypatch.setenv("SENTRY_TAG_EXTERNAL_FAILURES", "true")
        from config import settings
        settings.reset_for_tests()
        with patch("utils.sentry_init.capture_exception") as mock_capture:
            from utils.external_calls import tagged_capture
            tagged_capture(RuntimeError("boom"), tag="line", level="warning")
            mock_capture.assert_called_once()
            # 第二個 positional 或 level kwarg = "warning"
            kwargs = mock_capture.call_args.kwargs
            assert kwargs.get("level") == "warning"
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/test_external_calls.py::TestTaggedCapture -v`
Expected: All FAIL with `ImportError: cannot import name 'tagged_capture' from 'utils.external_calls'` or `ModuleNotFoundError`.

- [ ] **Step 1.3: Implement `tagged_capture`**

```python
# utils/external_calls.py
"""共用外呼 helper：retry_with_backoff（Phase 4 用）+ tagged_capture（Phase 1 起用）。

對應 spec docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md §4.1。
無新 dependency；retry/breaker 套件由 utils/circuit_breaker.py 在 Phase 3 提供。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

TagType = Literal["line", "supabase", "external_http"]
_VALID_TAGS: frozenset[str] = frozenset({"line", "supabase", "external_http"})


def tagged_capture(
    exc: BaseException,
    tag: TagType,
    *,
    level: Literal["error", "warning"] = "error",
) -> None:
    """上報 exception 到 Sentry，scope 帶 tag='external' + tag=<tag>。

    Args:
        exc: 要上報的 exception
        tag: 'line' / 'supabase' / 'external_http' 三選一
        level: Sentry event level

    行為：
    - settings.sentry.tag_external_failures=False → no-op（test 友善）
    - sentry_sdk 未 init → 內部 capture_exception 自動 no-op（utils.sentry_init 既有保護）
    - 任何 sentry 錯誤都吞掉（不能傳染回主邏輯）

    Phase 1 內 line_service 4xx 分流由 caller 自行決定 level / tag；本 helper 不分流。
    """
    if tag not in _VALID_TAGS:
        raise ValueError(f"tag must be one of {_VALID_TAGS!r}, got {tag!r}")

    from config import settings as _settings
    if not getattr(_settings.sentry, "tag_external_failures", True):
        return

    try:
        import sentry_sdk
        from utils.sentry_init import capture_exception as _capture
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("external", tag)
            _capture(exc, level=level)
    except Exception:  # noqa: BLE001 — Sentry 錯誤不能往上傳
        logger.debug("tagged_capture failed (silenced)", exc_info=True)
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/test_external_calls.py::TestTaggedCapture -v`
Expected: All 5 tests PASS.

If `test_respects_disabled_env_flag` fails because settings has no `tag_external_failures`, that's **expected** — Task 3 adds the field. Mark this test as `@pytest.mark.xfail(reason="settings field added in Task 3", strict=False)` temporarily, or run Tasks 1+3 together.

**Mitigation**: skip Step 1.4 verification for `test_respects_disabled_env_flag` until after Task 3; other 4 tests must pass.

- [ ] **Step 1.5: Commit**

```bash
git add utils/external_calls.py tests/test_external_calls.py
git commit -m "feat(resilience): utils/external_calls.tagged_capture helper (Phase 1)

對應 spec §4.1。Phase 1 起 LINE/Supabase/外部 HTTP 18 站點呼叫此 helper
取代 logger.warning，將失敗 tag 後送 Sentry；scope tag='external' + tag=<line/supabase/external_http>。
settings.sentry.tag_external_failures 預設 True，test 可關。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `utils/external_calls.py` — `retry_with_backoff` (TDD)

**Files:**
- Modify: `utils/external_calls.py`
- Modify: `tests/test_external_calls.py`

- [ ] **Step 2.1: Write failing tests for `retry_with_backoff`**

Append to `tests/test_external_calls.py`:

```python
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
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: sleeps.append(s))
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
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: sleeps.append(s))
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
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: sleeps.append(s))
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
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/test_external_calls.py::TestRetryWithBackoff -v`
Expected: All FAIL with `ImportError: cannot import name 'retry_with_backoff'`.

- [ ] **Step 2.3: Implement `retry_with_backoff`**

Append to `utils/external_calls.py`:

```python
def retry_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_seconds: float = 1.0,
    cap_seconds: float = 10.0,
    jitter: float = 0.2,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Exponential backoff with ±jitter%. 拋出最後一次 exception。

    Args:
        fn: 無參數可呼叫；用 lambda 包裝有參數的 caller
        attempts: 總嘗試次數（不是「retry 次數」；attempts=1 表示不重試）
        base_seconds: 第 1 次 retry 前 sleep 秒數，之後 2x 指數成長
        cap_seconds: sleep 上限（避免極端情況一次睡幾分鐘）
        jitter: ±jitter 比例隨機抖動，避免 thundering herd
        retry_on: 只有屬於這些 type 的 exception 才重試；其他直接拋

    Phase 1 不被任何 caller 呼叫；Phase 4 SupabaseStorage.save 包此 helper。
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            delay = min(base_seconds * (2 ** i), cap_seconds)
            factor = random.uniform(1.0 - jitter, 1.0 + jitter)
            time.sleep(delay * factor)
    assert last_exc is not None  # for type checker
    raise last_exc
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/test_external_calls.py::TestRetryWithBackoff -v`
Expected: All 7 tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add utils/external_calls.py tests/test_external_calls.py
git commit -m "feat(resilience): retry_with_backoff helper (Phase 4 預備)

Phase 1 不被 caller 呼叫；放在 utils/external_calls.py 與 tagged_capture 同檔，
Phase 4 SupabaseStorage.save() 啟用 3 次指數退避。±20% jitter 防 thundering herd。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Config — `tag_external_failures` field

**Files:**
- Modify: `config/sentry.py`
- Modify: `tests/test_external_calls.py`（解除 Step 1.4 的 xfail）

- [ ] **Step 3.1: Write failing test for config field**

Append to `tests/test_external_calls.py`:

```python
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
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/test_external_calls.py::TestSettingsField -v`
Expected: FAIL with `AttributeError: 'SentrySettings' object has no attribute 'tag_external_failures'`.

- [ ] **Step 3.3: Add field to `config/sentry.py`**

In `config/sentry.py`, add field after `traces_sample_rate`:

```python
    tag_external_failures: bool = True
```

Full updated `SentrySettings` class:

```python
class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTRY_", extra="ignore", case_sensitive=False
    )

    dsn: str | None = Field(default=None, repr=False)
    environment: str = "production"
    release: str | None = None
    traces_sample_rate: float = _TRACES_DEFAULT
    tag_external_failures: bool = True  # Phase 1 P1 resilience: 外呼站點 tagged_capture 總開關

    @field_validator("traces_sample_rate", mode="before")
    @classmethod
    def _coerce_traces_rate(cls, v: object) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return _TRACES_DEFAULT

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)
```

- [ ] **Step 3.4: Run all `tests/test_external_calls.py` to verify**

Run: `pytest tests/test_external_calls.py -v`
Expected: 2 new TestSettingsField tests PASS + previously xfail TestTaggedCapture::test_respects_disabled_env_flag now also PASS. Total 14 PASS.

If you marked any test xfail in Step 1.4, remove the `@pytest.mark.xfail` decorator now.

- [ ] **Step 3.5: Commit**

```bash
git add config/sentry.py tests/test_external_calls.py
git commit -m "feat(config): SentrySettings.tag_external_failures (default True)

對應 spec §13。Phase 1 起 utils.external_calls.tagged_capture 內部以此 flag
判斷是否真的呼叫 sentry_sdk.capture_exception — test 可設 false 避免污染
test project。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: LINE — `_record_line_response` helper + 7 站點重構 (TDD)

**Files:**
- Modify: `services/line_service.py`
- Modify: `tests/test_line_service.py`

**4xx 分流規則**（同 spec §5.1）:
- 401/403 → `tagged_capture(exc, "line", level="error")`，process-local in-memory dedup 1 hr，return False
- 404/400 → `tagged_capture(exc, "line", level="warning")`，return False
- 429 → `tagged_capture(exc, "line", level="warning")`，return False
- 5xx → `tagged_capture(exc, "line", level="error")`，return False
- timeout/network (`requests.RequestException`) → `tagged_capture(exc, "line", level="error")`，return False

- [ ] **Step 4.1: Write failing tests in `tests/test_line_service.py`**

Append a new test class:

```python
class TestSentryTaggedCapture:
    """Phase 1 P1 resilience：驗證 LINE 失敗呼叫 tagged_capture + 4xx 分流."""

    @pytest.fixture(autouse=True)
    def _reset_401_dedup(self):
        """每個 test 前清空 401 dedup state，避免 test 間污染."""
        from services import line_service
        line_service._RECENT_LINE_401_403.clear()
        yield

    def test_network_error_calls_tagged_capture(self, monkeypatch):
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        monkeypatch.setattr(
            "services.line_service.requests.post",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("boom")),
        )
        with patch("services.line_service.tagged_capture") as mock_capture:
            assert svc._push_to_user("Uabc", "x") is False
            mock_capture.assert_called_once()
            exc_arg = mock_capture.call_args.args[0]
            assert isinstance(exc_arg, ConnectionError)
            assert mock_capture.call_args.kwargs.get("tag") == "line" \
                or mock_capture.call_args.args[1] == "line"
            assert mock_capture.call_args.kwargs.get("level") == "error"

    def test_5xx_calls_tagged_capture_level_error(self, monkeypatch):
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        def mock_post(*a, **k):
            resp = MagicMock()
            resp.status_code = 503
            resp.text = "Service Unavailable"
            return resp
        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        with patch("services.line_service.tagged_capture") as mock_capture:
            assert svc._push_to_user("Uabc", "x") is False
            mock_capture.assert_called_once()
            assert mock_capture.call_args.kwargs.get("level") == "error"

    def test_429_calls_tagged_capture_level_warning(self, monkeypatch):
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        def mock_post(*a, **k):
            resp = MagicMock()
            resp.status_code = 429
            resp.text = "rate limited"
            return resp
        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        with patch("services.line_service.tagged_capture") as mock_capture:
            assert svc._push_to_user("Uabc", "x") is False
            mock_capture.assert_called_once()
            assert mock_capture.call_args.kwargs.get("level") == "warning"

    def test_401_dedup_only_first_call_captures(self, monkeypatch):
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        def mock_post(*a, **k):
            resp = MagicMock()
            resp.status_code = 401
            resp.text = "Unauthorized"
            return resp
        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        with patch("services.line_service.tagged_capture") as mock_capture:
            svc._push_to_user("Uabc", "x")
            svc._push_to_user("Uabc", "y")
            svc._push_to_user("Uabc", "z")
            # dedup：1 小時內同 status code 只發一次
            assert mock_capture.call_count == 1

    def test_404_no_dedup_warning_level(self, monkeypatch):
        """404 不 dedup（不同 user_id 都該發），level=warning."""
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        def mock_post(*a, **k):
            resp = MagicMock()
            resp.status_code = 404
            resp.text = "Not found"
            return resp
        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        with patch("services.line_service.tagged_capture") as mock_capture:
            svc._push_to_user("Uabc", "x")
            svc._push_to_user("Udef", "y")
            assert mock_capture.call_count == 2
            for call in mock_capture.call_args_list:
                assert call.kwargs.get("level") == "warning"

    def test_success_no_capture(self, monkeypatch):
        from services.line_service import LineService
        svc = LineService()
        svc.configure("token", "group", True)
        def mock_post(*a, **k):
            resp = MagicMock()
            resp.status_code = 200
            return resp
        monkeypatch.setattr("services.line_service.requests.post", mock_post)
        with patch("services.line_service.tagged_capture") as mock_capture:
            assert svc._push_to_user("Uabc", "x") is True
            mock_capture.assert_not_called()
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/test_line_service.py::TestSentryTaggedCapture -v`
Expected: All FAIL with `ImportError: cannot import name 'tagged_capture'` or `AttributeError: module 'services.line_service' has no attribute '_RECENT_LINE_401_403'`.

- [ ] **Step 4.3: Refactor `services/line_service.py`**

Add at top (after existing imports):

```python
import time
from utils.external_calls import tagged_capture
```

Add module-level dedup state + helper after `_LINE_REPLY_URL` constant:

```python
# Phase 1 P1 resilience: 401/403 process-local dedup（1 hr TTL）
# Phase 4 改成 line_token_health table 跨 worker / 跨 restart dedup；本 fast-path 保留。
_LINE_401_DEDUP_TTL_SECONDS = 3600
_RECENT_LINE_401_403: dict[int, float] = {}  # status_code → last_capture_ts


def _record_line_response(
    resp_or_exc: object,
    *,
    context: str,
) -> bool:
    """LINE API call 結果統一處理：log + tagged_capture + 4xx 分流；回 True 表示成功送出。

    Args:
        resp_or_exc: requests.Response（status_code/text 屬性）或 BaseException
        context: 描述呼叫源 (e.g. "_push_to_user", "_reply"), 寫入 log 訊息

    Returns:
        True 若 HTTP 200；False 任何失敗（exception / 非 200）
    """
    # Exception path
    if isinstance(resp_or_exc, BaseException):
        logger.exception("LINE %s 失敗（network/exception）", context)
        tagged_capture(resp_or_exc, tag="line", level="error")
        return False

    # Response path
    resp = resp_or_exc
    status = getattr(resp, "status_code", 0)
    body = getattr(resp, "text", "")[:200]

    if status == 200:
        return True

    # 4xx 分流
    if status in (401, 403):
        # process-local dedup：1 hr 內同 status 只發一次
        now = time.time()
        last = _RECENT_LINE_401_403.get(status)
        if last is None or (now - last) >= _LINE_401_DEDUP_TTL_SECONDS:
            logger.exception("LINE %s 失敗 status=%s body=%s", context, status, body)
            tagged_capture(
                RuntimeError(f"LINE {context} returned {status}: {body}"),
                tag="line",
                level="error",
            )
            _RECENT_LINE_401_403[status] = now
        else:
            logger.warning("LINE %s 失敗 status=%s（已 dedup）", context, status)
        return False

    if status in (400, 404):
        logger.warning("LINE %s 失敗 status=%s body=%s", context, status, body)
        tagged_capture(
            RuntimeError(f"LINE {context} returned {status}: {body}"),
            tag="line",
            level="warning",
        )
        return False

    if status == 429:
        logger.warning("LINE %s rate limited body=%s", context, body)
        tagged_capture(
            RuntimeError(f"LINE {context} rate limited 429: {body}"),
            tag="line",
            level="warning",
        )
        return False

    # 5xx 或其他
    logger.exception("LINE %s 失敗 status=%s body=%s", context, status, body)
    tagged_capture(
        RuntimeError(f"LINE {context} returned {status}: {body}"),
        tag="line",
        level="error",
    )
    return False
```

Now refactor each `_push*` / `_reply*` method to use `_record_line_response`. Replace existing pattern:

```python
# BEFORE
try:
    resp = requests.post(...)
    if resp.status_code != 200:
        logger.warning("LINE 個人推播失敗: %s %s", resp.status_code, resp.text)
        return False
    return True
except Exception as exc:
    logger.warning("LINE 個人推播失敗: %s", exc)
    return False
```

```python
# AFTER
try:
    resp = requests.post(...)
    return _record_line_response(resp, context="_push_to_user")
except Exception as exc:
    return _record_line_response(exc, context="_push_to_user")
```

Apply to all 7 sites (rough context names):

| 行（pre-refactor） | method | context 字串 |
|------|--------|---------|
| 265 | `push_text_to_group` | `"push_text_to_group"` |
| 310 | `_push_to_user` | `"_push_to_user"` |
| 347 | `push_flex_to_user` | `"push_flex_to_user"` |
| 379 | `_push_to_user_with_quick_reply` | `"_push_to_user_with_quick_reply"` |
| 413 | `_reply_with_quick_reply` | `"_reply_with_quick_reply"` |
| 445 | `_reply` | `"_reply"` |

**For `_reply` (行 445) — special**: 既有 method 對「token 缺失」直接 return False，不打 API。保留此早退邏輯：

```python
def _reply(self, reply_token: str, text: str) -> bool:
    if not self._token or not reply_token:
        return False
    try:
        resp = requests.post(
            _LINE_REPLY_URL,
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": text}],
            },
            timeout=5,
        )
        return _record_line_response(resp, context="_reply")
    except Exception as exc:
        return _record_line_response(exc, context="_reply")
```

Apply similar refactor to all 7 methods. Keep all early-return guards (`if not self._enabled or not self._token...`) — they stay before the try.

- [ ] **Step 4.4: Run tests**

Run new tests + existing line_service tests to verify no regression:

```bash
pytest tests/test_line_service.py -v
```

Expected:
- New `TestSentryTaggedCapture` 6 tests PASS
- Existing tests still PASS (especially `test_push_to_user_network_error` which now exercises new path)

If existing test `test_push_to_user_network_error` fails because it expected the old logger.warning behavior, update it to verify the new return value semantics (already `assert ... is False`，應仍 pass).

- [ ] **Step 4.5: Commit**

```bash
git add services/line_service.py tests/test_line_service.py
git commit -m "feat(line): _record_line_response helper + tagged_capture (Phase 1)

對應 spec §5.1。7 處 _push*/_reply* method 改用統一 helper：
- logger.warning → logger.exception
- 4xx 分流：401/403 error+dedup / 400/404/429 warning / 5xx error
- 失敗呼叫 tagged_capture(exc, tag='line', level=...) 進 Sentry

Phase 1 dedup 為 process-local（1 hr TTL）；Phase 4 落地 line_token_health
table 後改跨 worker 持久化，本 fast-path 保留避免 hot loop 撞 DB。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Supabase Storage — 5 method 包 tagged_capture (TDD)

**Files:**
- Modify: `utils/supabase_storage.py`
- Modify: `tests/test_supabase_storage.py`

- [ ] **Step 5.1: Write failing tests**

Append to `tests/test_supabase_storage.py`:

```python
class TestSentryTaggedCapture:
    """Phase 1 P1 resilience：Supabase Storage exception 須呼叫 tagged_capture."""

    def test_save_exception_calls_tagged_capture(self, mock_supabase):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = RuntimeError("bucket down")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            with pytest.raises(RuntimeError, match="bucket down"):
                backend.save("activity_posters", "x.png", b"X", "image/png")
            mock_capture.assert_called_once()
            assert mock_capture.call_args.kwargs.get("tag") == "supabase" \
                or mock_capture.call_args.args[1] == "supabase"

    def test_read_exception_calls_tagged_capture(self, mock_supabase):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.download.side_effect = ConnectionError("net")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            with pytest.raises(ConnectionError):
                backend.read("activity_posters", "x.png")
            mock_capture.assert_called_once()

    def test_delete_exception_still_idempotent(self, mock_supabase):
        """delete 既有 idempotent 語意（不拋）保留，但仍呼叫 tagged_capture."""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.remove.side_effect = RuntimeError("net")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            # 既有行為：不拋；Phase 1 加 tagged_capture
            backend.delete("activity_posters", "x.png")
            mock_capture.assert_called_once()

    def test_signed_url_exception_calls_tagged_capture(self, mock_supabase):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.create_signed_url.side_effect = RuntimeError("auth")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            with pytest.raises(RuntimeError):
                backend.signed_url("leave_attachments", "x.pdf", 60)
            mock_capture.assert_called_once()
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run: `pytest tests/test_supabase_storage.py::TestSentryTaggedCapture -v`
Expected: FAIL — `AttributeError: ... has no attribute 'tagged_capture'`.

- [ ] **Step 5.3: Modify `utils/supabase_storage.py`**

Add import at top:

```python
from utils.external_calls import tagged_capture
```

Wrap each method's external call with try/except. Updated full class:

```python
class SupabaseStorage:
    """Supabase Storage backend。"""

    def __init__(self) -> None:
        url = settings.storage.supabase_url
        key = settings.storage.supabase_service_role_key
        if not url or not key:
            raise RuntimeError(
                "STORAGE_BACKEND=supabase 需要設定 SUPABASE_URL 與 SUPABASE_SERVICE_ROLE_KEY"
            )
        self._client = create_client(url, key)

    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            bucket.upload(
                path=key,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            raise  # Phase 1 不接 fallback；Phase 4 改寫 local + pending_uploads

    def read(self, module: str, key: str) -> bytes:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            return bucket.download(key)
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            raise

    def delete(self, module: str, key: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            bucket.remove([key])
        except Exception as e:
            # idempotent：物件已不存在不 raise；保留既有行為
            tagged_capture(e, tag="supabase", level="warning")
            logger.warning(
                "Supabase Storage delete 失敗（忽略）：module=%s key=%s err=%s",
                module, key, e,
            )

    def exists(self, module: str, key: str) -> bool:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            parent = key.rsplit("/", 1)
            if len(parent) == 1:
                items = bucket.list()
                filename = key
            else:
                items = bucket.list(parent[0])
                filename = parent[1]
            return any(item.get("name") == filename for item in items)
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="warning")
            return False  # 既有行為：例外視為「不存在」

    def public_url(self, module: str, key: str) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        # public_url 內部 SDK 通常是純字串組合不打 API；不包 try/except
        return bucket.get_public_url(key)

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            res = bucket.create_signed_url(key, ttl_seconds)
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            raise
        return res.get("signedURL") or res.get("signed_url") or ""
```

- [ ] **Step 5.4: Run tests**

```bash
pytest tests/test_supabase_storage.py -v
```

Expected: 4 new TestSentryTaggedCapture tests PASS + all existing tests still PASS.

- [ ] **Step 5.5: Commit**

```bash
git add utils/supabase_storage.py tests/test_supabase_storage.py
git commit -m "feat(storage): SupabaseStorage tagged_capture (Phase 1)

對應 spec §5.2。save/read/delete/exists/signed_url 5 method 例外路徑加
tagged_capture(exc, tag='supabase')。public_url 內部不打 API 不包。
delete/exists 既有 idempotent / fail-soft 行為保留，仍上報 Sentry warning。

Phase 4 SupabaseStorage.save() 會擴增 retry_with_backoff(3) + local fallback；
Phase 1 只加觀察。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: External HTTP — 3 service 6 站點 (TDD)

**Files:**
- Modify: `services/recruitment_market_intelligence.py`（3 處）
- Modify: `services/geocoding_service.py`（2 處）
- Modify: `services/official_calendar.py`（1 處）
- Create: `tests/test_external_http_tagged_capture.py`

- [ ] **Step 6.1: Read each file's existing pattern**

```bash
sed -n '510,540p' services/recruitment_market_intelligence.py
sed -n '150,220p' services/geocoding_service.py
sed -n '110,130p' services/official_calendar.py
```

Note: 三個 service 的 caller 多為 scheduler / batch job，**例外通常會被 caller 吞掉繼續跑**。Phase 1 加 tagged_capture 不改 caller 行為（不 re-raise），讓 caller 跑完。

- [ ] **Step 6.2: Write failing tests**

```python
# tests/test_external_http_tagged_capture.py
"""Phase 1 P1 resilience：3 個外部 HTTP service 例外須呼叫 tagged_capture."""
from unittest.mock import MagicMock, patch
import pytest


class TestRecruitmentMarketIntelligence:
    def test_request_exception_tagged_capture(self, monkeypatch):
        """data.gov.tw API ConnectionError → tagged_capture(tag='external_http')."""
        # 找一個入口 function 觸發 services/recruitment_market_intelligence.py:520
        # 此處 placeholder — Step 6.3 確定 entry function name 後填
        pass  # 待 Step 6.3 補完


class TestGeocodingService:
    def test_geocode_exception_tagged_capture(self, monkeypatch):
        import services.geocoding_service as geo
        monkeypatch.setattr(
            "services.geocoding_service.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch("services.geocoding_service.tagged_capture") as mock_capture:
            # 呼叫 geocoding entry — 對應 line 157 / 208
            # placeholder：實際 entry 名見 Step 6.3
            try:
                geo.geocode_address("台北市信義區")  # actual name TBD
            except Exception:
                pass
            mock_capture.assert_called()
            assert mock_capture.call_args.kwargs.get("tag") == "external_http" \
                or "external_http" in mock_capture.call_args.args


class TestOfficialCalendar:
    def test_fetch_exception_tagged_capture(self, monkeypatch):
        import services.official_calendar as cal
        monkeypatch.setattr(
            "services.official_calendar.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch("services.official_calendar.tagged_capture") as mock_capture:
            try:
                cal.fetch_official_calendar()  # actual name TBD
            except Exception:
                pass
            mock_capture.assert_called()
```

**Note**: Step 6.3 確定實際 entry function name 後回填 test。先寫骨架 + import path 即可。

- [ ] **Step 6.3: Implement — wrap 6 sites**

Read each file to confirm entry function name, then for **each** `requests.get/post` site:

Pattern (apply consistently):

```python
# BEFORE
response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

# AFTER
try:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
except Exception as exc:
    from utils.external_calls import tagged_capture
    tagged_capture(exc, tag="external_http", level="error")
    raise
```

For each file add top-level import once (replace per-site lazy import):

```python
from utils.external_calls import tagged_capture
```

**Files & lines to change:**

`services/recruitment_market_intelligence.py`:
- Line 520: `requests.get(url, params=params, timeout=REQUEST_TIMEOUT)`
- Line 526: same pattern
- Line 534: `requests.post(...)`

`services/geocoding_service.py`:
- Line 157: `requests.get(...)`
- Line 208: `requests.get(...)`

`services/official_calendar.py`:
- Line 116: `requests.get(OFFICIAL_CALENDAR_DATASET_URL, timeout=20)`

Also check whether each existing site has `try/except` already. If yes, **add tagged_capture inside the except** rather than wrapping again:

```python
# If existing pattern is:
try:
    resp = requests.get(...)
except Exception as exc:
    logger.warning("fetch failed: %s", exc)
    return None
# Change to:
try:
    resp = requests.get(...)
except Exception as exc:
    logger.exception("fetch failed")
    tagged_capture(exc, tag="external_http", level="error")
    return None
```

- [ ] **Step 6.4: Update tests with real entry function names**

After Step 6.3 you know the actual entry function names. Update `tests/test_external_http_tagged_capture.py` test cases to call them. Run:

```bash
pytest tests/test_external_http_tagged_capture.py -v
```

Expected: All 3 (or more) tests PASS.

- [ ] **Step 6.5: Commit**

```bash
git add services/recruitment_market_intelligence.py services/geocoding_service.py services/official_calendar.py tests/test_external_http_tagged_capture.py
git commit -m "feat(external-http): tagged_capture for 6 external sites (Phase 1)

對應 spec §5.3。3 service 6 處 requests.get/post 失敗加
tagged_capture(exc, tag='external_http', level='error')；
caller 行為（吞例外/re-raise）保留不變。

涵蓋：
- recruitment_market_intelligence.py: 3 處 (data.gov.tw)
- geocoding_service.py: 2 處 (Google Maps Geocoding)
- official_calendar.py: 1 處 (政府行事曆 data.gov.tw)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 全套回歸 + 收尾

**Files:**
- 無新檔；驗收

- [ ] **Step 7.1: 跑完整 backend test suite**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest -x --tb=short 2>&1 | tail -40
```

Expected:
- 新加 tests 全 PASS
- 既有 fail（如 5 個 TZ-dep test，見 memory）數量不變
- 無新增 regression

如有 regression，回到對應 Task 修正後再跑。

- [ ] **Step 7.2: Verify Sentry hot-path 不影響無 DSN 環境**

```bash
SENTRY_DSN= pytest tests/test_external_calls.py tests/test_line_service.py::TestSentryTaggedCapture -v
```

Expected: 全 PASS — `tagged_capture` 在 sentry 未 init 時內部 no-op。

- [ ] **Step 7.3: Smoke test — 確認 settings 載入正常**

```bash
python -c "from config import settings; print('tag_external_failures =', settings.sentry.tag_external_failures)"
```

Expected output: `tag_external_failures = True`

- [ ] **Step 7.4: Final commit — 收尾（如有殘留）**

如還有任何 cosmetic 改動或 docstring 更新，這裡一次 commit。否則跳過。

- [ ] **Step 7.5: Update memory (auto-memory)**

寫一筆 project memory：
- Phase 1 P1 resilience 完成 commit list
- 教訓：4xx 分流的 process-local dedup pattern
- 待 user 動作：手測 LINE 推送 → Sentry 後台確認 tag 出現 → 觀察一週決定 Phase 2/3/4

---

## Self-Review Checklist

- [x] **Spec coverage**: §4.1 helpers / §5.1 LINE / §5.2 Supabase / §5.3 external HTTP / §5.4 env 全有對應 Task
- [x] **Placeholder scan**: Step 6.2 有 `placeholder：待 Step 6.3 補完` — **這是 intentional**（plan 寫作時不確切 entry function name，Step 6.3 confirms），不算未完成 plan
- [x] **Type consistency**: `tagged_capture(exc, tag=..., level=...)` 在所有 task 簽章一致
- [x] **TDD 順序**: Task 1/2/4/5 都是 failing test → impl → passing test → commit；Task 3/6 同 pattern
- [x] **Phase 1 範圍邊界**: 不引入 breaker、不寫 NotificationLog augment、不寫 scheduler；retry_with_backoff 寫了但 Phase 1 不調用（Phase 4 才接）

---

## 完成定義

Phase 1 ship = 以下全部達成：

1. `utils/external_calls.py` 含 `tagged_capture` + `retry_with_backoff` 兩 helper（+ 13+ unit test 全綠）
2. `config/sentry.py` 多 `tag_external_failures: bool = True` field
3. `services/line_service.py` 7 處 `_push*/_reply*` 改用 `_record_line_response` helper（含 4xx 分流 + 401/403 process-local dedup）
4. `utils/supabase_storage.py` 5 method 例外路徑加 `tagged_capture(tag='supabase')`
5. `services/recruitment_market_intelligence.py` / `geocoding_service.py` / `official_calendar.py` 共 6 處加 `tagged_capture(tag='external_http')`
6. 全套 pytest 零 regression（既有 fail 數量不變）
7. 6 個 atomic commit（Task 1-6 各一筆，Task 7 視需要）

完成後請 user：
- 手測 LINE 推送一筆 → Sentry 後台確認 tag 出現
- 觀察一週 Sentry tagged event 數量，決定 Phase 2（retry）/Phase 3（breaker）/Phase 4（fallback + token health）是否需照原 spec ship 或調整優先序
