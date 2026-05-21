# Config 集中化 Phase 1 設計

日期：2026-05-21
範圍：ivy-backend
狀態：spec（待 user 審）

---

## 1. 動機

env 變數讀取散落 ~168 處／**49 檔（35 prod 檔 + 14 test 檔）／73 個獨特變數**，現況有幾個明確痛點：

1. 無集中 schema：`os.getenv("X")` 各檔自管預設值、自做型別轉換。
2. 大量重複 pattern：`.lower() in ("1","true","yes")`（~25 處）、`int(os.getenv(...))`（~15 處）、`,` 分隔列表（5 處）。
3. `.env.example` 只列 13 變數、剩 60 個沒有範例；新人 onboard 須翻 code 自查。
4. 型別缺失：bool 欄位實際是字串、int 欄位每次 cast、列表欄位每次 split — IDE 無法輔助。
5. test 端 `patch.dict(os.environ, ...)` + `importlib.reload(main)` 模式脆弱、執行慢。

Phase 1 目標：以 `pydantic-settings` 集中、加型別、`.env.example` 補齊、零行為變化。
**不含** feature flag DB 化（Phase 2 獨立 spec）、不含前端 `VITE_*`、不含 env 變數 rename。

---

## 2. 架構總覽

```
ivy-backend/
├── config/                          ← 新增
│   ├── __init__.py                  ← settings, get_settings, reset_for_tests
│   ├── base.py                      ← Settings 主 class（組合 10 sub-Settings）
│   ├── core.py                      ← CoreSettings
│   ├── parent_db.py                 ← ParentDBSettings
│   ├── network.py                   ← NetworkSettings
│   ├── scheduler.py                 ← SchedulerSettings
│   ├── sentry.py                    ← SentrySettings
│   ├── line.py                      ← LineSettings
│   ├── recruitment.py               ← RecruitmentSettings
│   ├── geocoding.py                 ← GeocodingSettings
│   ├── storage.py                   ← StorageSettings
│   ├── misc.py                      ← MiscSettings
│   └── validators.py                ← parse_bool_env, parse_csv_list, ...
├── requirements.txt                 ← + pydantic-settings>=2.0
├── .env.example                     ← 補齊 73 變數註解
└── tests/conftest.py                ← + settings autoreset fixture
```

### 2.1 主 API（`config/__init__.py`）

```python
from functools import lru_cache
from .base import Settings

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

settings = get_settings()              # module-level alias（eager；給直接 import 用）

def reset_for_tests() -> None:
    """ 清 lru_cache，下次 get_settings() 重讀 env。test 用。"""
    get_settings.cache_clear()
```

兩種寫法皆可：

```python
# (a) 直接 import — 簡潔，但 import time validate
from config import settings
settings.database.url

# (b) lazy — 避免 module-import-time validate
from config import get_settings
def some_handler():
    return get_settings().sentry.dsn
```

---

## 3. Domain 切分（73 變數 → 10 sub-Settings）

env 變數**名稱保留不變**（部署環境/CI 已有舊名）。Pydantic field 用 snake_case，靠 `env_prefix` 或 `validation_alias` 對應。

### 3.1 CoreSettings (7)

| Pydantic field | env 變數 | 型別 | Default | 說明 |
|---|---|---|---|---|
| `env` | `ENV` | `Literal['development','production']` | `'development'` | |
| `database_url` | `DATABASE_URL` | `str` | `postgresql://localhost:5432/ivymanagement` | prod 必填 |
| `jwt_secret_key` | `JWT_SECRET_KEY` | `str` | dev: 隨機; prod: 必填 | **startup fail-loud**：prod 時 main.py 啟動就檢查 |
| `jwt_absolute_lifetime_hours` | `JWT_ABSOLUTE_LIFETIME_HOURS` | `int` | `8` | |
| `enable_api_docs` | `ENABLE_API_DOCS` | `BoolEnv` | `False` | prod 預設 False、需顯式開 |
| `admin_init_username` | `ADMIN_INIT_USERNAME` | `str \| None` | `None` | startup seed 用 |
| `admin_init_password` | `ADMIN_INIT_PASSWORD` | `str \| None` | `None` | startup seed 用 |

Property：
- `is_production` ← 取代 `main.py:_is_production()`、4 處 inline check
- `dev_router_enabled` ← 取代 `main.py:_dev_router_enabled()`、env in `_DEV_ROUTER_ENV_ALLOWLIST`

### 3.2 ParentDBSettings (4)

| field | env | 型別 | Default |
|---|---|---|---|
| `user` | `PARENT_DB_USER` | `str \| None` | `None`（**lazy dep fail-loud**：parent router 被呼叫才檢查） |
| `password` | `PARENT_DB_PASSWORD` | `str \| None` | `None`（**lazy dep fail-loud**） |
| `rls_guard_enabled` | `PARENT_RLS_GUARD_ENABLED` | `BoolEnv` | `False` |
| `rls_metrics_disabled` | `PARENT_RLS_METRICS_DISABLED` | `BoolEnv` | `False` |

`models/parent_db.py` 既有 fail-loud + `reset_for_tests` 慣例**保留**，只將內部 `os.environ.get(...)` 替換為 `settings.parent_db.user` 等。

**兩種 prod-required 模式釐清**：
- **Startup fail-loud**（JWT_SECRET_KEY）：main.py 開機時用 `if settings.core.is_production and not settings.core.jwt_secret_key: raise` 一次性檢查。
- **Lazy dep fail-loud**（PARENT_DB_USER/PASSWORD）：admin-only 部署不需要 parent_db，這兩個值只在 `get_parent_session_dep()` 被注入時才 fail。對應 `models/parent_db.py` 現有慣例。

### 3.3 NetworkSettings (7)

| field | env | 型別 | Default |
|---|---|---|---|
| `cors_origins` | `CORS_ORIGINS` | `CsvList` | `[]`（dev 時 main.py 額外加 localhost） |
| `allowed_hosts` | `ALLOWED_HOSTS` | `CsvList` | `[]` |
| `trusted_proxy_ips` | `TRUSTED_PROXY_IPS` | `str` | `'*'` |
| `csp_script_hashes` | `CSP_SCRIPT_HASHES` | `CsvList` | `[]` |
| `cookie_samesite` | `COOKIE_SAMESITE` | `Literal['lax','strict','none']` | `'lax'` |
| `school_wifi_ips` | `SCHOOL_WIFI_IPS` | `CsvList` | `[]` |
| `rate_limit_backend` | `RATE_LIMIT_BACKEND` | `str` | `'memory'` |

### 3.4 SchedulerSettings (22)

依現有 scheduler 變數整理（`*_ENABLED` → BoolEnv、`*_INTERVAL*` → int seconds）：

```python
class SchedulerSettings(BaseSettings):
    activity_waitlist_sweeper_enabled: BoolEnv = False
    activity_waitlist_scheduler_enabled: BoolEnv = False
    activity_waitlist_sweep_interval_seconds: int = 600
    activity_waitlist_check_interval: int = 300
    activity_waitlist_reminder_offset_hours: int = 24
    activity_waitlist_final_reminder_offset_hours: int = 6
    activity_waitlist_confirm_window_hours: int = 48

    medication_reminder_enabled: BoolEnv = False
    medication_reminder_check_interval: int = 60
    medication_reminder_hour: int = 8
    medication_reminder_minute: int = 0

    auto_graduation_enabled: BoolEnv = False
    auto_graduation_check_interval: int = 86400
    auto_graduation_month: int = 7
    auto_graduation_day: int = 31
    auto_graduation_preview_days: int = 30

    salary_auto_snapshot_enabled: BoolEnv = False
    salary_snapshot_check_interval: int = 86400

    official_calendar_sync_enabled: BoolEnv = False
    official_calendar_sync_interval: int = 86400

    finance_reconciliation_enabled: BoolEnv = False
    security_gc_disabled: BoolEnv = False
```

### 3.5 SentrySettings (4)

| field | env | 型別 | Default |
|---|---|---|---|
| `dsn` | `SENTRY_DSN` | `str \| None` | `None` |
| `environment` | `SENTRY_ENVIRONMENT` | `str` | `'production'` |
| `release` | `SENTRY_RELEASE` | `str \| None` | `None` |
| `traces_sample_rate` | `SENTRY_TRACES_SAMPLE_RATE` | `float` | `0.1` |

Property：`enabled` = `bool(dsn)`，取代各處 `if SENTRY_DSN:` 檢查。

### 3.6 LineSettings (5)

| field | env | 型別 | Default |
|---|---|---|---|
| `login_channel_id` | `LINE_LOGIN_CHANNEL_ID` | `str \| None` | `None` |
| `login_channel_secret` | `LINE_LOGIN_CHANNEL_SECRET` | `str \| None` | `None` |
| `liff_id` | `LIFF_ID` | `str \| None` | `None` |
| `channel_access_token` | `LINE_CHANNEL_ACCESS_TOKEN` | `str \| None` | `None` |
| `vite_liff_id` | `VITE_LIFF_ID` | `str \| None` | `None` |（後端讀來 echo 給前端） |

Bot 的 access_token / channel_secret 仍走 DB `line_configs`（保留現況）。

### 3.7 RecruitmentSettings (14)

| field | env | Default |
|---|---|---|
| `ivykids_username` | `IVYKIDS_USERNAME` | `None` |
| `ivykids_password` | `IVYKIDS_PASSWORD` | `None` |
| `ivykids_login_url` | `IVYKIDS_LOGIN_URL` | `'https://www.ivykids.tw/manage/'` |
| `ivykids_data_url` | `IVYKIDS_DATA_URL` | `'https://www.ivykids.tw/manage/make_an_appointment/'` |
| `ivykids_sync_enabled` | `IVYKIDS_SYNC_ENABLED` | `False` |
| `ivykids_sync_interval_minutes` | `IVYKIDS_SYNC_INTERVAL_MINUTES` | `10` |
| `campus_name` | `RECRUITMENT_CAMPUS_NAME` | `None` |
| `campus_address` | `RECRUITMENT_CAMPUS_ADDRESS` | `None` |
| `campus_lat` | `RECRUITMENT_CAMPUS_LAT` | `None` (float) |
| `campus_lng` | `RECRUITMENT_CAMPUS_LNG` | `None` (float) |
| `campus_travel_mode` | `RECRUITMENT_CAMPUS_TRAVEL_MODE` | `'driving'` |
| `tgos_app_id` | `TGOS_APP_ID` | `None` |
| `tgos_api_key` | `TGOS_API_KEY` | `None` |
| `market_timeout_seconds` | `RECRUITMENT_MARKET_TIMEOUT_SECONDS` | `8` |

### 3.8 GeocodingSettings (5)

| field | env | 型別 | Default |
|---|---|---|---|
| `google_maps_api_key` | `GOOGLE_MAPS_API_KEY` | `str \| None` | `None` |
| `provider` | `GEOCODING_PROVIDER` | `Literal['google','nominatim','tgos']` | `'nominatim'` |
| `user_agent` | `GEOCODING_USER_AGENT` | `str` | `'ivyManageSystem/1.0'` |
| `contact_email` | `GEOCODING_CONTACT_EMAIL` | `str \| None` | `None` |
| `timeout_seconds` | `GEOCODING_TIMEOUT_SECONDS` | `int` | `8` |

### 3.9 StorageSettings (7)

| field | env | 型別 | Default |
|---|---|---|---|
| `backend` | `STORAGE_BACKEND` | `Literal['local','supabase']` | `'local'` |
| `root` | `STORAGE_ROOT` | `Path` | `Path('./uploads')` |
| `supabase_url` | `SUPABASE_URL` | `str \| None` | `None` |
| `supabase_service_role_key` | `SUPABASE_SERVICE_ROLE_KEY` | `str \| None` | `None` |
| `supabase_signed_url_ttl` | `SUPABASE_STORAGE_SIGNED_URL_TTL` | `int` | `3600` |
| `growth_report_root` | `GROWTH_REPORT_ROOT` | `Path` | `Path('./growth_reports')` |
| `growth_report_max_bytes` | `GROWTH_REPORT_MAX_BYTES` | `int` | `5_242_880` |

### 3.10 MiscSettings (6)

進不了其他 domain 的邊角：

| field | env | 型別 | Default |
|---|---|---|---|
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | `str \| None` | `None` |
| `pos_cash_deposit_warning_threshold` | `POS_CASH_DEPOSIT_WARNING_THRESHOLD` | `int` | `5000` |
| `enable_leave_ot_offset` | `ENABLE_LEAVE_OT_OFFSET` | `BoolEnv` | `False` |
| `activity_query_token_ttl_days` | `ACTIVITY_QUERY_TOKEN_TTL_DAYS` | `int` | `30` |
| `ivy_mcp_username` | `IVY_MCP_USERNAME` | `str \| None` | `None` |
| `ivy_mcp_password` | `IVY_MCP_PASSWORD` | `str \| None` | `None` |

---

## 4. 核心 helper

### 4.1 Reusable validators（`config/validators.py`）

```python
def parse_bool_env(v: str | bool | None) -> bool:
    """ '1' / 'true' / 'yes' (case-insensitive) → True;  其餘 / None → False """
    if isinstance(v, bool): return v
    if v is None: return False
    return str(v).strip().lower() in ("1", "true", "yes")

def parse_csv_list(v: str | list[str] | None) -> list[str]:
    if v is None: return []
    if isinstance(v, list): return v
    return [s.strip() for s in str(v).split(",") if s.strip()]
```

每個 sub-Settings 用 `BeforeValidator` 套：

```python
from typing import Annotated
from pydantic import BeforeValidator

BoolEnv = Annotated[bool, BeforeValidator(parse_bool_env)]
CsvList = Annotated[list[str], BeforeValidator(parse_csv_list)]
```

`parse_bool_env` 認的字串集合**精準保留現況**（`1`/`true`/`yes`），**不**新增 `on` 等別名（避免 scope creep）。

### 4.2 主 Settings（`config/base.py`）

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core import CoreSettings
from .parent_db import ParentDBSettings
from .network import NetworkSettings
from .scheduler import SchedulerSettings
from .sentry import SentrySettings
from .line import LineSettings
from .recruitment import RecruitmentSettings
from .geocoding import GeocodingSettings
from .storage import StorageSettings
from .misc import MiscSettings

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",            # 未知 env 不爆
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
```

不設 `env_nested_delimiter`，因為 73 變數沒有用 `__` delimiter（皆為 flat 名稱）；保留純粹避免日後誤觸發。

### 4.3 conftest autoreset fixture

```python
# tests/conftest.py
import pytest

@pytest.fixture(autouse=True)
def _reset_settings_cache():
    yield
    from config import reset_for_tests
    reset_for_tests()
```

scope 為 `function`（default），spike_rls module-scope fixture 不受影響。確保每個 test 收尾自動 cache_clear，現有 `patch.dict(os.environ)` test 不會污染後續。

### 4.4 敏感欄位 redact

`Settings.model_dump()` 可能被誤呼叫 leak 敏感欄位（JWT_SECRET_KEY / 各 password / API key）。每個 sub-Settings 對敏感欄位用 `Field(..., repr=False)`，並加 `model_dump_safe()` helper（黑名單與 Sentry PII denylist 概念對齊）：

```python
# config/base.py
_SENSITIVE_KEY_SUBSTRINGS = (
    "secret", "password", "token", "key", "dsn", "api_key",
)

def _scrub(data: dict, denylist: tuple[str, ...]) -> dict:
    """ 遞迴掃 dict，key 含敏感 substring 的 value 取代為 '***'。"""
    if not isinstance(data, dict):
        return data
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _scrub(v, denylist)
        elif isinstance(k, str) and any(s in k.lower() for s in denylist) and v not in (None, ""):
            out[k] = "***"
        else:
            out[k] = v
    return out

class Settings(BaseSettings):
    # ... 略
    def model_dump_safe(self) -> dict:
        """ 安全 dump：敏感欄位以 '***' 取代。供 /debug/config endpoint 或日誌使用。 """
        return _scrub(self.model_dump(), _SENSITIVE_KEY_SUBSTRINGS)
```

註：substring 匹配可能誤遮無害欄位（與 Sentry PII denylist 同類問題），如有誤遮再加 exempt list。Phase 1 不需 exempt（73 變數沒有此類衝突）。

---

## 5. 遷移策略

5 個 commit、同一 PR，每個 commit 可獨立 review。

### Commit 1 — Settings 基建

- `pip install pydantic-settings>=2.0` + requirements.txt
- 新增 `config/` 全套 12 檔（1 base + 10 sub + 1 validators）
- `.env.example` 補齊 73 變數註解、按 domain 分區
- `tests/conftest.py` 新增 autoreset fixture
- **不動任何 callsite**，先讓基建獨立可 import / pytest passes

驗收：`from config import settings; print(settings.model_dump_safe())` runs，pytest 4400+ 全綠（沒人用 settings、行為零變化）。

### Commit 2 — 改寫 prod sites（35 檔 / ~168 處）

機械替換對照：

| 現況 | 改後 |
|------|------|
| `os.getenv("ENV", "development").lower() in ("production","prod")` | `settings.core.is_production` |
| `os.environ.get("ENV", "")` | `settings.core.env` |
| `int(os.getenv("X", "600"))`（已是 int 型別） | `settings.<domain>.x` |
| `os.getenv("X","").lower() in ("1","true","yes")` | `settings.<domain>.x`（BoolEnv） |
| `os.environ.get("CORS_ORIGINS", "").split(",")` | `settings.network.cors_origins` |
| `os.environ.get("PARENT_DB_USER")` + RuntimeError if missing | 由 ParentDBSettings `@model_validator` 集中 fail-loud |

35 檔分配（可 subagent 並行做、但同一 PR）：
- `main.py`（最大宗，13 處）
- `api/auth.py`、`api/leaves.py`、`api/activity/pos.py`、`api/portal/leaves.py`、`api/portfolio/reports.py`（5）
- `models/base.py`、`models/parent_db.py`（2）
- `services/*` 11 檔（scheduler / geocoding / recruitment / activity）
- `utils/*` 9 檔（auth / cookie / errors / rate_limit / request_ip / security_headers / sentry_init / storage / supabase_storage）
- `startup/seed.py`
- `mcp_server/activity_crud/client.py`
- `evals/core/llm_attacker.py`
- `scripts/setup_line_richmenu.py`

`scripts/dump_openapi.py` **保留** `os.environ`（CLI 工具，獨立於 Settings 生命週期）。

### Commit 3 — 改寫 test sites（~40 處 / 14 檔）

```python
# 改前
def test_x(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    importlib.reload(main)
    # ...

# 改後
def test_x(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    from config import reset_for_tests, get_settings
    reset_for_tests()
    settings = get_settings()
    # ... assertions on settings.core.is_production etc.
```

涉及檔案 14 個：
- `tests/test_cookie_samesite.py`、`tests/test_csp_headers.py`、`tests/test_safe_500.py`、`tests/test_misc_medium_authz.py`（4）
- `tests/evals/test_framework.py`（1）
- `tests/spike_rls/conftest.py`（**保留**直接讀 ENV pattern — test fixture 自讀 env 合理；只加 allow-list）
- `tests/spike_rls/test_rls_guard.py` + `test_rls_phase1{,_pilot,b_leaves,c_reads,d_milestones,e,f,g}.py`（8 個，`os.environ.setdefault/pop` 改 `monkeypatch.setenv`）

### Commit 4 — 移除 main.py 散落 helper

- 刪 `main.py:_is_production()` → 改用 `settings.core.is_production`
- 刪 `main.py:_dev_router_enabled()` → 改用 `settings.core.dev_router_enabled`
- 刪 `main.py:_DEV_ROUTER_ENV_ALLOWLIST` constant → 移入 `CoreSettings`

### Commit 5 — CI 防回退 gate

```yaml
- name: Block os.getenv outside config/
  run: |
    if git grep -nE 'os\.(getenv|environ)' -- '*.py' \
       ':!config/' \
       ':!tests/conftest.py' \
       ':!scripts/dump_openapi.py' \
       ':!alembic/env.py'; then
      echo "❌ os.getenv 只能在 config/ 與明確 allow-list 內使用"
      exit 1
    fi
```

Allow-list：
- `config/` — Settings 內部
- `tests/conftest.py` — autoreset fixture 需要
- `scripts/dump_openapi.py` — CLI 工具獨立生命週期
- `alembic/env.py` — Alembic CLI 早於 FastAPI startup

---

## 6. 風險與緩解

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| Pydantic startup validate fail-loud → dev .env 不全起不來 | 中 | 中 | 非 prod-required 欄位給 default；prod-required 用 `@model_validator(mode="after")` 條件 fail |
| 168 處機械替換打字錯誤 / domain 歸錯 | 中 | 高 | (a) subagent-driven 各 domain 並行同 PR；(b) pytest 4400+ 全綠；(c) `git diff` 用 grep -c 互驗 net delta；(d) CI gate |
| Test conftest autoreset 與 spike_rls fixture 衝突 | 低 | 中 | function-scope；先在 spike_rls 跑一輪驗 |
| `import time` validate 失敗炸 startup | 低 | 高 | `get_settings()` lazy；可選 `from config import settings` eager（dev 早期失敗反而好） |
| `lru_cache` 在 multi-worker 各 worker 一份 | 中 | 低 | 預期行為（env 不變動），無需處理 |
| `.env.example` 補齊時 leak prod 線索 | 低 | 中 | 只放 placeholder，review 逐欄掃 |

---

## 7. 驗收標準

- [ ] `git grep -nE 'os\.(getenv|environ)' -- '*.py' ':!config/' ':!tests/conftest.py' ':!scripts/dump_openapi.py' ':!alembic/env.py'` 為空
- [ ] `pytest` 全套 4400+ 通過、零回歸
- [ ] CI grep gate 啟用 green
- [ ] `.env.example` 73 變數全列、有中文註解、按 domain 分區
- [ ] dev 缺 `JWT_SECRET_KEY` 時 warning（不 fail）；prod 缺則 fail-loud
- [ ] `models/parent_db.py` 改讀 settings 後 spike_rls 15 test 全綠
- [ ] `settings.model_dump_safe()` 對 dsn / password / key / token / secret 欄位 redact 為 `***`
- [ ] Settings 變更後 `requirements.txt` `pip-audit` 仍綠

---

## 8. Scope 明確不做（defer）

| 議題 | 為何 defer |
|------|-----------|
| Feature flag DB 化 | Phase 2 獨立 spec（cache layer / audit log / scope） |
| 前端 `VITE_*` typed wrapper | backend-only |
| Env 變數 rename | 部署/CI 已有舊名，rename 風險過大 |
| `models/parent_db.py` env-driven singleton 架構替換 | 只動內部讀法、不動 fail-loud / reset_for_tests pattern |
| `tests/spike_rls/conftest.py` `ADMIN_URL` 計算 | Test fixture 自讀 env 合理 |
| Settings 變更 audit log | YAGNI |
| `parse_bool_env` 新增 `on` / `enabled` 別名 | 精準保留現況集合（`1`/`true`/`yes`） |

---

## 9. Phase 2 雛形（供參考、本 spec 不負責）

- Feature flag DB 表 + admin-only toggle UI
- Cache layer（Redis or in-memory + TTL）
- Audit log（誰、何時、改了什麼）
- Per-scope（global / per-tenant / per-user）
- 與本 Phase 1 Settings 共存（runtime flag override env default）
