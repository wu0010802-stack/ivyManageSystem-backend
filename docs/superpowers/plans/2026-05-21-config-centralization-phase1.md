# Config 集中化 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ivy-backend 73 個 env 變數從散落 ~168 處的 `os.getenv` 集中到 `pydantic-settings` 驅動的 `config/` 模組，建立型別安全、`.env.example` 完整、test friendly 的單一來源。

**Architecture:** `config/` 包 10 個 sub-Settings（按 domain 拆檔）+ 主 `Settings` 用 `Field(default_factory=...)` 組合 + `lru_cache` singleton + `reset_for_tests()` helper。所有 callsite 改 `from config import settings; settings.<domain>.<field>`，所有 test 改 `monkeypatch.setenv` + `reset_for_tests()`。零行為變化、env 變數名保留現名。

**Tech Stack:** Python 3.11 / FastAPI / Pydantic 2 + `pydantic-settings>=2.0` / pytest / pytest-monkeypatch

**Spec:** `docs/superpowers/specs/2026-05-21-config-centralization-phase1-design.md`

---

## File Structure

新增：
- `config/__init__.py` — `settings`、`get_settings`、`reset_for_tests` 對外 API
- `config/base.py` — 主 `Settings` class、`_scrub` helper、`model_dump_safe()`
- `config/validators.py` — `parse_bool_env`、`parse_csv_list`、`BoolEnv`、`CsvList` 型別 alias
- `config/core.py` — `CoreSettings`
- `config/parent_db.py` — `ParentDBSettings`
- `config/network.py` — `NetworkSettings`
- `config/scheduler.py` — `SchedulerSettings`
- `config/sentry.py` — `SentrySettings`
- `config/line.py` — `LineSettings`
- `config/recruitment.py` — `RecruitmentSettings`
- `config/geocoding.py` — `GeocodingSettings`
- `config/storage.py` — `StorageSettings`
- `config/misc.py` — `MiscSettings`
- `tests/test_config/__init__.py`
- `tests/test_config/test_validators.py`
- `tests/test_config/test_core.py`
- `tests/test_config/test_parent_db.py`
- `tests/test_config/test_network.py`
- `tests/test_config/test_scheduler.py`
- `tests/test_config/test_sentry.py`
- `tests/test_config/test_line.py`
- `tests/test_config/test_recruitment.py`
- `tests/test_config/test_geocoding.py`
- `tests/test_config/test_storage.py`
- `tests/test_config/test_misc.py`
- `tests/test_config/test_base.py`

修改：
- `requirements.txt` — 加 `pydantic-settings>=2.0`
- `.env.example` — 補齊 73 變數註解
- `tests/conftest.py` — 加 `_reset_settings_cache` autouse fixture
- 35 個 prod 檔 — `os.getenv` → `settings.<domain>.<field>`
- 14 個 test 檔 — `patch.dict(os.environ)` / `os.environ.setdefault` → `monkeypatch.setenv` + `reset_for_tests()`
- `.github/workflows/ci.yml` — 加 grep gate

---

## Commit 1 — Settings 基建（無 callsite 變化）

### Task 1: 加 pydantic-settings 依賴

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 加依賴**

打開 `requirements.txt` 找到 `pydantic>=2.0.0` 行下方加：

```
pydantic-settings>=2.0,<3.0
```

- [ ] **Step 2: 安裝**

Run: `pip install -r requirements.txt`
Expected: `Successfully installed pydantic-settings-2.x.x`

- [ ] **Step 3: 確認 import 可用**

Run: `python -c "from pydantic_settings import BaseSettings, SettingsConfigDict; print('ok')"`
Expected: `ok`

---

### Task 2: config/validators.py + 測試

**Files:**
- Create: `config/validators.py`
- Create: `tests/test_config/__init__.py`（空檔）
- Create: `tests/test_config/test_validators.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/__init__.py`（空檔）。

Create `tests/test_config/test_validators.py`:

```python
import pytest
from config.validators import parse_bool_env, parse_csv_list


class TestParseBoolEnv:
    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "True", "yes", "YES", " 1 ", " true "])
    def test_truthy(self, v):
        assert parse_bool_env(v) is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "FALSE", "", "off", "enabled", "xyz", None])
    def test_falsy(self, v):
        assert parse_bool_env(v) is False

    def test_bool_passthrough(self):
        assert parse_bool_env(True) is True
        assert parse_bool_env(False) is False


class TestParseCsvList:
    def test_basic(self):
        assert parse_csv_list("a,b,c") == ["a", "b", "c"]

    def test_strip_whitespace(self):
        assert parse_csv_list(" a , b , c ") == ["a", "b", "c"]

    def test_empty_filtered(self):
        assert parse_csv_list("a,,b,") == ["a", "b"]

    def test_none(self):
        assert parse_csv_list(None) == []

    def test_empty_string(self):
        assert parse_csv_list("") == []

    def test_list_passthrough(self):
        assert parse_csv_list(["a", "b"]) == ["a", "b"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_validators.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config.validators'`

- [ ] **Step 3: 寫實作**

Create `config/validators.py`:

```python
"""Reusable Pydantic validators for env-derived fields."""
from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator


def parse_bool_env(v: str | bool | None) -> bool:
    """Accept '1' / 'true' / 'yes' (case-insensitive). Everything else → False."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def parse_csv_list(v: str | list[str] | None) -> list[str]:
    """Parse comma-separated string into list of trimmed non-empty strings."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [s.strip() for s in str(v).split(",") if s.strip()]


BoolEnv = Annotated[bool, BeforeValidator(parse_bool_env)]
CsvList = Annotated[list[str], BeforeValidator(parse_csv_list)]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_validators.py -v`
Expected: 12 passed

- [ ] **Step 5: 暫不 commit**（最後 Commit 1 一起 commit）

---

### Task 3: config/core.py + 測試

**Files:**
- Create: `config/core.py`
- Create: `tests/test_config/test_core.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_core.py`:

```python
import pytest
from config.core import CoreSettings


def test_defaults(monkeypatch):
    """全部 env 清空時應給 development default."""
    for var in ("ENV", "DATABASE_URL", "JWT_SECRET_KEY", "ENABLE_API_DOCS",
                "ADMIN_INIT_USERNAME", "ADMIN_INIT_PASSWORD",
                "JWT_ABSOLUTE_LIFETIME_HOURS"):
        monkeypatch.delenv(var, raising=False)
    s = CoreSettings()
    assert s.env == "development"
    assert s.database_url == "postgresql://localhost:5432/ivymanagement"
    assert s.enable_api_docs is False
    assert s.jwt_absolute_lifetime_hours == 8
    assert s.admin_init_username is None
    assert s.admin_init_password is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://prod/db")
    monkeypatch.setenv("JWT_SECRET_KEY", "supersecret")
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    monkeypatch.setenv("JWT_ABSOLUTE_LIFETIME_HOURS", "12")
    s = CoreSettings()
    assert s.env == "production"
    assert s.database_url == "postgresql://prod/db"
    assert s.jwt_secret_key == "supersecret"
    assert s.enable_api_docs is True
    assert s.jwt_absolute_lifetime_hours == 12


def test_is_production_property(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert CoreSettings().is_production is True
    monkeypatch.setenv("ENV", "prod")
    assert CoreSettings().is_production is True
    monkeypatch.setenv("ENV", "development")
    assert CoreSettings().is_production is False
    monkeypatch.setenv("ENV", "")
    assert CoreSettings().is_production is False


def test_dev_router_enabled(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    assert CoreSettings().dev_router_enabled is True
    monkeypatch.setenv("ENV", "test")
    assert CoreSettings().dev_router_enabled is True
    monkeypatch.setenv("ENV", "production")
    assert CoreSettings().dev_router_enabled is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_core.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 寫實作**

Create `config/core.py`:

```python
"""Core application settings: env, database, JWT, admin init."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv

_DEV_ROUTER_ENVS = frozenset({"development", "dev", "test", "testing", ""})


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    env: str = "development"
    database_url: str = "postgresql://localhost:5432/ivymanagement"
    jwt_secret_key: str | None = None
    jwt_absolute_lifetime_hours: int = 8
    enable_api_docs: BoolEnv = False
    admin_init_username: str | None = None
    admin_init_password: str | None = None

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in ("production", "prod")

    @property
    def dev_router_enabled(self) -> bool:
        return self.env.strip().lower() in _DEV_ROUTER_ENVS
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_core.py -v`
Expected: 4 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 4: config/parent_db.py + 測試

**Files:**
- Create: `config/parent_db.py`
- Create: `tests/test_config/test_parent_db.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_parent_db.py`:

```python
import pytest
from config.parent_db import ParentDBSettings


def test_defaults(monkeypatch):
    for var in ("PARENT_DB_USER", "PARENT_DB_PASSWORD",
                "PARENT_RLS_GUARD_ENABLED", "PARENT_RLS_METRICS_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    s = ParentDBSettings()
    assert s.user is None
    assert s.password is None
    assert s.rls_guard_enabled is False
    assert s.rls_metrics_disabled is False


def test_env_reads(monkeypatch):
    monkeypatch.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "secret")
    monkeypatch.setenv("PARENT_RLS_GUARD_ENABLED", "true")
    monkeypatch.setenv("PARENT_RLS_METRICS_DISABLED", "1")
    s = ParentDBSettings()
    assert s.user == "ivy_parent_login"
    assert s.password == "secret"
    assert s.rls_guard_enabled is True
    assert s.rls_metrics_disabled is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_parent_db.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/parent_db.py`:

```python
"""Parent portal RLS-isolated DB credentials + RLS feature flags."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class ParentDBSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    user: str | None = Field(default=None, validation_alias="PARENT_DB_USER", repr=False)
    password: str | None = Field(default=None, validation_alias="PARENT_DB_PASSWORD", repr=False)
    rls_guard_enabled: BoolEnv = Field(default=False, validation_alias="PARENT_RLS_GUARD_ENABLED")
    rls_metrics_disabled: BoolEnv = Field(default=False, validation_alias="PARENT_RLS_METRICS_DISABLED")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_parent_db.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 5: config/network.py + 測試

**Files:**
- Create: `config/network.py`
- Create: `tests/test_config/test_network.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_network.py`:

```python
import pytest
from config.network import NetworkSettings


def test_defaults(monkeypatch):
    for var in ("CORS_ORIGINS", "ALLOWED_HOSTS", "TRUSTED_PROXY_IPS",
                "CSP_SCRIPT_HASHES", "COOKIE_SAMESITE", "SCHOOL_WIFI_IPS",
                "RATE_LIMIT_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    s = NetworkSettings()
    assert s.cors_origins == []
    assert s.allowed_hosts == []
    assert s.trusted_proxy_ips == "*"
    assert s.csp_script_hashes == []
    assert s.cookie_samesite == "lax"
    assert s.school_wifi_ips == []
    assert s.rate_limit_backend == "memory"


def test_csv_parsing(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,https://example.com")
    monkeypatch.setenv("ALLOWED_HOSTS", " a.com , b.com ")
    monkeypatch.setenv("SCHOOL_WIFI_IPS", "192.168.1.0/24,10.0.0.0/8")
    s = NetworkSettings()
    assert s.cors_origins == ["http://localhost:5173", "https://example.com"]
    assert s.allowed_hosts == ["a.com", "b.com"]
    assert s.school_wifi_ips == ["192.168.1.0/24", "10.0.0.0/8"]


def test_cookie_samesite_literal(monkeypatch):
    monkeypatch.setenv("COOKIE_SAMESITE", "strict")
    s = NetworkSettings()
    assert s.cookie_samesite == "strict"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_network.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/network.py`:

```python
"""Network-related settings: CORS, hosts, proxy, CSP, cookies, rate limit."""
from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import CsvList


class NetworkSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    cors_origins: CsvList = []
    allowed_hosts: CsvList = []
    trusted_proxy_ips: str = "*"
    csp_script_hashes: CsvList = []
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    school_wifi_ips: CsvList = []
    rate_limit_backend: str = "memory"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_network.py -v`
Expected: 3 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 6: config/scheduler.py + 測試

**Files:**
- Create: `config/scheduler.py`
- Create: `tests/test_config/test_scheduler.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_scheduler.py`:

```python
import pytest
from config.scheduler import SchedulerSettings

_ALL_VARS = (
    "ACTIVITY_WAITLIST_SWEEPER_ENABLED", "ACTIVITY_WAITLIST_SCHEDULER_ENABLED",
    "ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS", "ACTIVITY_WAITLIST_CHECK_INTERVAL",
    "ACTIVITY_WAITLIST_REMINDER_OFFSET_HOURS", "ACTIVITY_WAITLIST_FINAL_REMINDER_OFFSET_HOURS",
    "ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS",
    "MEDICATION_REMINDER_ENABLED", "MEDICATION_REMINDER_CHECK_INTERVAL",
    "MEDICATION_REMINDER_HOUR", "MEDICATION_REMINDER_MINUTE",
    "AUTO_GRADUATION_ENABLED", "AUTO_GRADUATION_CHECK_INTERVAL",
    "AUTO_GRADUATION_MONTH", "AUTO_GRADUATION_DAY", "AUTO_GRADUATION_PREVIEW_DAYS",
    "SALARY_AUTO_SNAPSHOT_ENABLED", "SALARY_SNAPSHOT_CHECK_INTERVAL",
    "OFFICIAL_CALENDAR_SYNC_ENABLED", "OFFICIAL_CALENDAR_SYNC_INTERVAL",
    "FINANCE_RECONCILIATION_ENABLED", "SECURITY_GC_DISABLED",
)


def test_defaults(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    s = SchedulerSettings()
    # bool defaults: 全部 False（含 disabled flags 也是 False = 預設啟用 GC）
    assert s.activity_waitlist_sweeper_enabled is False
    assert s.medication_reminder_enabled is False
    assert s.auto_graduation_enabled is False
    assert s.salary_auto_snapshot_enabled is False
    assert s.official_calendar_sync_enabled is False
    assert s.finance_reconciliation_enabled is False
    assert s.security_gc_disabled is False
    # interval defaults
    assert s.activity_waitlist_sweep_interval_seconds == 600
    assert s.activity_waitlist_check_interval == 300
    assert s.medication_reminder_check_interval == 60
    assert s.auto_graduation_check_interval == 86400
    # time-of-day
    assert s.medication_reminder_hour == 8
    assert s.medication_reminder_minute == 0
    assert s.auto_graduation_month == 7
    assert s.auto_graduation_day == 31


def test_bool_env_parsing(monkeypatch):
    monkeypatch.setenv("MEDICATION_REMINDER_ENABLED", "yes")
    monkeypatch.setenv("ACTIVITY_WAITLIST_SWEEPER_ENABLED", "1")
    monkeypatch.setenv("SECURITY_GC_DISABLED", "TRUE")
    s = SchedulerSettings()
    assert s.medication_reminder_enabled is True
    assert s.activity_waitlist_sweeper_enabled is True
    assert s.security_gc_disabled is True


def test_int_parsing(monkeypatch):
    monkeypatch.setenv("ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS", "1200")
    monkeypatch.setenv("MEDICATION_REMINDER_HOUR", "9")
    s = SchedulerSettings()
    assert s.activity_waitlist_sweep_interval_seconds == 1200
    assert s.medication_reminder_hour == 9
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_scheduler.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/scheduler.py`:

```python
"""Background scheduler enable/interval/time-of-day settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Activity waitlist
    activity_waitlist_sweeper_enabled: BoolEnv = False
    activity_waitlist_scheduler_enabled: BoolEnv = False
    activity_waitlist_sweep_interval_seconds: int = 600
    activity_waitlist_check_interval: int = 300
    activity_waitlist_reminder_offset_hours: int = 24
    activity_waitlist_final_reminder_offset_hours: int = 6
    activity_waitlist_confirm_window_hours: int = 48

    # Medication reminder
    medication_reminder_enabled: BoolEnv = False
    medication_reminder_check_interval: int = 60
    medication_reminder_hour: int = 8
    medication_reminder_minute: int = 0

    # Auto graduation
    auto_graduation_enabled: BoolEnv = False
    auto_graduation_check_interval: int = 86400
    auto_graduation_month: int = 7
    auto_graduation_day: int = 31
    auto_graduation_preview_days: int = 30

    # Salary auto snapshot
    salary_auto_snapshot_enabled: BoolEnv = False
    salary_snapshot_check_interval: int = 86400

    # Official calendar sync
    official_calendar_sync_enabled: BoolEnv = False
    official_calendar_sync_interval: int = 86400

    # Misc schedulers
    finance_reconciliation_enabled: BoolEnv = False
    security_gc_disabled: BoolEnv = False
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_scheduler.py -v`
Expected: 3 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 7: config/sentry.py + 測試

**Files:**
- Create: `config/sentry.py`
- Create: `tests/test_config/test_sentry.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_sentry.py`:

```python
import pytest
from config.sentry import SentrySettings


def test_defaults(monkeypatch):
    for var in ("SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_RELEASE",
                "SENTRY_TRACES_SAMPLE_RATE"):
        monkeypatch.delenv(var, raising=False)
    s = SentrySettings()
    assert s.dsn is None
    assert s.environment == "production"
    assert s.release is None
    assert s.traces_sample_rate == 0.1
    assert s.enabled is False


def test_enabled_when_dsn_set(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.5")
    s = SentrySettings()
    assert s.dsn == "https://abc@sentry.io/1"
    assert s.enabled is True
    assert s.traces_sample_rate == 0.5
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_sentry.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/sentry.py`:

```python
"""Sentry error tracking settings. DSN empty = no-op (整套 Sentry 自動關閉)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRY_", extra="ignore", case_sensitive=False)

    dsn: str | None = Field(default=None, repr=False)
    environment: str = "production"
    release: str | None = None
    traces_sample_rate: float = 0.1

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_sentry.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 8: config/line.py + 測試

**Files:**
- Create: `config/line.py`
- Create: `tests/test_config/test_line.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_line.py`:

```python
import pytest
from config.line import LineSettings


def test_defaults(monkeypatch):
    for var in ("LINE_LOGIN_CHANNEL_ID", "LINE_LOGIN_CHANNEL_SECRET",
                "LIFF_ID", "LINE_CHANNEL_ACCESS_TOKEN", "VITE_LIFF_ID"):
        monkeypatch.delenv(var, raising=False)
    s = LineSettings()
    assert s.login_channel_id is None
    assert s.login_channel_secret is None
    assert s.liff_id is None
    assert s.channel_access_token is None
    assert s.vite_liff_id is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "1234")
    monkeypatch.setenv("LIFF_ID", "1234-abcdef")
    monkeypatch.setenv("VITE_LIFF_ID", "1234-abcdef")
    s = LineSettings()
    assert s.login_channel_id == "1234"
    assert s.liff_id == "1234-abcdef"
    assert s.vite_liff_id == "1234-abcdef"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_line.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/line.py`:

```python
"""LINE Login + LIFF settings. Bot messaging token 仍走 DB line_configs，不在此檔。"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LineSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    login_channel_id: str | None = Field(default=None, validation_alias="LINE_LOGIN_CHANNEL_ID")
    login_channel_secret: str | None = Field(default=None, validation_alias="LINE_LOGIN_CHANNEL_SECRET", repr=False)
    liff_id: str | None = Field(default=None, validation_alias="LIFF_ID")
    channel_access_token: str | None = Field(default=None, validation_alias="LINE_CHANNEL_ACCESS_TOKEN", repr=False)
    vite_liff_id: str | None = Field(default=None, validation_alias="VITE_LIFF_ID")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_line.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 9: config/recruitment.py + 測試

**Files:**
- Create: `config/recruitment.py`
- Create: `tests/test_config/test_recruitment.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_recruitment.py`:

```python
import pytest
from config.recruitment import RecruitmentSettings

_ALL_VARS = (
    "IVYKIDS_USERNAME", "IVYKIDS_PASSWORD", "IVYKIDS_LOGIN_URL", "IVYKIDS_DATA_URL",
    "IVYKIDS_SYNC_ENABLED", "IVYKIDS_SYNC_INTERVAL_MINUTES",
    "RECRUITMENT_CAMPUS_NAME", "RECRUITMENT_CAMPUS_ADDRESS",
    "RECRUITMENT_CAMPUS_LAT", "RECRUITMENT_CAMPUS_LNG", "RECRUITMENT_CAMPUS_TRAVEL_MODE",
    "TGOS_APP_ID", "TGOS_API_KEY",
    "RECRUITMENT_MARKET_TIMEOUT_SECONDS",
)


def test_defaults(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    s = RecruitmentSettings()
    assert s.ivykids_username is None
    assert s.ivykids_password is None
    assert s.ivykids_login_url == "https://www.ivykids.tw/manage/"
    assert s.ivykids_data_url == "https://www.ivykids.tw/manage/make_an_appointment/"
    assert s.ivykids_sync_enabled is False
    assert s.ivykids_sync_interval_minutes == 10
    assert s.campus_name is None
    assert s.campus_lat is None
    assert s.campus_lng is None
    assert s.campus_travel_mode == "driving"
    assert s.market_timeout_seconds == 8


def test_lat_lng_float(monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CAMPUS_LAT", "25.0330")
    monkeypatch.setenv("RECRUITMENT_CAMPUS_LNG", "121.5654")
    s = RecruitmentSettings()
    assert s.campus_lat == 25.0330
    assert s.campus_lng == 121.5654
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_recruitment.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/recruitment.py`:

```python
"""Recruitment-related settings: IVYKIDS sync, campus geo, TGOS fallback."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class RecruitmentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # IVYKIDS sync
    ivykids_username: str | None = Field(default=None, validation_alias="IVYKIDS_USERNAME")
    ivykids_password: str | None = Field(default=None, validation_alias="IVYKIDS_PASSWORD", repr=False)
    ivykids_login_url: str = Field(default="https://www.ivykids.tw/manage/", validation_alias="IVYKIDS_LOGIN_URL")
    ivykids_data_url: str = Field(default="https://www.ivykids.tw/manage/make_an_appointment/", validation_alias="IVYKIDS_DATA_URL")
    ivykids_sync_enabled: BoolEnv = Field(default=False, validation_alias="IVYKIDS_SYNC_ENABLED")
    ivykids_sync_interval_minutes: int = Field(default=10, validation_alias="IVYKIDS_SYNC_INTERVAL_MINUTES")

    # Campus geo (RECRUITMENT_CAMPUS_* prefix)
    campus_name: str | None = Field(default=None, validation_alias="RECRUITMENT_CAMPUS_NAME")
    campus_address: str | None = Field(default=None, validation_alias="RECRUITMENT_CAMPUS_ADDRESS")
    campus_lat: float | None = Field(default=None, validation_alias="RECRUITMENT_CAMPUS_LAT")
    campus_lng: float | None = Field(default=None, validation_alias="RECRUITMENT_CAMPUS_LNG")
    campus_travel_mode: str = Field(default="driving", validation_alias="RECRUITMENT_CAMPUS_TRAVEL_MODE")

    # TGOS fallback
    tgos_app_id: str | None = Field(default=None, validation_alias="TGOS_APP_ID")
    tgos_api_key: str | None = Field(default=None, validation_alias="TGOS_API_KEY", repr=False)

    # Market intelligence
    market_timeout_seconds: int = Field(default=8, validation_alias="RECRUITMENT_MARKET_TIMEOUT_SECONDS")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_recruitment.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 10: config/geocoding.py + 測試

**Files:**
- Create: `config/geocoding.py`
- Create: `tests/test_config/test_geocoding.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_geocoding.py`:

```python
import pytest
from config.geocoding import GeocodingSettings


def test_defaults(monkeypatch):
    for var in ("GOOGLE_MAPS_API_KEY", "GEOCODING_PROVIDER",
                "GEOCODING_USER_AGENT", "GEOCODING_CONTACT_EMAIL",
                "GEOCODING_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    s = GeocodingSettings()
    assert s.google_maps_api_key is None
    assert s.provider == "nominatim"
    assert s.user_agent == "ivyManageSystem/1.0"
    assert s.contact_email is None
    assert s.timeout_seconds == 8


def test_env_reads(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza...")
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GEOCODING_TIMEOUT_SECONDS", "15")
    s = GeocodingSettings()
    assert s.google_maps_api_key == "AIza..."
    assert s.provider == "google"
    assert s.timeout_seconds == 15
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_geocoding.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/geocoding.py`:

```python
"""Geocoding settings: Google Maps / Nominatim / TGOS fallback chain."""
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeocodingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    google_maps_api_key: str | None = Field(default=None, validation_alias="GOOGLE_MAPS_API_KEY", repr=False)
    provider: Literal["google", "nominatim", "tgos"] = Field(default="nominatim", validation_alias="GEOCODING_PROVIDER")
    user_agent: str = Field(default="ivyManageSystem/1.0", validation_alias="GEOCODING_USER_AGENT")
    contact_email: str | None = Field(default=None, validation_alias="GEOCODING_CONTACT_EMAIL")
    timeout_seconds: int = Field(default=8, validation_alias="GEOCODING_TIMEOUT_SECONDS")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_geocoding.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 11: config/storage.py + 測試

**Files:**
- Create: `config/storage.py`
- Create: `tests/test_config/test_storage.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_storage.py`:

```python
from pathlib import Path
import pytest
from config.storage import StorageSettings


def test_defaults(monkeypatch):
    for var in ("STORAGE_BACKEND", "STORAGE_ROOT",
                "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                "SUPABASE_STORAGE_SIGNED_URL_TTL",
                "GROWTH_REPORT_ROOT", "GROWTH_REPORT_MAX_BYTES"):
        monkeypatch.delenv(var, raising=False)
    s = StorageSettings()
    assert s.backend == "local"
    assert s.root == Path("./uploads")
    assert s.supabase_url is None
    assert s.supabase_service_role_key is None
    assert s.supabase_signed_url_ttl == 3600
    assert s.growth_report_root == Path("./growth_reports")
    assert s.growth_report_max_bytes == 5_242_880


def test_supabase_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://xxx.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "eyJ...")
    monkeypatch.setenv("SUPABASE_STORAGE_SIGNED_URL_TTL", "7200")
    s = StorageSettings()
    assert s.backend == "supabase"
    assert s.supabase_url == "https://xxx.supabase.co"
    assert s.supabase_signed_url_ttl == 7200
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_storage.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/storage.py`:

```python
"""File storage settings: local FS / Supabase Storage / growth reports."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    backend: Literal["local", "supabase"] = Field(default="local", validation_alias="STORAGE_BACKEND")
    root: Path = Field(default=Path("./uploads"), validation_alias="STORAGE_ROOT")
    supabase_url: str | None = Field(default=None, validation_alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(default=None, validation_alias="SUPABASE_SERVICE_ROLE_KEY", repr=False)
    supabase_signed_url_ttl: int = Field(default=3600, validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL")
    growth_report_root: Path = Field(default=Path("./growth_reports"), validation_alias="GROWTH_REPORT_ROOT")
    growth_report_max_bytes: int = Field(default=5_242_880, validation_alias="GROWTH_REPORT_MAX_BYTES")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_storage.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 12: config/misc.py + 測試

**Files:**
- Create: `config/misc.py`
- Create: `tests/test_config/test_misc.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_misc.py`:

```python
import pytest
from config.misc import MiscSettings


def test_defaults(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "POS_CASH_DEPOSIT_WARNING_THRESHOLD",
                "ENABLE_LEAVE_OT_OFFSET", "ACTIVITY_QUERY_TOKEN_TTL_DAYS",
                "IVY_MCP_USERNAME", "IVY_MCP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    s = MiscSettings()
    assert s.anthropic_api_key is None
    assert s.pos_cash_deposit_warning_threshold == 5000
    assert s.enable_leave_ot_offset is False
    assert s.activity_query_token_ttl_days == 30
    assert s.ivy_mcp_username is None
    assert s.ivy_mcp_password is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    monkeypatch.setenv("POS_CASH_DEPOSIT_WARNING_THRESHOLD", "10000")
    monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "60")
    s = MiscSettings()
    assert s.enable_leave_ot_offset is True
    assert s.pos_cash_deposit_warning_threshold == 10000
    assert s.activity_query_token_ttl_days == 60
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_misc.py -v`
Expected: FAIL

- [ ] **Step 3: 寫實作**

Create `config/misc.py`:

```python
"""Miscellaneous settings that don't fit other domains."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class MiscSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY", repr=False)
    pos_cash_deposit_warning_threshold: int = Field(default=5000, validation_alias="POS_CASH_DEPOSIT_WARNING_THRESHOLD")
    enable_leave_ot_offset: BoolEnv = Field(default=False, validation_alias="ENABLE_LEAVE_OT_OFFSET")
    activity_query_token_ttl_days: int = Field(default=30, validation_alias="ACTIVITY_QUERY_TOKEN_TTL_DAYS")
    ivy_mcp_username: str | None = Field(default=None, validation_alias="IVY_MCP_USERNAME")
    ivy_mcp_password: str | None = Field(default=None, validation_alias="IVY_MCP_PASSWORD", repr=False)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config/test_misc.py -v`
Expected: 2 passed

- [ ] **Step 5: 暫不 commit**

---

### Task 13: config/base.py + __init__.py + model_dump_safe + 測試

**Files:**
- Create: `config/base.py`
- Create: `config/__init__.py`
- Create: `tests/test_config/test_base.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_config/test_base.py`:

```python
import pytest


def test_settings_composes_sub_settings(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    from config import reset_for_tests, get_settings
    reset_for_tests()
    s = get_settings()
    assert s.core.env == "production"
    assert s.core.is_production is True
    assert s.sentry.dsn == "https://abc@sentry.io/1"
    assert s.sentry.enabled is True


def test_get_settings_singleton():
    from config import reset_for_tests, get_settings
    reset_for_tests()
    assert get_settings() is get_settings()


def test_reset_for_tests_clears_cache(monkeypatch):
    from config import reset_for_tests, get_settings
    monkeypatch.setenv("ENV", "development")
    reset_for_tests()
    a = get_settings()
    assert a.core.env == "development"
    monkeypatch.setenv("ENV", "production")
    reset_for_tests()
    b = get_settings()
    assert b.core.env == "production"
    assert a is not b


def test_model_dump_safe_redacts_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "supersecret")
    monkeypatch.setenv("PARENT_DB_PASSWORD", "p4ssw0rd")
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza...")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
    from config import reset_for_tests, get_settings
    reset_for_tests()
    dumped = get_settings().model_dump_safe()
    # 含敏感 substring 的欄位被 redact
    assert dumped["core"]["jwt_secret_key"] == "***"
    assert dumped["parent_db"]["password"] == "***"
    assert dumped["sentry"]["dsn"] == "***"
    assert dumped["geocoding"]["google_maps_api_key"] == "***"
    # database_url 含 'url' 不在 denylist，照常出（雖然字串裡有密碼但 substring 不匹配 'url'）
    # 非敏感欄位照常出
    assert dumped["core"]["env"] == "development" or dumped["core"]["env"] == "production"


def test_model_dump_safe_preserves_none(monkeypatch):
    """敏感欄位若值為 None 不要 redact 成 '***'，方便 debug 看出未設。"""
    for var in ("JWT_SECRET_KEY", "PARENT_DB_PASSWORD", "SENTRY_DSN"):
        monkeypatch.delenv(var, raising=False)
    from config import reset_for_tests, get_settings
    reset_for_tests()
    dumped = get_settings().model_dump_safe()
    assert dumped["core"]["jwt_secret_key"] is None
    assert dumped["parent_db"]["password"] is None
    assert dumped["sentry"]["dsn"] is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config/test_base.py -v`
Expected: FAIL with `ImportError: cannot import name 'reset_for_tests' from 'config'`

- [ ] **Step 3: 寫 base.py**

Create `config/base.py`:

```python
"""Centralized Settings combining all sub-Settings domains."""
from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core import CoreSettings
from .geocoding import GeocodingSettings
from .line import LineSettings
from .misc import MiscSettings
from .network import NetworkSettings
from .parent_db import ParentDBSettings
from .recruitment import RecruitmentSettings
from .scheduler import SchedulerSettings
from .sentry import SentrySettings
from .storage import StorageSettings


_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "secret", "password", "token", "api_key", "dsn",
)


def _scrub(data: Any, denylist: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _scrub(v, denylist)
        elif (
            isinstance(k, str)
            and any(s in k.lower() for s in denylist)
            and v not in (None, "")
        ):
            out[k] = "***"
        else:
            out[k] = v
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    core: CoreSettings = Field(default_factory=CoreSettings)
    parent_db: ParentDBSettings = Field(default_factory=ParentDBSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    line: LineSettings = Field(default_factory=LineSettings)
    recruitment: RecruitmentSettings = Field(default_factory=RecruitmentSettings)
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    misc: MiscSettings = Field(default_factory=MiscSettings)

    def model_dump_safe(self) -> dict[str, Any]:
        """Dump settings with sensitive fields redacted to '***'."""
        return _scrub(self.model_dump(), _SENSITIVE_KEY_SUBSTRINGS)
```

- [ ] **Step 4: 寫 __init__.py**

Create `config/__init__.py`:

```python
"""Centralized application settings (Phase 1)."""
from __future__ import annotations

from functools import lru_cache

from .base import Settings

__all__ = ["Settings", "settings", "get_settings", "reset_for_tests"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings singleton. Use this in lazy contexts."""
    return Settings()


def reset_for_tests() -> None:
    """Clear lru_cache so next get_settings() call re-reads env. Test use only."""
    get_settings.cache_clear()


settings: Settings = get_settings()
"""Module-level Settings alias for eager imports."""
```

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/test_config/test_base.py -v`
Expected: 5 passed

- [ ] **Step 6: 跑全套 config 測試確認沒有 cross-domain 衝突**

Run: `pytest tests/test_config/ -v`
Expected: ~30 passed (12 validators + 4 core + 2 parent_db + 3 network + 3 scheduler + 2 sentry + 2 line + 2 recruitment + 2 geocoding + 2 storage + 2 misc + 5 base)

- [ ] **Step 7: 暫不 commit**

---

### Task 14: tests/conftest.py 加 autoreset fixture

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: 看現況**

Run: `head -50 tests/conftest.py`
記下現有的 imports 與 fixture 風格。

- [ ] **Step 2: 加 autoreset fixture**

打開 `tests/conftest.py`，在檔案末尾加：

```python

@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每個 test 進場前 + 收尾後都清 Settings lru_cache。

    進場 reset 保證 test 從乾淨 cache 開始（避免上個 test 的 cache 殘留 + monkeypatch
    在進入 test 函式時設好 env，需要 reset 才能讓 settings 看到新值）。
    收尾 reset 避免污染後續 test。
    """
    from config import reset_for_tests
    reset_for_tests()
    yield
    reset_for_tests()
```

注意：如檔頂未 `import pytest` 則加上。

> **設計關鍵**：before+after 都 reset 不只是雙保險。monkeypatch fixture 是在 test 函式進入時生效；如果 `_reset_settings_cache` 只 yield-then-reset，則進 test 時 `get_settings()` 還用上一個 test 留下的 cache，即使該 test 有 `monkeypatch.setenv` 也讀不到。before 也 reset 才能讓 monkeypatch 設好的 env 被新 cache 讀到。

- [ ] **Step 3: 驗證沒破 spike_rls**

Run: `pytest tests/spike_rls/ -v --tb=short 2>&1 | tail -30`
Expected: 全綠（4319 baseline 中 spike_rls 15 個 test 不變）

- [ ] **Step 4: 跑全套 pytest baseline 確認沒破任何 test**

Run: `pytest --tb=no -q 2>&1 | tail -5`
Expected: 與 baseline `4319 passed` 對齊（容忍 pre-existing fail：test_audit_router 3 條、test_supabase_storage 6 errors）

- [ ] **Step 5: 暫不 commit**

---

### Task 15: .env.example 補齊 73 變數

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 改寫整個 .env.example**

完整覆寫 `.env.example`：

```bash
# ───────────────────────────────────────────────────────────────────────
# ivy-backend env 變數範本
# 對應 config/ 下 10 個 sub-Settings（一處對齊 = 部署一處設定）
# 帶 # 開頭為註解；要啟用某 env 取消註解並填值。
# 缺漏即用 Settings default（見 docs/superpowers/specs/2026-05-21-config-centralization-phase1-design.md）
# ───────────────────────────────────────────────────────────────────────

# ── Core (config/core.py) ─────────────────────────────────────────────
# 環境識別。`production` / `prod` 觸發 prod 模式（含 JWT 必填、docs 預設關）。
ENV=development

# 資料庫連線字串。Dev 預設 localhost；prod 必填。
DATABASE_URL=postgresql://localhost:5432/ivymanagement

# JWT 簽章金鑰。Prod 必填（缺則 main.py startup fail-loud），dev 缺則 warning。
# JWT_SECRET_KEY=your-32-char-random-string

# JWT absolute lifetime。預設 8 小時。
# JWT_ABSOLUTE_LIFETIME_HOURS=8

# API docs (/docs) 是否啟用。Prod 預設 False；dev 預設 True。
# ENABLE_API_DOCS=true

# 第一次啟動時 seed admin user。設定後跑 startup/seed.py 自動建立。
# ADMIN_INIT_USERNAME=admin
# ADMIN_INIT_PASSWORD=changeme-on-first-login

# ── ParentDB (config/parent_db.py) ────────────────────────────────────
# 家長端 RLS 隔離 DB role。Admin-only 部署可不填；parent router 被呼叫才檢查。
# PARENT_DB_USER=ivy_parent_login
# PARENT_DB_PASSWORD=your-parent-login-password
# 家長端 RLS guard / metrics flags。預設關。
# PARENT_RLS_GUARD_ENABLED=false
# PARENT_RLS_METRICS_DISABLED=false

# ── Network (config/network.py) ───────────────────────────────────────
# CORS 來源。逗號分隔；dev 預設允許 localhost:5173/3000。
# CORS_ORIGINS=http://localhost:5173,https://my-app.example.com
# Host header allow list（用於 TrustedHostMiddleware）。Prod 建議設。
# ALLOWED_HOSTS=my-app.example.com,api.example.com
# 反向代理可信來源 IP（為 X-Forwarded-For 處理）。預設 '*'。
# TRUSTED_PROXY_IPS=10.0.0.0/8
# CSP script-src hashes（避免 inline script CSP 阻擋）。逗號分隔。
# CSP_SCRIPT_HASHES=sha256-AAA...,sha256-BBB...
# 認證 cookie SameSite 屬性。lax / strict / none。
# COOKIE_SAMESITE=lax
# 校內 WiFi IP 段（用於打卡定位寬鬆判定）。逗號分隔 CIDR。
# SCHOOL_WIFI_IPS=192.168.1.0/24,10.0.0.0/8
# Rate limit 後端。預設 memory；prod 可改 redis（需 REDIS_URL，未實作）。
# RATE_LIMIT_BACKEND=memory

# ── Scheduler (config/scheduler.py) ───────────────────────────────────
# 才藝候補名單掃描排程。
# ACTIVITY_WAITLIST_SWEEPER_ENABLED=false
# ACTIVITY_WAITLIST_SCHEDULER_ENABLED=false
# ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS=600     # 預設 10 分鐘
# ACTIVITY_WAITLIST_CHECK_INTERVAL=300              # 預設 5 分鐘
# ACTIVITY_WAITLIST_REMINDER_OFFSET_HOURS=24
# ACTIVITY_WAITLIST_FINAL_REMINDER_OFFSET_HOURS=6
# ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS=48

# 服藥提醒排程（每日定時掃當天用藥）。
# MEDICATION_REMINDER_ENABLED=false
# MEDICATION_REMINDER_CHECK_INTERVAL=60             # 預設 60 秒輪詢
# MEDICATION_REMINDER_HOUR=8
# MEDICATION_REMINDER_MINUTE=0

# 自動畢業（學年結束自動 graduate 大班）。
# AUTO_GRADUATION_ENABLED=false
# AUTO_GRADUATION_CHECK_INTERVAL=86400              # 每日檢查
# AUTO_GRADUATION_MONTH=7                            # 預設 7 月
# AUTO_GRADUATION_DAY=31
# AUTO_GRADUATION_PREVIEW_DAYS=30

# 月薪自動快照（月底每月一次）。
# SALARY_AUTO_SNAPSHOT_ENABLED=false
# SALARY_SNAPSHOT_CHECK_INTERVAL=86400

# 教育部行事曆同步。
# OFFICIAL_CALENDAR_SYNC_ENABLED=false
# OFFICIAL_CALENDAR_SYNC_INTERVAL=86400

# 財務對帳排程 / 安全 GC（過期 session、refresh token GC）。
# FINANCE_RECONCILIATION_ENABLED=false
# SECURITY_GC_DISABLED=false                         # 預設「啟用 GC」

# ── Sentry (config/sentry.py) ─────────────────────────────────────────
# DSN 留空即整套 Sentry no-op。
# SENTRY_DSN=https://xxx@sentry.io/yyy
# SENTRY_ENVIRONMENT=production
# SENTRY_RELEASE=
# SENTRY_TRACES_SAMPLE_RATE=0.1

# ── LINE Login + LIFF (config/line.py) ────────────────────────────────
# 重要：LINE Login Channel 與既有 Messaging Bot 必須掛同一 Provider。
# Messaging Bot 的 channel_access_token / channel_secret 仍存 DB line_configs 表。
# LINE_LOGIN_CHANNEL_ID=your-line-login-channel-id
# LINE_LOGIN_CHANNEL_SECRET=your-line-login-channel-secret
# LIFF_ID=your-liff-app-id
# LINE_CHANNEL_ACCESS_TOKEN=                           # 後端讀來 echo（保留現況）
# VITE_LIFF_ID=your-liff-app-id                        # 後端讀來 echo 給前端 config endpoint

# ── Recruitment (config/recruitment.py) ───────────────────────────────
# 義華校官網後台同步（招生統計）。
# IVYKIDS_USERNAME=your-ivykids-backend-account
# IVYKIDS_PASSWORD=your-ivykids-backend-password
# IVYKIDS_LOGIN_URL=https://www.ivykids.tw/manage/
# IVYKIDS_DATA_URL=https://www.ivykids.tw/manage/make_an_appointment/
# IVYKIDS_SYNC_ENABLED=false
# IVYKIDS_SYNC_INTERVAL_MINUTES=10

# 招生生活圈分析用本園基準點。
# RECRUITMENT_CAMPUS_NAME=義華幼兒園
# RECRUITMENT_CAMPUS_ADDRESS=台北市XX區XX路123號
# RECRUITMENT_CAMPUS_LAT=25.0330
# RECRUITMENT_CAMPUS_LNG=121.5654
# RECRUITMENT_CAMPUS_TRAVEL_MODE=driving               # driving | walking | transit

# TGOS 政府地圖 API fallback（若無 Google API）。
# TGOS_APP_ID=your-tgos-app-id
# TGOS_API_KEY=your-tgos-api-key

# 市場情報 fetch timeout（招生附近幼兒園查詢）。
# RECRUITMENT_MARKET_TIMEOUT_SECONDS=8

# ── Geocoding (config/geocoding.py) ───────────────────────────────────
# Google Maps Platform 後端專用 key（Geocoding + Routes + Places）。
# 建議用「後端專用」key，與前端分開。
# GOOGLE_MAPS_API_KEY=your-backend-google-maps-api-key

# Provider 選擇：google / nominatim / tgos。
# GEOCODING_PROVIDER=nominatim
# GEOCODING_USER_AGENT=ivyManageSystem/1.0 (contact: your-email@example.com)
# GEOCODING_CONTACT_EMAIL=your-email@example.com
# GEOCODING_TIMEOUT_SECONDS=8

# ── Storage (config/storage.py) ───────────────────────────────────────
# 檔案儲存後端：local / supabase。
# STORAGE_BACKEND=local
# STORAGE_ROOT=./uploads
# SUPABASE_URL=https://xxx.supabase.co
# SUPABASE_SERVICE_ROLE_KEY=eyJ...
# SUPABASE_STORAGE_SIGNED_URL_TTL=3600
# 成長報告（學期 PDF）特殊路徑與大小上限。
# GROWTH_REPORT_ROOT=./growth_reports
# GROWTH_REPORT_MAX_BYTES=5242880                      # 5 MB

# ── Misc (config/misc.py) ─────────────────────────────────────────────
# Anthropic API key（evals/llm_attacker 評估用，optional）。
# ANTHROPIC_API_KEY=sk-ant-...
# POS 現金保管庫警告閾值（超過此金額顯示提醒匯款）。
# POS_CASH_DEPOSIT_WARNING_THRESHOLD=5000
# 是否啟用「請假抵加班」邏輯（暫時 feature flag）。
# ENABLE_LEAVE_OT_OFFSET=false
# 才藝家長查詢連結 token TTL。
# ACTIVITY_QUERY_TOKEN_TTL_DAYS=30
# MCP server（才藝 CRUD）登入帳密。
# IVY_MCP_USERNAME=mcp-bot
# IVY_MCP_PASSWORD=your-mcp-bot-password
```

- [ ] **Step 2: 確認語法正確（用 dotenv 解析驗證）**

Run: `python -c "from dotenv import dotenv_values; v = dotenv_values('.env.example'); print(f'parsed {len(v)} vars')"`
Expected: 約 73-75 個（含註解打開後的數量；只算未註解的會看到 `ENV` 與 `DATABASE_URL` 兩個）

實際只需驗證可解析、無 syntax error：

Run: `python -c "from dotenv import dotenv_values; dotenv_values('.env.example')"`
Expected: 無例外

- [ ] **Step 3: 暫不 commit**

---

### Task 16: Commit 1 整體驗證 + 第一個 commit

**Files:**
- 確認 Commit 1 範圍的所有新檔/改檔已就位

- [ ] **Step 1: 跑全套 pytest 確認零回歸**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: baseline 4319 + 新增 config 測試（30+）= 4349+ passed，pre-existing fail 數不變

- [ ] **Step 2: import smoke test**

Run: `python -c "from config import settings, get_settings, reset_for_tests; print(settings.model_dump_safe())"`
Expected: 整個 dict dump 出來、敏感欄位顯示 `***`、無例外

- [ ] **Step 3: 啟動 dev server 確認沒打破 startup**

Run: `python -c "from main import app; print('app imported ok')"`
Expected: `app imported ok`（注意此時 callsite 還沒切換，settings 還沒被任何 prod code 用到，但 startup 必須 import-clean）

- [ ] **Step 4: Commit**

```bash
git add config/ tests/test_config/ tests/conftest.py requirements.txt .env.example
git commit -m "$(cat <<'EOF'
feat(config): introduce centralized Settings (no callsites changed yet)

新增 config/ 模組，用 pydantic-settings 集中 73 個 env 變數的型別、預設值、validator：
- 10 個 sub-Settings 按 domain 拆檔（core/parent_db/network/scheduler/sentry/
  line/recruitment/geocoding/storage/misc）
- 主 Settings 用 default_factory 組合 + lru_cache singleton + reset_for_tests()
- model_dump_safe() 用 substring 對 secret/password/token/api_key/dsn 自動 redact
- BoolEnv / CsvList 解 .lower() in ("1","true","yes") 與 csv split 重複 pattern
- 各 sub-Settings 配 pytest TDD 測試 30+ 個 case
- tests/conftest.py 加 _reset_settings_cache autouse fixture
- .env.example 補齊 73 變數註解（原僅 13 個）

零 callsite 變更；callsite 切換見後續 commit。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: 確認 commit 成功**

Run: `git log -1 --stat`
Expected: 看到 config/ 12 新檔 + tests/test_config/ 12 新檔 + tests/conftest.py + requirements.txt + .env.example

---

## Commit 2 — Prod sites 切換（35 prod 檔）

每個 task 都遵循同一機械替換規則表：

| 現況 | 改後 |
|------|------|
| `os.getenv("ENV", "development").lower() in ("production", "prod")` | `settings.core.is_production` |
| `os.environ.get("ENV", "")` | `settings.core.env` |
| `int(os.getenv("X", "600"))` | `settings.<domain>.x` |
| `os.getenv("X", "").lower() in ("1", "true", "yes")` | `settings.<domain>.x` |
| `os.environ.get("CORS_ORIGINS", "").split(",")` | `settings.network.cors_origins` |
| `os.environ.get("PARENT_DB_USER")` + RuntimeError if missing | `settings.parent_db.user` + 由 `models/parent_db.py` 既有 fail-loud helper 檢查 |

所有 callsite 改 `from config import settings`（top of file），不要在函式內 lazy import。

### Task 17: utils/ 9 檔切換

**Files:**
- Modify: `utils/auth.py`, `utils/cookie.py`, `utils/errors.py`, `utils/rate_limit.py`, `utils/request_ip.py`, `utils/security_headers.py`, `utils/sentry_init.py`, `utils/storage.py`, `utils/supabase_storage.py`

- [ ] **Step 1: 逐檔機械替換**

對每個檔案依序：

1. 在頂部 imports 區加 `from config import settings`
2. `grep -n 'os\.getenv\|os\.environ' utils/<file>.py` 找出每處 callsite
3. 對照「機械替換規則表」改寫
4. 移除 `import os` 若不再使用（用 `grep -c 'os\.' utils/<file>.py` 驗證）

示範（`utils/cookie.py`）：

改前：
```python
import os

def get_samesite() -> str:
    return os.environ.get("COOKIE_SAMESITE", "lax").lower()
```

改後：
```python
from config import settings

def get_samesite() -> str:
    return settings.network.cookie_samesite
```

- [ ] **Step 2: 跑 utils 相關 unit test**

Run: `pytest tests/test_cookie_samesite.py tests/test_csp_headers.py -v --tb=short`
Expected: 全綠（注意 conftest autoreset fixture 會自動清 cache）

- [ ] **Step 3: 跑全套 pytest 確認沒破其他 test**

Run: `pytest --tb=no -q 2>&1 | tail -5`
Expected: 與 baseline 對齊

- [ ] **Step 4: grep 驗證 utils/ 沒殘留 os.getenv**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'utils/*.py'`
Expected: 空

- [ ] **Step 5: 暫不 commit**

---

### Task 18: models/ 2 檔切換

**Files:**
- Modify: `models/base.py`, `models/parent_db.py`

- [ ] **Step 1: models/base.py 機械替換**

加 `from config import settings`，把 `os.environ.get("DATABASE_URL")` 改 `settings.core.database_url`。注意 `models/base.py` 是 SQLAlchemy engine creation，需在 import time 取值 — `settings` module-level 已 cache 第一份，OK。

- [ ] **Step 2: models/parent_db.py 機械替換（保留 fail-loud）**

`models/parent_db.py` 既有結構：
- URL-explicit factories（給 test 用）— 接受顯式參數，不讀 env，**保留不動**
- env-driven singleton + fail-loud — 內部讀 env，**只改 env 讀法**

改前：
```python
def get_parent_engine() -> Engine:
    user = os.environ.get("PARENT_DB_USER")
    password = os.environ.get("PARENT_DB_PASSWORD")
    if not user or not password:
        raise RuntimeError("PARENT_DB_USER/PASSWORD must be set ...")
    # ...
```

改後：
```python
from config import settings

def get_parent_engine() -> Engine:
    user = settings.parent_db.user
    password = settings.parent_db.password
    if not user or not password:
        raise RuntimeError("PARENT_DB_USER/PASSWORD must be set ...")
    # ...
```

**注意**：`reset_for_tests()` helper 內部若有 `os.environ.pop(...)` 邏輯不要動（test fixture 仍需操作 env，Settings 跟著重讀）。

- [ ] **Step 3: 跑 spike_rls 15 test 確認 fail-loud 邏輯不變**

Run: `pytest tests/spike_rls/ -v --tb=short`
Expected: 15 passed（fail-loud RuntimeError 在 env 缺時仍 raise）

- [ ] **Step 4: grep 驗證 models/ 沒殘留 os.getenv**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'models/*.py'`
Expected: 空

- [ ] **Step 5: 暫不 commit**

---

### Task 19: services/ 13 檔切換

**Files:**
- Modify: `services/activity_query_token.py`, `services/activity_service.py`, `services/activity_waitlist_scheduler.py`, `services/approval/cross_type_offset.py`, `services/finance_reconciliation_scheduler.py`, `services/geocoding_service.py`, `services/graduation_scheduler.py`, `services/medication_reminder_scheduler.py`, `services/official_calendar_scheduler.py`, `services/recruitment_ivykids_sync.py`, `services/recruitment_market_intelligence.py`, `services/salary_snapshot_scheduler.py`, `services/security_gc_scheduler.py`

- [ ] **Step 1: 機械替換**

對每個檔案依規則表改寫。示範（`services/medication_reminder_scheduler.py`）：

改前：
```python
import os

ENABLED = os.getenv("MEDICATION_REMINDER_ENABLED", "").lower() in ("1", "true", "yes")
INTERVAL = int(os.getenv("MEDICATION_REMINDER_CHECK_INTERVAL", "60"))
HOUR = int(os.getenv("MEDICATION_REMINDER_HOUR", "8"))
```

改後：
```python
from config import settings

def _is_enabled() -> bool:
    return settings.scheduler.medication_reminder_enabled

def _interval() -> int:
    return settings.scheduler.medication_reminder_check_interval

def _hour() -> int:
    return settings.scheduler.medication_reminder_hour
```

> **注意**：原本 module-level constant 改成 function 是為了讓 test 改 env 後即時生效（autoreset fixture 會清 settings cache，下次 `settings.scheduler.medication_reminder_enabled` 重讀）。若原本是 module top-level constant 被 scheduler loop 用，**直接** `settings.scheduler.X` inline 也可。

- [ ] **Step 2: 跑 services 相關 test**

Run: `pytest tests/ -k "scheduler or geocoding or recruitment or activity or graduation or medication" --tb=short -q`
Expected: 全綠

- [ ] **Step 3: grep 驗證 services/ 沒殘留 os.getenv**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'services/**/*.py'`
Expected: 空

- [ ] **Step 4: 暫不 commit**

---

### Task 20: api/ 5 檔切換

**Files:**
- Modify: `api/auth.py`, `api/leaves.py`, `api/activity/pos.py`, `api/portal/leaves.py`, `api/portfolio/reports.py`

- [ ] **Step 1: 機械替換**

對每檔依規則表改寫。示範（`api/portfolio/reports.py`）：

改前：
```python
import os
GROWTH_REPORT_ROOT = os.environ.get("GROWTH_REPORT_ROOT", "./growth_reports")
GROWTH_REPORT_MAX_BYTES = int(os.environ.get("GROWTH_REPORT_MAX_BYTES", "5242880"))
```

改後：
```python
from config import settings
# GROWTH_REPORT_ROOT 改 settings.storage.growth_report_root 直接使用
# GROWTH_REPORT_MAX_BYTES 改 settings.storage.growth_report_max_bytes
```

- [ ] **Step 2: 跑 api 相關 test**

Run: `pytest tests/ -k "auth or leaves or portfolio or activity_pos" --tb=short -q`
Expected: 全綠

- [ ] **Step 3: grep 驗證 api/ 沒殘留 os.getenv**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'api/**/*.py'`
Expected: 空

- [ ] **Step 4: 暫不 commit**

---

### Task 21: 邊角檔切換

**Files:**
- Modify: `startup/seed.py`, `scripts/setup_line_richmenu.py`, `mcp_server/activity_crud/client.py`, `evals/core/llm_attacker.py`

- [ ] **Step 1: 機械替換**

對每檔依規則表改寫。`scripts/dump_openapi.py` **不改**（CLI 工具獨立生命週期、需動態切 ENV）。

`evals/core/llm_attacker.py` 中 `os.environ.get("ANTHROPIC_API_KEY")` 改 `settings.misc.anthropic_api_key`。

- [ ] **Step 2: 跑相關 test**

Run: `pytest tests/evals/ tests/test_misc_medium_authz.py --tb=short -q`
Expected: 全綠

- [ ] **Step 3: grep 驗證**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'startup/**/*.py' 'scripts/setup_line_richmenu.py' 'mcp_server/**/*.py' 'evals/**/*.py'`
Expected: 空（`scripts/dump_openapi.py` 不在範圍內，仍保留 os.getenv）

- [ ] **Step 4: 暫不 commit**

---

### Task 22: main.py 切換（最大宗 13 處）

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 加 import**

在 `main.py` 頂部 imports 區加：

```python
from config import settings
```

- [ ] **Step 2: 機械替換 13 處**

逐處改寫。**過渡策略**：

- main.py 的呼叫端（13 處 `os.getenv` callsite）**直接改成** `settings.core.is_production` 等（不再呼叫 helper 函式）
- 但 helper 函式 `_is_production()` / `_dev_router_enabled()` 本身**保留**，內部改成 lazy `get_settings()` 包裝（給既有 test 對 helper 的 mock 路徑用）
- Task 28 才把 helper 函式 + `_DEV_ROUTER_ENV_ALLOWLIST` 常數整個砍掉

13 處 callsite 對照表：

| 行號 | 改前 | 改後 |
|------|------|------|
| ~125 | `if os.environ.get("ENV", "development").lower() in ("production", "prod"):` | `if settings.core.is_production:` |
| ~161 | helper `_is_production`: `return os.environ.get("ENV", "development").lower() in (...)` | `return settings.core.is_production` |
| ~179 | helper `_dev_router_enabled`: `return os.environ.get("ENV", "").lower() in _DEV_ROUTER_ENV_ALLOWLIST` | `return settings.core.dev_router_enabled` |
| ~193 | `channel_id=os.environ.get("LINE_LOGIN_CHANNEL_ID", "")` | `channel_id=settings.line.login_channel_id or ""` |
| ~212 | `env_label = os.environ.get("ENV", "development").lower()` | `env_label = settings.core.env` |
| ~226 | `interval = int(os.getenv("ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS", "600"))` | `interval = settings.scheduler.activity_waitlist_sweep_interval_seconds` |
| ~266 | `if os.getenv("ACTIVITY_WAITLIST_SWEEPER_ENABLED", "").lower() in (...)` | `if settings.scheduler.activity_waitlist_sweeper_enabled:` |
| ~338 | `if os.getenv("MEDICATION_REMINDER_ENABLED", "").lower() in (...)` | `if settings.scheduler.medication_reminder_enabled:` |
| ~538 | `_env_name = os.environ.get("ENV", "development").lower()` | `_env_name = settings.core.env` |
| ~540 | `_cors_env = os.environ.get("CORS_ORIGINS", "")` | `_cors_env = ",".join(settings.network.cors_origins)` 或直接用 list |
| ~541 | `_docs_force_enable = os.environ.get("ENABLE_API_DOCS", "").lower() in (...)` | `_docs_force_enable = settings.core.enable_api_docs` |
| ~720 | `_allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "")` | 改用 `settings.network.allowed_hosts`（已是 list） |
| ~749 | `forwarded_allow = os.getenv("TRUSTED_PROXY_IPS", "*")` | `forwarded_allow = settings.network.trusted_proxy_ips` |

> 注意：`_cors_env` 與 `_allowed_hosts_env` 的下游 code 若期望 str，要改成讀 `settings.network.cors_origins` (list)；若期望 split 後的 list，直接用即可。逐處檢查 downstream 使用。

- [ ] **Step 3: helper 函式改成 lazy 包裝**

把 main.py 的 helper 改成：

```python
def _is_production() -> bool:
    from config import get_settings
    return get_settings().core.is_production


def _dev_router_enabled() -> bool:
    from config import get_settings
    return get_settings().core.dev_router_enabled
```

> 為何 lazy `get_settings()` 而非 module-level `settings`：helper 在 main.py import 時就會被多處呼叫，但 import 是 module-level；用 `get_settings()` 不會在 import 時 cache 一個過時的 instance，配合 Task 14 conftest 進場 reset，每個 test 都看到最新 env。

- [ ] **Step 4: 跑 main 相關 test**

Run: `pytest tests/test_misc_medium_authz.py tests/test_csp_headers.py tests/test_safe_500.py --tb=short -q`
Expected: **全綠**（Task 14 已 before+after reset，test 內 `patch.dict(os.environ, {"ENV": ...})` 在進入 test 函式時，autoreset 已先把 cache 清掉，第一次呼叫 `_is_production()` 會用 patched env 重 cache）

- [ ] **Step 5: 跑全套 pytest 確認 baseline**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: 與 baseline 對齊（容忍 pre-existing fail）

- [ ] **Step 6: grep 驗證 main.py 沒殘留**

Run: `git grep -nE 'os\.(getenv|environ)' -- 'main.py'`
Expected: 空。`_DEV_ROUTER_ENV_ALLOWLIST` 常數本身可以留（不含 os.getenv），Task 28 才把整個 helper 與常數一起砍。

- [ ] **Step 7: 暫不 commit**

---

### Task 23: Commit 2

- [ ] **Step 1: 跑全套 pytest 最後驗證**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: baseline 對齊

- [ ] **Step 2: grep 驗證 prod sites 沒殘留 os.getenv**

Run: `git grep -nE 'os\.(getenv|environ)' -- '*.py' ':!config/' ':!tests/' ':!scripts/dump_openapi.py' ':!alembic/' ':!*.scratch/'`
Expected: 空

- [ ] **Step 3: Commit**

```bash
git add api/ models/ services/ utils/ main.py startup/ scripts/setup_line_richmenu.py mcp_server/ evals/
git commit -m "$(cat <<'EOF'
refactor(config): migrate all 35 prod callsites to centralized Settings

把所有 prod code 的 os.getenv / os.environ.get 改讀 settings.<domain>.<field>：
- utils/ 9 檔（auth/cookie/errors/rate_limit/request_ip/security_headers/
  sentry_init/storage/supabase_storage）
- models/ 2 檔（base.py + parent_db.py 保留 fail-loud 慣例）
- services/ 13 檔（含 6 個 scheduler 系 + 3 個 activity + 2 個 recruitment +
  geocoding/approval/finance/security/graduation/medication/salary/calendar）
- api/ 5 檔（auth/leaves/activity_pos/portal_leaves/portfolio_reports）
- main.py 13 處（startup + middleware + dev router + scheduler bootstrap）
- 邊角：startup/seed.py、scripts/setup_line_richmenu.py、
  mcp_server/activity_crud/client.py、evals/core/llm_attacker.py

保留：scripts/dump_openapi.py（CLI 獨立生命週期）、alembic/env.py（CLI）、
tests/spike_rls/conftest.py（test fixture 自讀 env）。

Test 端切換在後續 commit。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Commit 3 — Test sites 切換（14 檔）

### Task 24: tests/ root 4 檔切換

**Files:**
- Modify: `tests/test_cookie_samesite.py`, `tests/test_csp_headers.py`, `tests/test_safe_500.py`, `tests/test_misc_medium_authz.py`

- [ ] **Step 1: 機械替換**

對每個 test 檔：

- `os.environ.setenv("X", v)` → `monkeypatch.setenv("X", v)`
- `os.environ.pop("X", None)` → `monkeypatch.delenv("X", raising=False)`
- 移除 `importlib.reload(main)` 配 `from config import reset_for_tests; reset_for_tests()`
- 斷言改讀 `settings.<domain>.<field>` 或保留呼叫 main.py helper（內部已切到 settings）

示範（`tests/test_cookie_samesite.py`）：

改前：
```python
def test_lax():
    os.environ.pop("COOKIE_SAMESITE", None)
    # ...

def test_strict(env_value):
    os.environ["COOKIE_SAMESITE"] = env_value
    # ...
```

改後：
```python
def test_lax(monkeypatch):
    monkeypatch.delenv("COOKIE_SAMESITE", raising=False)
    # conftest autoreset fixture 已在 test 開始前 reset_for_tests
    # ...

def test_strict(env_value, monkeypatch):
    monkeypatch.setenv("COOKIE_SAMESITE", env_value)
    from config import reset_for_tests
    reset_for_tests()
    # ...
```

- [ ] **Step 2: 跑這 4 個 test 確認全綠**

Run: `pytest tests/test_cookie_samesite.py tests/test_csp_headers.py tests/test_safe_500.py tests/test_misc_medium_authz.py -v --tb=short`
Expected: 全綠

- [ ] **Step 3: 暫不 commit**

---

### Task 25: tests/evals/test_framework.py 切換

**Files:**
- Modify: `tests/evals/test_framework.py`

- [ ] **Step 1: 機械替換**

`os.environ.pop("ANTHROPIC_API_KEY", None)` 改為 `monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)` + `reset_for_tests()`。

- [ ] **Step 2: 跑 evals test**

Run: `pytest tests/evals/ -v --tb=short`
Expected: 全綠

- [ ] **Step 3: 暫不 commit**

---

### Task 26: tests/spike_rls/ 7 個 test 檔切換

**Files:**
- Modify: `tests/spike_rls/test_rls_guard.py`, `tests/spike_rls/test_rls_phase1_pilot.py`, `tests/spike_rls/test_rls_phase1b_leaves.py`, `tests/spike_rls/test_rls_phase1c_reads.py`, `tests/spike_rls/test_rls_phase1d_milestones.py`, `tests/spike_rls/test_rls_phase1e.py`, `tests/spike_rls/test_rls_phase1f.py`, `tests/spike_rls/test_rls_phase1g.py`

- [ ] **Step 1: 機械替換 module-scope fixture 中的 env 操作**

每檔 module-scope fixture 中：

```python
# 改前
os.environ.setdefault("DATABASE_URL", _ADMIN_URL)
os.environ["PARENT_DB_USER"] = "ivy_parent_login"
os.environ["PARENT_DB_PASSWORD"] = _PARENT_LOGIN_PW
```

改成用 monkeypatch fixture 注入。**注意**：spike_rls fixture 是 module-scope，不能直接收 function-scope `monkeypatch`。需用：

```python
@pytest.fixture(scope="module")
def _parent_env(monkeypatch_module):  # 需 pytest-monkeypatch 的 monkeypatch_module 或自建
    monkeypatch_module.setenv("DATABASE_URL", _ADMIN_URL)
    monkeypatch_module.setenv("PARENT_DB_USER", "ivy_parent_login")
    monkeypatch_module.setenv("PARENT_DB_PASSWORD", _PARENT_LOGIN_PW)
    from config import reset_for_tests
    reset_for_tests()
    yield
    reset_for_tests()
```

如果沒 `monkeypatch_module` fixture，**保留現況** `os.environ[...]` 操作（spike_rls/conftest.py 已 allow-list），但結尾 `pop` 後加 `reset_for_tests()`：

```python
os.environ.pop("PARENT_DB_USER", None)
os.environ.pop("PARENT_DB_PASSWORD", None)
from config import reset_for_tests
reset_for_tests()
```

- [ ] **Step 2: 跑全 spike_rls 確認 15 個 test 全綠**

Run: `pytest tests/spike_rls/ -v --tb=short`
Expected: 15 passed

- [ ] **Step 3: 暫不 commit**

---

### Task 27: Commit 3

- [ ] **Step 1: 跑全套 pytest baseline**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: baseline 對齊

- [ ] **Step 2: grep 驗證所有 test 用 monkeypatch 而非 os.environ[...]=**

Run: `git grep -nE 'os\.environ\[' -- 'tests/*.py' ':!tests/spike_rls/'`
Expected: 空

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit -m "$(cat <<'EOF'
test(config): migrate test sites to monkeypatch.setenv + reset_for_tests

把 14 個 test 檔的 env 操作從 patch.dict(os.environ) / os.environ.setdefault
改為 monkeypatch.setenv / monkeypatch.delenv + reset_for_tests()：

- tests/test_cookie_samesite.py、test_csp_headers.py、test_safe_500.py、
  test_misc_medium_authz.py（4 root tests）
- tests/evals/test_framework.py（1 evals）
- tests/spike_rls/test_rls_guard.py + 7 phase tests（module-scope fixture
  保留 os.environ 操作但加 reset_for_tests on teardown）

移除 importlib.reload(main) 工作模式（不再需要，Settings lazy 讀 env）。
spike_rls/conftest.py 自讀 env 計算 ADMIN_URL 保留不動（test fixture 慣例）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Commit 4 — main.py helper 收尾

### Task 28: 砍 main.py 散落 helper + Commit 4

**Files:**
- Modify: `main.py`

- [ ] **Step 1: 砍 `_is_production()` helper**

把所有 `_is_production()` 呼叫處改 `settings.core.is_production`，刪除 helper 函式定義（~main.py:161）。

- [ ] **Step 2: 砍 `_dev_router_enabled()` helper**

把所有呼叫處改 `settings.core.dev_router_enabled`，刪除 helper 函式定義（~main.py:179）+ `_DEV_ROUTER_ENV_ALLOWLIST` 常數（已搬入 `config/core.py`）。

- [ ] **Step 3: 跑相關 test 確認沒破**

Run: `pytest tests/test_misc_medium_authz.py tests/test_csp_headers.py tests/test_safe_500.py --tb=short -q`
Expected: 全綠（test 已在 Commit 3 改用 monkeypatch + reset_for_tests）

- [ ] **Step 4: grep 驗證**

Run: `git grep -n '_is_production\|_dev_router_enabled\|_DEV_ROUTER_ENV_ALLOWLIST' -- 'main.py'`
Expected: 空（helper 完全移除）

- [ ] **Step 5: 跑全套 pytest 最後驗證**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: baseline 對齊

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
refactor(config): collapse main.py env helpers into Settings property

刪除 main.py 散落 env 邏輯：
- _is_production() → settings.core.is_production
- _dev_router_enabled() → settings.core.dev_router_enabled
- _DEV_ROUTER_ENV_ALLOWLIST 常數 → CoreSettings._DEV_ROUTER_ENVS

零行為變化；property 集中在 config/core.py、單一來源。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Commit 5 — CI 防回退 gate

### Task 29: 加 grep gate + Commit 5

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: 看現況**

Run: `cat .github/workflows/ci.yml | head -80`
找到 pytest job 的 steps 區塊，準備在其前後加新 step。

- [ ] **Step 2: 加 grep gate step**

在 `pytest` 那個 step 之前加：

```yaml
      - name: Block os.getenv outside config/
        run: |
          if git grep -nE 'os\.(getenv|environ)' -- '*.py' \
             ':!config/' \
             ':!tests/conftest.py' \
             ':!tests/spike_rls/conftest.py' \
             ':!tests/spike_rls/test_rls_*.py' \
             ':!scripts/dump_openapi.py' \
             ':!alembic/env.py'; then
            echo "::error::os.getenv / os.environ.get 只能在 config/ 與明確 allow-list 內使用"
            echo "::error::請改讀 settings.<domain>.<field> （見 docs/superpowers/specs/2026-05-21-config-centralization-phase1-design.md）"
            exit 1
          fi
          echo "✅ os.getenv allow-list 檢查通過"
```

- [ ] **Step 3: local 驗證 gate 邏輯**

Run:
```bash
git grep -nE 'os\.(getenv|environ)' -- '*.py' \
  ':!config/' \
  ':!tests/conftest.py' \
  ':!tests/spike_rls/conftest.py' \
  ':!tests/spike_rls/test_rls_*.py' \
  ':!scripts/dump_openapi.py' \
  ':!alembic/env.py'
```
Expected: 空（exit 0 但 grep 找不到時 exit 1，所以 wrapper 用 `if ... ;then exit 1; fi`）

- [ ] **Step 4: 模擬回退測試（暫時加一行 os.getenv 驗證 gate 會炸）**

Run: `echo 'import os; X=os.getenv("X")' > /tmp/_test.py && cp /tmp/_test.py utils/_test_regression.py`

Run 完整 gate 邏輯：
```bash
if git grep -nE 'os\.(getenv|environ)' -- '*.py' ':!config/' ':!tests/conftest.py' ':!tests/spike_rls/conftest.py' ':!tests/spike_rls/test_rls_*.py' ':!scripts/dump_openapi.py' ':!alembic/env.py'; then echo "GATE FIRED"; fi
```
Expected: `GATE FIRED` + 印出 utils/_test_regression.py 那行

Run: `rm utils/_test_regression.py`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
chore(ci): block re-introduction of os.getenv outside config/

防止未來 PR 在 config/ 之外直接讀 env。Allow-list：
- config/ — Settings 內部
- tests/conftest.py、tests/spike_rls/conftest.py — test fixture 自讀 env
- tests/spike_rls/test_rls_*.py — module-scope env 設定
- scripts/dump_openapi.py — CLI 工具獨立生命週期
- alembic/env.py — Alembic CLI 早於 FastAPI startup

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 最終驗收

### Task 30: 完整驗收 checklist

- [ ] **Step 1: 整體 grep 驗證**

Run:
```bash
git grep -nE 'os\.(getenv|environ)' -- '*.py' \
  ':!config/' \
  ':!tests/conftest.py' \
  ':!tests/spike_rls/conftest.py' \
  ':!tests/spike_rls/test_rls_*.py' \
  ':!scripts/dump_openapi.py' \
  ':!alembic/env.py'
```
Expected: 空

- [ ] **Step 2: 全套 pytest 跑完**

Run: `pytest --tb=no -q 2>&1 | tail -10`
Expected: 4319 + 30+ config tests passed，pre-existing fail 數不變

- [ ] **Step 3: import smoke**

Run: `python -c "from main import app; from config import settings; print('startup ok', settings.core.env)"`
Expected: `startup ok development`

- [ ] **Step 4: dev server 起得來**

Run: `cd ~/Desktop/ivyManageSystem && ./start.sh` 並 `curl localhost:8088/`
Expected: server 起來、200 OK

Stop server (Ctrl+C)。

- [ ] **Step 5: model_dump_safe redact 驗證**

Run: `python -c "from config import settings; import json; print(json.dumps(settings.model_dump_safe(), indent=2, default=str))"`
Expected: 整個 dict dump、JWT_SECRET_KEY / 各 password / API key / DSN 顯示 `***`

- [ ] **Step 6: git log 確認 5 commit 結構乾淨**

Run: `git log --oneline -10`
Expected: 5 個 commit 清楚對應 Commit 1-5

- [ ] **Step 7: PR push**

Run: `git push -u origin feat/config-centralization-phase1-2026-05-21-backend`

---

## Phase 2 預告（不在本 plan）

Feature flag DB 化（admin-only toggle）— 獨立 spec/plan：
- 表 + admin API + cache layer（Redis or in-memory TTL）
- audit log（誰、何時、改了什麼）
- per-scope（global / per-tenant / per-user）
- 與本 Phase 1 Settings 共存（runtime flag override env default）
