# JWT Secret Rotation 設計（kid header + 多 key 容忍）

- 起草日期：2026-05-21
- 範圍：ivy-backend（FastAPI / `utils/auth.py`）
- 不在範圍：ivy-frontend（前端只持有 cookie，零改動）、ParentRefreshToken（DB-backed opaque token，無 JWT 簽章曝險）

## 問題

`utils/auth.py:17` 全系統共用單一 `JWT_SECRET_KEY`，沒有版本識別、沒有 rotation 流程：

- Secret 一旦外流，必須立即輪換並一次踢全員（強制重登）。
- JWT header 沒有 `kid`，verify 端無法區分簽章 key 版本，因此無法做「雙 key 並存」的軟著陸。
- Sign / verify call site 散落：除中央 `utils/auth.decode_token*`，另有兩處（`api/auth.py:738` logout 抽 audit user_id、`utils/audit.py:339` 靜默解析）繞過中央路徑直呼 `jose.jwt.decode`，rotation 改動容易漏。

目標是讓 secret rotation 變成「準備好 → 切換 → 廢止」的順位三步驟，過程中既有 session 不被踢、無需協調重啟時間窗。

## 設計概要

1. JWT header 加 `kid`。
2. 引入「current」與「accept-only olds」雙層 key set：
   - `JWT_SECRET_KEY`（既有 env，不動）— sign + verify 用。
   - `JWT_SECRET_KEYS_OLDS`（新 env，JSON list）— accept-only，僅供 verify 比對。
3. Verify 端：
   - 若 token 有 `kid` → 用對應 key 驗（找不到對應 → 401）。
   - 若 token 沒有 `kid`（過渡期舊 token）→ 依序試「current + olds」全部，任一過則通。
4. 把另兩處繞過中央 decode 的點收攏到新 helper `decode_token_for_audit(token)`，確保 multi-key 邏輯只有一份。

## env schema（向下相容）

```bash
# 必填：current key（簽章用 + verify 第一順位）
JWT_SECRET_KEY=<32+ bytes urlsafe random>

# 選填：歷史 keys（accept-only，JSON list of strings）
# 預設 "[]"。rotation 進行時把舊 key 列入；rotation 完成後清空。
JWT_SECRET_KEYS_OLDS=["<old_key_value_1>", "<old_key_value_2>"]

# kid 由 secret 雜湊衍生，不需獨立 env
```

### 為什麼不用「JWT_SECRET_KEYS dict + JWT_SECRET_KEY_ID」

- 既有 `.env` / deployment manifest 已有 `JWT_SECRET_KEY`，增量做法不需要動現有任何環境變數。
- Rotation 流程的心理模型乾淨：current 是「目前的 JWT_SECRET_KEY」，olds 是「以前的 JWT_SECRET_KEY」。
- Runbook 步驟：把現在的 `JWT_SECRET_KEY` 值塞進 olds、把新值寫進 `JWT_SECRET_KEY` 即可。

### `kid` 衍生規則

`kid = sha256(secret).hexdigest()[:12]`。

- 確定性：相同 secret → 相同 kid，不需額外 env 對應表。
- 不洩漏 secret：12 字 hex 從 SHA-256 截，無法反推。
- 短：JWT header 不會明顯變胖（每個 token ~+20 bytes header）。
- 抗碰撞：12 hex chars = 48 bits，rotation 級別碰撞機率可忽略。

## 程式碼改動

### `utils/auth.py`

```python
import json
import hashlib

_jwt_secret = os.environ.get("JWT_SECRET_KEY", "")
_olds_raw = os.environ.get("JWT_SECRET_KEYS_OLDS", "[]")

# 解析 olds：解析失敗在 prod fail-loud，dev 警告後當空 list
try:
    _olds = json.loads(_olds_raw)
    if not isinstance(_olds, list) or not all(isinstance(k, str) for k in _olds):
        raise ValueError("JWT_SECRET_KEYS_OLDS 必須是 JSON list of strings")
except (json.JSONDecodeError, ValueError) as e:
    if _is_dev:
        logger.warning("JWT_SECRET_KEYS_OLDS 解析失敗，視為空：%s", e)
        _olds = []
    else:
        raise RuntimeError(f"JWT_SECRET_KEYS_OLDS 解析失敗：{e}")

# 既有 fail-loud / dev 隨機產生邏輯保留
if not _jwt_secret:
    ...  # 不動

JWT_SECRET_KEY = _jwt_secret  # 既有變數保留，向下相容（測試 import 之）

def _kid_for(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]

# 模組常數：current kid + verify candidates
_CURRENT_KID = _kid_for(JWT_SECRET_KEY)
_VERIFY_KEYS: dict[str, str] = {_CURRENT_KID: JWT_SECRET_KEY}
for _old in _olds:
    if _old:
        _VERIFY_KEYS[_kid_for(_old)] = _old

# 過渡期：沒 kid 的 legacy token 用此 ordered list 嘗試
_LEGACY_TRY_ORDER: list[str] = [JWT_SECRET_KEY] + [k for k in _olds if k]
```

#### Sign（`create_access_token`）

```python
return jwt.encode(
    to_encode,
    JWT_SECRET_KEY,
    algorithm=JWT_ALGORITHM,
    headers={"kid": _CURRENT_KID},
)
```

#### Verify（重構 `decode_token` / `decode_token_allow_expired`）

抽出共用函式：

```python
def _select_secret_for_verify(token: str) -> str | None:
    """從 token header 抽 kid，回傳對應 secret；無 kid 回 None（呼叫端走 legacy 路徑）。"""
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None
    kid = header.get("kid")
    if not kid:
        return None
    return _VERIFY_KEYS.get(kid)  # 未知 kid → None，呼叫端會 401

def _decode_with_keys(token: str, *, allow_expired: bool = False) -> dict:
    """主 verify 入口。已先過 _check_token_algorithm。"""
    options = {"verify_exp": False} if allow_expired else {}

    # 1) 有 kid：用對應 key（未知 kid → 401）
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")

    kid = header.get("kid")
    if kid:
        secret = _VERIFY_KEYS.get(kid)
        if not secret:
            raise HTTPException(status_code=401, detail="無效或過期的 Token")
        return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options=options)

    # 2) 無 kid：legacy token，依序試所有 keys
    last_error: Exception | None = None
    for secret in _LEGACY_TRY_ORDER:
        try:
            return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options=options)
        except JWTError as e:
            last_error = e
    raise HTTPException(status_code=401, detail="無效或過期的 Token")
```

`decode_token` / `decode_token_allow_expired` 改用 `_decode_with_keys`，原有的 grace / jti 檢查邏輯保留。

#### 新增 `decode_token_for_audit`

```python
def decode_token_for_audit(token: str) -> dict | None:
    """專供 audit 路徑使用：multi-key 容忍、verify_exp=False、不檢 jti / token_version。
    純粹從 token 抽 user_id / username 寫 audit log，無 401 拋出（失敗回 None）。
    """
    if not token:
        return None
    try:
        _check_token_algorithm(token)
        return _decode_with_keys(token, allow_expired=True)
    except (JWTError, HTTPException):
        return None
```

### `api/auth.py:738` logout audit 抽 user_id

```python
# Before：from jose import jwt as _jose_jwt; _jose_jwt.decode(...) verify_exp=False
# After：
from utils.auth import decode_token_for_audit
_payload = decode_token_for_audit(token) or {}
audit_user_id = _payload.get("user_id")
audit_username = _payload.get("name")
```

### `utils/audit.py:339`

同上，改呼叫 `decode_token_for_audit(token)`。

### `services/activity_query_token` / `api/activity/_shared`

**本次不動**。檔頭加 deprecation 註解：

```python
# DEPRECATION（2026-05-21）：本模組借用 JWT_SECRET_KEY 做 HMAC，未支援 multi-key。
# JWT secret rotation 後（JWT_SECRET_KEY 變值），既有外發 activity query token 會失效。
# Follow-up：解耦到專屬 ACTIVITY_TOKEN_HMAC_KEY env，並支援 olds list 容忍 rotation。
```

`docs/parent_rls_env_vars.md`（或新建 `docs/jwt_secret_rotation.md`）的 rotation runbook 註記這個 side effect。

## Rotation 標準流程（runbook，寫成 doc）

| 步驟 | 動作 | 等多久 | 為什麼 |
|------|------|--------|--------|
| 1 | 產生新 32+ bytes urlsafe secret 為 `new_key` | — | — |
| 2 | 把 **目前** `JWT_SECRET_KEY` 值複製進 `JWT_SECRET_KEYS_OLDS` list、把 `new_key` 寫進 `JWT_SECRET_KEY`。restart。 | — | 此刻起新簽 token 用 new_key 帶 kid_new；舊 kid_old token 仍可驗。 |
| 3 | 觀察 ≥ `JWT_ABSOLUTE_LIFETIME_HOURS`（12h）+ 2h grace = 14h | 14h | 所有 staff session 最長壽命已到，舊 kid_old token 不應再出現。 |
| 4 | 把 `JWT_SECRET_KEYS_OLDS` 清空 `[]`。restart。 | — | 廢止舊 key，外流的 kid_old token 無法再驗證通過。 |

外流緊急情況（不要軟著陸）：跳到步驟 4 + 對應使用者 `token_version` bump。

## 測試（新增 ≈12 case，集中在 `tests/test_jwt_rotation.py`）

| 名稱 | 場景 | 預期 |
|------|------|------|
| `test_sign_includes_kid_header` | 新簽 token | header 含 `kid`，值等於 `sha256(JWT_SECRET_KEY)[:12]` |
| `test_verify_with_current_key` | current 簽 + current 驗 | pass |
| `test_verify_with_old_kid_in_olds_list` | 舊 key 簽（kid_old）+ olds 含舊 key | pass |
| `test_verify_with_old_kid_not_in_olds` | 舊 key 簽 + olds 不含 | 401 |
| `test_verify_unknown_kid_rejected` | 偽造 kid header | 401 |
| `test_verify_legacy_no_kid_with_current_key` | 模擬升版前舊 token（無 kid，用 current 簽） | pass（走 legacy try-loop） |
| `test_verify_legacy_no_kid_with_old_key` | 升版前 token，secret 已 rotate | pass if old 在 olds，否則 401 |
| `test_verify_legacy_no_kid_unknown_key` | 升版前 token，secret 早被刪 | 401 |
| `test_olds_json_invalid_dev_warns` | dev 模式下 `JWT_SECRET_KEYS_OLDS=garbage` | warning + 視為空 list |
| `test_olds_json_invalid_prod_raises` | prod `ENV=production` + invalid olds | RuntimeError |
| `test_decode_token_for_audit_works_across_rotation` | audit decode 支援 multi-key + verify_exp=False | pass |
| `test_decode_token_for_audit_returns_none_on_failure` | token 解析失敗 | 回 None（不拋） |

既有 49 個 test（`test_auth.py` / `test_jwt_blocklist.py` / `test_jwt_algorithm_check.py` 等）import `JWT_SECRET_KEY` 保持不變，會繼續通過（簽 / 驗都用 current）。

## 不在範圍 / Follow-ups

- **ParentRefreshToken**：DB-backed opaque token，hash 存 DB，無 JWT 簽章，不在本次。
- **`services/activity_query_token` HMAC**：JWT secret rotation 會讓既有外發 token 失效。本次只加 deprecation 註解；解耦到 `ACTIVITY_TOKEN_HMAC_KEY` 列下次。
- **CI 提示**：未來加 hook 偵測 prod env 沒設 `JWT_SECRET_KEY` 或 `JWT_SECRET_KEYS_OLDS` 含小於 32 bytes 的值時 fail。
- **Sentry 告警**：rotation 進行中如果出現大量 `unknown kid` → 401 應該觸發告警（可能是 kid mismatch / olds 漏 sync），列 Sentry alert config follow-up。

## 風險

| 風險 | 緩解 |
|------|------|
| `JWT_SECRET_KEYS_OLDS` 解析錯誤 → 啟動失敗 | prod fail-loud（明確錯誤訊息）；dev fallback 警告 |
| 攻擊者偽造 kid 試圖選用較弱 secret | 所有 secret 來源都是同強度 urlsafe(32+)；只接受 `_VERIFY_KEYS` 已登錄的 kid |
| Legacy try-loop 被當 oracle | 每個 secret 仍走 `jwt.decode` 簽章驗證，無 timing leak；無 kid token 在過渡期 ≤14h 後不會再出現 |
| Rotation 第 2 步漏複製舊 key 進 olds | 既有未過期 token 全被踢；運維 SOP 註明檢查清單，CI 偵測 |
| `verify_exp=False` audit 路徑被當成 auth bypass | `decode_token_for_audit` 僅供 audit 寫入，**不** 設定 `current_user`、不通過 `Depends`；review 階段檢查 call site |

## 工作量估算

- 程式碼改動：`utils/auth.py` ≈ +60/-15 行；`api/auth.py` -10/+3；`utils/audit.py` -5/+1
- 測試：新增 `tests/test_jwt_rotation.py` ≈ 250 行
- 文件：新增 `docs/jwt_secret_rotation.md`（runbook）
- 預計：1 工作日（含 PR review）
