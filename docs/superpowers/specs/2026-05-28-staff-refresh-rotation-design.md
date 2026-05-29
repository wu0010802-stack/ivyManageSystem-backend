# Spec F: 員工端 refresh rotation + sessions (#11)

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：🟠 P1 #11 — 員工端無 refresh token rotation / 無 active session 列表 / 無遠端強制登出
**對應 spec 系列**：A B C D E ✅ / **F (staff refresh)** (最後一個)

---

## 1. Why

### 1.1 攻擊面

`utils/auth.py:41-43` 自註「家長端已有 ParentRefreshToken family rotation，staff 端用較簡化的 absolute lifetime」。員工端：
- **無 refresh rotation** → 員工 token 不能短效 + reissue，被竊後在 absolute lifetime (預設 8h) 內可任意用
- **無 /sessions 端點** → 員工不知自己有多少 active session，無法看哪裝置登入過
- **無 per-device 強制下線** → 員工發現帳號被異常登入只能改密碼 (一鍋端踢全部 session)
- **未記 UA/IP** → 無 forensic trail 追溯異常登入裝置

### 1.2 既有 ParentRefreshToken 設計可複用

`models/parent_refresh_token.py` 完整實作:
- token raw 永不入庫；只存 sha256(raw) hex (64 字)
- `family_id` 串同一裝置 rotation 鏈；reuse 偵測整 family revoke
- `used_at != NULL` 後再被送來 → reuse；5s race window 容忍
- `expires_at` 預設 +30 天；GC 7 天後刪
- `user_agent` / `ip` 觀測欄位

`api/parent_portal/auth.py` 807 行內含 rotation logic 可 port。

### 1.3 設計決策（user 拍板 2026-05-28）

| 決策 | Choice |
|------|--------|
| Scope | Full port BE Phase 1 (12-18h) + FE Phase 2 (4-6h) |
| Spec 範圍 | BE+FE 同 spec.md (兩 PR 兩 worktree) |
| Force logout | 雙模式（per-session revoke + bump token_version 全踢） |
| UA parsing | Raw string (存原始 header) |

---

## 2. Goals / Non-goals

### Goals
- (G1) **BE**: 新 `models/staff_refresh_token.py` 1:1 copy ParentRefreshToken schema, table `staff_refresh_tokens`
- (G2) **BE**: alembic migration 建 staff_refresh_tokens 表（含 indexes / FK / sequence）
- (G3) **BE**: `utils/auth.py` / `api/auth.py` 加 staff refresh rotation logic (port from parent_portal/auth.py)：
  - login 成功簽 access_token + refresh_token (set as HttpOnly cookie path=/api/auth)
  - 新 endpoint `POST /api/auth/refresh` — rotation：驗 refresh token → 新 access + 新 refresh (rotate) → 標 old `used_at`
  - reuse detection：used_at != NULL 後再被送來 → 整 family revoke + 強制 logout
- (G4) **BE**: 新 endpoint `GET /api/auth/sessions` 列出當前 user 所有 active session（family_id / user_agent / ip / created_at / last_used）
- (G5) **BE**: 新 endpoint `DELETE /api/auth/sessions/{family_id}` — per-session revoke（mark revoked_at on all tokens in family）
- (G6) **BE**: 新 endpoint `POST /api/auth/sessions/logout-all` — bump token_version + revoke all StaffRefreshToken family（雙模式：剛回應 user 拍板）
- (G7) **BE**: login flow 簽 refresh token 時記 UA + IP（raw string）
- (G8) **BE**: logout flow revoke current family
- (G9) **BE**: 6-10 pytest cover rotation / reuse detection / sessions list / per-session revoke / logout-all
- (G10) **FE (ivy-frontend)**: SettingsAccountTab 加 Active Sessions 表格 (current session highlight + revoke button per row + revoke all button)
- (G11) **FE**: refresh interceptor — access token 過期時 auto call `/api/auth/refresh`，refresh 失敗導向 login
- (G12) 零回歸：既有 5582 pytest baseline + 新 6-10 test 全綠
- (G13) Migration 自帶 GC scheduler（refresh token 過期 7 天後刪除）

### Non-goals
- 不重寫 ParentRefreshToken (家長端不動，純複製 schema/logic 新 table)
- 不引入 OAuth / OpenID (純 JWT + DB-backed refresh family)
- 不改既有 staff JWT secret / 加密演算法
- 不引入「device fingerprinting」(只記 UA + IP)
- 不在本 spec 內處理 staff 端 audit_logs 加 UA/IP (audit middleware 既有設計)
- 不在本 spec 內處理 P0/P1 其餘 audit findings (A-E ✅)

---

## 3. Architecture

### 3.1 PR 結構

| Repo | Branch | Commit | 工時 |
|------|--------|--------|------|
| **ivy-backend** | `feat/staff-refresh-rotation-2026-05-28-backend` | 4-5 commits (model+migration / rotation logic / sessions endpoints / tests / GC) | 12-18h |
| **ivy-frontend** | `feat/staff-refresh-rotation-2026-05-28-frontend` | 2-3 commits (refresh interceptor / SettingsAccountTab UI) | 4-6h |

兩 PR cross-reference spec.md，BE PR 先 merge (API 存在) → FE PR 才能 call new endpoints。

### 3.2 StaffRefreshToken model + migration (PR-F-BE-C1)

新檔 `models/staff_refresh_token.py` 1:1 copy ParentRefreshToken schema：

```python
"""models/staff_refresh_token.py — 員工端長效 refresh token

設計重點 (1:1 對齊 ParentRefreshToken):
- token raw 永不入庫；只存 sha256(raw) hex
- family_id 串同一裝置 rotation 鏈；reuse 偵測整 family revoke
- used_at != NULL 後再被送來 → reuse；5s race tolerance
- expires_at 預設 +30 天；GC 7 天後刪

Spec F (audit P1 #11)
"""

import uuid
from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Index, Integer, String,
)
from models.base import Base


class StaffRefreshToken(Base):
    __tablename__ = "staff_refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    family_id = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    token_hash = Column(String(64), nullable=False, unique=True)
    parent_token_id = Column(
        BigInteger,
        ForeignKey("staff_refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    user_agent = Column(String(255), nullable=True)
    ip = Column(String(45), nullable=True)

    __table_args__ = (
        Index("ix_staff_refresh_user_family", "user_id", "family_id"),
        Index("ix_staff_refresh_expires_at", "expires_at"),
    )
```

alembic migration `staffrt01_staff_refresh_tokens.py`：建表 + indexes + FK + sequence.

### 3.3 Rotation logic (PR-F-BE-C2)

新 `services/staff_refresh.py` 或加進 `utils/auth.py`：

```python
from hashlib import sha256
import secrets

def issue_refresh_token(user_id: int, family_id: str | None = None,
                        parent_token_id: int | None = None,
                        user_agent: str | None = None, ip: str | None = None) -> str:
    """生成 refresh token 並寫 DB。回傳 raw token (給 cookie)。"""
    raw = secrets.token_urlsafe(64)
    token_hash = sha256(raw.encode()).hexdigest()
    expires_at = now_taipei_naive() + timedelta(days=30)
    
    with session_scope() as session:
        rt = StaffRefreshToken(
            user_id=user_id,
            family_id=family_id or str(uuid.uuid4()),
            token_hash=token_hash,
            parent_token_id=parent_token_id,
            expires_at=expires_at,
            user_agent=user_agent,
            ip=ip,
        )
        session.add(rt)
        session.commit()
    return raw


def rotate_refresh_token(raw_token: str, user_agent: str | None, ip: str | None) -> tuple[str, int]:
    """Verify + rotate. Return (new_raw_token, user_id). Raise on reuse / expired / invalid."""
    token_hash = sha256(raw_token.encode()).hexdigest()
    
    with session_scope() as session:
        rt = session.query(StaffRefreshToken).filter(
            StaffRefreshToken.token_hash == token_hash
        ).first()
        
        if not rt:
            raise HTTPException(401, "Invalid refresh token")
        
        if rt.revoked_at:
            raise HTTPException(401, "Token revoked")
        
        if rt.expires_at < now_taipei_naive():
            raise HTTPException(401, "Token expired")
        
        # Reuse detection: used_at != NULL → 整 family revoke
        if rt.used_at:
            # 5s race tolerance: 同 token double request
            if (now_taipei_naive() - rt.used_at).total_seconds() < 5:
                # race window, return last issued token (idempotent)
                # ... complex logic, see parent flow ...
                pass
            else:
                # True reuse → revoke entire family + bump token_version (force logout)
                session.query(StaffRefreshToken).filter(
                    StaffRefreshToken.family_id == rt.family_id
                ).update({"revoked_at": now_taipei_naive()})
                user = session.query(User).get(rt.user_id)
                user.token_version = (user.token_version or 0) + 1
                session.commit()
                raise HTTPException(401, "Token reuse detected - family revoked")
        
        # Normal rotation
        rt.used_at = now_taipei_naive()
        new_raw = secrets.token_urlsafe(64)
        new_hash = sha256(new_raw.encode()).hexdigest()
        new_rt = StaffRefreshToken(
            user_id=rt.user_id,
            family_id=rt.family_id,  # 同 family
            token_hash=new_hash,
            parent_token_id=rt.id,
            expires_at=now_taipei_naive() + timedelta(days=30),
            user_agent=user_agent,
            ip=ip,
        )
        session.add(new_rt)
        session.commit()
        return new_raw, rt.user_id
```

`api/auth.py` 加：
- `POST /api/auth/refresh` — call `rotate_refresh_token` → set new access + refresh cookie
- login 端 (既有 `/api/auth/login`) 加 issue refresh + set cookie + 記 UA/IP

### 3.4 /sessions endpoints (PR-F-BE-C3)

```python
@router.get("/sessions", response_model=list[SessionItemOut])
def list_my_sessions(current_user: dict = Depends(get_current_user)):
    """List active StaffRefreshToken family for current user."""
    with session_scope() as session:
        # 同一 family 多 token (rotation 鏈)；展示時聚合到 family 層級取最新 token
        sql = """
        SELECT family_id, MAX(created_at) as last_active, MAX(user_agent) as user_agent,
               MAX(ip) as ip, COUNT(*) as token_count
        FROM staff_refresh_tokens
        WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > NOW()
        GROUP BY family_id
        ORDER BY last_active DESC
        """
        rows = session.execute(text(sql), {"uid": current_user["user_id"]}).all()
        return [SessionItemOut(**r._mapping) for r in rows]


@router.delete("/sessions/{family_id}")
def revoke_session(family_id: str, current_user: dict = Depends(get_current_user)):
    """Revoke per-session — mark all tokens in family as revoked。"""
    with session_scope() as session:
        n = session.query(StaffRefreshToken).filter(
            StaffRefreshToken.user_id == current_user["user_id"],
            StaffRefreshToken.family_id == family_id,
        ).update({"revoked_at": now_taipei_naive()})
        session.commit()
    return {"revoked": n}


@router.post("/sessions/logout-all")
def logout_all_sessions(current_user: dict = Depends(get_current_user)):
    """Force logout all sessions — bump token_version + revoke all StaffRefreshToken family."""
    with session_scope() as session:
        user = session.query(User).get(current_user["user_id"])
        user.token_version = (user.token_version or 0) + 1  # 既有 mechanism, 踢所有 access token
        session.query(StaffRefreshToken).filter(
            StaffRefreshToken.user_id == user.id,
            StaffRefreshToken.revoked_at.is_(None),
        ).update({"revoked_at": now_taipei_naive()})
        session.commit()
    return {"logout_all": True}
```

### 3.5 GC scheduler (PR-F-BE-C4)

加進 `services/scheduler.py` 既有 scheduler：

```python
def gc_expired_refresh_tokens():
    """每日清過期 + revoked >7d 的 staff_refresh_tokens。"""
    cutoff = now_taipei_naive() - timedelta(days=7)
    with session_scope() as session:
        n = session.query(StaffRefreshToken).filter(
            (StaffRefreshToken.expires_at < cutoff) |
            (StaffRefreshToken.revoked_at < cutoff)
        ).delete()
        session.commit()
        logger.info("GC staff_refresh_tokens: %d deleted", n)
```

每日 03:00 跑（與既有 GC 排程同步）。

### 3.6 FE Phase 2 (PR-F-FE)

**Files**:
- `src/api/staffAuth.ts` 加 `listSessions()` / `revokeSession(familyId)` / `logoutAll()` wrapper
- `src/api/index.ts` axios interceptor: 401 access token 過期時 → call `/api/auth/refresh` → retry original; refresh 失敗 redirect login
- `src/views/SettingsAccountTab.vue` (or similar) 加 Active Sessions 表格 (current highlight / UA truncate / revoke button per row + revoke all button)

vitest test (2-3 個):
- session list 渲染
- revoke session call API
- refresh interceptor retry

---

## 4. 測試計畫

**BE pytest (6-10 tests)** in `tests/test_staff_refresh_rotation.py`:
1. test_login_issues_refresh_token (login 後 DB 有 StaffRefreshToken)
2. test_refresh_rotates_token (call /refresh → 新 token + 舊 used_at != NULL)
3. test_refresh_token_reuse_detected (re-call old token → 整 family revoked + 401)
4. test_refresh_token_5s_race_tolerance (同 token double request 5s 內不 revoke)
5. test_refresh_expired_token_rejected
6. test_list_sessions (logged-in user 看到自己的 family list)
7. test_revoke_per_session (DELETE /sessions/{family_id} → mark revoked)
8. test_logout_all (POST /sessions/logout-all → bump token_version + revoke all)
9. test_user_cannot_revoke_others_session (403 or 404 on other user's family)
10. test_login_records_ua_ip (StaffRefreshToken.user_agent + ip 不為空)

**FE vitest (2-3 tests)** in `src/views/__tests__/SettingsAccountTab.test.ts`:
- sessions list render
- revoke session call API
- refresh interceptor 行為

**回歸**：既有 5582 baseline + 新 8-13 test 全綠。

---

## 5. Roll-out

### 5.1 部署步驟 (BE Phase 1)

1. BE PR merge + `alembic upgrade head` (staffrt01)
2. 後端 service 重啟 → 新 login 簽 refresh token，既有 access token (無 refresh) 仍可用直到 expire
3. 既有員工下次 login → 拿到 refresh token，啟動 rotation flow
4. **不影響 既有 access token**（既有 staff 仍用 absolute lifetime 直到 token 過期；新 login 用 rotation）
5. FE Phase 2 PR merge → SettingsAccountTab 顯示 active sessions

### 5.2 回退方案

- Revert BE PR → alembic downgrade (drop staff_refresh_tokens table)
- 既有 access token 仍正常運作 (新 endpoints 移除 → FE 沒 endpoint 可 call)

### 5.3 監控指標

7 天觀察：
- StaffRefreshToken table size 增長 (~每 staff login 1 row + rotation 新增)
- `Token reuse detected` log 量 (應為 0；非 0 表 cookie 洩漏或 race window > 5s)
- `/sessions` endpoint usage count

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| 既有 staff 同時持多 access token (無 refresh)，遷移到 rotation 流程時 access token 過期後就要重 login | 短暫 UX 中斷 (員工要重 login) | 既有 token absolute lifetime 8h，過期自然 re-login 觸發 rotation；無強制踢出 |
| Refresh token 30 天長效，被竊風險 | 整 30 天攻擊面 | reuse detection + per-session revoke + logout-all 雙重防護；cookie HttpOnly + SameSite |
| 5s race tolerance window 在分散式 (multi-worker) 可能有 clock skew | race window 真實值可能 >5s | 觀測 prod log 看「reuse detected」誤判率；必要時調大 window |
| StaffRefreshToken table 增長無限 | DB size grow | GC scheduler 每日清過期+revoked >7d，spec §3.5 |
| logout-all 配合 bump token_version 在多 worker 場景需 access token cache invalidation | access token 立刻無效但 cache 可能延遲 | 既有 token_version 機制已是 prod-ready；bump 後下次 refresh 即拒 |

---

## 7. Out of scope

- FE Phase 2 (在 spec.md 但 separate PR/branch)
- 不引入 OAuth / OpenID
- 不對家長端 ParentRefreshToken 改動
- 不在 spec 內處理 device fingerprinting (只記 UA + IP)
- 不引入 audit_logs 對 session revoke 寫 audit (既有 audit middleware 已 cover write_audit_log)

---

## 8. 驗收 checklist

PR 合併 + deploy 後 USER 手動驗證：

- [ ] `alembic upgrade head` 跑 staffrt01 無錯
- [ ] login → DB 看到一筆 StaffRefreshToken (user_agent + ip 不為空)
- [ ] `POST /api/auth/refresh` → 新 token + 舊 used_at != NULL
- [ ] 重複送舊 token → 401 + 整 family revoked
- [ ] `GET /api/auth/sessions` → 看到自己的 active sessions list (含 UA/IP)
- [ ] `DELETE /api/auth/sessions/{family_id}` → 該 family revoked，其他 family 仍可用
- [ ] `POST /api/auth/sessions/logout-all` → 所有 session revoked + 自己被踢出
- [ ] FE SettingsAccountTab 顯示 sessions list + revoke 按鈕
- [ ] 7 天 GC 跑後過期 token 已清

---

## 9. 後續 follow-up

- 加 webhook on suspicious revoke event (reuse detection) → Sentry alert
- 加 last_used timestamp column (現在用 max(created_at) over family；精確值需 update 每次 rotation)
- 加 device label 讓 user 自定義 ("我的 MBP" 替代 "Chrome 124 on macOS")
- Spec D `ivy_audit_writer` role 對 staff_refresh_tokens 表 GRANT SELECT/INSERT (若要 audit_logs 寫 token revoke 事件)
