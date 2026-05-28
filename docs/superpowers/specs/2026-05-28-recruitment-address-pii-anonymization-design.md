# 招生地址 PII 降精度 + 同意 + cache TTL — 設計規格

**Topic**: Sub-PR B（P0 sprint 第五輪 — 兒童行蹤合規）
**Date**: 2026-05-28
**Status**: Draft（待 user review）
**Scope**: BE (ivy-backend) + FE (ivy-frontend) cross-repo
**Compliance**: 個資法 §8（告知）／§9（敏感資料蒐集）／§19（蒐集範圍必要性）；兒童行蹤高敏

---

## 1. 問題陳述

第五輪 P0 audit finding 第 2 條（兒童行蹤可被反查）：

- `services/geocoding_service.py` 的 `_strip_floor_suffix` 僅去樓號，**保留門牌號**
- `services/recruitment_market_intelligence.py` / `api/recruitment/hotspots.py` 將家長填寫的**完整住宅地址**送 Google Geocoding API
- 前端 `RecruitmentAddressHeatmap.vue` 預設 `maxZoom: 19`、`panMapTo` setZoom 16 = 住宅級可定位
- 每筆 visit 用 `L.circleMarker` 個別 render，popup 顯示原始 `formatted_address`
- `RecruitmentGeocodeCache` 永久保存座標，無 retention
- `RecruitmentVisit` 無 consent 欄位 — 跨境送 Google 未告知未同意

**違規點**：
1. §8 告知同意 — 跨境傳輸 Google 未告知
2. §9 敏感資料 — 兒童住址屬高敏，蒐集需特別告知
3. §19 範圍必要性 — 招生分析不需「住宅門牌」精度

**量化內部洩漏面**：具 `RECRUITMENT_VIEW` 權限的所有招生人員、admin 皆可瀏覽 heatmap，視覺上可定位他人小孩住家。

---

## 2. 設計目標

| ID | Goal | 驗證方式 |
|----|------|--------|
| G1 | 送 Google API 的地址降到「巷」級（去門牌號） | 單元測試 `test_truncate_to_lane` 涵蓋 11 種變體 |
| G2 | Heatmap UI 無法定位個別住家 | Frontend `maxZoom = 14`；marker per bucket；popup mask formatted_address |
| G3 | 低密度 bucket（< K=5 筆 visit）不出 marker | k-anonymity suppression unit test |
| G4 | 招生人員為每筆 visit 留下家長同意 attestation 紀錄 | `RecruitmentVisit.geocoding_consent_at` 欄；form checkbox |
| G5 | 既有 visit 不被打擾且 heatmap 不空白 | grandfather migration（`consent_at = created_at`） |
| G6 | Cache 90d 自動 GC | scheduler step + 單元測試 |
| G7 | 家長 DSR opt-out 連動清除 consent + cache 對應 row | `parent_portal/opt_out` 行為驗證 |

---

## 3. 採行的決策（brainstorming 階段確認）

| 決策 | 選擇 | 替代方案 | 棄用理由 |
|------|------|---------|-------|
| 隱私強度 | **Strict** | Pragmatic / Burn | 兒童行蹤高敏需 audit 完整建議 |
| 既有 row 處理 | **Grandfather**（consent_at = created_at） | Backfill NULL / Wipe | 業主不希望 heatmap 一夜變空 |
| 聚合策略 | **B 方案：server-side 100m grid + 移除 individual marker** | A 重 H3 hex / C 僅 zoom 限制 | A bundle 太重、C 隱私不足 |
| K-anonymity threshold | **K=5 預設 + config-driven**（業主偏好較保守，超越 GDPR K=3 慣例） | K=3 / K=2 / hardcoded | 業主決議：低密度區 marker 變少可接受、district 背景數仍能看趨勢；K=2 統計意義弱；config 化讓業主可調 2–10 |

---

## 4. 後端變動

### 4.1 `services/geocoding_service.py`

新增公開純函式 `truncate_address_to_lane(address: str) -> str`：

```python
def truncate_address_to_lane(address: str) -> str:
    """招生地址 PII 降精度：去樓號 + 去門牌號（\d+號|\d+弄），保留「\d+巷」級。

    例：
      臺北市文山區興隆路四段30巷5號3樓  → 臺北市文山區興隆路四段30巷
      臺北市信義區忠孝東路五段100號     → 臺北市信義區忠孝東路五段
      臺北市中正區重慶南路一段122號之2  → 臺北市中正區重慶南路一段
      新北市板橋區文化路一段188巷5弄8號 → 新北市板橋區文化路一段188巷
    """
    s = _strip_floor_suffix(address or "")
    # 先剃號（含 \d+號之\d+）
    s = re.sub(r"\d+號(?:之\d+)?.*$", "", s).strip()
    # 再剃弄（不剃巷）
    s = re.sub(r"\d+弄.*$", "", s).strip()
    return s
```

**為何不重用 `_simplify_road_segment`**：後者把「\d+巷」也剃掉，違反「保留巷級」目的。新函式獨立命名避免 caller 誤用。

修改點：
- `_geocode_with_google`（line 155）：呼叫 `requests.get` 前先 `address = truncate_address_to_lane(address)`
- `_geocode_with_nominatim`（line 202）：同前
- `_build_nominatim_query_candidates` 仍保留原 fallback 候選邏輯，但 input 已 truncated

### 4.2 `api/recruitment/hotspots.py`

**`_query_address_hotspots` 上游改 GROUP BY truncated**（解決 cache UNIQUE conflict）：

```python
truncated_address = func.coalesce(
    sa.func.regexp_replace(  # PG-only; SQLite fallback 走 Python 端 truncate（測試）
        sa.func.regexp_replace(RecruitmentVisit.address, r'\d+號(?:之\d+)?.*$', ''),
        r'\d+弄.*$', ''
    ),
    RecruitmentVisit.address,
).label("truncated_address")
```

- SQLite 測試環境：用 SQLAlchemy hybrid `expression`（dialect-aware），或讀回 Python 端後再 `truncate_address_to_lane`
- 後續所有 `address` 出現皆改 truncated

**`sync_recruitment_address_hotspots` cache insert**：

```python
# 改成 SELECT-then-INSERT 防多 visit 落在同一巷導致 IntegrityError
existing = session.query(RecruitmentGeocodeCache).filter_by(
    address=hotspot["address"]  # 已是 truncated
).one_or_none()
if not existing:
    existing = RecruitmentGeocodeCache(address=hotspot["address"])
    session.add(existing)
```

**新增 bucket aggregation endpoint logic**（同個 `/api/recruitment/address-hotspots`，response 結構微調）：

```python
buckets_query = (
    session.query(
        func.round(RecruitmentGeocodeCache.lat * 1000) / 1000,  # 3 decimal
        func.round(RecruitmentGeocodeCache.lng * 1000) / 1000,
        RecruitmentGeocodeCache.district,
        func.sum(visit_subq.visit).label("visit_count"),
        func.sum(visit_subq.deposit).label("deposit_count"),
        func.avg(RecruitmentGeocodeCache.lat).label("center_lat"),  # 密度加權中心
        func.avg(RecruitmentGeocodeCache.lng).label("center_lng"),
    )
    .join(visit_subq, ...)
    .group_by(...)
)

# k-anonymity K suppression — K 從 config 讀，預設 5（業主決議比 GDPR K=3 更保守），clamp [2, 10]（不允許 K=1）
K = max(2, min(10, settings.recruitment.k_anonymity_threshold))
buckets_rendered = [b for b in buckets if b.visit_count >= K]
suppressed_by_district = defaultdict(int)
for b in buckets:
    if b.visit_count < K:
        suppressed_by_district[b.district] += b.visit_count
```

response schema：

```json
{
  "buckets": [
    {
      "center_lat": 25.014,
      "center_lng": 121.567,
      "district": "文山區",
      "visit_count": 8,
      "deposit_count": 3
    }
  ],
  "district_residual_visits": {
    "文山區": 4,
    "中正區": 1
  }
}
```

### 4.3 `services/security_gc_scheduler.py` 新增 step

```python
async def _gc_recruitment_geocode_cache(session):
    """GC RecruitmentGeocodeCache rows older than 90 days from resolved_at."""
    cutoff = datetime.now(TAIPEI_TZ) - timedelta(days=90)
    deleted = session.query(RecruitmentGeocodeCache).filter(
        RecruitmentGeocodeCache.resolved_at < cutoff
    ).delete(synchronize_session=False)
    logger.info("recruitment_geocode_cache GC removed %d rows", deleted)
```

接入既有 scheduler tick loop，每日跑一次（同 jwt_blocklist GC 節奏）。

### 4.4 Alembic migration（revision id 建議 `rcrgeoconsent01`）

```python
def upgrade():
    op.add_column(
        "recruitment_visits",
        sa.Column("geocoding_consent_at", sa.DateTime(), nullable=True),
    )
    # Grandfather：既有 row 視為已同意（avoid heatmap blank-out）
    op.execute(
        "UPDATE recruitment_visits "
        "SET geocoding_consent_at = created_at "
        "WHERE geocoding_consent_at IS NULL"
    )
    # 清空 cache（下次 sync 以 truncated key 重灌；operational note: ~200 Google API call 預算）
    op.execute("DELETE FROM recruitment_geocode_cache")


def downgrade():
    op.drop_column("recruitment_visits", "geocoding_consent_at")
```

**Single head check**：跑前 `alembic heads`，需與最新 head 鏈接（如有 mergeheads conflict，新增 merge migration）。

### 4.5 Pydantic schemas + endpoint behaviour

- `schemas/recruitment.py`：`RecruitmentVisitCreate` / `RecruitmentVisitUpdate` 加 `geocoding_consent: bool = False`（**預設不勾** — 招生人員 explicit attestation 責任）
- `api/recruitment/visits.py` POST / PUT：
  - `geocoding_consent=True` → 寫 `geocoding_consent_at = now_taipei_naive()`
  - `geocoding_consent=False` → 寫 NULL，並 audit log 記錄
- `_query_address_hotspots` 加 filter：`WHERE geocoding_consent_at IS NOT NULL`

### 4.6 DSR opt-out cascade（接 P0c-2 既有 endpoint）

**現實限制**：`RecruitmentVisit` 無 FK 到 `Guardian` / `Student`，無法 join 取出「該家長底下所有 visit」。match 路徑：

1. **Primary path（best-effort fuzzy）**：以 `(child_name, birthday)` 為自然鍵 match `RecruitmentVisit` rows（生日 + 童名同時相符視為同一兒童）
2. **Fallback**：若 visit row 無 `birthday`，僅以 `child_name + phone` match — 標 `match_confidence="low"` 進 audit log

```python
# api/parent_portal/opt_out.py 內延伸
def _cascade_recruitment_consent(session, guardian, opt_out_reason):
    # 從 guardian → student 取 child_name + birthday
    for student in guardian.students:
        candidates = session.query(RecruitmentVisit).filter(
            RecruitmentVisit.child_name == student.name,
            (RecruitmentVisit.birthday == student.birthday) |
            (RecruitmentVisit.birthday.is_(None)),
        ).all()
        for visit in candidates:
            if visit.geocoding_consent_at is None:
                continue  # already revoked
            old_consent = visit.geocoding_consent_at
            visit.geocoding_consent_at = None
            # 刪該住址 truncated cache row（若該 lane 不再有任何 active visit）
            truncated = truncate_address_to_lane(visit.address)
            other_active = session.query(RecruitmentVisit).filter(
                RecruitmentVisit.address == visit.address,
                RecruitmentVisit.geocoding_consent_at.isnot(None),
                RecruitmentVisit.id != visit.id,
            ).first()
            if not other_active:
                session.query(RecruitmentGeocodeCache).filter_by(
                    address=truncated
                ).delete()
            # audit log
            log_audit(
                event_type="recruitment_consent_revoked_dsr",
                target_type="recruitment_visit",
                target_id=visit.id,
                details={
                    "old_consent_at": old_consent.isoformat(),
                    "truncated_address": truncated,
                    "cascade_cache_deleted": not bool(other_active),
                    "match_confidence": "high" if visit.birthday else "low",
                },
            )
```

**邊界**：若 visit 屬於 guardian 已不見的 historical record（家長已離開系統），DSR 無法主動 cascade — 仰賴 cache 90d TTL 自然 GC。spec §8 列為 known gap。

### 4.7 `RecruitmentIvykidsRecord` 同步處理（業主決議納入本 PR）

義華校官網同步 (`recruitment_ivykids_records`) 同樣有 `address` 欄位，且透過獨立 sync pipeline 進入系統。若不一併處理，將留下 PII 漏洞。

修改點：
1. `RecruitmentIvykidsRecord` 加 `geocoding_consent_at` column（同 alembic migration `rcrgeoconsent01`）
   - **不 grandfather**：義華校 sync 過來的 record 無從推斷家長是否同意；新 record `geocoding_consent_at = NULL`（不上 heatmap）
   - 既有 row 一律 NULL
2. ivykids sync service（`services/recruitment_ivykids_sync.py` 或對應位置）：sync 時固定寫 NULL
3. `_query_address_hotspots` 加 UNION 將 `RecruitmentIvykidsRecord` 也納入 aggregate，但 filter `WHERE geocoding_consent_at IS NOT NULL`（NULL 全 filter 掉，等同不上 heatmap）
4. admin tooling：未來可加「ivykids → 本園 visit」轉換時的 consent 補錄（**follow-up，本 PR 不做**）
5. test 新增：`test_ivykids_records_excluded_from_heatmap_by_default`

**業主決議**：ivykids record 因來源無 consent 證據，預設不上 heatmap；如業主需要 ivykids 資料進 heatmap，需後續開發「轉本園 visit + 補 consent」流程。本 PR 把資料安全鎖死即可。

---

## 5. 前端變動

### 5.1 `src/components/recruitment/RecruitmentAddressHeatmap.vue`

| 行為 | 原 | 新 |
|------|-----|-----|
| Leaflet `maxZoom` | 19 | 14 |
| Marker | per address `L.circleMarker(hotspot.lat, hotspot.lng)` | per bucket `L.circleMarker(bucket.center_lat, bucket.center_lng)` |
| Popup | `formatted_address` + visit/deposit | 「本街區共 N 筆 visit / M 筆 deposit」(不顯地址) |
| `< K=5` bucket | render | 不 render；可選 chloropleth district 底色顯 `district_residual_visits`（業主決議比 GDPR K=3 更保守） |
| `panMapTo(school)` `setZoom(16)` | 保留（學校公開資料） | 不變 |

### 5.2 新 visit form（`RecruitmentVisitForm.vue` 等對應元件）

```vue
<el-form-item label="家長同意">
  <el-checkbox v-model="form.geocoding_consent">
    家長已口頭同意以本住址進行招生區位分析（送至 Google Maps）— <strong>需明確確認</strong>
  </el-checkbox>
  <div class="form-hint">
    <strong>預設不勾</strong>。招生人員需明確確認家長口頭同意後手動勾選；勾選即代表 attestation 責任歸招生人員。家長可隨時於家長端 DSR opt-out 撤回。
  </div>
</el-form-item>

<el-alert v-if="!form.geocoding_consent" type="info" :closable="false">
  未勾選同意 → 本筆 visit 不會進入招生 heatmap 區位分析。
</el-alert>
```

預設 `false`（業主決議：招生人員 attestation 責任 explicit；接受 heatmap 缺資料風險）。文案明確標**招生人員 attestation**，非家長自簽 — 屬合規習慣做法且閉環透過家長端 DSR opt-out 可撤回。

### 5.3 OpenAPI typed regen

完成 BE 後跑：

```bash
cd ivy-backend && python scripts/dump_openapi.py
cd ivy-frontend && npm run gen:api
```

確認 `RecruitmentVisitCreate.geocoding_consent` + `/address-hotspots` 新 response shape 有反映。

---

## 6. 測試計畫

### 6.1 Backend (pytest)

| 測試 | 目的 |
|------|------|
| `test_truncate_to_lane_strips_door_number` | 巷+號 → 巷 |
| `test_truncate_to_lane_strips_alley` | 巷+弄+號 → 巷 |
| `test_truncate_to_lane_strips_floor` | 樓中樓/B1/之5 → 去乾淨 |
| `test_truncate_to_lane_preserves_lane_only` | 純路（無巷）→ 不動 |
| `test_geocoding_google_uses_truncated` | mock requests，assert query 不含號 |
| `test_geocoding_nominatim_uses_truncated` | 同前 nominatim path |
| `test_hotspots_k_anonymity_suppression` | 3 個 bucket（counts 1/2/5）只 render counts=5 那個；其他進 residual |
| `test_hotspots_cache_dedup_after_truncate` | 同巷 5 visit → cache 只 1 row（ON CONFLICT/SELECT path） |
| `test_consent_default_true_on_create` | POST create → `geocoding_consent_at` not NULL |
| `test_consent_revoke_via_opt_out_clears_cache` | DSR opt-out → consent NULL + cache row 不見 |
| `test_geocode_cache_gc_90d` | scheduler tick 把 91 天 row 刪除 |
| `test_hotspots_filters_null_consent` | consent NULL 的 visit 不進 aggregate |

### 6.2 Frontend (vitest)

| 測試 | 目的 |
|------|------|
| `RecruitmentAddressHeatmap.spec.ts: renders bucket markers only when count >= 3` | mock API 回 K=5/K=5 各 1 bucket，render assert |
| `RecruitmentAddressHeatmap.spec.ts: popup hides formatted_address` | popup text assert |
| `RecruitmentAddressHeatmap.spec.ts: maxZoom is 14` | mapInstance options assert |
| `RecruitmentVisitForm.spec.ts: consent checkbox defaults to true` | mount + checkbox state |
| `RecruitmentVisitForm.spec.ts: unchecking consent sends false to API` | submit form mock axios |

### 6.3 Manual smoke
- `start.sh` 起兩端 → 招生 → 新增 visit 不勾 consent → 確認該 visit 未進 heatmap
- 既有 visit dashboard heatmap 仍顯示（grandfather 生效）
- 把一個 grid 內 visit 數降到 < 3 → 確認 marker 消失

---

## 7. Migration / Deployment 注意

1. **Single head check**：跑前 `alembic heads`，若多 head 須加 merge migration
2. **Operational cost**：DELETE cache 後第一次 heatmap 開啟會觸發 sync → 約 200 個 Google Geocoding API call（單價 $0.005 / call ≈ $1.00 USD），業主可接受
3. **CI matrix**：postgresql 環境 `regexp_replace` 可直接跑；SQLite 環境需走 Python fallback path（測試環境多用 SQLite，需 dialect-aware）
4. **Sentry denylist**：`address`、`formatted_address`、`matched_address` 已在既有 denylist 內，本 PR 不需新增
5. **Audit log**：consent 寫入/清除事件需進 `audit_log`，欄位 `event_type="recruitment_consent_*"`
6. **Config 新增 env var**：`RECRUITMENT_K_ANONYMITY_THRESHOLD=3`（預設 3，clamp [2, 10]）；落在 `config/recruitment.py`（Config Phase 1 已落地的 sub-Settings pattern，sentry denylist 不需新增）

---

## 8. Out of Scope（明列 follow-up）

| 項目 | 為何不在本 PR |
|------|--------------|
| H3 hex polygon 視覺呈現 | server-side grid 已達同等隱私強度；FE bundle/工程量考量 |
| LIFF 家長端 self-consent 流程 | 依賴 P0c LIFF consent modal 基建擴展，需獨立 sprint |
| 政府申報書地址欄處理 | 不送 Google、未上 heatmap，無風險 |
| 既有 `formatted_address` retention | 既有 cache DROP 後一次清空，未來新 entry 90d 自動 GC |
| Existing `RecruitmentVisitForm` 重排版 | 僅加 checkbox + hint 文，UI 大改非本 PR |
| ivykids record 轉本園 visit + 補 consent 流程 | 業主決議：先把資料鎖死，未來再開「補錄 consent」UX |

---

## 9. 風險矩陣

| 風險 | 嚴重度 | 機率 | 緩解 |
|------|------|-----|------|
| Google API 一次性 ~200 call 預算超支 | 低 | 低 | 已估 ~$1，operational note 標明 |
| `regexp_replace` SQLite 不相容 | 中 | 中 | dialect-aware fallback：SQLite 走 Python loop |
| Heatmap 業主第一週看不到 marker（K=5 suppression 較嚴 + consent 預設不勾） | 中 | 高 | district_residual_visits 仍可看趨勢；`RECRUITMENT_K_ANONYMITY_THRESHOLD` env var 可調 [2, 10]；form 加 `el-alert` 提醒招生人員 |
| consent 預設不勾，招生人員忘勾 → heatmap 缺資料且家長已同意但實際未紀錄 | 中 | 高 | 業主決議接受；前端 form `el-alert` 提醒；spec §10 driver smoke 驗；考慮 follow-up：第一週後若 consent 率 < 70% 加 SOP 訓練 |
| Consent 預設 true 被質疑 | 中 | 低 | 文案標 attestation；DSR opt-out 閉環 |
| Migration 多 head | 高 | 中 | 跑前 `alembic heads` 檢查；spec §7.1 明列 |
| 既有家長 LIFF user 看到「opt-out 即可清地圖」覺得突兀 | 低 | 中 | 既有 LIFF consent modal 已落地，behavior 一致 |

---

## 10. 成功標準（merge 前必驗）

- [ ] BE pytest 全綠（含 12 new test，0 regression）
- [ ] FE vitest 全綠（含 5 new test）
- [ ] `npm run typecheck` 0 error，`npm run build` 成功
- [ ] OpenAPI codegen 後 schema.d.ts 無漂移（`npm run gen:api:check` 過）
- [ ] alembic single head check pass
- [ ] manual smoke：新增 visit 不勾 consent → 不上 heatmap
- [ ] 既有 row grandfather 後 heatmap 仍有 marker
- [ ] DSR opt-out 後對應 cache row 消失（DB query 驗）

---

## 11. 參考

- 個資法 §8 / §9 / §19 全文
- P0c-2 DSR endpoints（既有）：`api/parent_portal/opt_out.py` / `_correct.py` / `_export.py`
- P0c-3 LIFF consent modal（既有）：`src/parent/views/ConsentModal.vue`
- audit 原文（第五輪 P0 #2）
- advisor pre-spec catch（k-anonymity K=5 / cache UNIQUE conflict / attestation wording）
- memory：`project_p0_security_sprint_2026_05_28`
