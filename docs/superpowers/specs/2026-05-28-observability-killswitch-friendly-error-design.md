# Spec — Observability / Kill Switch / Friendly Error（3 P0 防護）

**日期**：2026-05-28
**狀態**：草稿（待 user review）
**Author flow**：brainstorming → spec → writing-plans → execute
**Base ref**：`origin/main`（P1 resilience 已 merged，HEAD `9e3de27`）

---

## 0. Scope & Non-Goals

修三項 P0 缺口：

| ID | 痛點 | 目標 |
|----|------|------|
| #3 | 凌晨系統 down 無人發現（MTTD 3-4 小時） | UptimeRobot ping + scheduler DB heartbeat + `/health/schedulers`，MTTD ≤ 5 分鐘 |
| #4 | 部署期家長 LIFF 看 5xx 而非「整修中」 | env-only maintenance / read-only middleware + 前端維護頁 |
| #5 | 家長綁定錯誤無 next-step；5xx 顯示 `Request failed with status code 500` | 前端 5xx/network 友善 fallback + 家長端 ~50 處 router 升級 BusinessError |

**Non-Goals**：
- 全 codebase 1261 處 inline `HTTPException(detail="字串")` 統一改 envelope（churn 太大、200+ test 會炸；只做家長端路徑）
- 自動接 UptimeRobot API / 接 cronitor / healthchecks.io SaaS（給 runbook 由 user 在 dashboard 設定即可，少一個雲端依賴）
- 全域 i18n（家長端 zh-TW only；訊息表硬編碼即可）
- DB-backed maintenance config（env-only 比 DB 安全 — 事故時 DB 可能掛）

**用 user 原話對齊**：
- user #3 訴求「30 分鐘 P0 防護」= UptimeRobot baseline。本 spec 含 DB-backed scheduler heartbeat 是 **scope expansion**（在 user 訴求基礎上加 1.5 天工程量），理由：既有 `utils/scheduler_observability.py` in-memory 版本 process restart 會丟，DB heartbeat 才能在 zeabur 重新部署後仍信任「最近一次成功時間」。若 user 想縮回 UptimeRobot-only，spec review 階段告知。
- user #5 訴求文字「後端改 `{detail: {message, code}}` 結構」；evidence 顯示前端 `src/api/index.ts:88-97` 與 `src/parent/api/index.ts:144-158` **已支援該 shape**、後端 `utils/exception_handlers.py` 已有 `BusinessError` envelope path。**真痛點是家長端 next-step + 5xx friendly fallback**，不需要動全 1261 處 inline HTTPException（會炸 200+ test）。

---

## 1. 既有狀態（必讀）

P1 resilience 已 merged（2026-05-28 `9e3de27`）：
- `api/integrations_health.py` — `GET /api/internal/integrations/health`（AUDIT_LOGS 權限）
- `utils/circuit_breaker.py` — LINE / SUPABASE / EXTERNAL_HTTP 3 個 breaker
- `services/notification/retry_scheduler.py` + `pending_uploads_scheduler.py` — 新加的 2 個 scheduler
- `services/line_token_health_scheduler.py` — 每日 08:00 LINE token 健康檢查 + `OPS_ALERT_LINE_GROUP_ID` 告警通道

`utils/scheduler_observability.py` 已存在但 **in-memory only**：
- `scheduler_iteration(name)` context manager — Sentry throttle（連續 3 次失敗才 capture）
- 5/13 scheduler 已用（`activity_waitlist`、`salary_snapshot`、`pii_retention`、`medication_reminder`、`recruitment_term_advance`）
- `SchedulerStats.last_success_at` 是 in-memory `dict[str, SchedulerStats]`，**process restart 全丟失**

`utils/exception_handlers.py` 已有 `BusinessError` envelope：
```python
# BusinessError, ValidationError, unhandled → envelope
{"detail": {"code": "...", "message": "...", "request_id": "..."}}
# HTTPException(detail="字串") → 透傳保兼容 1261 處 inline + 200+ test
{"detail": "字串"}
```

前端 axios interceptor 已支援兩種 shape（`src/api/index.ts:88-97`、`src/parent/api/index.ts:144-158`）。

---

## 2. Architecture Overview

### 2.1 三項彼此獨立 — 各自 PR

| PR | Repo | Files | 依賴 |
|----|------|-------|------|
| **BE-A** scheduler heartbeat | ivy-backend | alembic + `utils/scheduler_observability.py` + 8 scheduler + `api/health.py`（或新增 `api/health_schedulers.py`） + `api/internal/uptime_webhook.py` | 無 |
| **BE-B** kill switch | ivy-backend | `utils/kill_switch.py` + `main.py` middleware order + `config/network.py` env | 無 |
| **BE-C** parent BusinessError | ivy-backend | `services/business_errors/parent.py` + ~10 router file + `utils/error_codes.py` registry | 無 |
| **FE-A** maintenance view | ivy-frontend | `views/MaintenanceView.vue` + `parent/views/MaintenanceView.vue` + 兩 interceptor + 兩 router | **依 BE-B**（要 envelope code 才能分辨 503 是維護 vs DB 掛） |
| **FE-B** 5xx/network fallback | ivy-frontend | `src/utils/errorHandler.ts` + interceptor priority | 無 |
| **FE-C** useFriendlyError | ivy-frontend | `src/utils/errorCodeRegistry.ts` + `src/composables/useFriendlyError.ts` + ~10 parent view 元件 | **依 BE-C**（要 BusinessError code 才能 mapping） |

**Merge 順序**：BE-A + BE-B + BE-C + FE-B 可並行；FE-A 等 BE-B；FE-C 等 BE-C。

### 2.2 共同設計原則

1. **零回歸 hard gate**：每 PR 既有 pytest / vitest 全綠才合
2. **既有 `scheduler_iteration` 不破壞**：只擴充 signature（新增 `expected_interval_seconds` kwarg，default `None` = 不檢查 lag），舊呼叫不改也能 work
3. **既有 1261 處 inline HTTPException 不動**：BE-C 只改 parent / portal / LIFF auth 路徑 ~50 處，加一個 `BusinessError` subclass 用 envelope；非家長端管理頁仍維持字串 detail（前端 displayMessage 直接顯示中文字串本來就 OK）
4. **env-only kill switch**：事故時 DB 可能掛，flip env 不依賴 DB；zeabur dashboard 直接編輯即可

---

## 3. Section 1：Scheduler Heartbeat & /health/schedulers（BE-A）

### 3.1 資料模型

新表 `scheduler_heartbeats`：

```python
class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeats"
    scheduler_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    expected_interval_seconds: Mapped[int] = mapped_column(Integer)
    last_rows_processed: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
```

- PK = `scheduler_name`：自然鍵，13 列固定
- 無 FK：純 ops 表
- `expected_interval_seconds`：每 scheduler 在啟動時 register 自己的 interval，`/health/schedulers` 用來計算 lag

**Alembic migration `schedhb01`**：
- 建表 `scheduler_heartbeats`（schema 見上）
- Seed 13 row（一一對應現有 scheduler）：
  ```python
  SCHEDULER_INTERVALS = {
      "activity_waitlist": 300,
      "medication_reminder": 300,
      "graduation": 3600,
      "salary_snapshot": 86400,
      "official_calendar": 86400,
      "finance_reconciliation": 86400,  # cron 02:00
      "recruitment_term_advance": 86400,
      "pii_retention": 86400,
      "security_gc": 86400,
      "leave_quota_expiry": 3600,
      "line_token_health": 86400,  # cron 08:00
      "notification_retry": 300,
      "pending_uploads": 300,
  }
  for name, interval in SCHEDULER_INTERVALS.items():
      op.execute(f"INSERT INTO scheduler_heartbeats (scheduler_name, expected_interval_seconds, consecutive_failures, last_rows_processed, updated_at) VALUES ('{name}', {interval}, 0, 0, NOW())")
  ```
- 初始 `last_success_at=NULL`，runtime 第一次 tick 成功時 UPDATE
- `downgrade`：`op.drop_table('scheduler_heartbeats')`

### 3.2 `utils/scheduler_observability.py` 擴充

```python
@contextmanager
def scheduler_iteration(
    scheduler_name: str,
    expected_interval_seconds: int | None = None,  # 新增 kwarg
) -> Iterator[None]:
    """既有 in-memory metrics + 新增 DB heartbeat persist。

    成功路徑：UPDATE scheduler_heartbeats SET last_success_at = NOW(),
              consecutive_failures = 0, last_error_message = NULL
    失敗路徑：UPDATE scheduler_heartbeats SET last_failure_at = NOW(),
              consecutive_failures += 1, last_error_message = str(exc)
    DB 寫失敗：吞掉（log warning），不破壞 scheduler loop
    """
```

**設計 trade-off**：
- DB UPDATE 每 tick 1 次（最頻繁 300s tick），13 個 scheduler 加總每分鐘 ≤ 3 次 UPDATE — 微乎其微
- 用 `with session.begin():` 獨立 transaction，不混入 scheduler 業務邏輯的 session
- DB 寫失敗 swallow，不破壞既有 swallow exception 設計（in-memory metrics 仍會反映）

### 3.3 13 scheduler 改造

5 個已用 `scheduler_iteration` 的：補 `expected_interval_seconds=N` kwarg。

8 個未用的（`graduation`、`official_calendar`、`finance_reconciliation`、`security_gc`、`leave_quota_expiry`、`line_token_health`、`notification/retry_scheduler`、`notification/pending_uploads`）：每個改造為 `with scheduler_iteration("name", expected_interval_seconds=N):`，原 `try/except + logger.exception` 區塊改進 context manager body。

**改造 pattern**（每 scheduler ~5 行）：
```python
# Before
while not stop_event.is_set():
    try:
        await some_tick_logic()
    except Exception:
        logger.exception("scheduler tick failed")
    await asyncio.sleep(INTERVAL)

# After
while not stop_event.is_set():
    with scheduler_iteration("scheduler_name", expected_interval_seconds=INTERVAL):
        await some_tick_logic()
    await asyncio.sleep(INTERVAL)
```

### 3.4 `GET /health/schedulers` endpoint

新檔 `api/health_schedulers.py`（或併入 `api/health.py`）：

```python
@router.get("/schedulers")
async def schedulers_health():
    """檢查所有 scheduler heartbeat lag。

    無權限（UptimeRobot 公開可打）。
    回 200 = 全綠；503 = 至少一個 scheduler lag > 2 × expected_interval。
    """
    rows = session.query(SchedulerHeartbeat).all()
    now = datetime.now(timezone.utc)
    lagging = []
    schedulers = []
    for row in rows:
        if row.last_success_at is None:
            lag_seconds = None
            is_lagging = False  # 啟動後尚未跑過，先寬容
        else:
            lag_seconds = (now - row.last_success_at).total_seconds()
            is_lagging = lag_seconds > 2 * row.expected_interval_seconds
        item = {
            "name": row.scheduler_name,
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
            "lag_seconds": lag_seconds,
            "expected_interval_seconds": row.expected_interval_seconds,
            "consecutive_failures": row.consecutive_failures,
        }
        schedulers.append(item)
        if is_lagging:
            lagging.append(item)
    if lagging:
        return JSONResponse(status_code=503, content={"status": "degraded", "lagging": lagging, "schedulers": schedulers})
    return {"status": "ok", "schedulers": schedulers}
```

**設計 trade-off**：
- 無權限：UptimeRobot 免費版不支援 header auth，且 lag 資訊不含 PII
- 503 vs 200 + JSON flag：用 503 讓 UptimeRobot 視為 down → 告警；200 + flag 要 user 自己寫 keyword check 複雜
- 啟動後尚未跑過：寬容回 `is_lagging=False`，避免冷啟動連續 alert

### 3.5 UptimeRobot 設定 runbook

新檔 `docs/sop/uptime-monitor-setup.md`：5-step 截圖式指南
1. 註冊 UptimeRobot 免費帳號（50 monitor / 5 分鐘間隔）
2. 加 monitor：`https://ivymanagement.app/health/ready`（1 分鐘間隔，HTTP 2xx 即綠）
3. 加 monitor：`https://ivymanagement.app/health/schedulers`（5 分鐘間隔）
4. Alert contact：LINE webhook（指到後端 `/api/internal/uptime-webhook` 接收 → push 到 `OPS_ALERT_LINE_GROUP_ID`）+ email
5. 整合驗證：暫停某 scheduler → 5 分鐘內收到 LINE 告警

**LINE webhook endpoint**（屬於 BE-A scope）：`POST /api/internal/uptime-webhook?token=<UPTIME_ROBOT_WEBHOOK_TOKEN>` — 收 UptimeRobot 標準格式（`monitorFriendlyName`、`alertType`、`alertDetails`）→ 組中文訊息 → `LineService.push_text_to_group(OPS_ALERT_LINE_GROUP_ID, message)`。

### 3.6 測試（BE-A）

- `tests/test_scheduler_heartbeat.py`：
  - `scheduler_iteration` 成功更新 DB row
  - `scheduler_iteration` 失敗 increment consecutive_failures
  - DB 寫失敗 swallow（mock session raise → context manager 不噴）
  - `/health/schedulers` 全綠 200
  - `/health/schedulers` 一個 lagging → 503 含 lagging list
  - `/health/schedulers` 啟動後尚未跑過（`last_success_at=NULL`）→ 200 不告警
- `tests/test_uptime_webhook.py`：
  - Token 正確 → 200 + LineService 被 call
  - Token 錯誤 → 401
  - UptimeRobot down payload → 中文訊息含 monitor 名稱
- 既有 5 個用 scheduler_iteration 的 test 全綠

---

## 4. Section 2：Maintenance / Read-Only Kill Switch（BE-B + FE-A）

### 4.1 後端 env 配置

三個新 env（加入 `config/network.py` 或 `config/ops.py`）：

| env | 預設 | 行為 |
|-----|------|------|
| `MAINTENANCE_MODE` | `0` | `1` 時所有非 bypass 路徑回 503 |
| `READ_ONLY_MODE` | `0` | `1` 時所有 `POST/PUT/PATCH/DELETE` 非 bypass 路徑回 503（GET/HEAD/OPTIONS 仍 work） |
| `MAINTENANCE_MESSAGE` | `"系統維護中，請稍後再試"` | 503 response.detail.message |

**Bypass 清單（hardcoded）**：
- `/health/live`（UptimeRobot 仍要看 alive）
- `/health/ready`（DB 仍要可達）
- `/health/schedulers`（同上）
- `/api/internal/uptime-webhook`
- `/auth/login`（admin 緊急進入 — 仍需驗證帳密通過 rate-limiter）
- `/auth/refresh`（admin session keep-alive）

### 4.2 KillSwitchMiddleware

新檔 `utils/kill_switch.py`：

```python
class KillSwitchMiddleware(BaseHTTPMiddleware):
    BYPASS_PATHS = frozenset({
        "/health/live", "/health/ready", "/health/schedulers",
        "/api/internal/uptime-webhook",
        "/auth/login", "/auth/refresh",
    })

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.BYPASS_PATHS:
            return await call_next(request)

        if settings.ops.maintenance_mode:
            return self._maintenance_response("MAINTENANCE_MODE", settings.ops.maintenance_message)
        if settings.ops.read_only_mode and request.method not in ("GET", "HEAD", "OPTIONS"):
            return self._maintenance_response("READ_ONLY_MODE", "系統暫時唯讀，請稍後再試")
        return await call_next(request)

    def _maintenance_response(self, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": {"message": message, "code": code, "retry_after": 300}},
            headers={"Retry-After": "300"},
        )
```

### 4.3 `main.py` middleware 註冊

插入位置（FastAPI 最後 add = 最外層 = 最先執行）：

```python
# Audit 仍是最內層（不變）
app.add_middleware(AuditMiddleware)

# 新增 KillSwitch：Audit 後 add，wrapper Audit。
# 目的：maintenance response 不寫 audit log（避免噴 N 倍）
app.add_middleware(KillSwitchMiddleware)

# 其餘不變
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
```

### 4.4 前端 maintenance view（FE-A）

**管理端**：
- 新元件 `src/views/MaintenanceView.vue`：簡潔 flex center，顯示後端 `detail.message` / fallback「系統維護中，請稍後再試」+ 「重新整理」按鈕（觸發 `/health/ready` 探測，回 200 自動 reload）
- `src/router/index.ts`：加 `{ path: '/maintenance', component: MaintenanceView, meta: { public: true } }`
- `src/api/index.ts` interceptor：
  ```typescript
  if (status === 503 && rawDetail?.code === 'MAINTENANCE_MODE') {
    router.replace('/maintenance')
    return Promise.reject(error)
  }
  ```

**家長端**（與管理端相同 pattern）：
- 新元件 `src/parent/views/MaintenanceView.vue`：家長端風格（鯨魚 icon、淺色背景、「我們正在升級系統」文案）
- `src/parent/router/index.ts`：加 `/parent/maintenance` 路由
- `src/parent/api/index.ts` interceptor：同上 redirect 邏輯

**Read-only 提示**（用 ElMessage 而非 redirect — read-only 仍可瀏覽）：
```typescript
if (status === 503 && rawDetail?.code === 'READ_ONLY_MODE') {
  ElMessage.warning('系統暫時唯讀，編輯功能暫不可用')
  return Promise.reject(error)
}
```

### 4.5 測試（BE-B + FE-A）

- `tests/test_kill_switch.py`：
  - `MAINTENANCE_MODE=1` → 503 envelope + `Retry-After` header
  - bypass paths 不受影響
  - `READ_ONLY_MODE=1` 對 GET 通過、對 POST/PUT/PATCH/DELETE 503
  - envelope shape: `detail.message`, `detail.code`, `detail.retry_after`
- `tests/views/MaintenanceView.test.js`：渲染後端訊息 fallback、重新整理按鈕
- `tests/api/index.test.js`：interceptor 對 503 + code MAINTENANCE_MODE 做 redirect

---

## 5. Section 3：Friendly Error（FE-B + FE-C + BE-C）

### 5.1 FE-B：5xx / network friendly fallback（無後端依賴）

`src/utils/errorHandler.ts` 擴充 `DEFAULT_MESSAGES`：
```typescript
const DEFAULT_MESSAGES = {
  SERVER_ERROR: '服務暫時無法使用，請稍後再試。若持續發生請聯絡園所',
  NETWORK_ERROR: '網路連線異常，請檢查網路後重試',
  TIMEOUT: '伺服器回應逾時，請稍後再試',
  // ...既有
}
```

`src/api/index.ts` + `src/parent/api/index.ts` interceptor 改 displayMessage 優先序：
```typescript
// 優先序：envelope message > 字串 detail > axios 錯誤分類 fallback > 'unknown'
const friendly = classifyError(error)  // 既有 errorHandler 函式
error.displayMessage =
    (rawDetail && typeof rawDetail === 'object' && rawDetail.message) ||
    (typeof rawDetail === 'string' && rawDetail) ||
    DEFAULT_MESSAGES[friendly] ||
    null
```

**關鍵改變**：對 5xx + 無 detail 的情境（typical network failure），原本 `displayMessage = null` 導致 caller 顯示 axios `Request failed with status code 500`，改為 fallback 到 `DEFAULT_MESSAGES.SERVER_ERROR`。

### 5.2 FE-C：useFriendlyError composable + errorCodeRegistry（依賴 BE-C）

新檔 `src/utils/errorCodeRegistry.ts`：
```typescript
export interface FriendlyError {
  message: string  // 顯示給家長的訊息（複用後端 BusinessError message 即可）
  nextStep?: string  // next-step hint，後端不給就用這份註冊表
  level?: 'error' | 'warning' | 'info'
}

export const ERROR_CODE_REGISTRY: Record<string, FriendlyError> = {
  // 家長綁定流程
  BIND_CODE_INVALID: {
    message: '綁定碼無效或已過期',
    nextStep: '請至 LINE 主選單重新取得綁定碼，或聯絡園所',
    level: 'warning',
  },
  LINE_BINDING_EXPIRED: {
    message: '您的綁定已過期，請重新登入',
    nextStep: '點選下方「重新綁定」按鈕，或聯絡園所重新發送邀請',
    level: 'warning',
  },
  STUDENT_NOT_FOUND: {
    message: '找不到對應的學生資料',
    nextStep: '請確認綁定的學生仍在學；如有疑問請聯絡園所',
    level: 'error',
  },
  // 預留 ~20 條家長端 code
}
```

新檔 `src/composables/useFriendlyError.ts`：
```typescript
export function useFriendlyError() {
  function getFriendly(error: AxiosError): FriendlyError {
    const detail = error.errorDetail  // 由 interceptor 抽出的 envelope
    const code = detail?.code as string | undefined
    if (code && ERROR_CODE_REGISTRY[code]) {
      // 優先用 backend message，fallback 用 registry message
      return {
        message: detail.message || ERROR_CODE_REGISTRY[code].message,
        nextStep: ERROR_CODE_REGISTRY[code].nextStep,
        level: ERROR_CODE_REGISTRY[code].level,
      }
    }
    return {
      message: error.displayMessage || '發生未預期的錯誤',
      level: 'error',
    }
  }
  return { getFriendly }
}
```

家長端關鍵元件 catch 處改用 `useFriendlyError`，用 `el-alert`（含 nextStep）取代裸 `ElMessage`。

### 5.3 BE-C：parent BusinessError（~50 處）

新檔 `services/business_errors/parent.py`（沿用既有 `utils/exception_handlers.py` envelope path）：
```python
from utils.exception_handlers import BusinessError

class BindCodeInvalid(BusinessError):
    code = "BIND_CODE_INVALID"
    status_code = 400
    default_message = "綁定碼無效或已過期"

class LineBindingExpired(BusinessError):
    code = "LINE_BINDING_EXPIRED"
    status_code = 401
    default_message = "您的綁定已過期，請重新登入"

class StudentNotFound(BusinessError):
    code = "STUDENT_NOT_FOUND"
    status_code = 404
    default_message = "找不到對應的學生資料"

# 預留 ~20 條家長端 BusinessError subclass
```

升級 router：
- `api/parent/bind.py`（綁定碼相關）
- `api/parent/auth.py`（LIFF login refresh）
- `api/parent/me.py`（DSR / consent）
- `api/portal/contact_book.py`（家長存取聯絡簿）
- `api/portal/student.py`（家長存取學生資料）
- `api/auth.py` LINE-related 區塊（~5 處）

每處改：
```python
# Before
raise HTTPException(status_code=400, detail="綁定碼無效或已過期")

# After
raise BindCodeInvalid()  # 或 raise BindCodeInvalid("自訂訊息")
```

**範圍上限**：~50 處（家長 / portal / LIFF auth path）。非家長端 1200+ 處不動。

### 5.4 `utils/error_codes.py` registry

新檔在 backend，集中註冊 BusinessError code 用 enum，避免 typo：
```python
class ErrorCode(str, Enum):
    BIND_CODE_INVALID = "BIND_CODE_INVALID"
    LINE_BINDING_EXPIRED = "LINE_BINDING_EXPIRED"
    STUDENT_NOT_FOUND = "STUDENT_NOT_FOUND"
    # ...
```

每個 BusinessError subclass 用 `code = ErrorCode.BIND_CODE_INVALID.value`。前端 registry 對齊這份清單（手動同步；後續 follow-up 可考慮 codegen）。

### 5.5 測試（BE-C + FE-B + FE-C）

- `tests/api/parent/test_bind_error_codes.py`：每個 BusinessError 對應正確 envelope shape + code + status
- `tests/utils/errorHandler.test.js`：
  - envelope (object detail) → 優先 message
  - 字串 detail → 直接用
  - 5xx 無 detail → fallback `SERVER_ERROR`
  - network error（無 response）→ fallback `NETWORK_ERROR`
- `tests/composables/useFriendlyError.test.js`：code → nextStep mapping、後端 message 優先於 registry message
- `tests/api/parent/bind.test.js`：家長端綁定錯誤頁顯示 message + nextStep

---

## 6. Alembic Migration（BE-A only）

`alembic/versions/<rev>_schedhb01_scheduler_heartbeats.py`：
- 建表 `scheduler_heartbeats`
- Seed 13 row（initial `last_success_at=NULL`，runtime UPDATE）
- downgrade: `drop_table`

**single head 檢查**：寫 spec 時 `alembic heads` 在 `9e3de27` 為單 head；BE-A 在新分支 add migration 不會引多 head（單一 PR 單一 migration）。若 user 在 BE-A 寫 spec 後並行加其他 migration，BE-A merge 前 rebase 處理。

---

## 7. Rollout

### 7.1 部署順序（user 自行決定每步間隔）

1. **BE-A merge** → prod `alembic upgrade head` → 觀察 1 hour `/health/schedulers` 都 200
2. **BE-B merge** → prod env **MAINTENANCE_MODE 仍為 0**（不開啟）→ 觀察 middleware 不影響正常流量
3. **BE-C merge** → 觀察家長端流量無 4xx 大量噴
4. **FE-B merge** → 既有家長收到 5xx 改顯示友善訊息
5. **FE-A merge** → 503 + MAINTENANCE_MODE code 自動 redirect 到維護頁（依賴 BE-B 已落地）
6. **FE-C merge** → 家長端綁定錯誤顯示 nextStep
7. **UptimeRobot 設定**（user 跑 runbook）
8. **整合驗證**：手動 `MAINTENANCE_MODE=1` 5 分鐘 → 確認家長 LIFF 看到維護頁 → 改 `0` → 自動恢復

### 7.2 USER 手動 ops 清單

| 動作 | 何時 | 風險 |
|------|------|------|
| `alembic upgrade head`（schedhb01） | BE-A merge 後 | 純 CREATE TABLE，無風險 |
| zeabur env `MAINTENANCE_MESSAGE` | BE-B merge 前可預先設定 | 不影響 — 預設 fallback 即可 |
| UptimeRobot 註冊 + 加 monitor | 任何時候 | 無風險 |
| 整合驗證 `MAINTENANCE_MODE=1` 短暫測試 | FE-A merge 後 | 5 分鐘維護期，提前公告 |
| LINE Group `OPS_ALERT_LINE_GROUP_ID` 已存在（P1 落地）| — | 重用 |

### 7.3 Rollback

每 PR 獨立可 revert，無 schema 連動依賴。`schedhb01` migration downgrade 乾淨。Kill switch env 設 `0` 即關閉。

---

## 8. Risks & Mitigations

| 風險 | Mitigation |
|------|-----------|
| 13 scheduler 改造（BE-A）有些 scheduler tick 內部開自己的 session，可能與 `scheduler_iteration` DB UPDATE 衝突 | `scheduler_iteration` UPDATE 用獨立 `session.begin()`；TDD：每個 scheduler 改完跑 narrow test |
| 既有 5 個用 scheduler_iteration 的 scheduler 加 `expected_interval_seconds` kwarg | kwarg `default=None` 不破壞既有 caller；既有 test 全綠才 merge |
| 1261 處 inline HTTPException 全在 router 內部 — BE-C 若改錯非家長端 router 會 leak envelope shape | 改動清單嚴格限定 `api/parent/`、`api/portal/`、`api/auth.py` LIFF 區塊；PR diff review 必勾 path |
| KillSwitchMiddleware bypass list 漏 `/health/schedulers` → UptimeRobot 在維護期看 503 誤判 down | Bypass list 列入 spec 並寫死，pytest 驗每個 bypass path |
| UptimeRobot 免費版 5 分鐘間隔（user 訴求 1 分鐘） | 用 1 分鐘間隔需付費；免費版 5 分鐘是 trade-off；MTTD 改為「5 分鐘內告警」而非「1 分鐘內」 |
| 前端 errorCodeRegistry 與後端 `ErrorCode` enum 漂移 | 手動同步；後續 follow-up codegen（從 OpenAPI schema 取 BusinessError discriminator）|
| Sentry envelope 加 `code` 後 issue grouping 變了 | BE-C 部署後監看 Sentry 1 週，必要時調 fingerprint rule |
| BE-A 8 個 scheduler 改造 PR 大；review fatigue | 一個 PR 但拆 8 個 commit（每 scheduler 一 commit + 1 個 context manager 變動 commit + 1 個 endpoint commit）|

---

## 9. Out of Scope（Follow-ups）

- **全 codebase HTTPException → BusinessError 統一**（1200+ 處非家長端）：churn 太大；維持字串 detail，前端 displayMessage 即可顯示
- **errorCodeRegistry 從 OpenAPI codegen**：手動同步先 work，codegen 後續
- **DB-backed maintenance config + message 自助編輯 UI**：env-only 已 cover P0
- **UptimeRobot API 自動化**：runbook 即可
- **Prometheus / Grafana metrics 接 `/health/schedulers`**：UptimeRobot + LINE 已達 MTTD ≤ 5 min 目標
- **多 worker DB heartbeat 分配策略**：所有 scheduler 已用 advisory lock 確保只一個 worker 跑，DB heartbeat 是該 worker 寫即可；多 worker 一致性 follow-up

---

## 10. 驗收條件

- [ ] BE-A：scheduler_heartbeats 表存在，13 row seed；`/health/schedulers` 端點回 200 / 503 邏輯正確；既有 pytest 全綠 + 新 ≥ 6 test
- [ ] BE-B：`MAINTENANCE_MODE=1` 在 dev 觸發 503 envelope；bypass list 6 個 path 全驗；既有 pytest 全綠 + 新 ≥ 4 test
- [ ] BE-C：~50 處家長端 router 改 BusinessError；envelope shape 含 `code`/`message`/`request_id`；既有 pytest 全綠 + 新 ≥ 10 test
- [ ] FE-A：dev 設 maintenance → 兩端自動 redirect 到 `/maintenance`、`/parent/maintenance`；`vitest` 全綠
- [ ] FE-B：dev 模擬 5xx 顯示 `服務暫時無法使用` 而非 axios 預設訊息；模擬 network error 顯示 `網路連線異常`
- [ ] FE-C：dev 觸發 `BIND_CODE_INVALID` 在家長綁定頁顯示 message + nextStep
- [ ] UptimeRobot runbook 完成；user 設定後手動測：暫停一個 scheduler 5 分鐘 → 收到 LINE 告警

---

## 11. PR / Commit 計畫

| PR | 標題 | Commit 數 | 規模 |
|----|------|-----------|------|
| BE-A | `feat(observability): scheduler heartbeat DB + /health/schedulers + uptime webhook` | ~12 | +700/-30 |
| BE-B | `feat(ops): maintenance / read-only kill switch middleware (env-only)` | ~5 | +250/-5 |
| BE-C | `feat(parent): BusinessError subclasses + envelope migration (~50 sites)` | ~10 | +400/-200 |
| FE-A | `feat(maintenance): 503 redirect to MaintenanceView (admin + parent)` | ~6 | +300/-10 |
| FE-B | `feat(error): friendly 5xx / network fallback in axios interceptor` | ~3 | +80/-30 |
| FE-C | `feat(parent): useFriendlyError composable + errorCodeRegistry + next-step UI` | ~8 | +400/-100 |

總計：6 PR、~44 commit、~2200 行 diff。預估工程量：BE 3 天 + FE 2 天 + ops 0.5 天 = **5.5 天**。
