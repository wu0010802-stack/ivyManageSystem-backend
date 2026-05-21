# JWT Secret Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `utils/auth.py` 的 JWT 簽章支援 secret rotation（kid header + 多 key 容忍），rotation 過程中既有 session 不被踢。

**Architecture:** `JWT_SECRET_KEY` 維持 current（簽 + 驗第一順位），新增 `JWT_SECRET_KEYS_OLDS` 為 JSON list of accept-only secrets。模組啟動時建 `_VERIFY_KEYS: dict[kid, secret]`（kid = `sha256(secret)[:12]`）+ `_LEGACY_TRY_ORDER` list。Sign 在 header 帶 `kid`；verify 有 `kid` 走 dict 查表、無 `kid` 試 list（過渡期）。順手把 `api/auth.py` logout + `utils/audit.py` 兩處繞過中央 decode 的點收攏到新 helper `decode_token_for_audit`。

**Tech Stack:** FastAPI / python-jose / pytest / SQLAlchemy（無動）

**Spec:** `docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md`

---

## 前置：建立 worktree

- [ ] **Step 0: 建立 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add .claude/worktrees/jwt-secret-rotation-2026-05-21-backend -b feat/jwt-secret-rotation-2026-05-21-backend main
cd .claude/worktrees/jwt-secret-rotation-2026-05-21-backend
```

從此以下所有路徑都相對於 worktree 根。

---

## File Structure

| 檔案 | 動作 | 責任 |
|------|------|------|
| `utils/auth.py` | 修改 | 加 env 解析、`_kid_for`、`_VERIFY_KEYS`、`_LEGACY_TRY_ORDER`、`_decode_with_keys`、`decode_token_for_audit`，sign 帶 kid |
| `tests/test_jwt_rotation.py` | 新建 | 12 個 multi-key / legacy / olds-invalid 場景 |
| `api/auth.py` | 修改（~5 行） | logout 改用 `decode_token_for_audit` 抽 audit user_id |
| `utils/audit.py` | 修改（~3 行） | `_extract_user_from_header` 改用 `decode_token_for_audit` |
| `services/activity_query_token.py` | 修改（純註解） | 檔頭加 deprecation 區塊 |
| `api/activity/_shared.py` | 修改（純註解） | 既有「server secret 沿用」註解段補 deprecation 連結 |
| `docs/jwt_secret_rotation.md` | 新建 | Rotation runbook（4 步驟操作手冊） |
| `.env.example` | 修改（若存在） | 列出 `JWT_SECRET_KEYS_OLDS` 範例值 |

---

## Task 1：env 解析 + kid 衍生（模組層常數）

純讀 env、純函式，無 sign/verify 行為改動 — 先把 multi-key 基礎建好，後續 task 才有東西用。

**Files:**
- Modify: `utils/auth.py`（在現有 `_jwt_secret = os.environ.get(...)` 之後加新區塊）
- Test: `tests/test_jwt_rotation.py`（新建）

- [ ] **Step 1.1: 寫第一個失敗測試 — kid 衍生**

新建 `tests/test_jwt_rotation.py`：

```python
"""
JWT secret rotation 測試：kid header + 多 key 容忍。

對應 spec：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
"""

import hashlib
import os
import importlib
import pytest
from fastapi import HTTPException


def _expected_kid(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _reload_auth(monkeypatch, *, current: str, olds: str | None = None, env: str = "development"):
    """重新載入 utils.auth 以套用環境變數。回傳 reloaded module。"""
    monkeypatch.setenv("JWT_SECRET_KEY", current)
    monkeypatch.setenv("ENV", env)
    if olds is None:
        monkeypatch.delenv("JWT_SECRET_KEYS_OLDS", raising=False)
    else:
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", olds)
    import utils.auth as auth_module
    return importlib.reload(auth_module)


class TestKidDerivation:

    def test_kid_is_sha256_truncated_to_12_hex(self, monkeypatch):
        """kid 必為 sha256(secret) 前 12 hex chars"""
        secret = "current-secret-v1"
        auth = _reload_auth(monkeypatch, current=secret)
        assert auth._CURRENT_KID == _expected_kid(secret)
        assert len(auth._CURRENT_KID) == 12

    def test_kid_changes_with_secret(self, monkeypatch):
        """不同 secret 產生不同 kid"""
        a = _reload_auth(monkeypatch, current="aaa")
        kid_a = a._CURRENT_KID
        b = _reload_auth(monkeypatch, current="bbb")
        kid_b = b._CURRENT_KID
        assert kid_a != kid_b
```

- [ ] **Step 1.2: 跑測試確認 fail**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/jwt-secret-rotation-2026-05-21-backend
pytest tests/test_jwt_rotation.py::TestKidDerivation -v
```

Expected: FAIL — `AttributeError: module 'utils.auth' has no attribute '_CURRENT_KID'`

- [ ] **Step 1.3: 在 `utils/auth.py` 加 env 解析 + kid 衍生**

在 `utils/auth.py` 找到 `JWT_SECRET_KEY = _jwt_secret`（約第 33 行）之後、`JWT_ALGORITHM = "HS256"` 之前，插入：

```python
JWT_SECRET_KEY = _jwt_secret


# ── Multi-key support for rotation ────────────────────────────────────────
# 設計：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
# JWT_SECRET_KEY 為 current（簽 + 驗第一順位）；
# JWT_SECRET_KEYS_OLDS 為 JSON list of accept-only secrets，rotation 過渡期用。
import json as _json


def _kid_for(secret: str) -> str:
    """kid = sha256(secret) 前 12 hex chars。確定性、不洩漏 secret。"""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


_olds_raw = os.environ.get("JWT_SECRET_KEYS_OLDS", "[]")
try:
    _olds = _json.loads(_olds_raw)
    if not isinstance(_olds, list) or not all(isinstance(k, str) for k in _olds):
        raise ValueError("JWT_SECRET_KEYS_OLDS 必須是 JSON list of strings")
except (_json.JSONDecodeError, ValueError) as _e:
    if _is_dev:
        logger.warning("JWT_SECRET_KEYS_OLDS 解析失敗，視為空 list：%s", _e)
        _olds = []
    else:
        raise RuntimeError(f"JWT_SECRET_KEYS_OLDS 解析失敗：{_e}")

_CURRENT_KID: str = _kid_for(JWT_SECRET_KEY)
# verify 查表：kid → secret。current 永遠在內；olds 接著加。
_VERIFY_KEYS: dict[str, str] = {_CURRENT_KID: JWT_SECRET_KEY}
for _old in _olds:
    if not _old:
        continue
    _VERIFY_KEYS[_kid_for(_old)] = _old

# 過渡期：沒帶 kid header 的 legacy token，依序試這個 list。
_LEGACY_TRY_ORDER: list[str] = [JWT_SECRET_KEY] + [k for k in _olds if k]
# ──────────────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"
```

- [ ] **Step 1.4: 跑測試確認 pass**

```bash
pytest tests/test_jwt_rotation.py::TestKidDerivation -v
```

Expected: PASS (2 tests)

- [ ] **Step 1.5: 加 verify keys 結構測試**

在 `tests/test_jwt_rotation.py` 加：

```python
class TestVerifyKeysLoading:

    def test_only_current_when_olds_empty(self, monkeypatch):
        """無 olds 時，_VERIFY_KEYS 只含 current"""
        auth = _reload_auth(monkeypatch, current="cur1")
        assert auth._VERIFY_KEYS == {_expected_kid("cur1"): "cur1"}
        assert auth._LEGACY_TRY_ORDER == ["cur1"]

    def test_olds_appended_to_verify_keys(self, monkeypatch):
        """olds 中所有 key 都進 _VERIFY_KEYS"""
        auth = _reload_auth(
            monkeypatch,
            current="cur2",
            olds='["old_a","old_b"]',
        )
        assert auth._VERIFY_KEYS == {
            _expected_kid("cur2"): "cur2",
            _expected_kid("old_a"): "old_a",
            _expected_kid("old_b"): "old_b",
        }
        assert auth._LEGACY_TRY_ORDER == ["cur2", "old_a", "old_b"]

    def test_empty_string_olds_skipped(self, monkeypatch):
        """olds 中空字串會被略過"""
        auth = _reload_auth(monkeypatch, current="cur3", olds='["", "real_old"]')
        assert _expected_kid("real_old") in auth._VERIFY_KEYS
        # 空字串不進 _VERIFY_KEYS 也不進 _LEGACY_TRY_ORDER
        assert "" not in auth._LEGACY_TRY_ORDER
        assert auth._LEGACY_TRY_ORDER == ["cur3", "real_old"]


class TestOldsParsing:

    def test_invalid_json_dev_falls_back_to_empty(self, monkeypatch):
        """dev 模式 JSON 損毀 → warning + 視為空 list"""
        auth = _reload_auth(monkeypatch, current="cur", olds="not-json", env="development")
        assert auth._olds == []

    def test_invalid_json_prod_raises(self, monkeypatch):
        """prod 模式 JSON 損毀 → RuntimeError"""
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "cur")
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", "not-json")
        import utils.auth as auth_module
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEYS_OLDS"):
            importlib.reload(auth_module)

    def test_non_list_value_rejected_in_prod(self, monkeypatch):
        """olds 不是 list（例：dict）→ prod fail-loud"""
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "cur")
        monkeypatch.setenv("JWT_SECRET_KEYS_OLDS", '{"v1":"x"}')
        import utils.auth as auth_module
        with pytest.raises(RuntimeError, match="list of strings"):
            importlib.reload(auth_module)
```

- [ ] **Step 1.6: 跑 6 個新測試**

```bash
pytest tests/test_jwt_rotation.py -v
```

Expected: PASS (8 tests total)

- [ ] **Step 1.7: Commit**

```bash
git add utils/auth.py tests/test_jwt_rotation.py
git commit -m "feat(auth): JWT_SECRET_KEYS_OLDS env + _VERIFY_KEYS dict + kid 衍生

JWT secret rotation 基礎建設（Task 1/6）。模組啟動解析
JWT_SECRET_KEYS_OLDS（JSON list），與 JWT_SECRET_KEY 合併
建立 _VERIFY_KEYS: dict[kid, secret] 與 _LEGACY_TRY_ORDER list。
kid = sha256(secret)[:12]。prod 環境 olds 解析失敗 fail-loud。

尚未動 sign/verify 行為 — 由後續 task 接上。

Spec: docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md"
```

---

## Task 2：Sign 加 kid header

`create_access_token` 在 header 帶 `kid`。

**Files:**
- Modify: `utils/auth.py:148-168`（`create_access_token` 函式末行的 `jwt.encode`）
- Test: `tests/test_jwt_rotation.py`

- [ ] **Step 2.1: 寫失敗測試**

加入 `tests/test_jwt_rotation.py`：

```python
class TestSignIncludesKid:

    def test_create_access_token_includes_kid_header(self, monkeypatch):
        """新簽 token 的 header 必含 kid，值等於 _CURRENT_KID"""
        from jose import jwt as jose_jwt
        auth = _reload_auth(monkeypatch, current="signing-cur")
        token = auth.create_access_token({"user_id": 1})
        header = jose_jwt.get_unverified_header(token)
        assert header["kid"] == auth._CURRENT_KID
        assert header["alg"] == "HS256"

    def test_kid_in_header_matches_secret_used_to_sign(self, monkeypatch):
        """kid 確實對應簽章 secret（用該 kid 對應的 secret 能驗過）"""
        from jose import jwt as jose_jwt
        auth = _reload_auth(monkeypatch, current="match-test")
        token = auth.create_access_token({"user_id": 1})
        header = jose_jwt.get_unverified_header(token)
        kid = header["kid"]
        secret = auth._VERIFY_KEYS[kid]
        # 用該 secret 應能解碼成功
        payload = jose_jwt.decode(token, secret, algorithms=["HS256"])
        assert payload["user_id"] == 1
```

- [ ] **Step 2.2: 跑測試確認 fail**

```bash
pytest tests/test_jwt_rotation.py::TestSignIncludesKid -v
```

Expected: FAIL — `KeyError: 'kid'`

- [ ] **Step 2.3: 改 `create_access_token` 加 headers**

在 `utils/auth.py`（約第 168 行）：

```python
    return jwt.encode(
        to_encode,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
        headers={"kid": _CURRENT_KID},
    )
```

（原本是 `return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)`。）

- [ ] **Step 2.4: 跑測試確認 pass**

```bash
pytest tests/test_jwt_rotation.py::TestSignIncludesKid -v
```

Expected: PASS

- [ ] **Step 2.5: 跑既有 auth 測試確認零 regression**

```bash
pytest tests/test_auth.py tests/test_jwt_algorithm_check.py tests/test_jwt_blocklist.py -v
```

Expected: 既有測試全部 PASS（因為簽 / 驗都還是用 current key）

- [ ] **Step 2.6: Commit**

```bash
git add utils/auth.py tests/test_jwt_rotation.py
git commit -m "feat(auth): create_access_token 在 JWT header 加 kid

新簽 token 帶 kid = sha256(JWT_SECRET_KEY)[:12]，供未來
multi-key verify 路徑識別簽章 key 版本。簽章本身仍使用
JWT_SECRET_KEY 不變，向下相容。"
```

---

## Task 3：Verify 支援 multi-key（含 legacy 無 kid）

`decode_token` / `decode_token_allow_expired` 改成走新 `_decode_with_keys` 函式：有 kid 查表、無 kid 試 legacy order。

**Files:**
- Modify: `utils/auth.py:274-280`（`decode_token`）、`utils/auth.py:324-355`（`decode_token_allow_expired`）
- Test: `tests/test_jwt_rotation.py`

- [ ] **Step 3.1: 寫失敗測試 — 多種 verify 場景**

在 `tests/test_jwt_rotation.py` 加：

```python
import base64
import json as _json


def _craft_token(header: dict, payload: dict, secret: str) -> str:
    """手動組 token：用指定 secret 簽，header 可任意指定（含 kid 偽造）"""
    from jose import jwt as jose_jwt
    return jose_jwt.encode(payload, secret, algorithm="HS256", headers=header)


def _craft_legacy_token_no_kid(payload: dict, secret: str) -> str:
    """模擬升版前舊 token：用指定 secret 簽，header 不帶 kid"""
    from jose import jwt as jose_jwt
    # python-jose 預設 header 不含 kid（除非顯式傳 headers）
    return jose_jwt.encode(payload, secret, algorithm="HS256")


class TestVerifyMultiKey:

    def test_verify_with_current_key(self, monkeypatch):
        """current 簽 + current 驗 → pass"""
        auth = _reload_auth(monkeypatch, current="cur-v1")
        token = auth.create_access_token({"user_id": 1})
        payload = auth.decode_token(token)
        assert payload["user_id"] == 1

    def test_verify_with_old_kid_in_olds_list(self, monkeypatch):
        """舊 key 簽（kid_old）→ 啟動時 olds 含舊 key → 驗 pass"""
        # 模擬：rotation 進行中，舊 token 仍在外
        auth = _reload_auth(monkeypatch, current="new-key", olds='["old-key"]')
        old_kid = _expected_kid("old-key")
        # 用 old-key 簽，header 帶 old kid
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 7, "exp": 9999999999},
            "old-key",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 7

    def test_verify_with_old_kid_not_in_olds(self, monkeypatch):
        """舊 key 簽 + olds 不含 → 401（rotation 完成後預期）"""
        auth = _reload_auth(monkeypatch, current="new-key")  # olds 為空
        old_kid = _expected_kid("removed-old")
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 7, "exp": 9999999999},
            "removed-old",
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401

    def test_verify_unknown_kid_rejected(self, monkeypatch):
        """偽造 kid header（從未在 _VERIFY_KEYS 中）→ 401"""
        auth = _reload_auth(monkeypatch, current="cur-v2")
        token = _craft_token(
            {"alg": "HS256", "kid": "deadbeefcafe"},
            {"user_id": 1, "exp": 9999999999},
            "cur-v2",  # 即使簽章用對的 secret，未知 kid 也拒絕
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401


class TestVerifyLegacyNoKid:

    def test_legacy_no_kid_verified_by_current(self, monkeypatch):
        """升版前 token（無 kid，用 current 簽）→ legacy try-loop 試到 current → pass"""
        auth = _reload_auth(monkeypatch, current="cur-legacy")
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "cur-legacy",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 9

    def test_legacy_no_kid_verified_by_old_in_olds(self, monkeypatch):
        """升版前舊 token，secret 已 rotate；olds 仍含舊 key → legacy try-loop pass"""
        auth = _reload_auth(monkeypatch, current="new", olds='["pre-rotate"]')
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "pre-rotate",
        )
        payload = auth.decode_token(token)
        assert payload["user_id"] == 9

    def test_legacy_no_kid_unknown_key_rejected(self, monkeypatch):
        """升版前 token，secret 早被刪 → legacy try-loop 全失敗 → 401"""
        auth = _reload_auth(monkeypatch, current="new")  # olds 空
        token = _craft_legacy_token_no_kid(
            {"user_id": 9, "exp": 9999999999},
            "long-gone-secret",
        )
        with pytest.raises(HTTPException) as excinfo:
            auth.decode_token(token)
        assert excinfo.value.status_code == 401


class TestDecodeTokenAllowExpiredMultiKey:

    def test_allow_expired_with_current_kid(self, monkeypatch):
        """current 簽的過期 token 在 grace 內 → decode_token_allow_expired pass"""
        import time
        auth = _reload_auth(monkeypatch, current="cur-grace")
        # 過期 30 秒（在 2h grace 內）
        past_exp = int(time.time()) - 30
        token = _craft_token(
            {"alg": "HS256", "kid": auth._CURRENT_KID},
            {"user_id": 1, "exp": past_exp},
            "cur-grace",
        )
        payload = auth.decode_token_allow_expired(token)
        assert payload["user_id"] == 1

    def test_allow_expired_with_old_kid(self, monkeypatch):
        """rotation 期間 olds 中舊 key 簽的過期 token 也能在 grace 內 decode"""
        import time
        auth = _reload_auth(monkeypatch, current="new", olds='["old-grace"]')
        old_kid = _expected_kid("old-grace")
        past_exp = int(time.time()) - 30
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 1, "exp": past_exp},
            "old-grace",
        )
        payload = auth.decode_token_allow_expired(token)
        assert payload["user_id"] == 1
```

- [ ] **Step 3.2: 跑測試確認 fail**

```bash
pytest tests/test_jwt_rotation.py::TestVerifyMultiKey tests/test_jwt_rotation.py::TestVerifyLegacyNoKid tests/test_jwt_rotation.py::TestDecodeTokenAllowExpiredMultiKey -v
```

Expected: FAIL（多數 case 因為現行 decode 只用 current `JWT_SECRET_KEY`，olds kid 直接掛掉）

- [ ] **Step 3.3: 在 `utils/auth.py` 加 `_decode_with_keys`**

在 `_check_token_algorithm`（約第 256 行）之後、`decode_token`（約第 274 行）之前插入：

```python
def _decode_with_keys(token: str, *, allow_expired: bool = False) -> dict:
    """Multi-key 容忍的 JWT decode。已先過 _check_token_algorithm。

    解析順序：
    1. 有 kid header → 用 _VERIFY_KEYS[kid]（未知 kid → 401）
    2. 無 kid（過渡期 legacy token）→ 依序試 _LEGACY_TRY_ORDER
    3. 都失敗 → 401
    """
    options = {"verify_exp": False} if allow_expired else {}

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")

    kid = header.get("kid")
    if kid:
        secret = _VERIFY_KEYS.get(kid)
        if not secret:
            raise HTTPException(status_code=401, detail="無效或過期的 Token")
        try:
            return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options=options)
        except JWTError:
            raise HTTPException(status_code=401, detail="無效或過期的 Token")

    # legacy（無 kid）：依序試 _LEGACY_TRY_ORDER
    for secret in _LEGACY_TRY_ORDER:
        try:
            return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options=options)
        except JWTError:
            continue
    raise HTTPException(status_code=401, detail="無效或過期的 Token")
```

- [ ] **Step 3.4: 改 `decode_token` 使用 `_decode_with_keys`**

`utils/auth.py` 約第 274-280 行原本：

```python
def decode_token(token: str) -> dict:
    _check_token_algorithm(token)
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")
```

改為：

```python
def decode_token(token: str) -> dict:
    _check_token_algorithm(token)
    return _decode_with_keys(token, allow_expired=False)
```

- [ ] **Step 3.5: 改 `decode_token_allow_expired` 使用 `_decode_with_keys`**

`utils/auth.py` 約第 324-355 行原本：

```python
def decode_token_allow_expired(token: str) -> dict:
    """解碼 token，允許在寬限期內的過期 token..."""
    _check_token_algorithm(token)
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        exp = payload.get("exp", 0)
        now = datetime.now(timezone.utc).timestamp()
        grace_seconds = JWT_REFRESH_GRACE_HOURS * 3600
        if now - exp > grace_seconds:
            raise HTTPException(...)
    except JWTError:
        raise HTTPException(...)
    if is_token_revoked(payload.get("jti", "")):
        raise HTTPException(...)
    return payload
```

改為：

```python
def decode_token_allow_expired(token: str) -> dict:
    """解碼 token，允許在寬限期內的過期 token（用於 refresh / end-impersonate / logout）。
    回傳 payload，若 token 無效、超出寬限期、或 jti 已被廢止則拋出 401。
    """
    _check_token_algorithm(token)
    # 一律 allow_expired=True 解，再手動檢 exp + grace。簽章錯誤仍會 raise 401。
    payload = _decode_with_keys(token, allow_expired=True)
    exp = payload.get("exp", 0)
    now = datetime.now(timezone.utc).timestamp()
    grace_seconds = JWT_REFRESH_GRACE_HOURS * 3600
    if now - exp > grace_seconds:
        raise HTTPException(
            status_code=401, detail="Token 已超過可刷新期限，請重新登入"
        )
    # JTI 廢止檢查
    if is_token_revoked(payload.get("jti", "")):
        raise HTTPException(status_code=401, detail="Token 已廢止，請重新登入")
    return payload
```

> 與原版差異：原版用 try/except 區分「未過期」與「過期但 in-grace」；簡化後一律 `verify_exp=False` 解再手動檢 `now - exp > grace`。對「未過期」case，`now - exp` 為負，必然 ≤ grace，行為等價。

- [ ] **Step 3.6: 跑新 + 既有 test 確認全綠**

```bash
pytest tests/test_jwt_rotation.py tests/test_auth.py tests/test_jwt_algorithm_check.py tests/test_jwt_blocklist.py -v
```

Expected: 全 PASS（既有 49 + 新 ≈10 case）。

如果 `test_jwt_algorithm_check.py` 有 case fail（因 `_decode_with_keys` 改了 raise 順序），檢查那批測試是否預期特定 detail 字串；本 plan 的 detail 字串保持與原本一致 (`"無效或過期的 Token"`)，應該不會壞。

- [ ] **Step 3.7: Commit**

```bash
git add utils/auth.py tests/test_jwt_rotation.py
git commit -m "feat(auth): decode_token / decode_token_allow_expired 走 multi-key

新增 _decode_with_keys：有 kid 查 _VERIFY_KEYS 表，無 kid 走
_LEGACY_TRY_ORDER list。decode_token 與 decode_token_allow_expired
改用之，rotation 期間舊 kid_old token 仍可驗。jti 廢止檢查、
JWT_REFRESH_GRACE_HOURS grace 邏輯不變。"
```

---

## Task 4：`decode_token_for_audit` helper + 收攏兩處繞過點

`api/auth.py:738` logout 抽 audit user_id + `utils/audit.py:339` 靜默解析都繞過中央 decode 直接呼 `jose.jwt.decode`，rotation 後會在 olds kid token 上失敗（拿不到 audit user_id）。改用新 helper。

**Files:**
- Modify: `utils/auth.py`（新增 `decode_token_for_audit`）
- Modify: `api/auth.py:736-749`（logout 抽 audit user_id）
- Modify: `utils/audit.py:326-344`（`_extract_user_from_header`）
- Test: `tests/test_jwt_rotation.py`

- [ ] **Step 4.1: 寫失敗測試**

加入 `tests/test_jwt_rotation.py`：

```python
class TestDecodeTokenForAudit:

    def test_audit_decode_works_with_current_kid(self, monkeypatch):
        """current kid 簽的有效 token → 回傳 payload"""
        auth = _reload_auth(monkeypatch, current="aud-cur")
        token = auth.create_access_token({"user_id": 42, "name": "alice"})
        payload = auth.decode_token_for_audit(token)
        assert payload["user_id"] == 42
        assert payload["name"] == "alice"

    def test_audit_decode_works_with_expired_token(self, monkeypatch):
        """已過期 token 也能解（audit 不檢 exp）"""
        import time
        auth = _reload_auth(monkeypatch, current="aud-exp")
        past_exp = int(time.time()) - 99999  # 大幅過期
        token = _craft_token(
            {"alg": "HS256", "kid": auth._CURRENT_KID},
            {"user_id": 42, "name": "bob", "exp": past_exp},
            "aud-exp",
        )
        payload = auth.decode_token_for_audit(token)
        assert payload["user_id"] == 42

    def test_audit_decode_works_with_old_kid_in_olds(self, monkeypatch):
        """rotation 期間，olds 中的舊 kid token 也能 audit decode"""
        auth = _reload_auth(monkeypatch, current="new-aud", olds='["old-aud"]')
        old_kid = _expected_kid("old-aud")
        token = _craft_token(
            {"alg": "HS256", "kid": old_kid},
            {"user_id": 99, "name": "carol", "exp": 9999999999},
            "old-aud",
        )
        payload = auth.decode_token_for_audit(token)
        assert payload["user_id"] == 99

    def test_audit_decode_returns_none_on_invalid_token(self, monkeypatch):
        """token 不可解 → 回 None，不拋"""
        auth = _reload_auth(monkeypatch, current="aud-cur")
        assert auth.decode_token_for_audit("garbage") is None
        assert auth.decode_token_for_audit("") is None
        assert auth.decode_token_for_audit(None) is None

    def test_audit_decode_returns_none_on_unknown_kid(self, monkeypatch):
        """未知 kid → 回 None（一致地不拋）"""
        auth = _reload_auth(monkeypatch, current="aud-cur")
        token = _craft_token(
            {"alg": "HS256", "kid": "fakefakefake"},
            {"user_id": 1, "exp": 9999999999},
            "aud-cur",
        )
        assert auth.decode_token_for_audit(token) is None
```

- [ ] **Step 4.2: 跑測試確認 fail**

```bash
pytest tests/test_jwt_rotation.py::TestDecodeTokenForAudit -v
```

Expected: FAIL — `AttributeError: module 'utils.auth' has no attribute 'decode_token_for_audit'`

- [ ] **Step 4.3: 在 `utils/auth.py` 加 helper**

在 `decode_token_allow_expired` 之後（約第 356 行）插入：

```python
def decode_token_for_audit(token: str | None) -> dict | None:
    """專供 audit 路徑使用的 decode：multi-key 容忍、verify_exp=False、不檢 jti / token_version。

    純粹從 token 抽 user_id / name 寫 audit log。失敗一律回 None，**不** 拋例外。

    安全性：本函式僅供 audit log 寫入路徑使用，**不可** 用於授權判斷。
    呼叫端不得用回傳值通過 require_permission / get_current_user 等守衛。
    """
    if not token:
        return None
    try:
        _check_token_algorithm(token)
        return _decode_with_keys(token, allow_expired=True)
    except (JWTError, HTTPException):
        return None
```

- [ ] **Step 4.4: 跑測試確認 pass**

```bash
pytest tests/test_jwt_rotation.py::TestDecodeTokenForAudit -v
```

Expected: PASS (5 tests)

- [ ] **Step 4.5: 改 `api/auth.py:736-749` logout 抽 audit**

`api/auth.py` 約第 736-749 行原本：

```python
    if token:
        try:
            from jose import jwt as _jose_jwt
            from utils.auth import JWT_SECRET_KEY, JWT_ALGORITHM

            _payload = _jose_jwt.decode(
                token,
                JWT_SECRET_KEY,
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False},
            )
            audit_user_id = _payload.get("user_id")
            audit_username = _payload.get("name")
        except Exception:
            pass  # token 格式無效，audit 仍繼續執行
```

改為：

```python
    if token:
        from utils.auth import decode_token_for_audit
        _payload = decode_token_for_audit(token) or {}
        audit_user_id = _payload.get("user_id")
        audit_username = _payload.get("name")
```

- [ ] **Step 4.6: 改 `utils/audit.py:326-344`**

`utils/audit.py` 約第 326-344 行原本：

```python
def _extract_user_from_header(request: Request):
    """從 Cookie 或 Authorization header 靜默解析 JWT，不拋錯"""
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]

    if not token:
        return None, None

    try:
        from jose import jwt
        from utils.auth import JWT_SECRET_KEY, JWT_ALGORITHM

        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("user_id"), payload.get("name")
    except Exception:
        return None, None
```

改為：

```python
def _extract_user_from_header(request: Request):
    """從 Cookie 或 Authorization header 靜默解析 JWT，不拋錯。

    走 utils.auth.decode_token_for_audit：multi-key 容忍、verify_exp=False。
    """
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]

    if not token:
        return None, None

    from utils.auth import decode_token_for_audit
    payload = decode_token_for_audit(token) or {}
    return payload.get("user_id"), payload.get("name")
```

- [ ] **Step 4.7: 跑相關既有 test 確認零 regression**

```bash
pytest tests/test_audit_router.py tests/test_auth.py tests/test_jwt_rotation.py -v
```

Expected: 全 PASS。`test_audit_router.py` 已知有 3 個 pre-existing fail（與本次無關，per CLAUDE.md memory），其他應全綠。

- [ ] **Step 4.8: Commit**

```bash
git add utils/auth.py api/auth.py utils/audit.py tests/test_jwt_rotation.py
git commit -m "refactor(auth): 兩處 audit 路徑改用 decode_token_for_audit

api/auth.py logout 與 utils/audit.py _extract_user_from_header
原本繞過中央 decode，直接呼 jose.jwt.decode（單 key、無 multi-key
容忍），rotation 期間 olds kid token 會抽不到 audit user_id。
新 helper decode_token_for_audit：multi-key + verify_exp=False
+ 失敗回 None。明確標註不可作為授權判斷。"
```

---

## Task 5：activity_query_token deprecation 註解

`services/activity_query_token` 借用 `JWT_SECRET_KEY` 做 HMAC，rotation 後既有外發 token 會失效。本次不解耦，只加 deprecation 註解告知後續維護者。

**Files:**
- Modify: `services/activity_query_token.py`（檔頭 docstring）
- Modify: `api/activity/_shared.py`（既有「server secret 沿用」註解段）

- [ ] **Step 5.1: 改 `services/activity_query_token.py` 檔頭**

找到檔頭 docstring（約第 1-15 行），在現有說明後加：

```python
"""
... (既有 docstring 內容)
"""

# DEPRECATION（2026-05-21）：本模組借用 JWT_SECRET_KEY 做 HMAC，未支援
# multi-key rotation。JWT secret rotation 後（JWT_SECRET_KEY 變值），
# 既有外發 activity query token 會失效。
#
# Follow-up：解耦到專屬 env ACTIVITY_TOKEN_HMAC_KEY 並支援 olds list 容忍
# rotation。spec 連結：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
```

- [ ] **Step 5.2: 改 `api/activity/_shared.py` 約第 220-228 行註解段**

找到既有「server secret 沿用 JWT_SECRET_KEY」段落，把該段註解結尾補：

```python
# DEPRECATION（2026-05-21）：JWT secret rotation 後既有 token 會失效；
# 參見 docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md。
```

- [ ] **Step 5.3: 跑 activity 相關 test 確認零 regression**

```bash
pytest tests/test_activity_query_token.py -v 2>&1 | tail -20
```

Expected: 既有 test 全 PASS（純加註解，無行為改動）。

- [ ] **Step 5.4: Commit**

```bash
git add services/activity_query_token.py api/activity/_shared.py
git commit -m "docs(activity): 加 JWT secret rotation 失效 deprecation 註解

activity_query_token 與 _shared 借用 JWT_SECRET_KEY 做 HMAC，
未支援 multi-key。JWT secret rotation 後既有外發 token 會失效。
本次只加註解，解耦到 ACTIVITY_TOKEN_HMAC_KEY 列 follow-up。"
```

---

## Task 6：Rotation runbook

寫一份操作手冊給未來真正執行 rotation 的人。

**Files:**
- Create: `docs/jwt_secret_rotation.md`
- Modify: `.env.example`（若存在）

- [ ] **Step 6.1: 寫 runbook**

新建 `docs/jwt_secret_rotation.md`：

```markdown
# JWT Secret Rotation Runbook

設計文件：`docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md`

## 環境變數

| Env | 必填 | 用途 |
|-----|------|------|
| `JWT_SECRET_KEY` | 是 | Current secret（簽 + 驗第一順位）。長度 ≥ 32 bytes urlsafe。 |
| `JWT_SECRET_KEYS_OLDS` | 否（預設 `[]`） | JSON list of accept-only secrets。Rotation 過渡期填入舊值。 |

## 標準 rotation 流程

### 前提

- 確認應用程式版本 ≥ `feat/jwt-secret-rotation-2026-05-21-backend` merged 後的 commit。
- 確認 `JWT_ABSOLUTE_LIFETIME_HOURS`（預設 12）與 `JWT_REFRESH_GRACE_HOURS`（預設 2）的值，總共 14h 為 staff session 最長壽命。

### 步驟

**Step 1：產生新 secret**

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把輸出（例 `new_xxxx...`）暫存為 `NEW_SECRET`。

**Step 2：啟動雙 key 並存**

把 **目前** `JWT_SECRET_KEY` 的值複製進 `JWT_SECRET_KEYS_OLDS`（JSON list），把 `NEW_SECRET` 寫進 `JWT_SECRET_KEY`。例如：

```bash
# 假設原本 JWT_SECRET_KEY=old_yyyy
JWT_SECRET_KEY=new_xxxx
JWT_SECRET_KEYS_OLDS=["old_yyyy"]
```

更新所有 deployment instance 並 restart。

啟動 log 應看到：
- `_VERIFY_KEYS` 載入 2 個 kid（current + 1 old）
- 無錯誤

從此刻起：
- 新簽 token 帶 kid = `sha256(new_xxxx)[:12]`
- 舊 token（kid = `sha256(old_yyyy)[:12]`）仍可驗
- 過渡期 14h 後不應再出現舊 kid token

**Step 3：等 ≥14h（過渡期）**

實務建議等 24h（含時區與意外）。期間監控 Sentry / log 有沒有大量 `無效或過期的 Token` 401 上升 — 若有就代表還沒換完，再等。

**Step 4：清空 OLDS**

```bash
JWT_SECRET_KEYS_OLDS=[]
```

更新所有 deployment instance 並 restart。

啟動 log 應看到：
- `_VERIFY_KEYS` 只剩 1 個 kid（current）

從此刻起，任何用舊 secret 簽的 token（含外流的）都會 401 — rotation 完成。

## 緊急 rotation（secret 已外流）

**不要走軟著陸**。直接：

1. 跳到 Step 1 產生新 secret。
2. 直接寫 `JWT_SECRET_KEY=new_xxxx` + `JWT_SECRET_KEYS_OLDS=[]`（不放外流的舊值進 olds）。restart。
3. 對所有受影響 user 跑：
   ```sql
   UPDATE users SET token_version = COALESCE(token_version, 0) + 1
     WHERE is_active = true;
   ```
   ↑ 把所有 token 立即 invalidate（搭配既有 `token_version` 機制）。
4. 對外公告強制重登。

## 風險與排錯

| 症狀 | 可能原因 | 處理 |
|------|---------|------|
| 啟動 RuntimeError `JWT_SECRET_KEYS_OLDS 解析失敗` | env 值不是合法 JSON list | 檢查 JSON 格式：`'["s1","s2"]'`（雙引號內字串） |
| Step 2 後大量 401 | OLDS 漏抄、或 deploy 沒到所有 instance | 確認所有 instance restart 完成；確認 OLDS 的舊值精確等於 rotation 前的 `JWT_SECRET_KEY` |
| Step 4 後 staff 被踢一波 | 過渡期 < 14h，仍有舊 kid token 在用 | 接受（rotation 完成預期行為） / 或等更久再 Step 4 |

## Side effect：activity query token

`services/activity_query_token` 借用 `JWT_SECRET_KEY` 做 HMAC（DB 存 hex digest），未支援 multi-key。rotation 後 **既有外發 activity query token 會無法驗證通過**。

緩解：
- 計畫 rotation 前先確認沒有正在發送的家長公告含 activity URL（或接受短期失效）。
- 長期解：解耦到 `ACTIVITY_TOKEN_HMAC_KEY` env（follow-up）。
```

- [ ] **Step 6.2: 改 `.env.example`（如果存在）**

```bash
test -f .env.example && grep -q JWT_SECRET_KEYS_OLDS .env.example || cat <<'EOF' >> .env.example

# JWT secret rotation（runbook: docs/jwt_secret_rotation.md）
# 平時為 [] ；rotation 過渡期填入舊值 JSON list，例：["old_secret_value"]
JWT_SECRET_KEYS_OLDS=[]
EOF
```

如果沒有 `.env.example`，跳過。

- [ ] **Step 6.3: Commit**

```bash
git add docs/jwt_secret_rotation.md .env.example 2>/dev/null
git commit -m "docs(auth): JWT secret rotation runbook + .env.example 範例

操作手冊涵蓋：標準 4 步驟 rotation、緊急（外流）流程、
排錯表、activity query token side effect 註記。"
```

---

## Task 7：全套 regression + push

- [ ] **Step 7.1: 跑全套 pytest**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/jwt-secret-rotation-2026-05-21-backend
pytest -x --tb=short 2>&1 | tail -40
```

Expected：
- 新增 `tests/test_jwt_rotation.py` 全綠
- 既有 `tests/test_auth.py` / `test_jwt_algorithm_check.py` / `test_jwt_blocklist.py` 全綠
- pre-existing fail（per CLAUDE.md memory）：`test_audit_router` 3 條 / `test_supabase_storage` — 接受不算 regression

如果新出現 fail，回頭看哪個 task 改動造成。

- [ ] **Step 7.2: 確認 alembic 沒有新 head**

```bash
alembic heads
```

Expected: 單一 head（本 plan 無 migration）。

- [ ] **Step 7.3: Push branch（不開 PR 由 user 決定）**

```bash
git push -u origin feat/jwt-secret-rotation-2026-05-21-backend
```

回報 user：
- branch 名稱
- 6 個 commit 摘要
- pytest 全綠 / pre-existing fail 數量
- 待 user 決定：本地 merge 還是開 PR

---

## Self-Review 檢查清單

實作完成後對照：

- [ ] Spec § env schema：`JWT_SECRET_KEY` + `JWT_SECRET_KEYS_OLDS` JSON list（Task 1 ✓）
- [ ] Spec § kid 衍生：`sha256(secret)[:12]`（Task 1 + 2 ✓）
- [ ] Spec § 程式碼改動 § Sign：`create_access_token` 帶 `kid`（Task 2 ✓）
- [ ] Spec § 程式碼改動 § Verify：`_decode_with_keys`（Task 3 ✓）
- [ ] Spec § 程式碼改動 § audit helper：`decode_token_for_audit` + 兩處 call site 收攏（Task 4 ✓）
- [ ] Spec § services/activity_query_token：deprecation 註解（Task 5 ✓）
- [ ] Spec § Rotation 標準流程：runbook 4 步驟（Task 6 ✓）
- [ ] Spec § 測試：≈12 case（Task 1-4 累積：kid 2 + verify keys 3 + olds parsing 3 + sign kid 2 + verify multi-key 4 + legacy no kid 3 + allow_expired multi-key 2 + audit 5 = 24 case，超過 spec 12）
