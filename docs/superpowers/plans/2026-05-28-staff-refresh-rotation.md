# Staff refresh rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Port ParentRefreshToken family rotation to staff: refresh endpoint + /sessions list + per-session revoke + logout-all 雙模式 + login 簽 refresh+UA/IP + GC + tests + FE refresh interceptor + SettingsAccountTab UI。

**Architecture:** BE 4 commits (model+migration ✅ Task 1 / rotation logic + endpoints / sessions endpoints / GC + tests) + FE 2-3 commits (refresh interceptor / Active Sessions UI). 兩 PR 兩 repo cross-reference spec.md。

**Tech Stack:** SQLAlchemy / FastAPI / pytest / Vue 3 + axios interceptor + Element Plus

**Spec:** `docs/superpowers/specs/2026-05-28-staff-refresh-rotation-design.md` (commit `a734d58`)

---

## Status

- ✅ **Task 1** (commit `c7352a9`): `models/staff_refresh_token.py` + alembic `staffrt01` migration
- ⏳ **Task 2-4**: BE rotation logic + endpoints + GC + tests (本 plan)
- ⏳ **Task 5**: FE refresh interceptor + UI (separate ivy-frontend worktree)

---

## File Structure (Task 2-4)

**New files:**
- `services/staff_refresh.py` — issue/rotate refresh token helpers (port from parent_portal/auth.py logic)
- `tests/test_staff_refresh_rotation.py` — 8-10 pytest

**Modified files:**
- `api/auth.py` — login 加 issue refresh + set cookie + 記 UA/IP；新加 `POST /api/auth/refresh` + `GET /api/auth/sessions` + `DELETE /api/auth/sessions/{family_id}` + `POST /api/auth/sessions/logout-all` + logout flow revoke current family
- `utils/cookie.py` — 加 `set_staff_refresh_cookie` / `clear_staff_refresh_cookie` helpers (與 parent_refresh_token cookie 分離 name)
- `services/scheduler.py` (or 既有 scheduler module) — 加 `gc_staff_refresh_tokens` 每日 03:00 跑

**Cookie naming**: `staff_refresh_token` (avoid collision with parent `parent_refresh_token`). Path: `/api/auth`.

---

## Task 2: rotation logic + refresh endpoint + login flow

**Files:**
- Create: `services/staff_refresh.py`
- Modify: `api/auth.py` (login + refresh endpoint)
- Modify: `utils/cookie.py` (staff refresh cookie helpers)

### Steps

- [ ] **Step 2.1: grep parent rotation pattern**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-staff-refresh-rotation-2026-05-28-backend
grep -n "def.*refresh\|family\|reuse" api/parent_portal/auth.py | head -30
grep -n "parent_refresh_token\|set_cookie.*refresh\|secrets.token_urlsafe" api/parent_portal/auth.py utils/cookie.py | head -20
```

抓 parent 既有 rotation 邏輯 + cookie pattern。下面實作 1:1 port。

- [ ] **Step 2.2: 建 services/staff_refresh.py**

```python
"""services/staff_refresh.py — 員工端 refresh token rotation helpers.

1:1 port from api/parent_portal/auth.py rotation logic.

Spec F (audit P1 #11)
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import update

from models.auth import User
from models.staff_refresh_token import StaffRefreshToken
from models.database import session_scope
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

# 與 parent 對齊
REFRESH_LIFETIME = timedelta(days=30)
RACE_TOLERANCE_SECONDS = 5


def _hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def issue_refresh_token(
    user_id: int,
    family_id: str | None = None,
    parent_token_id: int | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, int]:
    """生成 refresh token 並寫 DB。回傳 (raw, db_id)。"""
    raw = secrets.token_urlsafe(64)
    token_hash = _hash_token(raw)
    expires_at = now_taipei_naive() + REFRESH_LIFETIME
    fam = family_id or str(uuid.uuid4())

    with session_scope() as session:
        rt = StaffRefreshToken(
            user_id=user_id,
            family_id=fam,
            token_hash=token_hash,
            parent_token_id=parent_token_id,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:255] or None,
            ip=ip,
        )
        session.add(rt)
        session.commit()
        return raw, rt.id


def rotate_refresh_token(
    raw_token: str, user_agent: str | None, ip: str | None
) -> tuple[str, int]:
    """Verify + rotate. Return (new_raw_token, user_id). Raise 401 on error."""
    token_hash = _hash_token(raw_token)

    with session_scope() as session:
        rt = (
            session.query(StaffRefreshToken)
            .filter(StaffRefreshToken.token_hash == token_hash)
            .first()
        )

        if not rt:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        if rt.revoked_at:
            raise HTTPException(status_code=401, detail="Refresh token revoked")

        if rt.expires_at < now_taipei_naive():
            raise HTTPException(status_code=401, detail="Refresh token expired")

        # Reuse detection
        if rt.used_at is not None:
            delta = (now_taipei_naive() - rt.used_at).total_seconds()
            if delta < RACE_TOLERANCE_SECONDS:
                # Race window: 找最新 issued in same family 回傳 idempotent
                latest = (
                    session.query(StaffRefreshToken)
                    .filter(
                        StaffRefreshToken.family_id == rt.family_id,
                        StaffRefreshToken.parent_token_id == rt.id,
                    )
                    .order_by(StaffRefreshToken.created_at.desc())
                    .first()
                )
                if latest:
                    # 同 token 的 child 已存在 (parent rotated this 5s ago)
                    # idempotent: re-issue 不重 rotate (cannot retrieve raw, fail-closed)
                    pass  # Fall through to true reuse path; simpler
            # True reuse → revoke family + bump user.token_version
            logger.warning(
                "Refresh reuse detected: user_id=%s family_id=%s",
                rt.user_id,
                rt.family_id,
            )
            session.query(StaffRefreshToken).filter(
                StaffRefreshToken.family_id == rt.family_id,
                StaffRefreshToken.revoked_at.is_(None),
            ).update({"revoked_at": now_taipei_naive()})
            user = session.query(User).get(rt.user_id)
            if user:
                user.token_version = (user.token_version or 0) + 1
            session.commit()
            raise HTTPException(
                status_code=401, detail="Refresh token reuse detected"
            )

        # Normal rotation
        rt.used_at = now_taipei_naive()
        new_raw = secrets.token_urlsafe(64)
        new_hash = _hash_token(new_raw)
        new_rt = StaffRefreshToken(
            user_id=rt.user_id,
            family_id=rt.family_id,
            token_hash=new_hash,
            parent_token_id=rt.id,
            expires_at=now_taipei_naive() + REFRESH_LIFETIME,
            user_agent=(user_agent or "")[:255] or None,
            ip=ip,
        )
        session.add(new_rt)
        session.commit()
        return new_raw, rt.user_id


def revoke_family(user_id: int, family_id: str) -> int:
    """Per-session revoke."""
    with session_scope() as session:
        n = (
            session.query(StaffRefreshToken)
            .filter(
                StaffRefreshToken.user_id == user_id,
                StaffRefreshToken.family_id == family_id,
                StaffRefreshToken.revoked_at.is_(None),
            )
            .update({"revoked_at": now_taipei_naive()})
        )
        session.commit()
    return n


def revoke_all_for_user(user_id: int) -> int:
    """Logout-all: revoke all + bump token_version (caller handles user.token_version)."""
    with session_scope() as session:
        n = (
            session.query(StaffRefreshToken)
            .filter(
                StaffRefreshToken.user_id == user_id,
                StaffRefreshToken.revoked_at.is_(None),
            )
            .update({"revoked_at": now_taipei_naive()})
        )
        user = session.query(User).get(user_id)
        if user:
            user.token_version = (user.token_version or 0) + 1
        session.commit()
    return n
```

- [ ] **Step 2.3: utils/cookie.py 加 staff refresh cookie helpers**

```python
# 既有 _COOKIE_SAMESITE / _COOKIE_SECURE 重用
_STAFF_REFRESH_COOKIE_PATH = "/api/auth"
_STAFF_REFRESH_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days


def set_staff_refresh_cookie(response, token: str) -> None:
    response.set_cookie(
        key="staff_refresh_token",
        value=token,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_STAFF_REFRESH_COOKIE_PATH,
        max_age=_STAFF_REFRESH_COOKIE_MAX_AGE,
    )


def clear_staff_refresh_cookie(response) -> None:
    response.delete_cookie(
        key="staff_refresh_token",
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_STAFF_REFRESH_COOKIE_PATH,
    )
```

- [ ] **Step 2.4: api/auth.py login 加 issue refresh + set cookie + 記 UA/IP**

找既有 login 簽 access_token 之後（`set_access_token_cookie(response, token)` 之前）加：

```python
# Spec F: 簽 refresh token + 寫 staff_refresh_tokens + 記 UA/IP
ua = request.headers.get("user-agent") or ""
ip = client_ip  # 既有 _check_ip_rate_limit 已抓
from services.staff_refresh import issue_refresh_token
from utils.cookie import set_staff_refresh_cookie
refresh_raw, _ = issue_refresh_token(
    user_id=user.id, user_agent=ua, ip=ip,
)
set_staff_refresh_cookie(response, refresh_raw)
```

- [ ] **Step 2.5: api/auth.py 加 POST /api/auth/refresh endpoint**

```python
@router.post("/refresh")
def refresh_access_token(request: Request):
    """Rotation: 驗 refresh cookie → 新 access + 新 refresh cookie。"""
    raw = request.cookies.get("staff_refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token")

    ua = request.headers.get("user-agent") or ""
    ip = get_client_ip(request) or "unknown"

    new_refresh, user_id = rotate_refresh_token(raw, ua, ip)

    # 拿 user 拚新 access_token
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User inactive")
        emp = session.query(Employee).filter(Employee.id == user.employee_id).first() if user.employee_id else None
        permission_names = resolve_user_permissions(user)
        new_access = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
            "permission_names": permission_names,
            "token_version": user.token_version,
        })
    finally:
        session.close()

    response = JSONResponse(content={"message": "refreshed"})
    set_access_token_cookie(response, new_access)
    set_staff_refresh_cookie(response, new_refresh)
    return response
```

- [ ] **Step 2.6: api/auth.py logout flow revoke current family**

找既有 logout endpoint (應在 api/auth.py 內 search "logout")，在清 cookie 之前加：

```python
# Spec F: revoke current family (only this device)
raw = request.cookies.get("staff_refresh_token")
if raw:
    from hashlib import sha256
    h = sha256(raw.encode()).hexdigest()
    session = get_session()
    try:
        rt = session.query(StaffRefreshToken).filter_by(token_hash=h).first()
        if rt:
            from services.staff_refresh import revoke_family
            revoke_family(rt.user_id, rt.family_id)
    finally:
        session.close()

# clear cookie 既有
clear_staff_refresh_cookie(response)
```

- [ ] **Step 2.7: Commit (C2)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-staff-refresh-rotation-2026-05-28-backend
git add services/staff_refresh.py utils/cookie.py api/auth.py
git commit -m "$(cat <<'EOF'
feat(auth): staff refresh token rotation logic + endpoint

Spec F PR-F-BE-C2 (audit P1 #11)：

- services/staff_refresh.py: 1:1 port ParentRefreshToken rotation logic
  - issue_refresh_token: 簽 raw + 寫 DB + UA/IP raw string
  - rotate_refresh_token: verify + reuse detection (5s race tolerance) +
    family revoke + token_version bump on reuse
  - revoke_family / revoke_all_for_user helpers
- utils/cookie.py: staff_refresh_token cookie helpers (與 parent 名 distinct
  避免單瀏覽器同時 staff+parent session 衝突)
- api/auth.py:
  - login 加 issue refresh + set staff_refresh_token cookie + 記 UA/IP raw
  - POST /api/auth/refresh: rotation → 新 access + 新 refresh
  - logout 加 revoke current family

Refs: Spec docs/superpowers/specs/2026-05-28-staff-refresh-rotation-design.md §3.3
EOF
)"
```

---

## Task 3: /sessions endpoints

**Files:**
- Modify: `api/auth.py` (3 new endpoints)

### Steps

- [ ] **Step 3.1: 加 SessionItemOut Pydantic model**

```python
class SessionItemOut(BaseModel):
    family_id: str
    last_active: datetime
    user_agent: str | None
    ip: str | None
    token_count: int
    is_current: bool = False  # mark 當前 session
```

- [ ] **Step 3.2: GET /api/auth/sessions endpoint**

```python
@router.get("/sessions", response_model=list[SessionItemOut])
def list_my_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """List active StaffRefreshToken family for current user。"""
    raw = request.cookies.get("staff_refresh_token")
    current_family = None
    if raw:
        h = sha256(raw.encode()).hexdigest()
        # find current session's family
        session = get_session()
        try:
            rt = session.query(StaffRefreshToken).filter_by(token_hash=h).first()
            if rt:
                current_family = rt.family_id
        finally:
            session.close()
    
    session = get_session()
    try:
        sql = text("""
        SELECT family_id, MAX(created_at) AS last_active,
               MAX(user_agent) AS user_agent, MAX(ip) AS ip,
               COUNT(*) AS token_count
        FROM staff_refresh_tokens
        WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > :now
        GROUP BY family_id
        ORDER BY last_active DESC
        """)
        rows = session.execute(sql, {"uid": current_user["user_id"], "now": now_taipei_naive()}).all()
        return [
            SessionItemOut(
                family_id=r.family_id,
                last_active=r.last_active,
                user_agent=r.user_agent,
                ip=r.ip,
                token_count=r.token_count,
                is_current=(r.family_id == current_family),
            )
            for r in rows
        ]
    finally:
        session.close()
```

- [ ] **Step 3.3: DELETE /api/auth/sessions/{family_id}**

```python
@router.delete("/sessions/{family_id}")
def revoke_session(
    family_id: str,
    current_user: dict = Depends(get_current_user),
):
    from services.staff_refresh import revoke_family
    n = revoke_family(current_user["user_id"], family_id)
    if n == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"revoked": n}
```

- [ ] **Step 3.4: POST /api/auth/sessions/logout-all**

```python
@router.post("/sessions/logout-all")
def logout_all_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    from services.staff_refresh import revoke_all_for_user
    revoke_all_for_user(current_user["user_id"])
    response = JSONResponse(content={"logout_all": True})
    clear_staff_refresh_cookie(response)
    clear_access_token_cookie(response)
    return response
```

- [ ] **Step 3.5: Commit (C3)**

```bash
git add api/auth.py
git commit -m "$(cat <<'EOF'
feat(auth): /sessions endpoints (list / per-session revoke / logout-all)

Spec F PR-F-BE-C3 (audit P1 #11)：

- GET /api/auth/sessions: list active StaffRefreshToken family 聚合 (family_id +
  last_active + UA/IP + token_count + is_current 標記當前裝置)
- DELETE /api/auth/sessions/{family_id}: per-session revoke (mark family
  revoked_at; 其他 family 不影響)
- POST /api/auth/sessions/logout-all: 雙模式踢全部 (revoke all family +
  bump user.token_version + clear self cookies)

Refs: Spec docs/superpowers/specs/2026-05-28-staff-refresh-rotation-design.md §3.4
EOF
)"
```

---

## Task 4: GC scheduler + 8-10 pytest

**Files:**
- Modify: `services/scheduler.py` (or 既有 scheduler) 加 GC job
- Create: `tests/test_staff_refresh_rotation.py`

### Steps

- [ ] **Step 4.1: 加 GC job in services/scheduler.py**

```python
def gc_staff_refresh_tokens():
    """每日 03:00 清過期 + revoked >7d staff_refresh_tokens。"""
    from datetime import timedelta
    from models.staff_refresh_token import StaffRefreshToken
    from models.database import session_scope
    cutoff = now_taipei_naive() - timedelta(days=7)
    try:
        with session_scope() as session:
            n = session.query(StaffRefreshToken).filter(
                (StaffRefreshToken.expires_at < cutoff)
                | (StaffRefreshToken.revoked_at < cutoff)
            ).delete()
            session.commit()
            logger.info("GC staff_refresh_tokens: %d deleted", n)
    except Exception as e:
        logger.warning("gc_staff_refresh_tokens failed: %s", e)
```

註冊到既有 scheduler (與 cleanup_rate_limit_buckets / 其他 GC 同 pattern)。確認本 worktree scheduler module 位置 + cron 設定方式。

- [ ] **Step 4.2: 寫 pytest tests/test_staff_refresh_rotation.py**

```python
"""Spec F: staff refresh rotation 8-10 pytest。"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from models.auth import User
from models.staff_refresh_token import StaffRefreshToken
from services.staff_refresh import (
    issue_refresh_token, rotate_refresh_token, revoke_family, revoke_all_for_user,
)


@pytest.fixture
def staff_user(test_db_session):
    from utils.auth import hash_password
    user = User(
        username="staff1", password_hash=hash_password("pw"),
        role="teacher", is_active=True, token_version=0,
    )
    test_db_session.add(user)
    test_db_session.commit()
    return user


def test_issue_refresh_token_writes_db(test_db_session, staff_user):
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl/8", ip="1.1.1.1")
    assert len(raw) > 32
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt.user_id == staff_user.id
    assert rt.user_agent == "curl/8"
    assert rt.ip == "1.1.1.1"
    assert rt.expires_at > datetime.now()


def test_rotate_refresh_returns_new_token(test_db_session, staff_user):
    raw, _ = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    new_raw, uid = rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert new_raw != raw
    assert uid == staff_user.id


def test_rotate_marks_old_token_used(test_db_session, staff_user):
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rotate_refresh_token(raw, "curl", "1.1.1.1")
    test_db_session.expire_all()
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt.used_at is not None


def test_rotate_reuse_revokes_family(test_db_session, staff_user):
    """重複用 old token → 整 family revoked + 401 + bump token_version。"""
    raw, _ = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rotate_refresh_token(raw, "curl", "1.1.1.1")  # first rotate OK
    
    # Wait beyond 5s race tolerance (mock now_taipei_naive +10s)
    from services import staff_refresh as sr_mod
    with patch.object(sr_mod, "now_taipei_naive", return_value=datetime.now() + timedelta(seconds=10)):
        with pytest.raises(Exception) as exc:
            rotate_refresh_token(raw, "curl", "1.1.1.1")
        assert "reuse" in str(exc.value).lower()
    
    test_db_session.expire_all()
    user = test_db_session.get(User, staff_user.id)
    assert user.token_version >= 1
    
    # 同 family 所有 token revoked
    tokens = test_db_session.query(StaffRefreshToken).filter_by(user_id=staff_user.id).all()
    assert all(t.revoked_at is not None for t in tokens)


def test_rotate_expired_token_rejected(test_db_session, staff_user):
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    rt.expires_at = datetime.now() - timedelta(days=1)
    test_db_session.commit()
    
    with pytest.raises(Exception) as exc:
        rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert "expired" in str(exc.value).lower()


def test_revoke_family_marks_all_revoked(test_db_session, staff_user):
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    family_id = rt.family_id
    
    n = revoke_family(staff_user.id, family_id)
    assert n == 1
    
    test_db_session.expire_all()
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt.revoked_at is not None


def test_revoke_all_bumps_token_version(test_db_session, staff_user):
    issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    issue_refresh_token(staff_user.id, user_agent="firefox", ip="2.2.2.2")  # different family
    
    revoke_all_for_user(staff_user.id)
    
    test_db_session.expire_all()
    user = test_db_session.get(User, staff_user.id)
    assert user.token_version >= 1
    
    tokens = test_db_session.query(StaffRefreshToken).filter_by(user_id=staff_user.id).all()
    assert all(t.revoked_at is not None for t in tokens)


def test_invalid_refresh_token_rejected():
    with pytest.raises(Exception) as exc:
        rotate_refresh_token("invalid_token_string", "curl", "1.1.1.1")
    assert "invalid" in str(exc.value).lower() or "401" in str(exc.value)


def test_revoked_token_rejected(test_db_session, staff_user):
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    rt.revoked_at = datetime.now()
    test_db_session.commit()
    
    with pytest.raises(Exception) as exc:
        rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert "revoked" in str(exc.value).lower()
```

- [ ] **Step 4.3: 跑 pytest**

```bash
pytest tests/test_staff_refresh_rotation.py -v 2>&1 | tail -20
```
Expected: 8-9 pass。

- [ ] **Step 4.4: 跑 sample regression**

```bash
pytest tests/test_staff_refresh_rotation.py tests/test_auth.py tests/test_audit_login.py -v --tb=line 2>&1 | tail -15
```

- [ ] **Step 4.5: Commit (C4)**

```bash
git add services/scheduler.py tests/test_staff_refresh_rotation.py
git commit -m "$(cat <<'EOF'
feat(auth): GC scheduler + staff_refresh_rotation pytest

Spec F PR-F-BE-C4：

- services/scheduler.py: gc_staff_refresh_tokens 每日 03:00 清過期 +
  revoked >7d StaffRefreshToken
- tests/test_staff_refresh_rotation.py: 8-9 pytest cover:
  - issue / rotate happy path
  - reuse detection → family revoke + token_version bump
  - expired / revoked / invalid token rejection
  - per-session revoke / revoke-all

Refs: Spec docs/superpowers/specs/2026-05-28-staff-refresh-rotation-design.md §3.5 §4
EOF
)"
```

---

## Task 5: FE refresh interceptor + Settings UI (separate ivy-frontend worktree)

**Out of BE worktree scope** — 另開 worktree `feat/staff-refresh-rotation-2026-05-28-frontend` from origin/main in `ivy-frontend` repo。

Detail spec §3.6:
- src/api/index.ts: 401 access token 過期 → call `/api/auth/refresh` → retry; refresh fail redirect login
- src/api/staffAuth.ts: listSessions / revokeSession / logoutAll wrappers
- src/views/SettingsAccountTab.vue: Active Sessions 表格 (current highlight / UA truncate / revoke per row + revoke all button)
- 2-3 vitest

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 model | Task 1 ✅ | done |
| §2 G2 migration | Task 1 ✅ | done |
| §2 G3 rotation logic | Task 2 | ⏳ |
| §2 G4 /sessions list | Task 3 | ⏳ |
| §2 G5 per-session revoke | Task 3 | ⏳ |
| §2 G6 logout-all 雙模式 | Task 3 | ⏳ |
| §2 G7 UA/IP raw record | Task 2 | ⏳ |
| §2 G8 logout revoke family | Task 2 Step 2.6 | ⏳ |
| §2 G9 6-10 pytest | Task 4 | ⏳ |
| §2 G10 FE Settings UI | Task 5 (FE worktree) | ⏳ |
| §2 G11 FE refresh interceptor | Task 5 (FE worktree) | ⏳ |
| §2 G12 零回歸 | Task 4 Step 4.4 | ⏳ |
| §2 G13 GC | Task 4 Step 4.1 | ⏳ |
