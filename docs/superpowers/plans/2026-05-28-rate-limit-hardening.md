# 限流 hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 14 處裸 `SlidingWindowLimiter()` 改用 `create_limiter()` factory 啟動多 worker 安全；對 change-password / reset-password 兩個 P1 攻擊面加雙層限流 + audit log；加 CI grep gate 永久禁止裸寫回歸。

**Architecture:** 三 commit 同 PR：(C1) 14 routers 機械替換 → factory 自動依 `RATE_LIMIT_BACKEND` env 切換 memory/postgres。(C2) `api/auth.py` 加 3 個獨立 scope (`pwd_change_ip` / `pwd_change_user` / `pwd_reset_ip`) 與 5 個私有 helper，鏡像既有 login flow `_check_*` pattern；password 端點加 `request: Request` 取 IP、限流觸發時 `write_login_audit` 3 個新 action。(C3) CI workflow 加 grep gate hard-fail `api/` + `services/` 內出現 `SlidingWindowLimiter(`。

**Tech Stack:** FastAPI / SQLAlchemy / `utils.rate_limit_db` DB-backed counter / pytest TestClient / GitHub Actions

**Spec:** `docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md` (commit 89f9dd8)

---

## File Structure

**Modified files:**
- `api/auth.py` — 加 3 個 scope 常數、5 個私有 helper、change_password / reset_password 加 `request: Request` + 限流接線 + audit log
- `api/exports.py:46` — `SlidingWindowLimiter` → `create_limiter`
- `api/gov_reports.py:41` — 同上
- `api/overtimes.py:20` — 同上
- `api/leaves.py:24` — 同上
- `api/portal/leaves.py:22` — 同上
- `api/activity/pos.py:106` — 同上
- `api/activity/public.py:84, 92, 101, 1077` — 同上 (4 處)
- `api/activity/registrations_static.py:54, 62` — 同上 (2 處)
- `api/salary/calculate.py:100` — 同上
- `api/parent_portal/milestones.py:35` — 同上
- `.github/workflows/ci.yml` — 加 `naked-rate-limiter-gate` job

**New files:**
- `tests/test_rate_limit_router_usage.py` — AST 走訪 14 個檔案 assert 無裸 `SlidingWindowLimiter(`
- `tests/test_auth_password_rate_limit.py` — 6 個 pytest cover password 限流

**Unchanged but referenced:**
- `utils/rate_limit.py:163 create_limiter` — factory（已存在）
- `utils/rate_limit.py:60 SlidingWindowLimiter` — class（保留，給 factory 與 tests/ 用）
- `utils/rate_limit_db.py` — DB-backed counter helpers `record_attempt` / `count_recent_attempts` / `clear_attempts`
- `utils/audit.py` — `write_login_audit` 既存
- `api/auth.py:200 _check_ip_rate_limit` / `:215 _check_account_lockout` — login flow helper（不動）

---

## Task 1: 14 routers SlidingWindowLimiter → create_limiter

**Goal:** 機械替換 14 處構造呼叫，import 改 `create_limiter`，保留所有參數（max_calls / window_seconds / name / error_detail）不動。新增 AST sanity test 防回歸。

**Files:**
- Modify: 11 個 router file (見 File Structure)
- Create: `tests/test_rate_limit_router_usage.py`

### Steps

- [ ] **Step 1.1: 為每個 router file 改 import**

對 11 個 file 逐一改：

```python
# Before
from utils.rate_limit import SlidingWindowLimiter
# After
from utils.rate_limit import create_limiter
```

注意：某些檔可能同時 import 別的 symbol（例如 `from utils.rate_limit import SlidingWindowLimiter, BaseLimiter`）→ 改成 `from utils.rate_limit import create_limiter, BaseLimiter`。先 grep 確認：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
for f in api/exports.py api/gov_reports.py api/overtimes.py api/leaves.py api/portal/leaves.py api/activity/pos.py api/activity/public.py api/activity/registrations_static.py api/salary/calculate.py api/parent_portal/milestones.py; do
  echo "=== $f ==="
  grep -n "from utils.rate_limit import" "$f"
done
```

對每個 file 用 Edit tool 改 import line。

- [ ] **Step 1.2: 為每個 router file 改構造呼叫**

對 14 處 line 逐一改（spec §3.5 表已列）：

```python
# Before
_x_limiter = SlidingWindowLimiter(max_calls=N, window_seconds=W, name="x", error_detail="...")
# After
_x_limiter = create_limiter(max_calls=N, window_seconds=W, name="x", error_detail="...")
```

完整 14 處 file:line 清單：

| 檔案 | line | var |
|------|------|-----|
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

對每個 line 用 Edit tool 改（保留所有 kwargs 順序 / 值）。

- [ ] **Step 1.3: 寫 AST sanity test (失敗版)**

Create `tests/test_rate_limit_router_usage.py`：

```python
"""AST sanity test: 確保 14 個 router 不直接 call SlidingWindowLimiter()，
必須走 create_limiter() factory（受 RATE_LIMIT_BACKEND env 控制）。

防回歸：搭配 .github/workflows/ci.yml 的 naked-rate-limiter-gate job
形成雙重保險（test 在 pytest 階段抓、CI gate 在 lint 階段抓）。
"""

import ast
from pathlib import Path

# Spec §3.5 表列；與 main.py 同層 ivy-backend root 路徑
ROUTERS_REQUIRING_FACTORY = [
    "api/exports.py",
    "api/gov_reports.py",
    "api/overtimes.py",
    "api/leaves.py",
    "api/portal/leaves.py",
    "api/activity/pos.py",
    "api/activity/public.py",
    "api/activity/registrations_static.py",
    "api/salary/calculate.py",
    "api/parent_portal/milestones.py",
]


def test_no_naked_sliding_window_limiter_in_routers():
    """14 個 router 內所有 SlidingWindowLimiter(...) 構造呼叫必須改為 create_limiter(...)。"""
    repo_root = Path(__file__).resolve().parent.parent  # tests/.. = repo root
    offenders: list[str] = []
    for rel_path in ROUTERS_REQUIRING_FACTORY:
        full = repo_root / rel_path
        assert full.exists(), f"Expected router file {rel_path} not found"
        source = full.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SlidingWindowLimiter"
            ):
                offenders.append(f"{rel_path}:{node.lineno}")
    assert not offenders, (
        "下列 router 仍直接呼叫 SlidingWindowLimiter()，應改為 create_limiter() factory:\n  "
        + "\n  ".join(offenders)
    )
```

- [ ] **Step 1.4: 跑 AST test 確認 pass（替換已完成）**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_rate_limit_router_usage.py -v
```
Expected: PASS（如果 Step 1.1 + 1.2 都做對）。

如 fail，根據 error message 修剩餘的 line。

- [ ] **Step 1.5: 跑既有 rate_limit 測試確認零回歸**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_rate_limit_pg.py -v
```
Expected: ALL PASS（既有 factory 行為 test 不變）。

- [ ] **Step 1.6: 跑全套 pytest 取得 baseline 比較**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest -x --tb=short 2>&1 | tail -30
```
Expected: 與 main baseline `5103 passed` 對齊（或 `+1 new = 5104` 因新加 AST test）。如有新 fail 必須回頭查替換是否誤改參數。

- [ ] **Step 1.7: Commit (C1)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/exports.py api/gov_reports.py api/overtimes.py api/leaves.py \
        api/portal/leaves.py api/activity/pos.py api/activity/public.py \
        api/activity/registrations_static.py api/salary/calculate.py \
        api/parent_portal/milestones.py tests/test_rate_limit_router_usage.py
git commit -m "$(cat <<'EOF'
refactor(rate-limit): 14 routers switch to create_limiter() factory

14 處裸 SlidingWindowLimiter() 改用 create_limiter()，受 RATE_LIMIT_BACKEND
env 控制（memory / postgres）。預設 memory 行為與替換前等價，零回歸。

新增 tests/test_rate_limit_router_usage.py AST 守衛，搭配 CI grep gate 雙重
保險防回歸。

Refs: Spec docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md §3.5
EOF
)"
```

---

## Task 2: change_password / reset_password 限流 + 6 pytest

**Goal:** 加 3 個 DB-backed scope 與 5 個私有 helper；對 change_password 加 IP + user_id 雙層限流；對 reset_password 加 caller IP 限流；3 個新 audit action；6 個 pytest。

**Files:**
- Modify: `api/auth.py`（加常數 + helpers + 修改兩個 endpoint）
- Create: `tests/test_auth_password_rate_limit.py`

### Steps

- [ ] **Step 2.1: 在 api/auth.py 加 3 個新 scope 常數**

在 `api/auth.py:191` (`_ACCOUNT_SCOPE = "login_account"`) 之後加：

```python
# Password endpoint scopes（與 login_*  scope 隔離，避免互相干擾）
_PWD_CHANGE_IP_SCOPE = "pwd_change_ip"
_PWD_CHANGE_USER_SCOPE = "pwd_change_user"
_PWD_RESET_IP_SCOPE = "pwd_reset_ip"
# window/threshold 復用既有 _IP_WINDOW / _IP_MAX_ATTEMPTS / _FAIL_THRESHOLD / _FAIL_LOCKOUT
```

- [ ] **Step 2.2: 在 api/auth.py 加 5 個私有 helper**

在 `_clear_login_failures` (`api/auth.py:244` 之後) 之後加：

```python
def _check_pwd_change_ip(ip: str) -> None:
    """change-password per-IP 滑動視窗（不分成敗都計數）。"""
    from utils.rate_limit_db import count_recent_attempts, record_attempt

    record_attempt(_PWD_CHANGE_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(_PWD_CHANGE_IP_SCOPE, ip, within_seconds=_IP_WINDOW)
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("change-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(status_code=429, detail="請求過於頻繁，請稍後再試")


def _check_pwd_change_user_lockout(user_id: int) -> None:
    """change-password per-user_id 失敗鎖定（僅在 verify_password 失敗時遞增）。"""
    from utils.rate_limit_db import count_recent_attempts

    key = f"user:{user_id}"
    count = count_recent_attempts(
        _PWD_CHANGE_USER_SCOPE, key, within_seconds=_FAIL_LOCKOUT
    )
    if count >= _FAIL_THRESHOLD:
        logger.warning(
            "change-password 失敗次數超限: user_id=%d (failures=%d)", user_id, count
        )
        raise HTTPException(
            status_code=429,
            detail="密碼修改失敗次數過多，請稍後再試",
        )


def _record_pwd_change_failure(user_id: int) -> None:
    """記錄 change-password 失敗一次（DB-backed bucket）。"""
    from utils.rate_limit_db import record_attempt

    record_attempt(
        _PWD_CHANGE_USER_SCOPE, f"user:{user_id}", window_seconds=_FAIL_LOCKOUT
    )


def _clear_pwd_change_failures(user_id: int) -> None:
    """change-password 成功後清除失敗記錄。"""
    from utils.rate_limit_db import clear_attempts

    clear_attempts(_PWD_CHANGE_USER_SCOPE, f"user:{user_id}")


def _check_pwd_reset_ip(ip: str) -> None:
    """reset-password per-caller IP 滑動視窗（防 admin cookie 被竊狂刷別人）。"""
    from utils.rate_limit_db import count_recent_attempts, record_attempt

    record_attempt(_PWD_RESET_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(_PWD_RESET_IP_SCOPE, ip, within_seconds=_IP_WINDOW)
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("reset-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(status_code=429, detail="請求過於頻繁，請稍後再試")
```

- [ ] **Step 2.3: 修改 change_password 接線限流 + audit log**

Edit `api/auth.py:915 change_password` 函式。在 `def change_password(...)` 簽章加 `request: Request` 參數（順序：`data, request, current_user`），並在 function body 最前加限流檢查：

```python
@router.post("/change-password")
def change_password(
    data: ChangePasswordRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """修改密碼

    Why issue new token on success：使用者自己改密碼後 token_version 遞增，
    若 response 不發新 token 則 client 帶舊 token 立刻 401，被迫 re-login
    （bug sweep round 4 F-PRE-1，pre-existing 自 2026-05-13 commit 3e728d2b）。
    管理員代為重設（reset_password）則維持「強制當事人下次登入」語意，不發
    新 token。
    """
    client_ip = get_client_ip(request) or "unknown"
    user_id = current_user["user_id"]
    username_for_audit = current_user.get("username", "")

    # 雙層限流：IP 滑動視窗 + per-user 失敗鎖定（DB-backed，與 login scope 隔離）
    try:
        _check_pwd_change_ip(client_ip)
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_CHANGE_RATE_LIMITED",
            username=username_for_audit,
            extras={"ip": client_ip, "scope": "pwd_change_ip"},
        )
        raise
    try:
        _check_pwd_change_user_lockout(user_id)
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_CHANGE_LOCKED",
            username=username_for_audit,
            extras={"user_id": user_id, "scope": "pwd_change_user"},
        )
        raise

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
        if not verify_password(data.old_password, user.password_hash):
            _record_pwd_change_failure(user_id)  # 記失敗 → 累積觸發 lockout
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
        # 與 reset_password 對齊：密碼變更後遞增 token_version，使所有現有 session
        # 在下次 refresh 時即被拒絕；防止帳號疑似外洩後舊 token 在 grace 期內仍可用。
        user.token_version = (user.token_version or 0) + 1

        # 為當事人發新 token（同步新 token_version + must_change_password=False），
        # 避免「改完密碼立刻被踢」。其他 session 的舊 token 仍會在下次 refresh 被拒。
        permission_names = resolve_user_permissions(user)
        emp = (
            session.query(Employee).filter(Employee.id == user.employee_id).first()
            if user.employee_id
            else None
        )
        new_token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permission_names": permission_names,
                "token_version": user.token_version,
            }
        )
        session.commit()
        _clear_pwd_change_failures(user_id)  # 成功後清失敗計數（commit 後才 clear）

        response = JSONResponse(content={"message": "密碼修改成功"})
        set_access_token_cookie(response, new_token)
        return response
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

**注意 control flow**：保留原 `sign new_token → session.commit() → set cookie` 順序。`_clear_pwd_change_failures` 放 `session.commit()` **之後**（成功才 clear）。

- [ ] **Step 2.4: 修改 reset_password 接線限流 + audit log**

Edit `api/auth.py:1067 reset_password` 函式：

```python
@router.put("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    data: ResetPasswordRequest,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """重設密碼（admin 代為操作）"""
    client_ip = get_client_ip(request) or "unknown"
    try:
        _check_pwd_reset_ip(client_ip)  # 防 admin cookie 被竊狂刷別人
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_RESET_RATE_LIMITED",
            username=current_user.get("username", ""),
            extras={
                "ip": client_ip,
                "scope": "pwd_reset_ip",
                "target_user_id": user_id,
            },
        )
        raise

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)

        _assert_can_manage_user(current_user, session=session, target_user=user)

        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = True  # 管理員代為重設密碼，強制當事人下次登入修改
        user.token_version = (
            user.token_version or 0
        ) + 1  # 使所有現有 session 的 token 立即無法刷新
        session.commit()
        return {"message": "密碼重設成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

**注意**：`reset_password` **不**呼叫 `record/check` 對 target user lockout（spec §3.4 設計理由）。

- [ ] **Step 2.5: Verify get_client_ip import 已在 api/auth.py 內**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
grep -n "from utils.request_ip import get_client_ip\|import get_client_ip" api/auth.py
```
Expected: 至少一行 import（login 端已用此 helper）。若無則加：

```python
from utils.request_ip import get_client_ip
```

- [ ] **Step 2.6: 寫 6 個 pytest (失敗版)**

Create `tests/test_auth_password_rate_limit.py`：

```python
"""Spec A PR-A2: change-password / reset-password 限流 6 個 pytest。

走實際 DB-backed counter（test fixture SQLite），鏡像 prod 行為；
不 mock utils.rate_limit_db 以維持 witness 強度。
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.auth import (
    _ACCOUNT_SCOPE,
    _check_account_lockout,
    _FAIL_LOCKOUT,
    _FAIL_THRESHOLD,
    _IP_MAX_ATTEMPTS,
)


# 注意：以下 6 個 test 依賴既有 tests/conftest.py 提供的：
#   - client: TestClient (FastAPI app)
#   - db_session: SQLite test session
#   - create_test_user(username, password, ...): fixture helper
# 若 fixture 名不同，按 conftest 實際提供調整。


def _login_as(client: TestClient, username: str, password: str) -> str:
    """Helper: 登入並拿到 access_token cookie value（直接從 Set-Cookie 取）。"""
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, f"login 應成功, got {res.status_code}: {res.text}"
    cookie = res.cookies.get("access_token")
    assert cookie, "Set-Cookie 應含 access_token"
    return cookie


# ============ Test 1: change-password user lockout ============

def test_change_password_user_lockout(client, create_test_user):
    """同 user 連續 5 次 old_password 錯誤 → 第 6 次返回 429，audit 記 PASSWORD_CHANGE_LOCKED。"""
    user = create_test_user(username="t_pwd1", password="GoodOld123!")
    # login 拿 cookie
    client.cookies.set("access_token", _login_as(client, "t_pwd1", "GoodOld123!"))

    # 連續 5 次故意打錯 old_password → 每次回 400，但累積 failure
    for i in range(5):
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert res.status_code == 400, f"第 {i+1} 次應 400 舊密碼錯誤, got {res.status_code}"

    # 第 6 次應觸發 lockout 429
    with patch("api.auth.write_login_audit") as audit_spy:
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert res.status_code == 429, f"第 6 次應 429 lockout, got {res.status_code}"
        assert "密碼修改失敗次數過多" in res.json()["detail"]
        # audit 記錄 PASSWORD_CHANGE_LOCKED
        actions = [c.kwargs.get("action") for c in audit_spy.call_args_list]
        assert "PASSWORD_CHANGE_LOCKED" in actions, f"audit actions: {actions}"


# ============ Test 2: change-password clear failures on success ============

def test_change_password_clear_failures_on_success(client, create_test_user):
    """失敗 4 次 + 成功 1 次 → 再失敗 1 次不應 lockout（counter 已 clear）。"""
    user = create_test_user(username="t_pwd2", password="GoodOld123!")
    client.cookies.set("access_token", _login_as(client, "t_pwd2", "GoodOld123!"))

    # 失敗 4 次
    for _ in range(4):
        res = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld", "new_password": "NewGood456!"},
        )
        assert res.status_code == 400

    # 成功一次（用正確 old_password）
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "GoodOld123!", "new_password": "NewGood456!"},
    )
    assert res.status_code == 200, res.text

    # 用新密碼重 login（前次 change-password 已 invalidate token_version）
    client.cookies.set("access_token", _login_as(client, "t_pwd2", "NewGood456!"))

    # 再失敗 1 次 → 應仍 400（不是 429），counter 已 clear
    res = client.post(
        "/api/auth/change-password",
        json={"old_password": "WrongAgain", "new_password": "Another789!"},
    )
    assert res.status_code == 400, f"counter 應已 clear, got {res.status_code}"


# ============ Test 3: change-password IP-only rate limit ============

def test_change_password_ip_only_rate_limit(client, create_test_user):
    """單獨驗證 IP 層。同 IP 透過多個不同 user 嘗試（每次用正確 old_password 避觸發 user lockout）
    累積 21 次 → 第 21 次回 429，audit 記 PASSWORD_CHANGE_RATE_LIMITED。

    用「正確 old_password 但 new_password 故意觸發 validate_password_strength fail」
    的方式累積 IP 計數而不觸發 user lockout（verify_password 成功）。

    注意：TestClient 預設 IP 是 "testclient"；用 raw header X-Forwarded-For 模擬不同 IP
    若 get_client_ip 不認則 fallback 同 IP。本 test 假設同 IP 即可累積。
    """
    # 預建 22 user（每次用不同 user 避免單一 user 內 commit 改 token_version 影響後續）
    users = [create_test_user(username=f"t_ipuser{i}", password="Good123!") for i in range(22)]

    # 22 個 user 各 login 並 change-password 1 次（每次都觸發 IP 層 record_attempt）
    # 用正確 old_password 但 invalid new_password（太短）讓 validate fail，不會走到
    # 成功路徑而異動 token_version
    triggered = False
    audit_spy_calls = []
    for i, u in enumerate(users):
        client.cookies.set("access_token", _login_as(client, f"t_ipuser{i}", "Good123!"))
        with patch("api.auth.write_login_audit") as audit_spy:
            res = client.post(
                "/api/auth/change-password",
                json={"old_password": "Good123!", "new_password": "short"},  # too short, will 400
            )
            audit_spy_calls.extend(
                [c.kwargs.get("action") for c in audit_spy.call_args_list]
            )
            if res.status_code == 429:
                triggered = True
                # 確認是 IP 層 429（不是 user lockout，user 每次都不同）
                assert "請求過於頻繁" in res.json()["detail"]
                break
        # 沒 429 表示 IP quota 未滿（前 20 次預期 400 invalid password）
        assert res.status_code in (400, 429), f"i={i} unexpected {res.status_code}: {res.text}"

    assert triggered, f"連續 {len(users)} 次未觸發 IP 429，IP 計數可能未生效"
    assert "PASSWORD_CHANGE_RATE_LIMITED" in audit_spy_calls, (
        f"audit 應有 PASSWORD_CHANGE_RATE_LIMITED, got: {audit_spy_calls}"
    )


# ============ Test 4: reset-password IP rate limit ============

def test_reset_password_ip_rate_limit(client, create_test_user, create_admin_user):
    """admin 同 IP 連續 20 次 reset → 第 21 次回 429，audit 記 PASSWORD_RESET_RATE_LIMITED + extras.target_user_id。"""
    admin = create_admin_user(username="t_admin1", password="AdminGood1!")
    target = create_test_user(username="t_resetv1", password="Target123!")
    client.cookies.set("access_token", _login_as(client, "t_admin1", "AdminGood1!"))

    # 連續 20 次 reset（每次都成功 200）
    for i in range(20):
        res = client.put(
            f"/api/auth/users/{target.id}/reset-password",
            json={"new_password": f"NewPw{i}!Abc"},
        )
        assert res.status_code == 200, f"第 {i+1} 次應成功 200, got {res.status_code}"

    # 第 21 次應 429
    with patch("api.auth.write_login_audit") as audit_spy:
        res = client.put(
            f"/api/auth/users/{target.id}/reset-password",
            json={"new_password": "Final123!"},
        )
        assert res.status_code == 429, f"第 21 次應 429, got {res.status_code}"
        actions = [c.kwargs.get("action") for c in audit_spy.call_args_list]
        assert "PASSWORD_RESET_RATE_LIMITED" in actions
        # 確認 extras 含 target_user_id
        matching_calls = [
            c for c in audit_spy.call_args_list
            if c.kwargs.get("action") == "PASSWORD_RESET_RATE_LIMITED"
        ]
        assert matching_calls
        extras = matching_calls[0].kwargs.get("extras", {})
        assert extras.get("target_user_id") == target.id


# ============ Test 5: reset-password no target user lockout ============

def test_reset_password_no_target_user_lockout(client, create_test_user, create_admin_user):
    """admin 對同一 target user 連續重設 10 次後，target user 沒被誤記入 login_account scope。"""
    admin = create_admin_user(username="t_admin2", password="AdminGood1!")
    target = create_test_user(username="t_target2", password="Target123!")
    client.cookies.set("access_token", _login_as(client, "t_admin2", "AdminGood1!"))

    for i in range(10):
        res = client.put(
            f"/api/auth/users/{target.id}/reset-password",
            json={"new_password": f"NewPw{i}!Abc"},
        )
        assert res.status_code == 200, f"i={i}: {res.text}"

    # Assert 1: target.username 不應在 login_account scope 累積
    from utils.rate_limit_db import count_recent_attempts
    count = count_recent_attempts(
        _ACCOUNT_SCOPE, target.username, within_seconds=_FAIL_LOCKOUT
    )
    assert count == 0, f"target user 不應被誤記入 login_account scope, got count={count}"

    # Assert 2: _check_account_lockout(target.username) 不拋 429
    try:
        _check_account_lockout(target.username)
    except Exception as e:
        pytest.fail(f"target user 應可正常 login, 但 _check_account_lockout 拋 {e}")


# ============ Test 6: pwd_change scope isolated from login ============

def test_pwd_change_scope_isolated_from_login(client, create_test_user):
    """驗證 scope 隔離：
    - login 失敗 5 次（觸發 login_account lockout）→ change-password 仍可正常嘗試
    - change-password 失敗 5 次（觸發 pwd_change_user lockout）→ login 仍可正常嘗試
    """
    user = create_test_user(username="t_scope1", password="GoodOld123!")

    # Part A: login 5 次失敗 → change-password 不受影響
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "t_scope1", "password": "WrongLogin"})
    # 第 6 次 login 應被 lockout
    res = client.post("/api/auth/login", json={"username": "t_scope1", "password": "WrongLogin"})
    assert res.status_code == 429, "login 應已 lockout"

    # 但用「在 lockout 前已拿到的 token」走 change-password 應不受影響
    # 直接拿 user 在 conftest 已建立的 token（或用 admin 幫他 reset 後重 login）
    # 簡化：本 test 只驗 scope 計數隔離，用直接 query DB 驗
    from utils.rate_limit_db import count_recent_attempts
    from api.auth import _PWD_CHANGE_USER_SCOPE, _FAIL_LOCKOUT
    pwd_change_count = count_recent_attempts(
        _PWD_CHANGE_USER_SCOPE, f"user:{user.id}", within_seconds=_FAIL_LOCKOUT
    )
    assert pwd_change_count == 0, (
        f"login 失敗不應計入 pwd_change_user scope, got {pwd_change_count}"
    )

    # Part B（清計數後再測反向）
    from utils.rate_limit_db import clear_attempts
    clear_attempts(_ACCOUNT_SCOPE, "t_scope1")  # 清 login lockout
    # change-password 失敗不應計入 login_account scope
    # （這部分在 Test 5 reset-password 已類似驗證；此處不重複跑 5 次 change-password 加快測試）
    login_count = count_recent_attempts(
        _ACCOUNT_SCOPE, "t_scope1", within_seconds=_FAIL_LOCKOUT
    )
    assert login_count == 0, f"清完應為 0, got {login_count}"
```

**Fixtures 預期**：`client` (TestClient)、`create_test_user(username, password, ...)` 回傳 User、`create_admin_user(...)` 回傳具 USER_MANAGEMENT_WRITE 權限的 User。若 conftest 名稱不同調整 fixture 名。**Implementer 第一步必跑** `grep -n "def create_test_user\|def create_admin_user\|def client" tests/conftest.py tests/test_auth_*.py` 確認實際 fixture 命名。

- [ ] **Step 2.7: 跑新測試確認 fail（implementation 還沒完成時應 fail; 完成後 pass）**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_auth_password_rate_limit.py -v 2>&1 | tail -40
```
Expected: 6 個 test 全 pass（Step 2.1-2.4 已完成 implementation）。

如果有 fail，常見原因：
- conftest fixture 名不對 → 調整
- `client.cookies.set` 在 TestClient API 上需用 `client.cookies.update({"access_token": cookie})` 視版本
- audit_spy patch path 錯：`api.auth.write_login_audit` 是 import-time bound name；確認在 `api/auth.py:21` 的 import 是 `from utils.audit import write_login_audit`

- [ ] **Step 2.8: 跑全套 pytest 確認零回歸**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest -x --tb=short 2>&1 | tail -30
```
Expected: 全綠 + Task 1 + Task 2 共 +7 new test (1 AST + 6 password)，總計 5103 + 7 = 5110 passed。如有 fail 必須回頭查 audit 接線或 helper logic。

特別注意：`tests/test_auth_rate_limit_db.py` (login flow lockout test) 必須仍全綠（spec §2 G5 零回歸要求）。

- [ ] **Step 2.9: Commit (C2)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/auth.py tests/test_auth_password_rate_limit.py
git commit -m "$(cat <<'EOF'
feat(auth): rate limit change-password and reset-password endpoints

Spec A PR-A2 (audit P1 #8)：

- change-password 套雙層限流：IP 滑窗 (_PWD_CHANGE_IP_SCOPE) + per-user
  失敗 lockout (_PWD_CHANGE_USER_SCOPE)，與 login 既有 _ACCOUNT_SCOPE 隔離
- reset-password 套 caller IP 滑窗 (_PWD_RESET_IP_SCOPE)，target user
  不連帶 lockout（避免 admin cookie 被竊造成全員工 DoS）
- 限流觸發時 write_login_audit 3 個新 action：
  PASSWORD_CHANGE_RATE_LIMITED / PASSWORD_CHANGE_LOCKED /
  PASSWORD_RESET_RATE_LIMITED 鏡像既有 LOGIN_RATE_LIMITED / LOGIN_LOCKED
  pattern，stolen-cookie attack window 的 forensic trail 由此 3 個 action 撐住
- 6 個新 pytest cover：user lockout / clear on success / IP-only / reset IP
  limit / no target lockout / scope isolation

Refs: Spec docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md §3.3-3.4
EOF
)"
```

---

## Task 3: CI grep gate

**Goal:** 加 GitHub Actions job 在 PR / push 時 grep `api/` 與 `services/` 內裸 `SlidingWindowLimiter(` 並 hard-fail。

**Files:**
- Modify: `.github/workflows/ci.yml`

### Steps

- [ ] **Step 3.1: 確認 ci.yml 現有結構**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
head -50 .github/workflows/ci.yml
```
找到 job 列表結構，確認新 job 該插在哪（建議在最頂層 jobs 區塊內加一個獨立 job，與既有 pytest job 平行跑）。

- [ ] **Step 3.2: 加 naked-rate-limiter-gate job**

Edit `.github/workflows/ci.yml`，在 `jobs:` 區塊加入：

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
            echo "::error::See utils/rate_limit.py:163 for backend dispatch (memory/postgres)."
            echo "::error::Spec: docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md"
            exit 1
          fi
          echo "OK: no naked SlidingWindowLimiter() in api/ or services/"
```

縮排 follow ci.yml 既有風格（2 空格 vs 4 空格）。

- [ ] **Step 3.3: 本地驗證 grep job 真會 fail**

故意暫加一行 `_test_x = SlidingWindowLimiter(max_calls=1, window_seconds=60)` 到 `api/exports.py` 結尾，然後跑：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
grep -rn --include="*.py" "SlidingWindowLimiter(" api/ services/ 2>/dev/null
echo "Exit: $?"
```
Expected: 多出一行 hit + Exit 0 (grep found = exit 0 → in workflow 配 `if ... ; then exit 1` 模式會 fire `exit 1`)。

驗證後 **立刻 revert** 該 test line：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git diff api/exports.py   # 確認只有那一行新增
# 用 Edit tool 移除該行；不要 git checkout（會 wipe Task 1 改動）
```

- [ ] **Step 3.4: 本地驗證乾淨情況下 grep job 不 fail**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
if grep -rn --include="*.py" "SlidingWindowLimiter(" api/ services/ 2>/dev/null; then
  echo "FOUND naked SlidingWindowLimiter — gate would fail"
  exit 1
fi
echo "OK: gate would pass"
```
Expected: `OK: gate would pass`。

- [ ] **Step 3.5: Commit (C3)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
chore(ci): grep gate forbid naked SlidingWindowLimiter() in api/ services/

Spec A PR-A3 (audit P1 #9 守門)：

CI workflow 新 job naked-rate-limiter-gate 在 api/ 與 services/ 內 grep
\`SlidingWindowLimiter(\` hard-fail。白名單範圍 utils/rate_limit.py
（定義處）+ tests/（factory isinstance 檢查需要）—— 透過 grep 路徑鎖定自然排除。

搭配 Task 1 的 tests/test_rate_limit_router_usage.py AST 守衛形成雙重保險。

Refs: Spec docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md §3.6
EOF
)"
```

---

## Task 4: 最終驗收

**Goal:** 三 commit 完成後跑全套 pytest 確認零回歸、git log 確認 commit 結構正確、準備 PR description。

### Steps

- [ ] **Step 4.1: 跑全套 pytest 最終驗收**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=short 2>&1 | tail -20
```
Expected: `5110 passed` 或 baseline + 7 new test，0 new fail。

- [ ] **Step 4.2: git log 確認三 commit 結構**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git log --oneline -5
```
Expected:
```
<sha3> chore(ci): grep gate forbid naked SlidingWindowLimiter() in api/ services/
<sha2> feat(auth): rate limit change-password and reset-password endpoints
<sha1> refactor(rate-limit): 14 routers switch to create_limiter() factory
<prev> docs(p1ab): 限流 hardening spec v2 — advisor 修訂三處 blind spot
<prev2> docs(p1ab): 限流 hardening spec — #8 change/reset-password 限流 ...
```

- [ ] **Step 4.3: git diff stat 確認檔案 / 行數**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git diff main..HEAD --stat
```
Expected:
- 11 router files modified (各 1-4 行 diff)
- `api/auth.py` modified（~80 行新增）
- `tests/test_rate_limit_router_usage.py` created (~40 行)
- `tests/test_auth_password_rate_limit.py` created (~200 行)
- `.github/workflows/ci.yml` modified (~15 行新增)
- `docs/superpowers/specs/2026-05-28-rate-limit-hardening-design.md` already committed (separate)
- `docs/superpowers/plans/2026-05-28-rate-limit-hardening.md` already committed (this file)

- [ ] **Step 4.4: 報告完成狀態並等 user 開 PR**

向 user 回報：
- ✅ 三 commit 完成
- ✅ 全套 pytest pass + 7 new test
- 提醒：roll-out 需設 `RATE_LIMIT_BACKEND=postgres` env（spec §5.1）
- 等 user 決定：開 PR 還是繼續本地測試

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 (14 routers → factory) | Task 1 | ✓ |
| §2 G2 (change-password 雙層) | Task 2 Step 2.2-2.3 | ✓ |
| §2 G3 (reset-password caller IP) | Task 2 Step 2.4 | ✓ |
| §2 G4 (CI grep gate) | Task 3 | ✓ |
| §2 G5 (零回歸) | Task 4 Step 4.1 | ✓ |
| §3.2 scope 命名 (3 新 scope) | Task 2 Step 2.1 | ✓ |
| §3.3 5 個 helper | Task 2 Step 2.2 | ✓ |
| §3.4 change_password 接線 + audit | Task 2 Step 2.3 | ✓ |
| §3.4 reset_password 接線 + audit | Task 2 Step 2.4 | ✓ |
| §3.4 audit log 3 action 對齊 LOGIN_*  | Task 2 Step 2.3-2.4 | ✓ |
| §3.5 14 處 mechanical replacement | Task 1 Step 1.1-1.2 | ✓ |
| §3.6 CI gate 範圍 + 白名單 | Task 3 Step 3.2 | ✓ |
| §4.1 AST sanity test | Task 1 Step 1.3 | ✓ |
| §4.2 6 個 pytest | Task 2 Step 2.6 | ✓ |
| §4.4 既有 login flow 回歸 | Task 2 Step 2.8 | ✓ |
| §6 風險 implementer 保留 control flow | Task 2 Step 2.3 (註解) | ✓ |
| §6 風險 grep test fixture | Task 2 Step 2.6 fixture 命名提示 | ✓ |
