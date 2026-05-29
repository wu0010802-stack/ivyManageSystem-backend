# 招生地址 PII 降精度 + consent + cache TTL 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 招生地址送 Google API 前降到「巷」級、heatmap 100m grid bucket + K=5 k-anonymity 抑制、新 visit form 加 admin attestation consent、cache 90d TTL，達成個資法 §8/§9/§19 合規。

**Architecture:** 純 backend-heavy 變動 + 兩個 frontend Vue 元件改寫；alembic single-head migration `rcrgeoconsent01`；config-driven K threshold；DSR opt-out cascade deferred 待 P0c-2 merge。

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / Pydantic / pydantic-settings / pytest / Vue 3 + script setup TS / Vitest / Leaflet / Element Plus

**Spec:** `docs/superpowers/specs/2026-05-28-recruitment-address-pii-anonymization-design.md`

**Pre-flight 驗證已完成**：
- BE 6 modify 對象皆存在於 origin/main：`services/geocoding_service.py`, `api/recruitment/{hotspots,records,shared}.py`, `services/security_gc_scheduler.py`, `models/recruitment.py`, `config/recruitment.py`
- alembic single head：`intghealth01`（migration 鏈接此 head）
- FE：`RecruitmentAddressHeatmap.vue` 與 `RecruitmentRecordDialog.vue` 為實際 visit form（非 spec 初版的 `RecruitmentVisitForm`）
- DSR opt-out endpoint **未** 在 origin/main → §4.6 cascade 列為 follow-up

---

## Task 1: 新增純函式 `truncate_address_to_lane` + 11 case 單元測試

**Files:**
- Create: `tests/test_geocoding_truncate.py`
- Modify: `services/geocoding_service.py:77-83`（加新公開函式於 `_strip_floor_suffix` 後）

- [ ] **Step 1: 寫 failing test（11 case 含中港路、巷弄、號之、樓中樓）**

```python
# tests/test_geocoding_truncate.py
"""truncate_address_to_lane — 招生地址降至「巷」級。"""

import pytest

from services.geocoding_service import truncate_address_to_lane


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 樓號處理（仍可保留巷）
        ("臺北市文山區興隆路四段30巷5號3樓", "臺北市文山區興隆路四段30巷"),
        ("臺北市文山區興隆路四段30巷5號B1", "臺北市文山區興隆路四段30巷"),
        ("臺北市信義區忠孝東路五段100號", "臺北市信義區忠孝東路五段"),
        ("臺北市中正區重慶南路一段122號之2", "臺北市中正區重慶南路一段"),
        # 弄處理（保留巷）
        ("新北市板橋區文化路一段188巷5弄8號", "新北市板橋區文化路一段188巷"),
        ("新北市板橋區文化路一段188巷5弄", "新北市板橋區文化路一段188巷"),
        # 純路（無巷）— 不動
        ("臺北市大安區仁愛路四段", "臺北市大安區仁愛路四段"),
        # 樓之 + 號之
        ("高雄市三民區建工路300號3樓之2", "高雄市三民區建工路"),
        # 純巷（已 truncated）
        ("臺北市文山區興隆路四段30巷", "臺北市文山區興隆路四段30巷"),
        # 邊界：空字串
        ("", ""),
        # 邊界：未含號的地址（e.g. 地名）
        ("臺北市中正區", "臺北市中正區"),
    ],
)
def test_truncate_address_to_lane(raw: str, expected: str) -> None:
    assert truncate_address_to_lane(raw) == expected


def test_truncate_address_to_lane_preserves_lane_only() -> None:
    """巷之後若還有弄/號要剃；單純巷要保留。"""
    assert truncate_address_to_lane("臺北市X路1巷100號") == "臺北市X路1巷"
    assert truncate_address_to_lane("臺北市X路1巷") == "臺北市X路1巷"
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_geocoding_truncate.py -v`
Expected: FAIL with `ImportError: cannot import name 'truncate_address_to_lane'`

- [ ] **Step 3: 實作 `truncate_address_to_lane`**

加在 `services/geocoding_service.py` 第 83 行（`_strip_floor_suffix` 函式後）：

```python
def truncate_address_to_lane(address: str) -> str:
    """招生地址 PII 降精度：去樓號 + 去門牌號（\\d+號|\\d+弄），保留「\\d+巷」級。

    例：
      臺北市文山區興隆路四段30巷5號3樓  → 臺北市文山區興隆路四段30巷
      臺北市信義區忠孝東路五段100號     → 臺北市信義區忠孝東路五段

    用途：招生地址送 Google Geocoding API / 入 RecruitmentGeocodeCache 前的 PII 降精度。
    """
    s = _strip_floor_suffix(address or "")
    # 先剃號（含 \d+號之\d+）
    s = re.sub(r"\d+號(?:之\d+)?.*$", "", s).strip()
    # 再剃弄（不剃巷）
    s = re.sub(r"\d+弄.*$", "", s).strip()
    return s
```

- [ ] **Step 4: 跑測試驗證 pass**

Run: `pytest tests/test_geocoding_truncate.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_geocoding_truncate.py services/geocoding_service.py
git commit -m "feat(geocoding): truncate_address_to_lane 招生地址巷級 PII 降精度

新增公開純函式去樓號 + 門牌號 + 弄，保留巷層級。
caller 在後續 task 接入 Google / Nominatim path。

合規：個資法 §19 範圍必要性 — 招生分析不需住宅門牌精度"
```

---

## Task 2: Google / Nominatim path 接入 truncate

**Files:**
- Modify: `services/geocoding_service.py:155, 202`（兩個 entry point）
- Modify: `tests/test_geocoding_truncate.py`（加 mock requests 驗 query 不含號）

- [ ] **Step 1: 寫 failing test mock requests**

加在 `tests/test_geocoding_truncate.py` 底：

```python
from unittest.mock import patch, MagicMock


@patch("services.geocoding_service.requests.get")
@patch("services.geocoding_service._GOOGLE_MAPS_API_KEY", "fake")
@patch("services.geocoding_service._GEOCODING_PROVIDER", "google")
def test_geocode_google_uses_truncated_address(mock_get: MagicMock) -> None:
    """Google geocode 必須先 truncate，不送原始門牌號。"""
    from services.geocoding_service import geocode_address

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "OK",
        "results": [{
            "geometry": {"location": {"lat": 25.0, "lng": 121.5}},
            "formatted_address": "臺北市文山區興隆路四段30巷"
        }]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    geocode_address("臺北市文山區興隆路四段30巷5號3樓")

    sent_address = mock_get.call_args.kwargs["params"]["address"]
    assert "5號" not in sent_address, f"門牌號未被 truncate: {sent_address}"
    assert "3樓" not in sent_address, f"樓號未被 truncate: {sent_address}"
    assert "30巷" in sent_address, f"巷層級被誤剃: {sent_address}"


@patch("services.geocoding_service.requests.get")
@patch("services.geocoding_service._GOOGLE_MAPS_API_KEY", "")
@patch("services.geocoding_service._GEOCODING_PROVIDER", "nominatim")
def test_geocode_nominatim_uses_truncated_address(mock_get: MagicMock) -> None:
    """Nominatim geocode 同樣先 truncate。"""
    from services.geocoding_service import geocode_address

    mock_resp = MagicMock()
    mock_resp.json.return_value = [{
        "lat": "25.0", "lon": "121.5",
        "display_name": "臺北市文山區興隆路四段30巷"
    }]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    geocode_address("臺北市文山區興隆路四段30巷5號3樓")

    # nominatim 走 candidate query 第一個
    first_call_query = mock_get.call_args_list[0].kwargs["params"]["q"]
    assert "5號" not in first_call_query, f"門牌號未被 truncate: {first_call_query}"
    assert "30巷" in first_call_query
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_geocoding_truncate.py::test_geocode_google_uses_truncated_address tests/test_geocoding_truncate.py::test_geocode_nominatim_uses_truncated_address -v`
Expected: FAIL — `assert "5號" not in ...` 失敗（原始地址完整送）

- [ ] **Step 3: 修 `_geocode_with_google` line 155**

`services/geocoding_service.py:155` 之前加一行：

```python
def _geocode_with_google(address: str) -> Optional[dict]:
    if not _GOOGLE_MAPS_API_KEY:
        return None

    # PII 降精度：巷級 truncate 後才送 Google
    truncated = truncate_address_to_lane(address)

    try:
        resp = EXTERNAL_HTTP_BREAKER.call(
            lambda: requests.get(
                _GOOGLE_GEOCODING_URL,
                params={
                    "address": _normalize_query_address(truncated),
                    ...
```

- [ ] **Step 4: 修 `_geocode_with_nominatim` line 202**

```python
def _geocode_with_nominatim(address: str) -> Optional[dict]:
    # PII 降精度：巷級 truncate 後才送 Nominatim
    truncated = truncate_address_to_lane(address)
    for query in _build_nominatim_query_candidates(truncated):
        ...
```

- [ ] **Step 5: 跑測試驗證 pass**

Run: `pytest tests/test_geocoding_truncate.py -v`
Expected: all 14 passed（12 truncate cases + 2 mocked geocode）

- [ ] **Step 6: Commit**

```bash
git add tests/test_geocoding_truncate.py services/geocoding_service.py
git commit -m "feat(geocoding): Google + Nominatim path 接入巷級 truncate

兩 entry point 均先 truncate_address_to_lane 才送出，
RecruitmentGeocodeCache 等下游 caller 自動 inherit。

合規：個資法 §8 / §9 — 跨境傳輸 Google 不再含住宅門牌"
```

---

## Task 3: Config `RECRUITMENT_K_ANONYMITY_THRESHOLD` + clamp test

**Files:**
- Modify: `config/recruitment.py`（加 field）
- Create: `tests/test_config_recruitment_k_anonymity.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_config_recruitment_k_anonymity.py
"""RECRUITMENT_K_ANONYMITY_THRESHOLD config 驗證。"""

import pytest


def test_k_anonymity_threshold_default_is_5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", raising=False)
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 5


def test_k_anonymity_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "3")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 3


def test_k_anonymity_threshold_clamp_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """clamp [2, 10] — K=1 dangerous → 升到 2"""
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "1")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 2


def test_k_anonymity_threshold_clamp_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "100")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 10
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_config_recruitment_k_anonymity.py -v`
Expected: FAIL — `AttributeError: 'RecruitmentSettings' object has no attribute 'k_anonymity_threshold'`

- [ ] **Step 3: 加 field 與 validator 到 `config/recruitment.py`**

在 `RecruitmentSettings` class 末尾（其他 Field 後）加：

```python
    # K-anonymity 抑制門檻（招生地址熱點 bucket 最小 visit 數，少於此值不 render marker）
    k_anonymity_threshold: int = Field(
        default=5,
        validation_alias="RECRUITMENT_K_ANONYMITY_THRESHOLD",
        ge=1,  # pydantic 層只擋 0 / 負，業務層再 clamp 到 [2, 10]
        le=1000,
    )

    @field_validator("k_anonymity_threshold")
    @classmethod
    def _clamp_k_threshold(cls, v: int) -> int:
        return max(2, min(10, v))
```

並在 file 頂端 import：

```python
from pydantic import Field, field_validator
```

- [ ] **Step 4: 跑測試驗證 pass**

Run: `pytest tests/test_config_recruitment_k_anonymity.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add config/recruitment.py tests/test_config_recruitment_k_anonymity.py
git commit -m "feat(config): RECRUITMENT_K_ANONYMITY_THRESHOLD env var (default 5, clamp [2, 10])

業主決議比 GDPR/HIPAA K=3 慣例更保守；config 化讓業主第一週後可調 K=3
若 marker 過稀。clamp [2, 10] 防 K=1 退化為 individual marker。"
```

---

## Task 4: Alembic migration `rcrgeoconsent01`（add consent column + grandfather + DROP cache）

**Files:**
- Create: `alembic/versions/20260528_rcrgeoconsent01_recruitment_geocoding_consent.py`

- [ ] **Step 1: 確認 single head**

Run: `alembic heads`
Expected: `intghealth01 (head)`（spec 預期）

- [ ] **Step 2: 寫 migration**

```python
"""recruitment geocoding consent + DROP cache

Revision ID: rcrgeoconsent01
Revises: intghealth01
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "rcrgeoconsent01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 加 consent 欄位（RecruitmentVisit + RecruitmentIvykidsRecord）
    op.add_column(
        "recruitment_visits",
        sa.Column("geocoding_consent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "recruitment_ivykids_records",
        sa.Column("geocoding_consent_at", sa.DateTime(), nullable=True),
    )

    # 2. RecruitmentVisit grandfather：既有 row 視為已同意（避 heatmap blank-out）
    op.execute(
        "UPDATE recruitment_visits "
        "SET geocoding_consent_at = created_at "
        "WHERE geocoding_consent_at IS NULL"
    )
    # 3. RecruitmentIvykidsRecord 不 grandfather（來源無 consent 證據，留 NULL → 不上 heatmap）

    # 4. 清空 cache（下次 sync 會以 truncated key 重灌；operational note: ~200 Google API call）
    op.execute("DELETE FROM recruitment_geocode_cache")


def downgrade() -> None:
    op.drop_column("recruitment_visits", "geocoding_consent_at")
    op.drop_column("recruitment_ivykids_records", "geocoding_consent_at")
    # 注意：cache DELETE 不 reversible（無 backup）；downgrade 後 cache 仍空，下次 sync 重灌
```

- [ ] **Step 3: 確認 alembic upgrade 可跑（在 dev DB）**

Run: `alembic upgrade head`
Expected: `Running upgrade intghealth01 -> rcrgeoconsent01`

Run: `psql $DATABASE_URL -c "\d recruitment_visits" | grep consent`
Expected: `geocoding_consent_at | timestamp without time zone | |`

- [ ] **Step 4: 確認 downgrade 可跑（safety）**

Run: `alembic downgrade -1`
Expected: `Running downgrade rcrgeoconsent01 -> intghealth01`

Run: `psql $DATABASE_URL -c "\d recruitment_visits" | grep consent`
Expected: empty

Run: `alembic upgrade head` （還原）

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260528_rcrgeoconsent01_recruitment_geocoding_consent.py
git commit -m "feat(alembic): rcrgeoconsent01 招生 geocoding consent + cache reset

- RecruitmentVisit + RecruitmentIvykidsRecord 加 geocoding_consent_at column
- RecruitmentVisit grandfather：既有 row consent_at = created_at
- RecruitmentIvykidsRecord 不 grandfather（NULL → 不上 heatmap，業主決議）
- DROP cache rows（下次 sync 以 truncated key 重灌，~200 Google API call 預算）

合規：個資法 §8 consent 紀錄；§19 retention via 後續 90d GC scheduler"
```

---

## Task 5: Model 加 `geocoding_consent_at` 欄位

**Files:**
- Modify: `models/recruitment.py:25` (RecruitmentVisit), `:71` (RecruitmentIvykidsRecord)

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_recruitment_consent_model.py
"""RecruitmentVisit/IvykidsRecord 加 geocoding_consent_at 欄位後可正常 ORM 操作。"""

from datetime import datetime

from models.base import session_scope
from models.recruitment import RecruitmentVisit, RecruitmentIvykidsRecord


def test_recruitment_visit_consent_at_default_none(use_test_db) -> None:
    with session_scope() as session:
        v = RecruitmentVisit(
            month="115.05", child_name="Test", grade="幼兒"
        )
        session.add(v)
        session.flush()
        assert v.geocoding_consent_at is None


def test_recruitment_visit_consent_at_set(use_test_db) -> None:
    with session_scope() as session:
        now = datetime(2026, 5, 28, 12, 0, 0)
        v = RecruitmentVisit(
            month="115.05", child_name="Test", grade="幼兒",
            geocoding_consent_at=now,
        )
        session.add(v)
        session.flush()
        assert v.geocoding_consent_at == now


def test_recruitment_ivykids_consent_at_default_none(use_test_db) -> None:
    with session_scope() as session:
        r = RecruitmentIvykidsRecord(
            external_id="test-123", month="115.05", child_name="Test"
        )
        session.add(r)
        session.flush()
        assert r.geocoding_consent_at is None
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_recruitment_consent_model.py -v`
Expected: FAIL — `AttributeError: 'RecruitmentVisit' object has no attribute 'geocoding_consent_at'`

- [ ] **Step 3: 加欄位到 `models/recruitment.py`**

在 `RecruitmentVisit` class 內，`updated_at` 之前（line ~57）加：

```python
    # PII consent attestation（招生人員口頭同意紀錄）
    geocoding_consent_at = Column(DateTime, nullable=True)
```

`RecruitmentIvykidsRecord` 內 `updated_at` 之前（line ~97）加：

```python
    geocoding_consent_at = Column(DateTime, nullable=True)
```

- [ ] **Step 4: 跑測試驗證 pass**

Run: `pytest tests/test_recruitment_consent_model.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add models/recruitment.py tests/test_recruitment_consent_model.py
git commit -m "feat(models): RecruitmentVisit/IvykidsRecord 加 geocoding_consent_at"
```

---

## Task 6: Pydantic `RecruitmentVisitCreate/Update` 加 consent field

**Files:**
- Modify: `api/recruitment/shared.py:622, 649`

- [ ] **Step 1: 找 既有 schema 結構**

Run: `grep -A 30 "class RecruitmentVisitCreate" api/recruitment/shared.py | head -35`
讀取目前所有 fields 以便加在正確位置（最後一個 Field 後）。

- [ ] **Step 2: 寫 failing test**

```python
# tests/test_recruitment_visit_schema_consent.py
"""RecruitmentVisitCreate/Update 接 geocoding_consent boolean。"""

from api.recruitment.shared import RecruitmentVisitCreate, RecruitmentVisitUpdate


def test_visit_create_default_consent_false() -> None:
    """業主決議 explicit attestation — 預設不勾"""
    payload = RecruitmentVisitCreate(
        month="115.05", child_name="Test", grade="幼兒"
    )
    assert payload.geocoding_consent is False


def test_visit_create_consent_true() -> None:
    payload = RecruitmentVisitCreate(
        month="115.05", child_name="Test", grade="幼兒",
        geocoding_consent=True,
    )
    assert payload.geocoding_consent is True


def test_visit_update_default_consent_none() -> None:
    """Update path: None = 不修改 consent；True/False = 修改"""
    payload = RecruitmentVisitUpdate()
    assert payload.geocoding_consent is None
```

- [ ] **Step 3: 跑測試驗證 fail**

Run: `pytest tests/test_recruitment_visit_schema_consent.py -v`
Expected: FAIL — validation error / no field

- [ ] **Step 4: 加 field**

`api/recruitment/shared.py` 內 `class RecruitmentVisitCreate(BaseModel)`：

```python
class RecruitmentVisitCreate(BaseModel):
    # ... 既有 fields ...
    geocoding_consent: bool = Field(
        default=False,
        description="家長已口頭同意以本住址進行招生區位分析（送至 Google Maps）；業主決議預設不勾。",
    )
```

`class RecruitmentVisitUpdate(BaseModel)`：

```python
class RecruitmentVisitUpdate(BaseModel):
    # ... 既有 fields ...
    geocoding_consent: bool | None = Field(
        default=None,
        description="None=不修改；True/False=修改 consent_at（True=寫 now()；False=寫 NULL）",
    )
```

- [ ] **Step 5: 跑測試驗證 pass**

Run: `pytest tests/test_recruitment_visit_schema_consent.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add api/recruitment/shared.py tests/test_recruitment_visit_schema_consent.py
git commit -m "feat(schemas): RecruitmentVisitCreate/Update 加 geocoding_consent 欄

預設 False（業主決議 explicit attestation 責任）；
Update 預設 None 區分「不修改」與「明確設值」"
```

---

## Task 7: `records.py` POST/PUT 接 consent → consent_at 寫入 + audit log

**Files:**
- Modify: `api/recruitment/records.py:170` (POST), `:186` (PUT)

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_recruitment_visit_consent_endpoint.py
"""POST/PUT /api/recruitment/records 接 consent → consent_at 寫入"""

from datetime import datetime, timedelta

# 假設 conftest.py 已提供 admin_client (FastAPI TestClient with admin token)
# + use_test_db fixture
from models.recruitment import RecruitmentVisit


def test_post_records_with_consent_true_sets_consent_at(admin_client, use_test_db) -> None:
    resp = admin_client.post("/api/recruitment/records", json={
        "month": "115.05", "child_name": "TC", "grade": "幼兒",
        "geocoding_consent": True,
    })
    assert resp.status_code == 201
    visit_id = resp.json()["id"]

    from models.base import session_scope
    with session_scope() as s:
        v = s.query(RecruitmentVisit).filter_by(id=visit_id).one()
        assert v.geocoding_consent_at is not None
        # 在 1 秒內 = 剛剛
        assert datetime.utcnow() - v.geocoding_consent_at < timedelta(seconds=10)


def test_post_records_with_consent_false_sets_null(admin_client, use_test_db) -> None:
    resp = admin_client.post("/api/recruitment/records", json={
        "month": "115.05", "child_name": "TC", "grade": "幼兒",
        "geocoding_consent": False,
    })
    assert resp.status_code == 201
    visit_id = resp.json()["id"]

    from models.base import session_scope
    with session_scope() as s:
        v = s.query(RecruitmentVisit).filter_by(id=visit_id).one()
        assert v.geocoding_consent_at is None


def test_put_records_consent_true_writes_now(admin_client, use_test_db) -> None:
    # create with consent=false
    create_resp = admin_client.post("/api/recruitment/records", json={
        "month": "115.05", "child_name": "TC2", "grade": "幼兒",
        "geocoding_consent": False,
    })
    visit_id = create_resp.json()["id"]

    # update to consent=true
    upd_resp = admin_client.put(f"/api/recruitment/records/{visit_id}", json={
        "geocoding_consent": True,
    })
    assert upd_resp.status_code == 200

    from models.base import session_scope
    with session_scope() as s:
        v = s.query(RecruitmentVisit).filter_by(id=visit_id).one()
        assert v.geocoding_consent_at is not None


def test_put_records_consent_false_clears(admin_client, use_test_db) -> None:
    create_resp = admin_client.post("/api/recruitment/records", json={
        "month": "115.05", "child_name": "TC3", "grade": "幼兒",
        "geocoding_consent": True,
    })
    visit_id = create_resp.json()["id"]

    upd_resp = admin_client.put(f"/api/recruitment/records/{visit_id}", json={
        "geocoding_consent": False,
    })
    assert upd_resp.status_code == 200

    from models.base import session_scope
    with session_scope() as s:
        v = s.query(RecruitmentVisit).filter_by(id=visit_id).one()
        assert v.geocoding_consent_at is None


def test_put_records_consent_none_preserves(admin_client, use_test_db) -> None:
    """Update 不帶 consent field → 保留既有"""
    create_resp = admin_client.post("/api/recruitment/records", json={
        "month": "115.05", "child_name": "TC4", "grade": "幼兒",
        "geocoding_consent": True,
    })
    visit_id = create_resp.json()["id"]

    upd_resp = admin_client.put(f"/api/recruitment/records/{visit_id}", json={
        "child_name": "TC4-rename"  # 只改其他欄
    })
    assert upd_resp.status_code == 200

    from models.base import session_scope
    with session_scope() as s:
        v = s.query(RecruitmentVisit).filter_by(id=visit_id).one()
        assert v.geocoding_consent_at is not None  # 保留
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_recruitment_visit_consent_endpoint.py -v`
Expected: 5 FAIL — consent_at 未被寫入

- [ ] **Step 3: 修 `records.py` POST handler（line ~170）**

讀取既有 POST handler 找 `RecruitmentVisit(...)` 建立處。加入 consent 邏輯：

```python
# 在 POST records handler 內，build RecruitmentVisit 前後加：
visit_data = payload.model_dump(exclude={"geocoding_consent"}, exclude_unset=False)
visit = RecruitmentVisit(**visit_data)
if payload.geocoding_consent:
    visit.geocoding_consent_at = now_taipei_naive()
# else: 預設 None 不動
session.add(visit)
```

- [ ] **Step 4: 修 PUT handler（line ~186）**

```python
# PUT records/{record_id}
existing = session.query(RecruitmentVisit).filter_by(id=record_id).one_or_none()
if not existing:
    raise HTTPException(404)

update_data = payload.model_dump(exclude_unset=True, exclude={"geocoding_consent"})
for key, value in update_data.items():
    setattr(existing, key, value)

# consent 三狀態處理
if payload.geocoding_consent is True:
    existing.geocoding_consent_at = now_taipei_naive()
elif payload.geocoding_consent is False:
    existing.geocoding_consent_at = None
# else (None): 保留既有
```

- [ ] **Step 5: 跑測試驗證 pass**

Run: `pytest tests/test_recruitment_visit_consent_endpoint.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add api/recruitment/records.py tests/test_recruitment_visit_consent_endpoint.py
git commit -m "feat(records): POST/PUT consent → consent_at 寫入

- POST: consent=true → now()；false → NULL
- PUT: consent=true → now()；false → NULL；None → 保留既有
- 業主決議：consent 預設 false，由招生人員 explicit attestation 勾選"
```

---

## Task 8: `hotspots._query_address_hotspots` GROUP BY truncated + consent filter

**Files:**
- Modify: `api/recruitment/hotspots.py:32-90`（_query_address_hotspots）

- [ ] **Step 1: 寫 failing test（驗 truncated GROUP BY + consent filter）**

```python
# tests/test_hotspots_truncate_and_consent.py
from datetime import datetime
from models.base import session_scope
from models.recruitment import RecruitmentVisit


def test_hotspots_groups_by_truncated_address(use_test_db, admin_client) -> None:
    """同巷不同戶 → group 同一 key（advisor catch: 不能只測 total，需測 hotspot 數=1）"""
    with session_scope() as s:
        for n in range(5):
            s.add(RecruitmentVisit(
                month="115.05", child_name=f"K{n}", grade="幼兒",
                address=f"臺北市文山區興隆路四段30巷{n+1}號",  # 5 個不同門牌號
                geocoding_consent_at=datetime.utcnow(),
            ))
    resp = admin_client.get("/api/recruitment/address-hotspots")
    assert resp.status_code == 200
    body = resp.json()
    hotspots = body.get("hotspots", [])
    # 關鍵 assert：5 戶不同門牌號 → truncate 後同一巷 → 應只有 1 個 hotspot 且 visit=5
    # 如果 truncate 沒生效，會看到 5 個 hotspot 每個 visit=1 → assert 不成立
    assert len(hotspots) == 1, f"truncate 失效：應 1 個 hotspot 實際 {len(hotspots)} 個"
    assert hotspots[0]["visit"] == 5, f"visit count 不對：{hotspots[0]}"
    assert hotspots[0]["address"] == "臺北市文山區興隆路四段30巷", \
        f"truncated address 不對：{hotspots[0]['address']}"


def test_hotspots_excludes_null_consent(use_test_db, admin_client) -> None:
    """consent_at = NULL 不進 hotspot pipeline"""
    with session_scope() as s:
        s.add(RecruitmentVisit(
            month="115.05", child_name="With", grade="幼兒",
            address="臺北市中山區某路1號",
            geocoding_consent_at=datetime.utcnow(),
        ))
        s.add(RecruitmentVisit(
            month="115.05", child_name="Without", grade="幼兒",
            address="臺北市中山區某路1號",  # 同址但無 consent
            geocoding_consent_at=None,
        ))
    resp = admin_client.get("/api/recruitment/address-hotspots")
    body = resp.json()
    aggregated = sum(b.get("visit", 0) for b in body.get("hotspots", []))
    assert aggregated == 1, f"無 consent visit 不應入 aggregate, 實際: {aggregated}"
```

- [ ] **Step 2: 跑測試驗證 fail**

Run: `pytest tests/test_hotspots_truncate_and_consent.py -v`
Expected: FAIL — 5 戶 group 成 5 個 hotspot；NULL consent 也入 pipeline

- [ ] **Step 3: 修 `_query_address_hotspots`**

```python
from services.geocoding_service import truncate_address_to_lane

def _query_address_hotspots(session, limit=None, dataset_scope=None):
    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)

    # Postgres regex truncate；SQLite fallback 走 Python 端
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        truncated_addr = sa.func.regexp_replace(
            sa.func.regexp_replace(
                sa.func.trim(RecruitmentVisit.address),
                r'\d+號(?:之\d+)?.*$', ''
            ),
            r'\d+弄.*$', ''
        ).label("truncated_address")
    else:
        # SQLite: 先撈 raw 後 Python truncate（測試環境）
        truncated_addr = sa.func.trim(RecruitmentVisit.address).label("truncated_address")

    rows_query = session.query(
        truncated_addr,
        RecruitmentVisit.district.label("district"),
        sa.func.count(RecruitmentVisit.id).label("visit"),
        sa.func.sum(dep_case).label("deposit"),
    )
    scope_filters = _dataset_scope_filters(dataset_scope)
    if scope_filters:
        rows_query = rows_query.filter(*scope_filters)

    rows = (
        rows_query
        .filter(
            RecruitmentVisit.address.isnot(None),
            sa.func.length(sa.func.trim(RecruitmentVisit.address)) > 0,
            RecruitmentVisit.geocoding_consent_at.isnot(None),  # ← 新增 consent gate
        )
        .group_by(truncated_addr, RecruitmentVisit.district)
        .all()
    )

    # SQLite path: 在 Python 端 truncate 後重 group（advisor catch: SQLite 沒 regex，必須真正 truncate）
    if dialect != "postgresql":
        from collections import defaultdict
        from types import SimpleNamespace

        # rows 此時是 raw full-address group（trim only），需要 Python truncate 後重 group
        regrouped: dict[tuple[str, str], dict] = defaultdict(lambda: {"visit": 0, "deposit": 0})
        for row in rows:
            real_truncated = truncate_address_to_lane(row.truncated_address)
            key = (real_truncated, row.district or "")
            regrouped[key]["visit"] += row.visit
            regrouped[key]["deposit"] += row.deposit
        rows = [
            SimpleNamespace(
                truncated_address=k[0], district=k[1],
                visit=v["visit"], deposit=v["deposit"],
            )
            for k, v in regrouped.items()
        ]

    # 後續 merged dict 累加（既有邏輯）但 key 改 truncated_address
    merged: dict[str, dict] = {}
    records_with_address = 0
    for row in rows:
        address = (row.truncated_address or "").strip()
        if not address:
            continue
        district = (
            (row.district or "").strip()
            or _extract_district_from_address(address)
            or "未填寫"
        )
        visit = row.visit or 0
        deposit = row.deposit or 0
        records_with_address += visit

        hotspot = merged.setdefault(address, {
            "address": address, "district": district,
            "visit": 0, "deposit": 0,
        })
        hotspot["visit"] += visit
        hotspot["deposit"] += deposit

    return list(merged.values()), records_with_address, len(merged)
```

- [ ] **Step 4: 跑測試驗證 pass**

Run: `pytest tests/test_hotspots_truncate_and_consent.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑既有 hotspots 測試確認 0 regression**

Run: `pytest tests/test_recruitment_api.py -v -k "hotspot"`
Expected: 全綠（既有 hotspot test 在 grandfather 後 consent_at 不會 NULL，仍能 pass）

- [ ] **Step 6: Commit**

```bash
git add api/recruitment/hotspots.py tests/test_hotspots_truncate_and_consent.py
git commit -m "feat(hotspots): GROUP BY truncated address + consent filter

- _query_address_hotspots 上游用 truncate_address_to_lane group
  (Postgres regexp_replace / SQLite fallback Python 端)
- WHERE geocoding_consent_at IS NOT NULL（NULL 不進 heatmap）
- 同巷不同戶併同 bucket，解 cache UNIQUE conflict 同時匿名化"
```

---

## Task 9: `hotspots` 100m grid bucket aggregation + K=5 suppression

**Files:**
- Modify: `api/recruitment/hotspots.py` — `_build_address_hotspots_response` 或新建 bucket helper
- Modify: `api/recruitment/hotspots.py` — `sync_recruitment_address_hotspots` ON CONFLICT/SELECT path
- 既有 endpoint `/api/recruitment/address-hotspots` response shape 加 `buckets` + `district_residual_visits`

- [ ] **Step 1: 找 既有 `_build_address_hotspots_response` 結構**

Run: `grep -n "_build_address_hotspots_response\|address-hotspots" api/recruitment/hotspots.py`

- [ ] **Step 2: 寫 failing test K-anonymity K=5**

```python
# tests/test_hotspots_k_anonymity.py
from datetime import datetime
from models.base import session_scope
from models.recruitment import RecruitmentVisit, RecruitmentGeocodeCache


def _seed_visit_at(session, lat: float, lng: float, count: int, district: str = "文山區") -> None:
    """種 N 筆 visit 同巷不同門牌號（驗 truncate 真實生效，advisor catch）。

    visit.address: 完整含門牌號（如 X 路 30 巷 5 號）
    cache.address: truncated（X 路 30 巷）— 因為 truncate_address_to_lane 之後才寫 cache
    """
    truncated_addr = f"臺北市{district}測試路{int(lat * 100)}巷"
    for i in range(count):
        session.add(RecruitmentVisit(
            month="115.05", child_name=f"K{i}-{lat}", grade="幼兒",
            address=f"{truncated_addr}{i+1}號",  # 完整地址，5 個不同號
            district=district,
            geocoding_consent_at=datetime.utcnow(),
        ))
    cache = session.query(RecruitmentGeocodeCache).filter_by(address=truncated_addr).one_or_none()
    if not cache:
        session.add(RecruitmentGeocodeCache(
            address=truncated_addr,  # cache key = truncated
            lat=lat, lng=lng, status="resolved",
            district=district, resolved_at=datetime.utcnow(),
        ))


def test_buckets_suppress_below_k(use_test_db, admin_client) -> None:
    """K=5：bucket visit_count=4 應被 suppress，count=8 應 render"""
    with session_scope() as s:
        _seed_visit_at(s, 25.014, 121.567, count=4)   # bucket A: 4 < K=5
        _seed_visit_at(s, 25.020, 121.570, count=8)   # bucket B: 8 >= K=5

    resp = admin_client.get("/api/recruitment/address-hotspots")
    body = resp.json()

    buckets = body.get("buckets", [])
    bucket_keys = {(b["center_lat"], b["center_lng"]) for b in buckets}
    assert (25.020, 121.570) in bucket_keys, "K=8 bucket should render"
    assert all(b["visit_count"] != 4 for b in buckets), "K=4 bucket should be suppressed"

    residual = body.get("district_residual_visits", {})
    assert residual.get("文山區", 0) >= 4, "suppressed visit_count 應計入 residual"


def test_district_residual_aggregates_multiple_suppressed(use_test_db, admin_client) -> None:
    with session_scope() as s:
        _seed_visit_at(s, 25.014, 121.567, count=2)   # 2 < K=5
        _seed_visit_at(s, 25.030, 121.580, count=3)   # 3 < K=5
        _seed_visit_at(s, 25.040, 121.590, count=4)   # 4 < K=5

    resp = admin_client.get("/api/recruitment/address-hotspots")
    body = resp.json()
    residual = body.get("district_residual_visits", {})
    assert residual.get("文山區", 0) == 2 + 3 + 4, "三 suppressed bucket 加總"
```

- [ ] **Step 3: 跑測試驗證 fail**

Run: `pytest tests/test_hotspots_k_anonymity.py -v`
Expected: FAIL — response 無 `buckets` / `district_residual_visits` 鍵

- [ ] **Step 4: 加 bucket aggregation logic**

新增 helper：

```python
# api/recruitment/hotspots.py 內
from config import settings


def _build_buckets_response(session, dataset_scope=None) -> dict:
    """100m grid bucket + K=5 k-anonymity suppression。

    回傳 {"buckets": [...], "district_residual_visits": {...}}
    bucket: {center_lat, center_lng, district, visit_count, deposit_count}
    """
    K = max(2, min(10, settings.recruitment.k_anonymity_threshold))

    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    dialect = session.bind.dialect.name

    if dialect == "postgresql":
        truncated_addr = sa.func.regexp_replace(
            sa.func.regexp_replace(
                sa.func.trim(RecruitmentVisit.address),
                r'\d+號(?:之\d+)?.*$', ''
            ),
            r'\d+弄.*$', ''
        )
    else:
        truncated_addr = sa.func.trim(RecruitmentVisit.address)

    # JOIN visit ⨝ cache
    rows = (
        session.query(
            sa.func.round(RecruitmentGeocodeCache.lat * 1000) / 1000,  # 3 decimal grid
            sa.func.round(RecruitmentGeocodeCache.lng * 1000) / 1000,
            RecruitmentGeocodeCache.district.label("cache_district"),
            sa.func.sum(1).label("visit_count"),
            sa.func.sum(dep_case).label("deposit_count"),
            sa.func.avg(RecruitmentGeocodeCache.lat).label("center_lat"),
            sa.func.avg(RecruitmentGeocodeCache.lng).label("center_lng"),
        )
        .join(RecruitmentGeocodeCache, RecruitmentGeocodeCache.address == truncated_addr)
        .filter(
            RecruitmentVisit.geocoding_consent_at.isnot(None),
            RecruitmentGeocodeCache.lat.isnot(None),
            RecruitmentGeocodeCache.lng.isnot(None),
        )
        .group_by(
            sa.func.round(RecruitmentGeocodeCache.lat * 1000),
            sa.func.round(RecruitmentGeocodeCache.lng * 1000),
            RecruitmentGeocodeCache.district,
        )
        .all()
    )

    buckets = []
    residual = {}
    for row in rows:
        visit_count = int(row.visit_count or 0)
        district = (row.cache_district or "未填寫").strip() or "未填寫"
        if visit_count >= K:
            buckets.append({
                "center_lat": float(row.center_lat),
                "center_lng": float(row.center_lng),
                "district": district,
                "visit_count": visit_count,
                "deposit_count": int(row.deposit_count or 0),
            })
        else:
            residual[district] = residual.get(district, 0) + visit_count

    return {"buckets": buckets, "district_residual_visits": residual}
```

在 `_build_address_hotspots_response` 加 `bucket_payload = _build_buckets_response(session, dataset_scope=dataset_scope)`，merge 進 response：

```python
response = {
    # ... 既有 keys ...
    **bucket_payload,
}
```

- [ ] **Step 5: 修 `sync_recruitment_address_hotspots` cache insert 為 SELECT-then-INSERT**

`api/recruitment/hotspots.py:228` 附近：

```python
# 改自 cached = RecruitmentGeocodeCache(address=hotspot["address"])
# 為 SELECT-then-INSERT 避免多 visit 落在同 lane 觸發 UNIQUE conflict
existing = session.query(RecruitmentGeocodeCache).filter_by(
    address=hotspot["address"]  # 已是 truncated（Task 8 起 hotspot dict.address 為 truncated）
).one_or_none()
if not existing:
    existing = RecruitmentGeocodeCache(address=hotspot["address"])
    session.add(existing)
    session.flush()
cached = existing
cached_rows[cached.address] = cached
```

- [ ] **Step 6: 跑測試驗證 pass**

Run: `pytest tests/test_hotspots_k_anonymity.py -v`
Expected: 2 passed

Run: `pytest tests/test_recruitment_api.py -v -k "hotspot"`
Expected: 全綠（既有 test 0 regression）

- [ ] **Step 7: Commit**

```bash
git add api/recruitment/hotspots.py tests/test_hotspots_k_anonymity.py
git commit -m "feat(hotspots): 100m grid bucket + K=5 k-anonymity suppression

- _build_buckets_response: round(lat*1000)/1000 grid (~100m), avg() 密度加權中心
- visit_count >= K → bucket render；< K → district_residual_visits 累加
- K 從 config.recruitment.k_anonymity_threshold 讀（預設 5，clamp [2,10]）
- sync_recruitment_address_hotspots cache insert 改 SELECT-then-INSERT
  防多 visit 落在同 truncated lane 觸發 IntegrityError"
```

---

## Task 10: `security_gc_scheduler` 加 cache 90d GC step

**Files:**
- Modify: `services/security_gc_scheduler.py`

- [ ] **Step 1: 找 既有 scheduler 結構**

Run: `grep -n "def.*gc\|async def\|scheduler" services/security_gc_scheduler.py | head -20`

- [ ] **Step 2: 寫 failing test**

```python
# tests/test_recruitment_geocode_cache_gc.py
from datetime import datetime, timedelta
from models.base import session_scope
from models.recruitment import RecruitmentGeocodeCache


def test_cache_gc_removes_old_rows(use_test_db) -> None:
    from services.security_gc_scheduler import _gc_recruitment_geocode_cache

    with session_scope() as s:
        # 91 天前的 row 應被刪
        old = RecruitmentGeocodeCache(
            address="臺北市X路100巷", lat=25.0, lng=121.5,
            status="resolved",
            resolved_at=datetime.utcnow() - timedelta(days=91),
        )
        # 89 天前 — 保留
        fresh = RecruitmentGeocodeCache(
            address="臺北市Y路200巷", lat=25.1, lng=121.6,
            status="resolved",
            resolved_at=datetime.utcnow() - timedelta(days=89),
        )
        s.add(old)
        s.add(fresh)
        s.flush()

    with session_scope() as s:
        deleted_count = _gc_recruitment_geocode_cache(s)
        s.commit()

    assert deleted_count == 1

    with session_scope() as s:
        remaining = s.query(RecruitmentGeocodeCache).all()
        assert len(remaining) == 1
        assert remaining[0].address == "臺北市Y路200巷"


def test_cache_gc_skips_null_resolved_at(use_test_db) -> None:
    """resolved_at = NULL（pending/failed）不刪"""
    from services.security_gc_scheduler import _gc_recruitment_geocode_cache

    with session_scope() as s:
        s.add(RecruitmentGeocodeCache(
            address="臺北市Z路", lat=None, lng=None,
            status="pending", resolved_at=None,
        ))

    with session_scope() as s:
        deleted = _gc_recruitment_geocode_cache(s)
        s.commit()

    assert deleted == 0
```

- [ ] **Step 3: 跑測試驗證 fail**

Run: `pytest tests/test_recruitment_geocode_cache_gc.py -v`
Expected: FAIL — `ImportError: _gc_recruitment_geocode_cache`

- [ ] **Step 4: 加 GC function 並 hook 到 tick loop**

`services/security_gc_scheduler.py` 內加：

```python
from datetime import datetime, timedelta

from models.recruitment import RecruitmentGeocodeCache


def _gc_recruitment_geocode_cache(session) -> int:
    """GC RecruitmentGeocodeCache rows older than 90 days from resolved_at.

    回傳刪除 row 數。NULL resolved_at（pending/failed）不刪。
    """
    cutoff = datetime.utcnow() - timedelta(days=90)
    deleted = session.query(RecruitmentGeocodeCache).filter(
        RecruitmentGeocodeCache.resolved_at.isnot(None),
        RecruitmentGeocodeCache.resolved_at < cutoff,
    ).delete(synchronize_session=False)
    logger.info("recruitment_geocode_cache GC removed %d rows", deleted)
    return deleted
```

並在既有 daily tick 內加 call（找既有「jwt_blocklist GC」相近位置）：

```python
async def _security_gc_tick():
    # ... 既有 GC steps ...
    with session_scope() as session:
        _gc_recruitment_geocode_cache(session)
```

- [ ] **Step 5: 跑測試驗證 pass**

Run: `pytest tests/test_recruitment_geocode_cache_gc.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add services/security_gc_scheduler.py tests/test_recruitment_geocode_cache_gc.py
git commit -m "feat(scheduler): RecruitmentGeocodeCache 90d GC

resolved_at 超過 90 天的 cache row 自動刪除；
hook 進既有 daily security GC tick loop。

合規：個資法 §19 retention 必要性 — 招生分析座標不無限保留"
```

---

## Task 11: RecruitmentIvykidsRecord 同 hotspot pipeline + 預設無 consent 不上 heatmap

**Files:**
- Modify: `api/recruitment/hotspots.py` _query/build helpers — 加 UNION（或第二 query）把 ivykids 也納入但 filter NULL consent
- Modify: `services/recruitment_ivykids_sync.py`（若存在）— sync 寫入時 consent_at = NULL（已是 column 預設）

- [ ] **Step 1: 找 ivykids sync service**

Run: `find services -name "*ivykids*" 2>&1`
Run: `grep -rln "RecruitmentIvykidsRecord" --include="*.py" 2>&1 | head -5`

- [ ] **Step 2: 寫 failing test**

```python
# tests/test_hotspots_ivykids_exclusion.py
from datetime import datetime
from models.base import session_scope
from models.recruitment import RecruitmentVisit, RecruitmentIvykidsRecord, RecruitmentGeocodeCache


def test_ivykids_records_excluded_when_consent_null(use_test_db, admin_client) -> None:
    """ivykids 來源無 consent 證據 → 預設 NULL → 不上 heatmap"""
    with session_scope() as s:
        # 5 筆 ivykids visit 同 truncated address，consent NULL
        for i in range(5):
            s.add(RecruitmentIvykidsRecord(
                external_id=f"ext-{i}",
                month="115.05",
                child_name=f"IK{i}",
                grade="幼兒",
                address="臺北市內湖區成功路四段100巷",
                geocoding_consent_at=None,
            ))
        s.add(RecruitmentGeocodeCache(
            address="臺北市內湖區成功路四段100巷",
            lat=25.08, lng=121.59, status="resolved",
            district="內湖區", resolved_at=datetime.utcnow(),
        ))

    resp = admin_client.get("/api/recruitment/address-hotspots")
    body = resp.json()
    buckets = body.get("buckets", [])
    # 5 visit 全無 consent → bucket 不出
    assert all(b.get("district") != "內湖區" for b in buckets)
    residual = body.get("district_residual_visits", {})
    assert residual.get("內湖區", 0) == 0  # NULL consent 連 residual 都不進


def test_ivykids_records_included_when_consent_set(use_test_db, admin_client) -> None:
    """如 admin 手動補 consent → ivykids 進 heatmap；同時驗 truncate 對 ivykids 也生效
    (advisor catch: 用 full address 5 個不同門牌號才能驗真實 truncate)"""
    with session_scope() as s:
        # 5 筆 ivykids 同巷不同號 + consent set
        for i in range(5):
            s.add(RecruitmentIvykidsRecord(
                external_id=f"ext-{i}",
                month="115.05",
                child_name=f"IK{i}",
                grade="幼兒",
                address=f"臺北市內湖區成功路四段100巷{i+1}號",  # 5 個不同 full address
                geocoding_consent_at=datetime.utcnow(),
            ))
        s.add(RecruitmentGeocodeCache(
            address="臺北市內湖區成功路四段100巷",  # cache key = truncated
            lat=25.08, lng=121.59, status="resolved",
            district="內湖區", resolved_at=datetime.utcnow(),
        ))

    resp = admin_client.get("/api/recruitment/address-hotspots")
    body = resp.json()
    buckets = body.get("buckets", [])
    # 5 筆全 truncate 到同一巷 + cache join → 應 1 個 bucket visit_count=5
    inhu_buckets = [b for b in buckets if b.get("district") == "內湖區"]
    assert len(inhu_buckets) == 1, \
        f"truncate 失效：應 1 bucket 實際 {len(inhu_buckets)}：{inhu_buckets}"
    assert inhu_buckets[0]["visit_count"] == 5, \
        f"visit_count 不對：{inhu_buckets[0]}"
```

- [ ] **Step 3: 跑測試驗證 fail**

Run: `pytest tests/test_hotspots_ivykids_exclusion.py -v`
Expected: FAIL — ivykids visit 完全沒進 pipeline（既有 query 只 join RecruitmentVisit）

- [ ] **Step 4: hotspots query 加 ivykids UNION**

修改 `_build_buckets_response`（Task 9）內，把 RecruitmentVisit + RecruitmentIvykidsRecord union 起來：

```python
def _build_buckets_response(session, dataset_scope=None) -> dict:
    K = max(2, min(10, settings.recruitment.k_anonymity_threshold))

    # 共用 truncate expression
    dialect = session.bind.dialect.name
    def _truncated(table):
        if dialect == "postgresql":
            return sa.func.regexp_replace(
                sa.func.regexp_replace(
                    sa.func.trim(table.address), r'\d+號(?:之\d+)?.*$', ''
                ), r'\d+弄.*$', ''
            )
        return sa.func.trim(table.address)

    # visit subquery (consent gate)
    visit_sub = session.query(
        _truncated(RecruitmentVisit).label("addr"),
        sa.func.count(RecruitmentVisit.id).label("v"),
        sa.func.sum(case((RecruitmentVisit.has_deposit == True, 1), else_=0)).label("d"),
    ).filter(RecruitmentVisit.geocoding_consent_at.isnot(None)).group_by(_truncated(RecruitmentVisit)).subquery()

    # ivykids subquery (consent gate — 預設 NULL 全 filter 掉)
    ivy_sub = session.query(
        _truncated(RecruitmentIvykidsRecord).label("addr"),
        sa.func.count(RecruitmentIvykidsRecord.id).label("v"),
        sa.func.sum(case((RecruitmentIvykidsRecord.has_deposit == True, 1), else_=0)).label("d"),
    ).filter(RecruitmentIvykidsRecord.geocoding_consent_at.isnot(None)).group_by(_truncated(RecruitmentIvykidsRecord)).subquery()

    # UNION ALL（同址兩來源累加；之後再按 cache lat/lng 分 grid）
    from sqlalchemy import select, union_all
    union_q = union_all(
        select(visit_sub.c.addr, visit_sub.c.v, visit_sub.c.d),
        select(ivy_sub.c.addr, ivy_sub.c.v, ivy_sub.c.d),
    ).subquery()

    # JOIN cache 取座標 + grid round（SQLite 路徑需 Python 端 truncate 後重 group）
    if dialect == "postgresql":
        rows = (
            session.query(
                sa.func.round(RecruitmentGeocodeCache.lat * 1000) / 1000,
                sa.func.round(RecruitmentGeocodeCache.lng * 1000) / 1000,
                RecruitmentGeocodeCache.district.label("cache_district"),
                sa.func.sum(union_q.c.v).label("visit_count"),
                sa.func.sum(union_q.c.d).label("deposit_count"),
                sa.func.avg(RecruitmentGeocodeCache.lat).label("center_lat"),
                sa.func.avg(RecruitmentGeocodeCache.lng).label("center_lng"),
            )
            .join(RecruitmentGeocodeCache, RecruitmentGeocodeCache.address == union_q.c.addr)
            .filter(
                RecruitmentGeocodeCache.lat.isnot(None),
                RecruitmentGeocodeCache.lng.isnot(None),
            )
            .group_by(
                sa.func.round(RecruitmentGeocodeCache.lat * 1000),
                sa.func.round(RecruitmentGeocodeCache.lng * 1000),
                RecruitmentGeocodeCache.district,
            )
            .all()
        )
    else:
        # SQLite：先 union raw（含 trim only address），Python truncate 後 join cache + group
        from collections import defaultdict
        raw_rows = session.execute(union_q.select()).all()  # [(addr, v, d), ...]

        # Python truncate
        truncated_acc: dict[str, dict] = defaultdict(lambda: {"v": 0, "d": 0})
        for raw_addr, v, d in raw_rows:
            real_truncated = truncate_address_to_lane(raw_addr or "")
            if not real_truncated:
                continue
            truncated_acc[real_truncated]["v"] += v or 0
            truncated_acc[real_truncated]["d"] += d or 0

        # JOIN cache by truncated key
        cache_map = {
            c.address: c for c in session.query(RecruitmentGeocodeCache).filter(
                RecruitmentGeocodeCache.address.in_(list(truncated_acc.keys())),
                RecruitmentGeocodeCache.lat.isnot(None),
                RecruitmentGeocodeCache.lng.isnot(None),
            ).all()
        }
        # Grid + aggregate
        grid_acc: dict[tuple, dict] = defaultdict(
            lambda: {"v": 0, "d": 0, "lat_sum": 0.0, "lng_sum": 0.0, "n": 0, "district": ""}
        )
        for truncated_addr, acc in truncated_acc.items():
            c = cache_map.get(truncated_addr)
            if not c:
                continue
            grid_key = (round(c.lat * 1000) / 1000, round(c.lng * 1000) / 1000, c.district or "")
            g = grid_acc[grid_key]
            g["v"] += acc["v"]
            g["d"] += acc["d"]
            g["lat_sum"] += c.lat
            g["lng_sum"] += c.lng
            g["n"] += 1
            g["district"] = c.district or g["district"]
        # 重組 row.proxy
        from types import SimpleNamespace
        rows = [
            SimpleNamespace(
                cache_district=g["district"],
                visit_count=g["v"],
                deposit_count=g["d"],
                center_lat=g["lat_sum"] / g["n"],
                center_lng=g["lng_sum"] / g["n"],
            )
            for g in grid_acc.values()
        ]

    # K=5 suppression 與 response shape
    K = max(2, min(10, settings.recruitment.k_anonymity_threshold))
    buckets = []
    residual: dict[str, int] = {}
    for row in rows:
        visit_count = int(row.visit_count or 0)
        district = (row.cache_district or "未填寫").strip() or "未填寫"
        if visit_count >= K:
            buckets.append({
                "center_lat": float(row.center_lat),
                "center_lng": float(row.center_lng),
                "district": district,
                "visit_count": visit_count,
                "deposit_count": int(row.deposit_count or 0),
            })
        else:
            residual[district] = residual.get(district, 0) + visit_count
    return {"buckets": buckets, "district_residual_visits": residual}
```

- [ ] **Step 5: 跑測試驗證 pass**

Run: `pytest tests/test_hotspots_ivykids_exclusion.py -v`
Expected: 2 passed

Run: `pytest tests/test_hotspots_k_anonymity.py -v`
Expected: 0 regression

- [ ] **Step 6: Commit**

```bash
git add api/recruitment/hotspots.py tests/test_hotspots_ivykids_exclusion.py
git commit -m "feat(hotspots): RecruitmentIvykidsRecord 同 hotspot pipeline + consent gate

UNION RecruitmentVisit ∪ RecruitmentIvykidsRecord 兩來源；
兩邊均 filter consent_at IS NOT NULL。

業主決議：ivykids record 因外部來源無 consent 證據預設 NULL，
未來如需 admin 手動補錄 consent 流程為 follow-up。"
```

---

## Task 12: BE OpenAPI dump → FE codegen

**Files:**
- Run: BE `scripts/dump_openapi.py`
- Run: FE `npm run gen:api`
- Verify: `ivy-frontend/src/api/_generated/schema.d.ts` 含新 `geocoding_consent` field + `buckets` response shape

- [ ] **Step 1: BE OpenAPI dump**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-pii-2026-05-28
python scripts/dump_openapi.py
```

Expected: `openapi.json` 產出於 worktree 根目錄

- [ ] **Step 2: FE codegen**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 變動

- [ ] **Step 3: 驗 schema diff 含 consent + buckets**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git diff src/api/_generated/schema.d.ts | head -100
```

Expected: 含 `geocoding_consent: boolean` in `RecruitmentVisitCreate`/`Update`、含 `buckets`/`district_residual_visits` in `/recruitment/address-hotspots` response

- [ ] **Step 4: Commit FE schema.d.ts only**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git checkout -b feat/recruitment-address-pii-2026-05-28-frontend origin/main
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema.d.ts for recruitment consent + bucket aggregation

對應後端 PR feat/recruitment-address-pii-2026-05-28-backend"
```

---

## Task 13: FE `RecruitmentAddressHeatmap.vue` maxZoom 14 + bucket marker + popup mask

**Files:**
- Modify: `ivy-frontend/src/components/recruitment/RecruitmentAddressHeatmap.vue`
- Create: `ivy-frontend/src/components/recruitment/__tests__/RecruitmentAddressHeatmap.spec.ts`

- [ ] **Step 1: 寫 failing vitest**

```typescript
// src/components/recruitment/__tests__/RecruitmentAddressHeatmap.spec.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import RecruitmentAddressHeatmap from '../RecruitmentAddressHeatmap.vue'

vi.mock('leaflet', () => ({
  default: {
    map: vi.fn(() => ({
      setView: vi.fn(),
      setZoom: vi.fn(),
      addLayer: vi.fn(),
      removeLayer: vi.fn(),
    })),
    tileLayer: vi.fn(() => ({ addTo: vi.fn() })),
    circleMarker: vi.fn(() => ({ addTo: vi.fn(), bindPopup: vi.fn(() => ({ openPopup: vi.fn() })) })),
    marker: vi.fn(() => ({ addTo: vi.fn(), bindPopup: vi.fn() })),
    divIcon: vi.fn(),
  }
}))

describe('RecruitmentAddressHeatmap', () => {
  it('maxZoom is 14 (not 19)', async () => {
    const L = (await import('leaflet')).default
    mount(RecruitmentAddressHeatmap, {
      props: {
        buckets: [],
        districtResidualVisits: {},
        campusLat: 25, campusLng: 121,
      }
    })
    // tileLayer called with maxZoom: 14
    const tileLayerCall = (L.tileLayer as any).mock.calls.find((c: any[]) =>
      c[1]?.maxZoom !== undefined
    )
    expect(tileLayerCall[1].maxZoom).toBe(14)
  })

  it('renders marker only when visit_count >= K=5', async () => {
    const L = (await import('leaflet')).default
    mount(RecruitmentAddressHeatmap, {
      props: {
        buckets: [
          { center_lat: 25.0, center_lng: 121.5, district: '文山區', visit_count: 4, deposit_count: 1 },  // 4 < 5
          { center_lat: 25.1, center_lng: 121.6, district: '中正區', visit_count: 8, deposit_count: 3 },  // 8 >= 5
        ],
        districtResidualVisits: {},
        campusLat: 25, campusLng: 121,
      }
    })
    // circleMarker 應只被叫 1 次（visit_count=8 那個）
    expect(L.circleMarker).toHaveBeenCalledTimes(1)
    expect((L.circleMarker as any).mock.calls[0][0]).toEqual([25.1, 121.6])
  })

  it('popup hides formatted_address, shows visit/deposit count', async () => {
    const L = (await import('leaflet')).default
    mount(RecruitmentAddressHeatmap, {
      props: {
        buckets: [
          { center_lat: 25.1, center_lng: 121.6, district: '中正區', visit_count: 8, deposit_count: 3 },
        ],
        districtResidualVisits: {},
        campusLat: 25, campusLng: 121,
      }
    })
    const popupCall = (L.circleMarker as any).mock.results[0].value.bindPopup.mock.calls[0][0]
    expect(popupCall).not.toContain('formatted_address')
    expect(popupCall).toContain('8')  // visit_count
    expect(popupCall).toContain('3')  // deposit_count
  })
})
```

- [ ] **Step 2: 跑 vitest 驗 fail**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm test -- RecruitmentAddressHeatmap
```

Expected: FAIL

- [ ] **Step 3: 修 `RecruitmentAddressHeatmap.vue`**

定位 `maxZoom: 19`（line 1087）改 14。

定位 per-visit marker loop（line 1128 附近）改成從 props.buckets 讀，每 bucket 一個 circleMarker：

```vue
<script setup lang="ts">
defineProps<{
  buckets: Array<{
    center_lat: number
    center_lng: number
    district: string
    visit_count: number
    deposit_count: number
  }>
  districtResidualVisits: Record<string, number>
  campusLat: number
  campusLng: number
  // ... 既有 props 保留
}>()

// renderBuckets() — 取代既有 hotspot loop
function renderBuckets() {
  if (!mapInstance || !leafletApi) return
  // 清除既有 markers
  bucketMarkers.forEach(m => mapInstance.removeLayer(m))
  bucketMarkers = []
  for (const b of props.buckets) {
    const marker = leafletApi.circleMarker([b.center_lat, b.center_lng], {
      radius: Math.min(20, 4 + b.visit_count),
      fillColor: b.deposit_count > 0 ? '#10b981' : '#3b82f6',
      color: '#fff', weight: 1, fillOpacity: 0.6,
    })
    marker.bindPopup(
      `<div class="map-popup">
        <strong>${escapeHtml(b.district)}</strong><br/>
        <span>本街區共 ${b.visit_count} 筆 visit / ${b.deposit_count} 筆 deposit</span>
      </div>`
    )
    marker.addTo(mapInstance)
    bucketMarkers.push(marker)
  }
}
</script>
```

tileLayer 加 maxZoom:

```vue
leafletApi.tileLayer(tileUrl, { maxZoom: 14, attribution: '...' }).addTo(mapInstance)
```

- [ ] **Step 4: 跑 vitest 驗 pass**

```bash
npm test -- RecruitmentAddressHeatmap
```

Expected: 3 passed

- [ ] **Step 5: 跑 typecheck + build**

```bash
npm run typecheck
npm run build
```

Expected: 0 error

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/components/recruitment/RecruitmentAddressHeatmap.vue \
       src/components/recruitment/__tests__/RecruitmentAddressHeatmap.spec.ts
git commit -m "feat(heatmap): maxZoom 14 + bucket marker + popup mask

- Leaflet maxZoom 19→14（街區級不可定位個別住家）
- per-visit circleMarker → per bucket（後端聚合 100m grid + K=5 suppression）
- popup 移除 formatted_address，改顯 district + visit/deposit count

PII 合規對應 BE feat/recruitment-address-pii-2026-05-28-backend"
```

---

## Task 14: FE `RecruitmentRecordDialog.vue` consent checkbox + el-alert + 預設 false

**Files:**
- Modify: `ivy-frontend/src/components/recruitment/RecruitmentRecordDialog.vue`
- Create/Modify: vitest

- [ ] **Step 1: 寫 failing vitest**

```typescript
// src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ElCheckbox, ElAlert } from 'element-plus'
import RecruitmentRecordDialog from '../RecruitmentRecordDialog.vue'

describe('RecruitmentRecordDialog consent', () => {
  it('consent checkbox defaults to false (explicit attestation)', () => {
    const wrapper = mount(RecruitmentRecordDialog, {
      props: { open: true, mode: 'create' },
      global: { plugins: [/* ElementPlus */] }
    })
    const cb = wrapper.findComponent(ElCheckbox)
    expect(cb.props('modelValue')).toBe(false)
  })

  it('shows el-alert when consent not granted', () => {
    const wrapper = mount(RecruitmentRecordDialog, {
      props: { open: true, mode: 'create' },
    })
    expect(wrapper.findComponent(ElAlert).exists()).toBe(true)
  })

  it('emits payload with geocoding_consent on submit', async () => {
    const wrapper = mount(RecruitmentRecordDialog, {
      props: { open: true, mode: 'create' },
    })
    // 勾選 consent
    await wrapper.findComponent(ElCheckbox).setValue(true)
    // 填必要欄並 submit
    await wrapper.find('[data-test="month-input"]').setValue('115.05')
    await wrapper.find('[data-test="child-name-input"]').setValue('TC')
    await wrapper.find('[data-test="submit"]').trigger('click')

    const emitted = wrapper.emitted('submit')
    expect(emitted).toBeTruthy()
    expect(emitted![0][0]).toMatchObject({ geocoding_consent: true })
  })
})
```

- [ ] **Step 2: 跑 vitest 驗 fail**

```bash
npm test -- RecruitmentRecordDialog
```

Expected: FAIL

- [ ] **Step 3: 修 `RecruitmentRecordDialog.vue`**

加 consent state 與 UI：

```vue
<script setup lang="ts">
import { ref } from 'vue'

const form = ref({
  // ... 既有 fields ...
  geocoding_consent: false,
})

function submit() {
  emit('submit', { ...form.value })
}
</script>

<template>
  <el-dialog>
    <!-- 既有 fields -->

    <el-form-item label="家長同意">
      <el-checkbox v-model="form.geocoding_consent">
        家長已口頭同意以本住址進行招生區位分析（送至 Google Maps）— <strong>需明確確認</strong>
      </el-checkbox>
      <div class="form-hint">
        <strong>預設不勾</strong>。招生人員需明確確認家長口頭同意後手動勾選；
        勾選即代表 attestation 責任歸招生人員。家長可隨時於家長端 DSR opt-out 撤回。
      </div>
    </el-form-item>

    <el-alert
      v-if="!form.geocoding_consent"
      type="info"
      :closable="false"
      title="未勾選同意 → 本筆 visit 不會進入招生 heatmap 區位分析"
    />
  </el-dialog>
</template>
```

- [ ] **Step 4: 跑 vitest + typecheck + build**

```bash
npm test -- RecruitmentRecordDialog
npm run typecheck
npm run build
```

Expected: 全綠

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/components/recruitment/RecruitmentRecordDialog.vue \
       src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts
git commit -m "feat(record-dialog): consent checkbox 預設 false + el-alert 提醒

業主決議 explicit attestation — 預設不勾，招生人員需明確確認後手動勾選。
未勾顯示 el-alert 提醒「本筆 visit 不進 heatmap」"
```

---

## Task 15: Sanity check + 兩 repo 推送準備

- [ ] **Step 1: BE 全 pytest 跑**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-pii-2026-05-28
pytest -x -v 2>&1 | tail -30
```

Expected: 既有 pytest 0 regression（含新 ~16 個 test 全綠）

- [ ] **Step 2: FE vitest + typecheck + build**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm test
npm run typecheck
npm run build
```

Expected: vitest 全綠（含新 6 test），typecheck 0 error，build success

- [ ] **Step 3: OpenAPI drift check（CI gate 預先驗）**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run gen:api:check
```

Expected: 0 diff（已 commit）

- [ ] **Step 4: alembic single head 驗**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-pii-2026-05-28
alembic heads
```

Expected: `rcrgeoconsent01 (head)`

- [ ] **Step 5: Manual smoke checklist**

1. `start.sh` 起兩端 dev server
2. login 招生角色 → 新增訪視 → **不勾 consent** → 確認該 visit 創建後 `geocoding_consent_at IS NULL`
3. 開既有 admin 帳號 → 招生 → heatmap → 確認 grandfather 既有 row 仍顯示 marker
4. 連續新增 4 筆同址（X 路 100 巷 1/2/3/4 號）勾 consent → 確認 heatmap **不出 marker**（< K=5）
5. 加第 5 筆 → 確認 marker 出現
6. PUT records/{id} consent=false → 確認 cache 該 row 不刪（DSR cascade 是 follow-up）但 hotspot pipeline 排除該筆
7. 跑 `python -c "from services.security_gc_scheduler import _gc_recruitment_geocode_cache; from models.base import session_scope; \n_=session_scope().__enter__(); print(_gc_recruitment_geocode_cache(_))"` 確認 GC 不噴錯

- [ ] **Step 6: 兩 repo push（待 user 確認）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-pii-2026-05-28
git push -u origin feat/recruitment-address-pii-2026-05-28-backend

cd /Users/yilunwu/Desktop/ivy-frontend
git push -u origin feat/recruitment-address-pii-2026-05-28-frontend
```

- [ ] **Step 7: 開 PR（待 user 確認）**

```bash
gh pr create --title "feat: 招生地址 PII 降精度 + consent + cache TTL" \
  --body "..." --base main
```

---

## 風險與緩解（per spec §9）

實作期間若遇：

1. **既有 hotspot test 在 SQLite 環境因 dialect-aware truncate 出 regression** — Task 8 Python-side fallback 已處理；測試環境 fixture `use_test_db` 預期是 SQLite
2. **alembic 多 head（user 並行 push 新 migration）** — Task 4 跑前 `alembic heads` 確認；若多 head 加 merge migration
3. **OpenAPI codegen alphabetize order 問題** — per memory `project_approval_status_enum_p1_2026_05_26` Task 4: 用 `--alphabetize` flag
4. **subagent 跑 BE Edit 觸發 black hook 全檔重排** — per memory `feedback_subagent_posttooluse_black_hook`：用 `bash python3 string.replace` 繞過

---

## Spec coverage self-review

| Spec section | Task |
|--------------|------|
| §4.1 `truncate_address_to_lane` | Task 1, 2 |
| §4.2 hotspots GROUP BY + ON CONFLICT + bucket aggregation + K suppression | Task 8, 9 |
| §4.3 scheduler 90d GC | Task 10 |
| §4.4 alembic `rcrgeoconsent01` | Task 4 |
| §4.5 Pydantic schema + endpoint behaviour | Task 6, 7 |
| §4.6 DSR cascade | **Deferred**（follow-up PR after P0c-2 merge） |
| §4.7 RecruitmentIvykidsRecord 同步處理 | Task 5（model）, Task 11（pipeline） |
| §5.1 heatmap UI | Task 13 |
| §5.2 visit form consent | Task 14 |
| §5.3 OpenAPI codegen | Task 12 |
| §6.1 BE pytest | Task 1, 2, 3, 5, 6, 7, 8, 9, 10, 11 |
| §6.2 FE vitest | Task 13, 14 |
| §6.3 Manual smoke | Task 15 Step 5 |
| §7 deployment notes | Task 4, 12, 15 |

Spec 全覆蓋 ✓（§4.6 列 deferred 為 known scope reduction）。
