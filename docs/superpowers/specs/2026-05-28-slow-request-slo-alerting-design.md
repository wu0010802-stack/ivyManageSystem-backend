# 慢請求 SLO 監控與告警設計

**日期**：2026-05-28
**範圍**：純後端 ivy-backend（middleware 加 metrics 收集 + LINE 告警），無 schema 變動
**狀態**：spec — 待 user review 後進 writing-plans
**所屬序列**：goal /audit-findings B 項（A→B→C 串行；C 為後續 sub-project）

---

## 1. 背景與問題

`utils/request_logging.py:20` 設了 `SLOW_REQUEST_THRESHOLD_MS = 2000`，但：

| 現況 | 問題 |
|------|------|
| 慢請求只寫 `logger.warning("SLOW %s %s → %d (%.1fms) [rid=%s]")` | 無 aggregation；prod logs 噴成河時人類無法即時感知頻率異常 |
| Sentry 已 init `traces_sample_rate=0.1`（DSN 缺即 no-op） | DSN 未設 → 無 performance dashboard；設了也只有事後查詢，不會即時通知 |
| `/health/ready` 已存在（`api/health.py:26`） | 無 external uptime ping → 服務當機要等使用者反映 |
| dr-runbook §5「監控告警待補」 | 文件層級也承認 gap |
| LINE 告警 channel：`services/line_service.py` 已有 `push_text_to_group(group_id, text)` | 基建在但無人接 ops 告警 |

**目標**：把慢請求從「事後翻 log」升級為「即時通知 + 趨勢看板」，建立第一層 SLO 監控。

---

## 2. 範圍

### In scope（本次做）

- 新建 `utils/slow_request_alerter.py`：sliding window in-memory counter + dedupe 觸發器
- 修改 `utils/request_logging.py`：慢請求記錄時 push 進 alerter
- 新建 `services/ops_alert.py`：薄包裝 `line_service.push_text_to_group`，包 try/except + DSN/group_id 缺即 no-op
- 新增 settings `config/ops_alert.py`：`OPS_ALERT_LINE_GROUP_ID` + `SLOW_REQUEST_ALERT_WINDOW_SECONDS=60` + `SLOW_REQUEST_ALERT_THRESHOLD=10`（每分鐘 10 次慢請求觸發）+ `SLOW_REQUEST_ALERT_COOLDOWN_SECONDS=300`（同類事件 5 分鐘 dedupe）
- 修改 `config/base.py`：注入 `ops_alert: OpsAlertSettings`
- 新增 pytest：alerter sliding window + dedupe 行為 + line 推送 mock
- 文件：`docs/sop/observability.md` 新建（或 update 既有 runbook §5），含 Sentry / UptimeRobot / LINE channel 三層設定步驟

### Out of scope（不做）

- 不改 `SLOW_REQUEST_THRESHOLD_MS = 2000`（threshold 本身已是合理 SLO，調整另案）
- 不換 Sentry 為其他 APM
- 不接 prometheus / grafana 自建 metrics stack（Sentry Performance 已有 dashboard、UptimeRobot 已有 uptime UI，沒理由疊三套）
- 不寫 percentile 計算（p95/p99 由 Sentry Performance 算，本地只算每分鐘計數）
- 不做 alert escalation（單通道 LINE 已足；分級可後續加 PagerDuty）
- 不改其他 endpoint（純 middleware 層加 counter）

### Follow-up（不在本 spec）

- C: response_model 全覆蓋 + CI gate（下一輪）
- Sentry Performance 自訂 dashboard widget（user UI 操作，不寫 code）
- Alert escalation（連續 N 分鐘超 threshold 升級 PagerDuty）

---

## 3. 設計

### §1 慢請求 in-memory sliding window counter

新建 `utils/slow_request_alerter.py`：

```python
"""慢請求 sliding window counter + dedupe 觸發器。

行為：
- record_slow(path, elapsed_ms, status) 由 RequestLoggingMiddleware 呼叫
- 每分鐘窗口計數，超 SLOW_REQUEST_ALERT_THRESHOLD 觸發 ops_alert._dispatch
- 同 path 5 分鐘 cooldown，避免單一壞端點刷螢幕
- 純 in-memory（process-local）；多 worker 各自獨立計數，over-report 容忍
  （prod 設 X workers × threshold 10 = 實際 prod 30+ 次/分才觸 alert，可接受）
"""

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from config import settings
from services.ops_alert import notify_slow_request_burst

logger = logging.getLogger(__name__)

# path → deque[timestamp]，append/popleft 操作 O(1)
_window: dict[str, Deque[float]] = defaultdict(deque)
# path → last_alert_ts（cooldown）
_last_alert: dict[str, float] = {}
_lock = threading.Lock()


def record_slow(path: str, elapsed_ms: float, status: int) -> None:
    """記錄一次慢請求；若達 threshold 且過 cooldown，觸發 LINE 告警。"""
    cfg = settings.ops_alert
    if not cfg.enabled:
        return

    now = time.monotonic()
    window_start = now - cfg.slow_request_alert_window_seconds

    with _lock:
        q = _window[path]
        q.append(now)
        # 淘汰窗口外的 timestamp
        while q and q[0] < window_start:
            q.popleft()
        count = len(q)
        last = _last_alert.get(path, 0)
        in_cooldown = (now - last) < cfg.slow_request_alert_cooldown_seconds

        if count >= cfg.slow_request_alert_threshold and not in_cooldown:
            _last_alert[path] = now
            should_alert = True
        else:
            should_alert = False

    if should_alert:
        notify_slow_request_burst(
            path=path,
            count=count,
            window_seconds=cfg.slow_request_alert_window_seconds,
            sample_elapsed_ms=elapsed_ms,
            sample_status=status,
        )


def reset_for_tests() -> None:
    """測試 helper：清空窗口與 cooldown 狀態。"""
    with _lock:
        _window.clear()
        _last_alert.clear()
```

### §2 LINE ops alert wrapper

新建 `services/ops_alert.py`：

```python
"""Ops 告警通道 — 薄包裝 line_service.push_text_to_group。

DSN/group_id 缺即 no-op；異常吞掉並 log，不可影響 caller (middleware) 主流程。
"""

import logging

from config import settings

logger = logging.getLogger(__name__)


def notify_slow_request_burst(
    *,
    path: str,
    count: int,
    window_seconds: int,
    sample_elapsed_ms: float,
    sample_status: int,
) -> None:
    """通知慢請求突發；caller 已過 threshold + cooldown 判斷。"""
    cfg = settings.ops_alert
    if not cfg.line_group_id:
        logger.warning(
            "Slow request burst detected but OPS_ALERT_LINE_GROUP_ID 未設；"
            "path=%s count=%d window=%ds",
            path, count, window_seconds,
        )
        return

    text = (
        f"⚠️ 慢請求突發\n"
        f"endpoint：{path}\n"
        f"窗口：{window_seconds}s 內 {count} 次 > 2000ms\n"
        f"範例：{sample_elapsed_ms:.0f}ms / status={sample_status}\n"
        f"env：{settings.core.env}"
    )

    try:
        # lazy import 避免 module-load cycle（line_service 拉 sqlalchemy 等重依賴）
        from services.line_service import get_line_service
        svc = get_line_service()
        if svc is None:
            logger.warning("LineService 未初始化；slow request alert 跳過 LINE push")
            return
        svc.push_text_to_group(cfg.line_group_id, text)
    except Exception as e:  # 告警失敗不可炸 middleware
        logger.error("Slow request alert push 失敗：%s", e, exc_info=True)
```

### §3 OpsAlertSettings

新建 `config/ops_alert.py`：

```python
"""Ops 告警設定（慢請求突發 → LINE group push）。

group_id 為 None 時 alerter 仍會計數但僅 log warn（避免無聲）。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsAlertSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPS_ALERT_", extra="ignore", case_sensitive=False
    )

    line_group_id: str | None = Field(default=None)
    slow_request_alert_window_seconds: int = 60
    slow_request_alert_threshold: int = 10
    slow_request_alert_cooldown_seconds: int = 300

    @property
    def enabled(self) -> bool:
        """alerter 是否啟用 sliding window 累積（True 即使 line_group_id 缺也累計）。"""
        return self.slow_request_alert_window_seconds > 0
```

`config/base.py` 加一行：

```python
from config.ops_alert import OpsAlertSettings

class Settings(...):
    ...
    ops_alert: OpsAlertSettings = Field(default_factory=OpsAlertSettings)
```

### §4 RequestLoggingMiddleware 接入

`utils/request_logging.py` 慢請求分支多一行：

```python
from utils.slow_request_alerter import record_slow  # top-level import

...

if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
    logger.warning(
        "SLOW %s %s → %d (%.1fms) [rid=%s]",
        method, path, status, elapsed_ms, request_id,
    )
    record_slow(path, elapsed_ms, status)  # ← 新增
else:
    ...
```

**為何 path 不 normalize**：path 形如 `/api/students/42`，會被 sentry sanitize 成 `/api/students/:id`，但本告警用 raw path 反而是 feature（同 student 42 連續 timeout 是 dependency 慢 vs 同 student 不同人 timeout 是熱點 — raw path counter 自然分桶）。若日後噪音多再改 sanitize。

### §5 Sentry tracing turn-on（無 code 變動）

Sentry SDK 已 init `traces_sample_rate=0.1`（`utils/sentry_init.py:248`），`FastApiIntegration(transaction_style="endpoint")` 已掛。User 只需設 env：

```bash
# ivy-backend/.env (zeabur prod env)
SENTRY_DSN=https://...@sentry.io/...
SENTRY_ENVIRONMENT=production
SENTRY_TRACES_SAMPLE_RATE=0.1   # 已是預設可省略
```

設好之後 Sentry Performance dashboard 自動有：transaction list / p50/p75/p95/p99 / endpoint breakdown / span waterfall。本 spec 不寫 code，只在 §7 文件化步驟。

### §6 UptimeRobot 設定（無 code 變動）

`/health/ready` 已存在（連 DB 跑 `SELECT 1` 失敗回 503）。User 設定步驟：

1. 註冊 UptimeRobot 免費帳號（50 monitor / 5min interval）
2. 加 monitor: HTTP(s) → `https://<prod-domain>/api/health/ready`
3. interval 5 min
4. alert contact：LINE Notify（如已棄用改 webhook 到自家 LINE bot）or email
5. 預期 keep alive：99.9% uptime SLO

本 spec 不寫 code，只在 §7 文件化步驟。

---

## 4. 風險與緩解

| 風險 | 機率 | 影響 | 緩解 |
|------|------|------|------|
| In-memory counter multi-worker 各自獨立計數（多算/少算） | 高（gunicorn 多 worker） | 低（threshold 設計時已含 multiplier 容忍） | 文件記錄；後續若不夠用再改 redis-backed counter |
| LINE push 異常（network、token 過期）影響 middleware response | 低 | 高 | `services/ops_alert.notify_*` 全包 try/except + log error |
| Sentry traces overhead（每 request 10% 採樣） | 低 | 低 | `traces_sample_rate=0.1` 已是業界默認；prod CPU 影響 < 2% |
| Sliding window 記憶體洩漏（path cardinality 無限） | 中（如有 path traversal 攻擊） | 中 | path 已過 starlette router，無 unknown path 累積；prod path 數量有界（166 router files） |
| Alert spam（同 path 反覆觸發） | 中 | 中 | 5 分鐘 cooldown per path；不同 path 各自獨立 |
| /health/ready DB ping 在 readonly replica 失敗誤報 down | 低 | 中 | 現用 master pool；replica 未來引入時再加 connect timeout |

---

## 5. 測試

### 純函式單測（alerter）

`tests/test_slow_request_alerter.py`：

1. **窗口計數**：60s 窗口內 record 5 次同 path → count=5，alert 未觸發（threshold 10）
2. **超 threshold 觸發**：60s 窗口內 record 10 次同 path → notify_slow_request_burst 被 call 1 次
3. **窗口外淘汰**：record 5 次後 monkeypatch time 跳 +120s 再 record 5 次 → count=5（前 5 已淘汰），未觸發
4. **Cooldown**：record 10 次觸發 alert → 立刻再 record 10 次同 path → notify 仍只 call 1 次（cooldown 中）
5. **Cooldown 過後**：cooldown_seconds 後再 record 10 次 → notify call 第二次
6. **不同 path 獨立**：path A record 10 次 + path B record 10 次 → notify call 2 次

mock `services.ops_alert.notify_slow_request_burst` 與 `time.monotonic`（用 `freezegun` 或手 monkeypatch）。

### Wrapper 單測（ops_alert）

`tests/test_ops_alert.py`：

1. line_group_id 未設 → 不 call line_service，僅 log warning
2. line_service 未 init → log warning，不炸
3. line_service.push_text_to_group 拋 exception → 吞掉 + log error，不 propagate

### Integration smoke（手測）

無 unit test 自動化（middleware response time 浮動難 deterministic 觸發）。

手測 checklist：
1. 設 `SLOW_REQUEST_ALERT_THRESHOLD=2` + `SLOW_REQUEST_ALERT_WINDOW_SECONDS=10`
2. 設 `OPS_ALERT_LINE_GROUP_ID=<test group>`
3. ab/wrk 對 dev backend 連續打 10 次某慢 endpoint（或 monkeypatch SLOW_REQUEST_THRESHOLD_MS=0）
4. 確認 LINE 收到「⚠️ 慢請求突發」訊息
5. 立刻再打 10 次同 endpoint → 不應再收第二則（cooldown）

---

## 6. 提交策略

單 PR in `ivy-backend` repo（worktree `feat/slow-request-slo-2026-05-28-backend`）。

4 commits（含 spec）：

1. **docs(spec)**: spec 落地
2. **feat(observability)**: `config/ops_alert.py` + `config/base.py` 注入
3. **feat(observability)**: `utils/slow_request_alerter.py` + `services/ops_alert.py` + middleware 接入
4. **test(observability)**: `tests/test_slow_request_alerter.py` + `tests/test_ops_alert.py`
5. **docs(sop)**: 新建 `docs/sop/observability.md` 含 Sentry / UptimeRobot / LINE channel 三層設定步驟，dr-runbook §5 改為 link

---

## 7. 完成定義（DoD）

**Code 部分（auto）**：
- [ ] spec 落地
- [ ] `OpsAlertSettings` 註冊到 `settings.ops_alert`
- [ ] `slow_request_alerter` 模組落地 + `record_slow` 從 middleware 呼叫
- [ ] `services/ops_alert.notify_slow_request_burst` 落地
- [ ] pytest 新增 9 條 (6 alerter + 3 wrapper) 全綠
- [ ] `pytest -q` 全套零 regression
- [ ] `docs/sop/observability.md` 落地

**User 部分（manual ops）**：
- [ ] 兩端 env 設 SENTRY_DSN（已有 prod sentry project 即可）
- [ ] 註冊 UptimeRobot 並加 `/api/health/ready` monitor
- [ ] 設一個 LINE 「ops alert」群組並把 bot 加入，env `OPS_ALERT_LINE_GROUP_ID=<gid>`
- [ ] zeabur env 設好後 `restart service`
- [ ] 手測 burst 驗證收到 LINE 訊息
