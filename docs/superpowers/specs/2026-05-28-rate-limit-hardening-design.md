# Spec A: 限流 hardening（#8 + #9）

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：
- 🟠 P1 #8 — change-password / reset-password 完全無限流
- 🟠 P1 #9 — 14 處 router 用裸 `SlidingWindowLimiter(...)`，多 worker 部署即被 N 倍繞過
**對應後續 spec**：B (CSRF) / D (audit append-only) / C (Logger PII) / E (LINE 跨境) / F (staff refresh rotation) — 為獨立 spec，本 spec 不處理。

---

## 1. Why

### 1.1 攻擊面

**#8**：`api/auth.py:915 change_password` 與 `api/auth.py:1067 reset_password` 兩端點只靠 `Depends(get_current_user)` / `require_staff_permission(USER_MANAGEMENT_WRITE)` 守認證，未呼叫任何限流。對比 `login()` 的雙層 lockout，password 端點完全裸奔：

- 攻擊情境 A：staff cookie 被竊（XSS、共用電腦、phishing）→ 攻擊者拿到 `access_token` cookie 後可對 `/change-password` 無限次嘗試猜 `old_password`；login 端 `_check_ip_rate_limit` 完全打不到此端點。
- 攻擊情境 B：`USER_MANAGEMENT_WRITE` 持有者帳號被竊 → 攻擊者對 `/users/{id}/reset-password` 無限次重設別人密碼，每次都遞增 target user `token_version` 觸發強制下線。可拒絕服務全院員工。

**#9**：`utils/rate_limit.py:163 create_limiter()` factory 已就緒，但 14 處 router 模組層級仍直接 `SlidingWindowLimiter(...)`：

```
api/exports.py:46                                    # 大量資料匯出
api/gov_reports.py:41                                # 政府申報書下載
api/overtimes.py:20                                  # batch-approve 加班
api/leaves.py:24                                     # batch-approve 假單
api/portal/leaves.py:22                              # 教師 portal 上傳附件
api/activity/pos.py:106                              # POS 結帳
api/activity/public.py:84, 92, 101, 1077             # 公開報名查詢/註冊/查單/確認
api/activity/registrations_static.py:54, 62          # 報名匯出/批次付款
api/salary/calculate.py:100                          # 薪資結算
api/parent_portal/milestones.py:35                   # 家長端表情符號 react
```

`utils/rate_limit.py:5-23` 自註：「Python dict 儲存於記憶體中，僅在單一 worker process 內有效…多 worker 部署（如 gunicorn -w 4）→ 每個 worker 有獨立計數器」。Zeabur 一旦水平擴展（多 replica 或 gunicorn workers > 1），這 14 處限流即被 worker_count 倍繞過。`/public/register` 與 `/salary/calculate` 兩端點影響最大：前者是匿名公開端點（DoS / 報名重複砸資料），後者是高 DB 寫成本。

### 1.2 為何 hardening spec 不偷 refactor

既有 `_check_ip_rate_limit` / `_check_account_lockout` hardcoded 用 `_IP_SCOPE="login_ip"` 與 `_ACCOUNT_SCOPE="login_account"` 兩個常數，與 login flow 共享 DB counter scope。**不重構**這兩個 helper 接 scope 參數，因為：
1. login flow 既有 5103 pytest 已通過，refactor 會牽連 4 條 test (`tests/test_auth_rate_limit_db.py`)。
2. Hardening 範圍越小越不易引入回歸；獨立 scope 用 `count_recent_attempts` / `record_attempt` 直接 call DB helper 反而 cleaner。

---

## 2. Goals / Non-goals

### Goals
- (G1) 14 處 `SlidingWindowLimiter(...)` → `create_limiter(...)`，保留 var 名 / max_calls / window_seconds / name / error_detail 不動。
- (G2) `change_password` 套 IP per-call + user_id failure-counter 雙層保護，獨立 scope。
- (G3) `reset_password` 套 caller IP rate limit，獨立 scope。target user **不**套 lockout（防 admin 被竊後造成全員工帳號連帶 DoS）。
- (G4) CI grep gate hard-fail 阻止 `api/` 與 `services/` 內再次出現裸 `SlidingWindowLimiter(`。
- (G5) 零回歸：既有 5103 pytest + login flow + 14 處限流行為（在 `RATE_LIMIT_BACKEND=memory` 預設下）完全等價。

### Non-goals
- 不調整既有 14 處 limiter 的 max_calls / window_seconds 數值（避免 scope creep；prod 觀察到誤擋再單獨另案）。
- 不引入 Redis-backed limiter（`PostgresLimiter` 已足夠且 infra 簡單）。
- 不重構既有 `_check_ip_rate_limit` / `_check_account_lockout` helper 接 scope 參數（保留 login flow scope 不動）。
- 不改前端（純後端 + CI）。
- 不在本 spec 內處理 P0/P1 其餘 6 條 audit findings（B / C / D / E / F 為獨立 spec）。

---

## 3. Architecture

### 3.1 PR 結構（單 PR 三 commit）

| Commit | 範圍 | 檔案數 | 風險 |
|--------|------|--------|------|
| **C1**：`refactor(rate-limit): 14 routers switch to create_limiter() factory` | A1 | 14 .py + 1 test | 零（factory 預設等價 raw） |
| **C2**：`feat(auth): rate limit change-password and reset-password` | A2 | `api/auth.py` + 4 new test | 低（新增獨立 scope，不動 login） |
| **C3**：`chore(ci): grep gate forbid naked SlidingWindowLimiter()` | A3 | `.github/workflows/ci.yml` | 零（純 CI 配置） |

三 commit 同 PR 走完整 review，一次合併。Commit 紀律維持 1 commit 1 件事。

### 3.2 限流 scope 命名（DB counter scope 鍵）

| Scope | 用途 | window | max | 行為 |
|-------|------|--------|-----|------|
| `login_ip`（既存） | login per-IP 滑動視窗 | 300s | 20 | 不分成敗都計數 |
| `login_account`（既存） | login per-username 失敗 lockout | 900s | 5 | 只記失敗、成功 clear |
| **`pwd_change_ip`**（新） | change-password per-IP 滑動視窗 | 300s | 20 | 不分成敗 |
| **`pwd_change_user`**（新） | change-password per-user_id 失敗 lockout | 900s | 5 | 只記失敗、成功 clear |
| **`pwd_reset_ip`**（新） | reset-password per-caller IP | 300s | 20 | 不分成敗 |

**為何不重用 `login_ip` scope**：同 IP 高頻 change-password 不該壓掉 login quota，反之亦然；獨立 scope 讓兩端點各自滑動視窗，誤封正常用戶風險低。同樣理由 `pwd_reset_ip` 與 `pwd_change_ip` 不共用（重設與自改密碼是不同操作主體）。

數值刻意與 login lockout 一致（threshold=5、lockout=900s、IP window=300s/max=20），降低運維混淆。

### 3.3 模組內新 helper（私有，僅供 `api/auth.py` 內 password 端點使用）

```python
# api/auth.py 模組層級新增常數
_PWD_CHANGE_IP_SCOPE = "pwd_change_ip"
_PWD_CHANGE_USER_SCOPE = "pwd_change_user"
_PWD_RESET_IP_SCOPE = "pwd_reset_ip"
# window/threshold 復用既有 _IP_WINDOW / _IP_MAX_ATTEMPTS / _FAIL_THRESHOLD / _FAIL_LOCKOUT
# （刻意與 login 同；不另建常數避免分歧）


def _check_pwd_change_ip(ip: str) -> None:
    from utils.rate_limit_db import count_recent_attempts, record_attempt
    record_attempt(_PWD_CHANGE_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(_PWD_CHANGE_IP_SCOPE, ip, within_seconds=_IP_WINDOW)
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("change-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(429, "請求過於頻繁，請稍後再試")


def _check_pwd_change_user_lockout(user_id: int) -> None:
    from utils.rate_limit_db import count_recent_attempts
    key = f"user:{user_id}"
    count = count_recent_attempts(_PWD_CHANGE_USER_SCOPE, key, within_seconds=_FAIL_LOCKOUT)
    if count >= _FAIL_THRESHOLD:
        logger.warning("change-password 失敗次數超限: user_id=%d (failures=%d)", user_id, count)
        raise HTTPException(429, "密碼修改失敗次數過多，請稍後再試")


def _record_pwd_change_failure(user_id: int) -> None:
    from utils.rate_limit_db import record_attempt
    record_attempt(_PWD_CHANGE_USER_SCOPE, f"user:{user_id}", window_seconds=_FAIL_LOCKOUT)


def _clear_pwd_change_failures(user_id: int) -> None:
    from utils.rate_limit_db import clear_attempts
    clear_attempts(_PWD_CHANGE_USER_SCOPE, f"user:{user_id}")


def _check_pwd_reset_ip(ip: str) -> None:
    from utils.rate_limit_db import count_recent_attempts, record_attempt
    record_attempt(_PWD_RESET_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(_PWD_RESET_IP_SCOPE, ip, within_seconds=_IP_WINDOW)
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("reset-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(429, "請求過於頻繁，請稍後再試")
```

**為何不抽 `_check_lockout(scope, key, threshold, lockout)` 一個 generic helper**：login flow 既有 helper 已 hardcoded 兩個 scope 常數，refactor 牽連 login 既有 test 與 audit log（`write_login_audit` 內 `extras["scope"]` 都是 hardcoded `"ip_sliding_window"` / `"account_lockout"`）；Spec A 不動 login flow。三個新 helper 複製 pattern 看似 DRY 違反，但 scope 是耦合點，獨立反而易於日後個別調參。

### 3.4 端點接線

**`api/auth.py:915 change_password`**：

```python
@router.post("/change-password")
def change_password(
    data: ChangePasswordRequest,
    request: Request,   # ← 新增
    current_user: dict = Depends(get_current_user),
):
    """修改密碼"""
    client_ip = get_client_ip(request) or "unknown"
    user_id = current_user["user_id"]

    # 雙層限流：IP 滑動視窗 + 帳號失敗鎖定
    _check_pwd_change_ip(client_ip)
    _check_pwd_change_user_lockout(user_id)

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(404, USER_NOT_FOUND)
        if not verify_password(data.old_password, user.password_hash):
            _record_pwd_change_failure(user_id)  # 記失敗 → 累積觸發 lockout
            raise HTTPException(400, "舊密碼錯誤")
        # ... 其餘成功流程不變 ...
        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = False
        user.token_version = (user.token_version or 0) + 1
        # ... new_token 簽發、cookie set 不變 ...
        session.commit()
        _clear_pwd_change_failures(user_id)  # 成功後 clear 失敗計數
        # ... return ...
```

**`api/auth.py:1067 reset_password`**：

```python
@router.put("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    data: ResetPasswordRequest,
    request: Request,   # ← 新增
    current_user: dict = Depends(require_staff_permission(Permission.USER_MANAGEMENT_WRITE)),
):
    """重設密碼（admin 代為操作）"""
    client_ip = get_client_ip(request) or "unknown"
    _check_pwd_reset_ip(client_ip)   # 防 admin cookie 被竊狂刷別人

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        # ... 其餘流程不變 ...
```

**注意**：`reset_password` **不**對 target user 套 lockout（不 record/check `f"user:{user_id}"`）。設計理由：reset_password 是 admin 主動操作、不是 password 驗證，每次都「成功」；若按 target user 累計，等同把每個被重設的 user 連帶暫時鎖死。攻擊者若能拿到 admin cookie 反而可批次鎖死全員工帳號，造成更大 DoS。**caller IP 限流 + 既存 audit middleware 記錄 admin user_id**是更合理的防線。

### 3.5 14 處 routers 機械替換（PR-A1）

替換規則：
```python
# Before
_x_limiter = SlidingWindowLimiter(max_calls=N, window_seconds=W, name="x", error_detail="...")
# After
_x_limiter = create_limiter(max_calls=N, window_seconds=W, name="x", error_detail="...")
```

完整 14 處 file:line 列表（已 verify）：

| 檔案 | line | 變數名 |
|------|------|--------|
| `api/exports.py` | 46 | `_export_rate_limit` |
| `api/gov_reports.py` | 41 | `_rate_limit` |
| `api/overtimes.py` | 20 | `_batch_approve_limiter` |
| `api/leaves.py` | 24 | `_batch_approve_limiter` |
| `api/portal/leaves.py` | 22 | `_attach_upload_limiter` |
| `api/activity/pos.py` | 106 | `_pos_checkout_limiter` |
| `api/activity/public.py` | 84 | `_public_query_limiter_instance` |
| `api/activity/public.py` | 92 | `_public_register_limiter_instance` |
| `api/activity/public.py` | 101 | `_public_inquiry_limiter_instance` |
| `api/activity/public.py` | 1077 | `_public_confirm_limiter_instance` |
| `api/activity/registrations_static.py` | 54 | `_export_limiter` |
| `api/activity/registrations_static.py` | 62 | `_batch_payment_limiter` |
| `api/salary/calculate.py` | 100 | `_salary_calc_limiter` |
| `api/parent_portal/milestones.py` | 35 | `_react_limiter` |

各檔需新增 / 調整的 import：
```python
# Before
from utils.rate_limit import SlidingWindowLimiter
# After
from utils.rate_limit import create_limiter
```

對 14 處所有 file，verify 是否還有其他地方使用 `SlidingWindowLimiter` 名稱（例如 `isinstance(x, SlidingWindowLimiter)` 或 type hint）。預期答案：無（這 14 處都只用於模組層級 factory 構造）。test fixture 仍可保留 `SlidingWindowLimiter` import（在白名單內）。

### 3.6 CI grep gate（PR-A3）

新增 job 到 `.github/workflows/ci.yml`：

```yaml
naked-rate-limiter-gate:
  name: Forbid naked SlidingWindowLimiter() in api/ services/
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - name: Grep
      run: |
        set -e
        if grep -rn --include="*.py" "SlidingWindowLimiter(" api/ services/ 2>/dev/null; then
          echo "::error::Use create_limiter() factory from utils.rate_limit, not raw SlidingWindowLimiter()."
          echo "::error::See utils/rate_limit.py:163 for backend dispatch (memory/postgres) and docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md"
          exit 1
        fi
        echo "OK: no naked SlidingWindowLimiter() outside utils/ + tests/"
```

白名單範圍：`utils/rate_limit.py`（定義處）+ `tests/`（factory 行為測試需要 `isinstance` 檢查 — `tests/test_rate_limit_pg.py:142,153,167`）。grep 範圍鎖 `api/` + `services/`，自然排除這兩處。

不加 `# noqa` 機制（避免雙重規則 + 嵌入原始註解可能交互全面關掉 noqa）。

### 3.7 行為等價性論證

`create_limiter()` 在 `RATE_LIMIT_BACKEND=memory` 或未設時回傳 `SlidingWindowLimiter` 實例（`utils/rate_limit.py:184-189`），與目前 prod 行為（已設或未設 `RATE_LIMIT_BACKEND`？需 USER 確認）完全等價。差別僅在當 USER 設 `RATE_LIMIT_BACKEND=postgres` 時自動切換為 `PostgresLimiter`。

**Roll-out 連動**：本 spec 改完後，USER 需在 Zeabur 後端 env 設 `RATE_LIMIT_BACKEND=postgres`（若未設）以啟動多 worker 安全的計數器。若不設，行為與目前一致；設了即生效（程式碼無需重 deploy）。

---

## 4. 測試計畫

### 4.1 PR-A1 (14 routers replacement)

- 全套 pytest 5103 baseline 必須 0 regression（factory 預設行為等價）。
- 新增 `tests/test_rate_limit_router_usage.py`：以 AST 走訪 14 個檔案，asserting 每處 `SlidingWindowLimiter(` 直接呼叫已消失，所有 limiter 都從 `create_limiter` 構造。此 test 防回歸（搭配 CI gate 雙重保險）。

```python
# tests/test_rate_limit_router_usage.py
import ast
from pathlib import Path

ROUTERS_REQUIRING_FACTORY = [
    "api/exports.py", "api/gov_reports.py", "api/overtimes.py", "api/leaves.py",
    "api/portal/leaves.py", "api/activity/pos.py", "api/activity/public.py",
    "api/activity/registrations_static.py", "api/salary/calculate.py",
    "api/parent_portal/milestones.py",
]

def test_no_naked_sliding_window_limiter():
    for path in ROUTERS_REQUIRING_FACTORY:
        source = Path(path).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "SlidingWindowLimiter", (
                    f"{path}: use create_limiter() factory instead of raw SlidingWindowLimiter()"
                )
```

### 4.2 PR-A2 (password rate limit)

新增 4 個 pytest 在 `tests/test_auth_password_rate_limit.py`：

1. **test_change_password_user_lockout**：mock 同 user 連續 5 次 old_password 錯誤 → 第 6 次返回 429。
2. **test_change_password_clear_failures_on_success**：失敗 4 次後成功一次，再次 trigger 失敗 → 第 6 次不再 429（counter 已 clear）。
3. **test_reset_password_ip_rate_limit**：mock 同 IP 連續 20 次 reset → 第 21 次返回 429。
4. **test_reset_password_no_target_user_lockout**：admin 對同一 target user 連續重設 10 次後，assert 兩件事：
   - `count_recent_attempts(_ACCOUNT_SCOPE, target_user.username, within_seconds=_FAIL_LOCKOUT) == 0`（沒被誤記入 login_account scope）
   - `_check_account_lockout(target_user.username)` 不拋 429（target user 仍可 login）

mock 策略：用 `monkeypatch` patch `utils.rate_limit_db.count_recent_attempts` 與 `record_attempt`，或實際走 SQLite test fixture（推薦後者，鏡像 prod 行為）。

### 4.3 PR-A3 (CI gate)

CI workflow 整合測試（不寫 unit test，但本地驗證）：
- 故意在 `api/exports.py` 插一行 `_x = SlidingWindowLimiter(max_calls=1, window_seconds=60)` 後跑 grep job → 預期 fail。
- 移除後 → 預期 pass。

### 4.4 既有 login flow 回歸驗證

`tests/test_auth_rate_limit_db.py` 既有 test 全綠（login flow 完全未動）。

---

## 5. Roll-out

### 5.1 部署步驟

1. PR 合併（單 PR，3 commit + 5 個新 test + CI workflow 更新）。
2. Zeabur 後端服務檢查 env：
   - **必要**：確認 `RATE_LIMIT_BACKEND=postgres`（若未設則無效，14 處仍以 memory 模式運作）。
   - **可選**：若 worker count = 1，memory 模式可接受但建議仍切 postgres 為未來水平擴展鋪路。
3. 部署後 smoke：
   - login 正常（既有 flow 未動）。
   - change-password 正常一次（測試帳號）。
   - 連續錯 5 次 change-password 確認 429。
   - 確認 Sentry 無 lockout 事件被誤報為 5xx。

### 5.2 回退方案

純 hotfix revert PR：行為立刻回到「14 處走 raw SlidingWindowLimiter、password 端點裸奔」。無 DB migration、無 schema 變動，回退零成本。

### 5.3 監控指標

- 7 天內觀察 Sentry / log 是否有 `429` 噴量異常：
  - `change-password IP 頻率超限` log line（warning level）
  - `change-password 失敗次數超限` log line
  - `reset-password IP 頻率超限` log line
- 若有大量誤觸（例如 HR 批次幫多 staff reset 過快），再單獨調 `_IP_MAX_ATTEMPTS` 或 `_FAIL_THRESHOLD`（另案）。

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| `PostgresLimiter` DB 連線失敗時 fail-open（`utils/rate_limit.py:147`）→ 限流空轉 | 限流暫時失效但不會 500 | 既有設計刻意 fail-open（DB 故障時優先保持 API 可用）；fail-open 已 log warning，Sentry 會收到 |
| password 端點 `request: Request` 新參數可能破壞既有 test fixture | A2 4 個新 test 通過 + 既有 password test 全綠 | 既有 `change_password` test 預期數量極少（password 變更不像登入那樣多測試覆蓋）；A2 PR 內補齊 |
| `pwd_change_user` 計數 key `f"user:{id}"` 與既有 `_ACCOUNT_SCOPE="login_account"` 共用 `rate_limit_buckets` 表但 scope 不同，理論隔離但 storage 共用 | 表大小成長略增 | 既有 `cleanup_rate_limit_buckets` GC 每 5 分鐘清過期，scope 多一個對表大小影響 < 1% |
| CI grep gate 漏網（例如 `SlidingWindowLimiter` 寫在多行 `\n` 後） | gate 失效 | `grep "SlidingWindowLimiter("` pattern 比對 `(`，必須是 call site；多行 split 不會發生（Python 慣例 constructor call 都在同一行）|
| 14 處替換時誤改參數順序 | limiter 行為異常 | AST 替換 + diff review；C1 commit 嚴格 1:1 mechanical |

---

## 7. Out of scope（明確列出）

以下 audit findings 在後續 spec 處理，本 spec **不**包含：

- **Spec B (#12)**：CSRF Origin/Referer middleware
- **Spec C (#7)**：Logger PII redaction（logging.Filter + 改 log call site）
- **Spec D (#10)**：audit_logs DB-layer append-only（Postgres role + REVOKE + trigger）
- **Spec E (#6)**：LINE 推播去識別化 + F1 consent flag + 隱私政策跨境告知
- **Spec F (#11)**：員工端 refresh token rotation + active sessions 列表 + 強制下線

本 spec 範圍嚴格鎖定限流 hardening 兩條（#8 + #9）。

---

## 8. 驗收 checklist（user 手測）

PR 合併後，USER 手動驗證：

- [ ] login 正常（既有 flow 不動）：對 admin 帳號連錯 5 次密碼 → 第 6 次回 429 「密碼錯誤次數過多」。
- [ ] login 後改密碼正常：login 一次成功 → 立即 change-password 一次成功。
- [ ] change-password 失敗 lockout：先 login 成功 → change-password 連錯 5 次 old_password → 第 6 次回 429 「密碼修改失敗次數過多」。
- [ ] change-password 成功清零：失敗 4 次後成功一次 → 再失敗一次不會立刻 429。
- [ ] reset-password IP 限流：admin 帳號連續 reset 20 次（同一 IP）→ 第 21 次回 429「請求過於頻繁」。
- [ ] reset-password 不連帶 target user 鎖：admin reset target user 5+ 次後 target user 仍可正常 login（沒有「target 被 admin 重設多次而導致 target 不能 login」副作用）。
- [ ] 14 處 limiter 行為驗證（任一處 smoke）：例如連續 POST `/api/salary/calculate` 超過 quota → 仍應 429（行為與替換前等價）。
- [ ] Zeabur env 確認 `RATE_LIMIT_BACKEND=postgres` 已設（若 worker > 1 必要）。
- [ ] CI grep gate 上線：刻意在某 router 加裸 `SlidingWindowLimiter()` 一次 → PR CI fail；revert 後 → PR CI pass。

---

## 9. 後續 follow-up（不在本 spec）

- 若 prod 7 天觀察 `change-password 失敗次數超限` log 高頻 → 評估是否從 `_FAIL_THRESHOLD=5` 調至 `7` 或調寬 `_FAIL_LOCKOUT`。
- 若 Zeabur 設 `RATE_LIMIT_BACKEND=postgres` 後 `rate_limit_buckets` 表暴增，加 monitoring 與 GC 頻率調整（既有 GC 已在 scheduler，每 5 分鐘清一次）。
- 評估是否把 `_check_pwd_change_*` 移到 `utils/auth_rate_limit.py` 共用模組（如果後續其他 auth 端點要重用）。當前 A2 只 2 個端點不抽。
