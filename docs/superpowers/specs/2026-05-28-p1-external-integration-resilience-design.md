# P1 外部整合韌性 — 設計規格

**日期**: 2026-05-28
**作者**: Claude (Opus 4.7, 1M context) + user 對齊
**狀態**: Draft for review
**對應 finding**: P1 #13 / #14 / #15 / #16（user 提供之 audit 結果）

## 1. 問題陳述

當前 codebase 對 LINE / Supabase / 政府/Google 外部 API 的失敗處理皆為「fire-and-forget + logger.warning」，缺乏 retry、缺乏 circuit breaker、缺乏 Sentry tag、缺乏 token 健康監測。實際後果：

- **LINE 全站故障 30 分鐘** → 所有假單/加班/接送/家長通知靜默吞掉，復原後無補送
- **LINE 慢回應 5s** → uvicorn worker 卡住主線程（DB pool 僅 5+5），batch approve fan-out 觸發雪崩風險
- **Supabase upload 失敗** → 直接 500 給使用者，無暫存、無 retry
- **LINE token 撤銷** → 401 與 200/500 同樣 return False，無告警

## 2. 範圍

- **In scope**:
  1. Sentry tag + `logger.exception` 涵蓋所有外部整合站點（LINE 7 處 / Supabase 5 處 / 外部 HTTP 6 處 = 18 站）
  2. LINE 推送失敗的持久化 retry 機制（NotificationLog augment + scheduler）
  3. Circuit breaker 純函式（per-process, 不上 Redis）覆蓋三類外呼
  4. Supabase Storage retry + local fallback + pending uploads scheduler
  5. LINE long-lived token 每日 liveness ping（user 已確認 token 類型）
  6. Backend admin `GET /admin/integrations/health` endpoint 暴露三個 breaker / token / pending 數量

- **Out of scope**:
  - 前端後台首頁徽章 UI（**Phase 5 follow-up**，本 spec 只供 endpoint）
  - LINE token rotation hook（long-lived token 不過期；user 已確認）
  - Redis 分散式 breaker state（YAGNI；prod 單 worker 也夠用，多 worker 各自獨立觀察是設計選擇）
  - 引入 `tenacity` / `pybreaker` / `circuitbreaker` 套件（80 行內可自寫，減少 supply chain）
  - Supabase bucket 使用量 >80% 告警（finding #15(c) — defer 到 P2 capacity-watch 議題，與本 spec 韌性議題正交）
  - 外呼 timeout linter rule（finding #14(c) — defer；目前所有 `requests.post/get` 已有 `timeout=` 參數，無此痛點）
  - 群組推送 (`dismissal.created`) retry — v1 不支援（無 NotificationLog row，且下次接送通知會自然覆蓋 UX）

## 3. 分階段交付（一個 spec，四個 PR）

| Phase | 內容 | 預估天數 | 立即效益 |
|-------|------|---------|---------|
| 1 | Sentry tag + `logger.exception` 全外呼站點 + 共用 helper | 1 天 | 量化故障率，為後續決策提供數據 |
| 2 | NotificationLog augment + retry scheduler（救漏發） | 2-3 天 | LINE 故障 30 分鐘也能在恢復後補送 |
| 3 | Circuit breaker 三 instance（防雪崩） | 1-2 天 | LINE 慢回應不再卡 worker；breaker open 直接走 retry |
| 4 | Supabase fallback + LINE token health daily ping + integrations/health endpoint | 2 天 | Storage 失敗暫存本地；token 撤銷立即告警 |

**Phase 1 ship 後先觀察一週數據**，user 再決定 P3/P4 是否需要降規或追加。

## 4. 共用基建（Phase 1 一次寫齊，後續 phase 沿用）

### 4.1 `utils/external_calls.py`

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
    """Exponential backoff with ±20% jitter. 拋出最後一次 exception。"""

def tagged_capture(
    exc: BaseException,
    tag: Literal["line", "supabase", "external_http"],
    *,
    level: Literal["error", "warning"] = "error",
    extra: dict | None = None,
) -> None:
    """Wrap utils.sentry_init.capture_exception 加 sentry scope tag。
    sentry-sdk 未 init 時自動 no-op。"""
```

- 純函式、無 state、有完整 unit test（攔截 sleep 不真睡）
- `retry_on` 預設 `(Exception,)`；caller 可窄化為 `(requests.RequestException, ConnectionError)` 等

### 4.2 `utils/circuit_breaker.py`

```python
class CircuitBreaker:
    """簡易 in-process state machine（CLOSED→OPEN→HALF_OPEN→CLOSED）。

    每 worker 獨立持有 state（is_open_until / consecutive_failures），
    無共享記憶體 — prod 多 worker 各自觀察各自 trip 是設計選擇，
    不上 Redis 分散式（YAGNI）。
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: int = 60,
        # 觸發 OPEN 的 exception types（caller 自訂；不傳預設都算）
        trip_on: tuple[type[BaseException], ...] | None = None,
    ): ...

    def call(self, fn: Callable[[], T]) -> T:
        """state == OPEN 直接拋 BreakerOpenError；其他狀態執行 fn 並更新 state。"""

    @property
    def state(self) -> Literal["closed", "open", "half_open"]: ...
    @property
    def stats(self) -> dict:  # 給 /admin/integrations/health
        ...


class BreakerOpenError(Exception):
    """Caller 知道是 breaker 拒絕；不是真的失敗 — caller 自決後備。"""


# Module-level singletons（main.py 不需 init）
LINE_BREAKER = CircuitBreaker("line", failure_threshold=5, recovery_seconds=60)
SUPABASE_BREAKER = CircuitBreaker("supabase", failure_threshold=5, recovery_seconds=60)
EXTERNAL_HTTP_BREAKER = CircuitBreaker(
    "external_http", failure_threshold=10, recovery_seconds=120,
)
```

- ~80 行實作（含 docstring 與 thread-safe `threading.Lock`）
- HALF_OPEN 時只允許 1 個 request 試探，成功則回 CLOSED 並 reset counter，失敗則重回 OPEN
- 4xx（特別是 401/403/404）**不算 trip 條件**（client error 不該 trip server breaker）；caller 傳 `trip_on=(requests.Timeout, requests.ConnectionError, ServerError5xx)` 控制粒度

## 5. Phase 1 — Sentry tagging（觀察先行）

### 5.1 LINE（`services/line_service.py` 7 處 `_push*` / `_reply*` method）

| 行號 | method | 改動 |
|------|--------|------|
| 265 | `push_text_to_group` | `logger.warning` → `logger.exception` + `tagged_capture(exc, 'line')`，但 4xx 不上報（分流到 token health metric） |
| 310 | `_push_to_user` | 同上 |
| 347 | `push_flex_to_user` | 同上 |
| 379 | `_push_to_user_with_quick_reply` | 同上 |
| 413 | `_reply_with_quick_reply` | 同上 |
| 445 | `_reply` | 同上 |
| (新增 helper `_record_line_response`) | 共用 4xx 分流邏輯 | 見下方 4xx 分流規則 |

**4xx 分流規則**:
- 401 / 403 → 視為 token 撤銷或權限錯誤
  - **Phase 1**：Sentry level=`error`，**用 process-local in-memory dedup**（`_recent_401_seen: set[str]` 1 小時 TTL）避免單 worker 內 spam；多 worker 各自獨立發第一次
  - **Phase 4**：改寫 `line_token_health` row 用 `consecutive_failures` 持久化 dedup（跨 worker / 跨 restart 也只發里程碑）；Phase 1 的 in-memory dedup 同時保留為 fast-path
- 404 / 400 → caller bug（不存在的 user_id 或惡意 payload），Sentry level=`warning`，**不算 breaker trip**
- 429 → rate limit，Sentry level=`warning`，**算 breaker trip**
- 5xx / timeout / network → Sentry level=`error`，**算 breaker trip**

### 5.2 Supabase（`utils/supabase_storage.py` 5 method）

每個 method 外層加 `try/except`，內呼 `tagged_capture(exc, 'supabase')`。`save()` Phase 4 會擴增 retry + fallback；Phase 1 只加觀察。

### 5.3 External HTTP（6 處）

| 檔案 | 行號 | tag |
|------|------|-----|
| `services/recruitment_market_intelligence.py` | 520, 526, 534 | `external_http` |
| `services/geocoding_service.py` | 157, 208 | `external_http` |
| `services/official_calendar.py` | 116 | `external_http` |

### 5.4 設定

- 新 env `SENTRY_TAG_EXTERNAL_FAILURES: bool = True`（`config/sentry.py` Settings）— test 可關掉避免污染 Sentry test project
- `tagged_capture` 內部 check 此 flag 決定是否真的呼叫 `sentry_sdk.capture_exception`

## 6. Phase 2 — LINE retry（NotificationLog augment + scheduler）

### 6.1 設計缺口校正

> **重要發現**：CHANNEL_MATRIX 顯示 25 個 event_type 中 **14 個是 LINE-only**（無 in_app channel，皆為 parent.* 與 activity 候補通知），而現行 `dispatch._fan_out` 只在 `"in_app" in evt.channels` 時才寫 NotificationLog row（[`services/notification/dispatch.py:291`](../../services/notification/dispatch.py)）。**這 14 個事件失敗時沒有 row 可被 retry scheduler 撈到**。

#### 解法：解耦「Inbox 可見性」與「持久化 audit row」

- **NotificationLog 一律寫 row**（包含 LINE-only 事件）— row 是 retry 的 source of truth
- 新增 `is_inbox_visible: bool = True` 欄位，由 channel matrix 決定（含 in_app channel 的事件 = True；parent.* / waitlist 等 LINE-only = False）
- 員工 inbox UI 查詢加 `WHERE is_inbox_visible = TRUE`，**不破壞既有 UX**（家長端不出現在員工 inbox）
- 群組推送 `dismissal.created` (`recipient_user_id IS NULL`) **不寫 NotificationLog**（model `recipient_user_id` is `nullable=False`，且下次接送通知會自然覆蓋 UX）— v1 不支援群組 retry，列入 follow-up

### 6.2 Alembic migration

```python
# alembic/versions/<revid>_p1_resilience_notif_log_retry.py
def upgrade():
    op.add_column("notification_logs", sa.Column(
        "line_retry_count", sa.Integer, nullable=False, server_default="0"
    ))
    op.add_column("notification_logs", sa.Column(
        "line_next_retry_at", sa.DateTime(timezone=True), nullable=True
    ))
    op.add_column("notification_logs", sa.Column(
        "is_inbox_visible", sa.Boolean, nullable=False, server_default=sa.true()
    ))
    # Backfill：所有既有 row 視為 inbox visible（與既有 UX 一致；
    # in_app channel 設計上必有 in_app，server_default=true 已對齊）
    op.create_index(
        "ix_notif_log_line_retry_pending",
        "notification_logs",
        ["line_next_retry_at"],
        postgresql_where=sa.text(
            "line_next_retry_at IS NOT NULL AND line_retry_count < 3"
        ),
    )

def downgrade():
    op.drop_index("ix_notif_log_line_retry_pending", table_name="notification_logs")
    op.drop_column("notification_logs", "is_inbox_visible")
    op.drop_column("notification_logs", "line_next_retry_at")
    op.drop_column("notification_logs", "line_retry_count")
```

### 6.3 dispatch.py 改動

1. `_fan_out` 改寫：**只要 matrix 含 line 或 ws，就寫 NotificationLog row**（不再以 in_app 為條件）
2. `is_inbox_visible` 依 `"in_app" in evt.channels` 計算
3. LINE channel 失敗時：
   - 寫 `channels_failed += [{"channel": "line", "error": <type>, "ts": <iso>}]`
   - 寫 `line_next_retry_at = now() + 30s`（首次失敗）
   - **使用 log_session 寫**（不是 business session）— advisor 抓的 phantom retry on rollback 風險已修
4. LINE channel 成功時：清空 `line_next_retry_at`（若有先前 retry 殘留）

### 6.4 Retry scheduler

新檔 `services/notification/retry_scheduler.py`：

```python
async def tick_line_retry(now_provider: Callable[[], datetime] = utc_now) -> dict:
    """每 5 分鐘 tick：撈 pending LINE retry 重發。

    Returns metric dict for /admin/integrations/health.
    """
    session = get_session_factory()()
    try:
        rows = session.query(NotificationLog).filter(
            NotificationLog.line_next_retry_at.is_not(None),
            NotificationLog.line_next_retry_at <= now_provider(),
            NotificationLog.line_retry_count < 3,
        ).limit(100).all()  # 單 tick 上限避免 spike

        for row in rows:
            try:
                # Reconstruct PendingEvent + 呼叫 LINE_HANDLERS[event_type]
                result = _retry_line_push(row)
                if result:
                    _mark_succeeded(session, row)
                else:
                    _schedule_next_or_final(session, row)
            except Exception as exc:
                logger.exception("LINE retry tick failed row=%s", row.id)
                tagged_capture(exc, "line")
                _schedule_next_or_final(session, row)
        session.commit()
        return {"attempted": len(rows), ...}
    finally:
        session.close()
```

- Backoff: 30s → 5min → 30min（指數）
- 第 3 次仍失敗：mark `channels_failed += [{"channel": "line", "error": ..., "final": true}]`、`line_next_retry_at = NULL`、保留 `line_retry_count = 3` 供 metric 查詢
- 註冊到既有 main.py lifespan scheduler 群（與 leave-quota-expiry / Phase 4 supabase-pending / token-health 共用一個 asyncio scheduler）

### 6.5 觀察性

- `GET /admin/integrations/health` 回 `line.retry_pending`（`COUNT WHERE line_next_retry_at IS NOT NULL AND line_retry_count < 3`）與 `line.retry_final_failed_24h`
- Sentry event 不重複上報（首次失敗已上報過；retry 失敗只 log）

## 7. Phase 3 — Circuit breaker

### 7.1 LINE breaker

- `LineService` 所有 `_push*` / `_reply*` method 入口包 `LINE_BREAKER.call(lambda: requests.post(...))`
- breaker OPEN 時 caller 拿到 `BreakerOpenError`：
  - dispatch._fan_out LINE 路徑收到 `BreakerOpenError` → 寫 retry 標記（與 Phase 2 失敗路徑共用）+ tagged_capture(level='warning')
  - hybrid path (`api/portfolio/reports.py` send-line) → 回 caller {sent: false, reason: 'breaker_open'}，前端訊息「LINE 暫時不可用，請稍後再試」
- trip 條件：`(requests.Timeout, requests.ConnectionError, LineServerError5xx)`；**4xx 不算 trip**

### 7.2 Supabase breaker

- `SupabaseStorage.save/read/delete/exists/...` 包 `SUPABASE_BREAKER.call(...)`
- breaker OPEN 時：
  - `save()` → 走 Phase 4 local fallback 寫 `data/uploads_pending/`
  - `read()` → 拋 caller，caller 自決（多數是 download endpoint，回 503 給 user 重試）
- trip 條件：`(supabase.SupabaseException, requests.Timeout, ConnectionError)`

### 7.3 External HTTP breaker

- recruitment_market_intelligence / geocoding_service / official_calendar 包 `EXTERNAL_HTTP_BREAKER.call(...)`
- breaker OPEN 時：caller 拿 `BreakerOpenError`，scheduler 跳過此 tick，下次再試（這些都是 batch scheduler，無 user-facing degradation）

### 7.4 timeout 約束

- LINE / Supabase / external HTTP 已有 `timeout=` 參數，本 spec 不改 timeout 值
- finding #14(c) 提的 linter rule（強制 timeout）defer — 目前 100% 有 timeout，加 linter 是 future-proof 但不解當前痛點

## 8. Phase 4 — Supabase fallback + LINE token health

### 8.1 Pending uploads model

```python
# models/pending_uploads.py
class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    module = Column(String(40), nullable=False)
    key = Column(String(255), nullable=False)
    content_type = Column(String(80), nullable=False)
    local_path = Column(String(500), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    succeeded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_pending_uploads_next_retry",
              "next_retry_at",
              postgresql_where=text("succeeded_at IS NULL AND attempts < 5")),
    )
```

### 8.2 `SupabaseStorage.save()` 改動

```python
def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
    bucket = self._client.storage.from_(_resolve_bucket(module))
    try:
        # Phase 3 breaker 在外層包覆
        retry_with_backoff(
            lambda: bucket.upload(path=key, file=data, file_options={...}),
            attempts=3, base_seconds=1.0, cap_seconds=8.0,
            retry_on=(requests.RequestException, ConnectionError, supabase.SupabaseException),
        )
    except Exception as exc:
        tagged_capture(exc, "supabase")
        if not settings.storage.local_fallback_enabled:
            raise  # default true；test 可關
        # Local fallback
        local_path = _stash_locally(module, key, data, content_type)
        _enqueue_pending_upload(module, key, content_type, local_path, str(exc))
        # 不 raise — caller 視為 save 成功（檔已在 local，scheduler 會同步）
```

- caller URL 取得：`public_url` / `signed_url` 對 pending file 走 **本機 fallback URL**（同 LocalStorage 的 `_API_PATH_PREFIX`），與既有 local mode 同 endpoint，**不增前端複雜度**
- 同步成功後 `_API_PATH_PREFIX` 回 Supabase URL — caller 端永遠用 `get_backend().public_url(...)` 取，不 cache URL，本機/雲端切換對 caller 透明

### 8.3 Pending uploads scheduler tick

```python
async def tick_pending_uploads() -> dict:
    """每 5 分鐘 tick：撈 pending uploads 重 push to Supabase。"""
    # 與 LINE retry tick 共用 scheduler，順序執行
    # backoff: 30s → 2min → 10min → 1hr → 6hr（5 次後 mark failed alert admin）
```

### 8.4 LINE token health

```python
# models/integration_health.py
class LineTokenHealth(Base):
    __tablename__ = "line_token_health"
    id = Column(Integer, primary_key=True)  # singleton row id=1
    last_check_at = Column(DateTime(timezone=True), nullable=False)
    healthy = Column(Boolean, nullable=False)
    last_error = Column(String(200), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
```

Scheduler tick（daily 08:00 Asia/Taipei）:

```python
async def tick_line_token_health() -> None:
    if not settings.line.enabled:
        return
    try:
        resp = requests.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {settings.line.channel_access_token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            _update_health(healthy=True, error=None, reset_failures=True)
        elif resp.status_code in (401, 403):
            _update_health(healthy=False, error=f"http_{resp.status_code}", increment_failures=True)
            tagged_capture(
                RuntimeError(f"LINE token unhealthy: {resp.status_code} {resp.text[:200]}"),
                "line",
                level="error",
            )
        else:
            _update_health(healthy=False, error=f"http_{resp.status_code}", increment_failures=True)
    except Exception as exc:
        _update_health(healthy=False, error=type(exc).__name__, increment_failures=True)
        tagged_capture(exc, "line", level="warning")
```

- User 已確認 long-lived token（無過期）→ 純 liveness ping；401/403 = 被撤銷
- 連續 N 次 401 後也只發 1 次 Sentry（用 `consecutive_failures` 去重，只在 0→1 與 7→8 等里程碑發）
- Phase 4 落地後，LINE 4xx call site 與 daily tick 共寫此 row（`last_check_at` / `consecutive_failures`）；call site 寫入用 `INSERT ... ON CONFLICT (id=1) DO UPDATE` upsert 模式，daily tick 同 pattern，互不衝突。Phase 1 的 in-memory dedup 保留為 fast-path（避免 hot loop 撞 DB）

### 8.5 Admin endpoint

```python
# api/admin/integrations_health.py
@router.get("/admin/integrations/health")
def get_integrations_health(
    _: User = Depends(require_permission(Permission.ADMIN_READ)),
    session: Session = Depends(get_session),
) -> IntegrationsHealthResponse:
    """後台首頁徽章資料 — Phase 5 前端 UI 落地後對接。"""
    return {
        "line": {
            "breaker": LINE_BREAKER.state,
            "token_healthy": _get_line_token_healthy(session),
            "retry_pending": _count_pending_line_retries(session),
            "retry_final_failed_24h": _count_final_failed(session, hours=24),
        },
        "supabase": {
            "breaker": SUPABASE_BREAKER.state,
            "pending_uploads": _count_pending_uploads(session),
        },
        "external_http": {"breaker": EXTERNAL_HTTP_BREAKER.state},
    }
```

## 9. 測試策略

### 9.1 Unit 純函式（Phase 1）
- `test_retry_with_backoff_attempts_exhausted_raises_last`
- `test_retry_with_backoff_jitter_within_bounds`
- `test_circuit_breaker_state_machine`（CLOSED→OPEN→HALF_OPEN→CLOSED 完整 path）
- `test_breaker_4xx_not_tripped`（caller 傳 trip_on 過濾）
- `test_tagged_capture_no_op_without_sentry`
- `test_tagged_capture_respects_disabled_env_flag`

### 9.2 dispatch retry 集成（Phase 2）
- `test_line_failure_writes_retry_marker_on_log_session_not_business`（advisor 抓的 phantom retry guard）
- `test_business_tx_rollback_no_retry_row`
- `test_retry_scheduler_picks_pending_row_and_resends`
- `test_retry_scheduler_backoff_exponential`
- `test_retry_scheduler_third_failure_marks_final`
- `test_parent_line_only_event_writes_log_row_for_retry`（新行為驗證）
- `test_inbox_query_filters_is_inbox_visible`（既有 inbox UX 不破壞）

### 9.3 Breaker 集成（Phase 3）
- `test_line_5xx_5_times_trips_breaker_then_dispatch_writes_retry`
- `test_line_4xx_not_tripped`（401/403 分流到 token health 而非 breaker）
- `test_supabase_breaker_open_routes_to_local_fallback`

### 9.4 Supabase fallback 集成（Phase 4）
- `test_supabase_save_retries_3_times_on_transient_error`
- `test_supabase_save_fallback_writes_local_and_enqueues_pending`
- `test_pending_uploads_scheduler_pushes_to_supabase`
- `test_local_fallback_disabled_raises_to_caller`

### 9.5 Token health（Phase 4）
- `test_line_token_health_ping_200_marks_healthy`
- `test_line_token_health_ping_401_marks_unhealthy_and_sentry`
- `test_line_token_health_consecutive_401_dedupe_sentry`

## 10. 風險與緩解

| 風險 | 緩解 |
|------|------|
| **Phantom retry on rollback**（advisor 抓） | NotificationLog 寫 retry marker 用 `log_session`（dispatch._fan_out 既有 pattern），業務 tx rollback 不留 phantom |
| **Multi-worker breaker state 不一致** | 設計選擇：每 worker 獨立觀察獨立 trip。實務上失敗事件會分散到各 worker，trip 速率近似一致 |
| **NotificationLog row 暴增**（14 parent 事件每次都寫一筆） | parent.* 事件量 ≈ in_app 事件量同數量級；新增 14 event 寫入無 disk 壓力。未來需要 archive 走既有 retention policy（spec 不處理） |
| **Retry scheduler 與 business tx 競態**（同 row 被 read 又被新 fan-out 改） | scheduler 用 `SELECT ... FOR UPDATE SKIP LOCKED`（PostgreSQL）；SQLite test 用 `query().with_for_update()` mock |
| **Local fallback 磁碟用滿** | `data/uploads_pending/` 限制 5GB（環境變數 `STORAGE_LOCAL_FALLBACK_MAX_MB=5000`），超過拒收並 Sentry alert |
| **CHANNEL_MATRIX 改動破壞既有測試** | 加 `is_inbox_visible` 預設 True，既有 11 個 in_app 事件行為不變；新 14 個 LINE-only 事件加 `is_inbox_visible=False`，inbox UI query 加 filter — 改動皆 additive |

## 11. 不做的事（YAGNI）

- 不引入 `pybreaker` / `tenacity` / `circuitbreaker` 套件（80 行內可寫，減 supply chain）
- 不上 Redis 分散式 breaker（per-worker state 是設計選擇）
- 不做 LINE token rotation（long-lived token user 已確認）
- 不另建 `parent_notification_outbox` 表（augment NotificationLog 即可，advisor 抓的）
- 不做群組推送 (`dismissal.created`) retry v1（無 NotificationLog row；下次接送通知自然覆蓋 UX）
- 不做前端後台徽章 UI（Phase 5 follow-up；本 spec backend only）
- 不做外呼 timeout linter rule（目前 100% 有 timeout，加 linter 無 immediate value）
- 不做 Supabase bucket capacity-watch（與韌性議題正交，另開 spec）

## 12. 驗收標準

Phase 1 完成 = 18 個外呼站點全部有 `tagged_capture`，CI grep gate 防回歸（`logger.warning.*requests\.` 不允許再出現）。

Phase 2 完成 = LINE 連續 5 次故障 + recovery，所有 pending NotificationLog row 在 30 分鐘內全部 retry 成功，inbox 查詢效能不退步（既有 `ix_notif_log_recipient_unread` index 仍命中）。

Phase 3 完成 = 模擬 LINE 連續 timeout 5 次後，breaker OPEN 60s 期間 `LINE_BREAKER.call()` 不打 LINE API、p99 latency 不受 LINE 慢回應影響。

Phase 4 完成 = 模擬 Supabase 整段不可用，前端 upload 仍 200（檔案進 local fallback），LINE token 撤銷 24 小時內 Sentry 有 alert。

## 13. 設定變更總覽

| ENV | Default | Phase |
|-----|---------|-------|
| `SENTRY_TAG_EXTERNAL_FAILURES` | `true` | 1 |
| `LINE_BREAKER_FAILURE_THRESHOLD` | `5` | 3 |
| `LINE_BREAKER_RECOVERY_SECONDS` | `60` | 3 |
| `SUPABASE_BREAKER_FAILURE_THRESHOLD` | `5` | 3 |
| `SUPABASE_BREAKER_RECOVERY_SECONDS` | `60` | 3 |
| `EXTERNAL_HTTP_BREAKER_FAILURE_THRESHOLD` | `10` | 3 |
| `STORAGE_LOCAL_FALLBACK_ENABLED` | `true` | 4 |
| `STORAGE_LOCAL_FALLBACK_MAX_MB` | `5000` | 4 |
| `LINE_TOKEN_HEALTH_PING_HOUR_TAIPEI` | `8` | 4 |

## 14. Migration / Deployment 順序

1. Phase 1 PR merge → Sentry 開始收 tagged event，**先觀察一週**
2. Phase 2 PR：Alembic migration `p1_resilience_notif_log_retry` → backfill `is_inbox_visible=true` → dispatch 改動 → scheduler 上線 → **驗證 inbox UX 不破壞**
3. Phase 3 PR：breaker 上線，**先觀察兩週** state transition 是否異常 trip
4. Phase 4 PR：Alembic migration `p1_resilience_pending_uploads_and_token_health` → SupabaseStorage 改動 → token health scheduler 上線 → admin endpoint 上線
5. Phase 5（**本 spec 外**）：前端後台徽章 UI 對接 `/admin/integrations/health`

每個 Phase 獨立可 rollback；rollback 時：
- Phase 1：revert call site；Sentry 不收新 tagged event（不影響其他系統）
- Phase 2：revert dispatch 改動；scheduler 不撈（new column 留著無害）
- Phase 3：revert breaker 包覆；外呼回到直接 `requests.*`
- Phase 4：disable scheduler；upload fail 回到直接 raise

---

**Follow-up（本 spec 外）**:
- Phase 5：前端後台首頁徽章 UI（對接 `/admin/integrations/health`）
- 群組推送 (`dismissal.created`) retry — 需設計新的 group_id retry table
- Supabase bucket 使用量 capacity-watch（與韌性正交）
- 外呼 timeout linter rule（CI grep gate）
- LINE retry scheduler 上 Redis distributed lock（多 worker 時）
