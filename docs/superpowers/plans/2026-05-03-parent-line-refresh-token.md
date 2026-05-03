# 家長端 LINE 登入長效 Refresh Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 家長端 30 天免重登；以雙 token + rotation + reuse detection 取代現有的 15 分鐘 access + 2 小時 grace 流程。

**Architecture:** 新表 `parent_refresh_tokens`（家族 rotation 鏈）+ 新端點 `POST /api/parent/auth/refresh`；liff-login / bind 成功時多發 refresh token；logout 撤銷當前 family。員工端（`/api/auth`）完全不動。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（PostgreSQL prod / SQLite test）+ Vue 3 + axios。

**Spec:** `docs/superpowers/specs/2026-05-03-parent-line-refresh-token-design.md`

**Branches:**
- 後端：`feat/parent-refresh-token-v1`（已建立、spec 已 commit）
- 前端：`feat/parent-refresh-token-v1`（執行 Phase 2 時建立）

---

## File Structure

### 後端（`~/Desktop/ivy-backend`）

| 檔案 | 動作 | 責任 |
|---|---|---|
| `models/parent_refresh_token.py` | NEW | `ParentRefreshToken` ORM 模型 |
| `models/database.py` | MODIFY | import `ParentRefreshToken` 讓 `models.database.*` 可取用 |
| `alembic/versions/20260503_b7c8d9e0f1g2_create_parent_refresh_tokens.py` | NEW | 建表 migration |
| `api/parent_portal/auth.py` | MODIFY | liff-login / bind / logout 邏輯擴充；新增 `/refresh` 端點 |
| `tests/test_parent_refresh_token.py` | NEW | refresh 端點測試（rotation / reuse / race / family） |
| `tests/test_parent_auth_liff.py` | MODIFY | 既有 liff-login / bind / logout 測試補 refresh cookie 斷言 |

### 前端（`~/Desktop/ivy-frontend`）

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/parent/api/index.js` | MODIFY | refresh 端點 URL 改家長端 + 409 RACE 重試 |
| `tests/unit/parent/api.refresh.test.js` | NEW | vitest 驗 interceptor 行為 |

---

## Phase 0：環境驗證（必做）

### Task 0: 確認測試環境可用

**Files:** 無

- [ ] **Step 1: 確認後端在 `feat/parent-refresh-token-v1` 分支**

```bash
cd ~/Desktop/ivy-backend && git branch --show-current
```
Expected: `feat/parent-refresh-token-v1`

- [ ] **Step 2: 跑現有家長 auth 測試確認 baseline 綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py -q
```
Expected: 全部 pass，無 ERROR/FAIL

- [ ] **Step 3: 確認 alembic 多 head 狀態（用來決定 down_revision）**

```bash
cd ~/Desktop/ivy-backend && alembic heads
```
記下輸出的 head revision（之後 migration 的 `down_revision` 用這個）。
若有多 head（merge 用）也要記下用哪一個當 base。

---

## Phase 1：後端

### Task 1: ParentRefreshToken ORM 模型

**Files:**
- Create: `models/parent_refresh_token.py`
- Modify: `models/database.py`

- [ ] **Step 1: 建立模型檔**

```python
# models/parent_refresh_token.py
"""models/parent_refresh_token.py — 家長端長效 refresh token

設計重點：
- token raw 永不入庫；只存 sha256(raw) hex（64 字）
- family_id 串起同一裝置的 rotation 鏈；reuse 偵測時整 family revoke
- used_at != NULL 後再被送來 → reuse；單一 race 窗（5 秒）容忍同 token 雙請求
- expires_at 預設 now + 30 天；GC 7 天後刪
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from models.base import Base


class ParentRefreshToken(Base):
    __tablename__ = "parent_refresh_tokens"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SQLite 不支援 PG UUID；用 String(36) 跨方言相容
    family_id = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    token_hash = Column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(raw refresh token) hex；DB 不存明文",
    )
    parent_token_id = Column(
        BigInteger,
        ForeignKey("parent_refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
        comment="rotation 上一個 token；可追溯 family",
    )
    used_at = Column(DateTime, nullable=True, comment="rotation 後填入；reuse 偵測欄位")
    revoked_at = Column(DateTime, nullable=True, comment="family 全撤銷時填入")
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    user_agent = Column(String(255), nullable=True, comment="觀測用，不參與決策")
    ip = Column(String(45), nullable=True, comment="IPv6 預留；觀測用")

    __table_args__ = (
        Index("ix_parent_refresh_user_family", "user_id", "family_id"),
        Index("ix_parent_refresh_expires_at", "expires_at"),
    )
```

- [ ] **Step 2: 在 `models/database.py` 匯出**

打開 `models/database.py`，找到 `from models.parent_binding import GuardianBindingCode` 那行（約 line 33），下面加一行：

```python
from models.parent_refresh_token import ParentRefreshToken  # noqa: F401
```

- [ ] **Step 3: 確認模型可被 import**

```bash
cd ~/Desktop/ivy-backend && python -c "from models.database import ParentRefreshToken; print(ParentRefreshToken.__tablename__)"
```
Expected: `parent_refresh_tokens`

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/parent_refresh_token.py models/database.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): 新增 ParentRefreshToken 模型

- family_id 串起 rotation 鏈、token_hash 唯一、parent_token_id 追溯上一筆
- 索引 (user_id, family_id) 供 logout / family revoke、(expires_at) 供 GC
- String(36) family_id 跨 PG/SQLite 相容

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Alembic Migration

**Files:**
- Create: `alembic/versions/20260503_b7c8d9e0f1g2_create_parent_refresh_tokens.py`

> 取一個未用過的 revision id（12 hex 字元），下方範本用 `b7c8d9e0f1g2`。`down_revision` 填 Phase 0 Task 0 Step 3 記下的 head。若不確定 head 是 `r3s4t5u6v7w8`（contact_book_templates）。

- [ ] **Step 1: 建 migration 檔**

```python
# alembic/versions/20260503_b7c8d9e0f1g2_create_parent_refresh_tokens.py
"""create parent_refresh_tokens table

家長端 30 天免重登：rotation + reuse detection。
詳見 spec：docs/superpowers/specs/2026-05-03-parent-line-refresh-token-design.md

Revision ID: b7c8d9e0f1g2
Revises: r3s4t5u6v7w8
Create Date: 2026-05-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b7c8d9e0f1g2"
down_revision = "r3s4t5u6v7w8"  # ← 若 alembic heads 顯示不同，改這行
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "parent_refresh_tokens" in tables:
        return
    if "users" not in tables:
        return

    op.create_table(
        "parent_refresh_tokens",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "parent_token_id",
            sa.BigInteger,
            sa.ForeignKey("parent_refresh_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("used_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
    )
    op.create_index(
        "ix_parent_refresh_user_family",
        "parent_refresh_tokens",
        ["user_id", "family_id"],
    )
    op.create_index(
        "ix_parent_refresh_expires_at",
        "parent_refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "parent_refresh_tokens" not in inspect(bind).get_table_names():
        return
    existing = _index_names(bind, "parent_refresh_tokens")
    if "ix_parent_refresh_expires_at" in existing:
        op.drop_index("ix_parent_refresh_expires_at", table_name="parent_refresh_tokens")
    if "ix_parent_refresh_user_family" in existing:
        op.drop_index("ix_parent_refresh_user_family", table_name="parent_refresh_tokens")
    op.drop_table("parent_refresh_tokens")
```

- [ ] **Step 2: 跑 migration（dry-run sql）確認語法 ok**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade head --sql 2>&1 | tail -50
```
Expected: 看到 `CREATE TABLE parent_refresh_tokens` 和兩個 `CREATE INDEX`，無 error。

- [ ] **Step 3: 實際 upgrade（dev 資料庫）**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
```
Expected: `Running upgrade r3s4t5u6v7w8 -> b7c8d9e0f1g2`

- [ ] **Step 4: 驗證表存在**

```bash
cd ~/Desktop/ivy-backend && python -c "
from models.base import _engine, init_engine
init_engine()
from sqlalchemy import inspect
print(inspect(_engine).get_columns('parent_refresh_tokens'))
" 2>&1 | head -3
```
Expected: 看到 `id`、`user_id`、`family_id`、`token_hash` 等欄位。

> 若 init_engine 路徑與專案不同，改成既有測試 fixture 所用的方式（如直接 `sqlite:///` 跑 `Base.metadata.create_all`）。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add alembic/versions/20260503_b7c8d9e0f1g2_create_parent_refresh_tokens.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): alembic migration 建 parent_refresh_tokens 表

包含 family/token_hash/parent_token 三個外鍵與 (user_id,family_id) /
(expires_at) 兩個索引。沿用既有 inspect-then-create 防重跑慣例。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 共用 helpers（雜湊、cookie、settings）

**Files:**
- Modify: `api/parent_portal/auth.py`

> 這個 task 不寫商業邏輯，只把後續 task 會用到的工具函式建好；先 commit 一筆讓後續每個 endpoint task 都能拉乾淨。

- [ ] **Step 1: 在 `api/parent_portal/auth.py` 既有 import 區下方加常數與 helpers**

打開 `api/parent_portal/auth.py`，找到既有 `_BIND_TOKEN_TTL_MINUTES = 5`（約 line 88），下方接著加：

```python
import secrets

from models.database import ParentRefreshToken

# ── refresh token ────────────────────────────────────────────────────────
_REFRESH_COOKIE = "parent_refresh_token"
_REFRESH_COOKIE_PATH = "/api/parent/auth"
_REFRESH_TTL_DAYS = 30
_REFRESH_RACE_TOLERANCE_SECONDS = 5  # 同 token 雙請求 race 容忍窗


def _hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gen_refresh_raw() -> str:
    # 384-bit 隨機，base64url 約 64 字
    return secrets.token_urlsafe(48)


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_REFRESH_COOKIE_PATH,
        max_age=_REFRESH_TTL_DAYS * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_REFRESH_COOKIE_PATH,
    )


def _issue_refresh_token(
    session,
    response: Response,
    *,
    user_id: int,
    family_id: str | None = None,
    parent_token_id: int | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> ParentRefreshToken:
    """寫一筆 ParentRefreshToken + 在 response 上 Set-Cookie。

    family_id=None → 視為新裝置，產生新 family_id。
    回傳 ORM 物件（caller 可進一步 commit / refresh）。
    """
    import uuid

    raw = _gen_refresh_raw()
    row = ParentRefreshToken(
        user_id=user_id,
        family_id=family_id or str(uuid.uuid4()),
        token_hash=_hash_refresh(raw),
        parent_token_id=parent_token_id,
        expires_at=_now() + timedelta(days=_REFRESH_TTL_DAYS),
        user_agent=(user_agent or "")[:255] or None,
        ip=(ip or "")[:45] or None,
    )
    session.add(row)
    session.flush()
    _set_refresh_cookie(response, raw)
    return row
```

- [ ] **Step 2: 確認 import 正確 + 既有測試還綠**

```bash
cd ~/Desktop/ivy-backend && python -c "from api.parent_portal.auth import _issue_refresh_token, _hash_refresh, _gen_refresh_raw; print('ok')"
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py -q
```
Expected: 第一條印 `ok`；第二條全綠。

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): refresh token 共用 helpers（hash/cookie/issue）

只增工具函式，不改 endpoint 行為；後續 task 拆 endpoint 用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: liff-login 成功時發 refresh token（TDD）

**Files:**
- Test: `tests/test_parent_auth_liff.py:255-269`（修改既有 `test_already_bound_line_user_returns_ok_with_access_token`）
- Modify: `api/parent_portal/auth.py`（liff_login 函式內）

- [ ] **Step 1: 加失敗測試**

打開 `tests/test_parent_auth_liff.py`，找到 `test_already_bound_line_user_returns_ok_with_access_token`（約 line 255）。在 `assert "access_token" in resp.cookies` 下面加：

```python
        # 同時應發 refresh token cookie（30 天免重登）
        assert "parent_refresh_token" in resp.cookies
        # DB 應寫一筆未用未過期的 refresh token row
        from models.database import ParentRefreshToken
        with session_factory() as session:
            row = session.query(ParentRefreshToken).first()
            assert row is not None
            assert row.user_id is not None
            assert row.used_at is None
            assert row.revoked_at is None
            assert row.family_id  # uuid 字串
            assert row.parent_token_id is None  # 新 family 起點
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py::TestLiffLogin::test_already_bound_line_user_returns_ok_with_access_token -v
```
Expected: FAIL — `parent_refresh_token` 不在 cookies 裡。

- [ ] **Step 3: 實作：liff-login 成功路徑加發 refresh**

打開 `api/parent_portal/auth.py`，找到 `liff_login` 函式內 `if user and user.role == "parent":` 區塊（約 line 291）。把該區塊改為：

```python
        if user and user.role == "parent":
            _issue_access_token(response, user)
            _issue_refresh_token(
                session,
                response,
                user_id=user.id,
                user_agent=request.headers.get("user-agent"),
                ip=request.client.host if request.client else None,
            )
            user.last_login = _now()
            session.commit()
            return {
                "status": "ok",
                "user": {
                    "user_id": user.id,
                    "name": user.username,
                    "role": "parent",
                },
            }
```

- [ ] **Step 4: 跑測試確認通過 + 整檔不退化**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py -q
```
Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py tests/test_parent_auth_liff.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): liff-login 成功時多發 30 天 refresh token cookie

DB 寫入 ParentRefreshToken row（family 起點 parent_token_id=NULL）。
記錄 user_agent / ip 供觀測，不參與決策。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: bind 成功時發 refresh token（TDD）

**Files:**
- Test: `tests/test_parent_auth_liff.py`（修改 `test_admin_creates_binding_code_and_parent_completes_bind`）
- Modify: `api/parent_portal/auth.py`（bind_first_child 函式）

- [ ] **Step 1: 加失敗測試**

找到 `test_admin_creates_binding_code_and_parent_completes_bind`（約 line 276），在 `assert "access_token" in bind_resp.cookies` 下面加：

```python
        assert "parent_refresh_token" in bind_resp.cookies
        from models.database import ParentRefreshToken
        with session_factory() as session:
            tokens = session.query(ParentRefreshToken).all()
            assert len(tokens) == 1
            assert tokens[0].used_at is None
            assert tokens[0].parent_token_id is None
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py::TestAdminBindingCodeAndParentBind::test_admin_creates_binding_code_and_parent_completes_bind -v
```
Expected: FAIL。

- [ ] **Step 3: 實作：bind_first_child 成功路徑加發 refresh**

在 `api/parent_portal/auth.py` `bind_first_child` 函式內，找到 `_issue_access_token(response, user)` 那行（約 line 377）。在它**下方**插入：

```python
        _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
```

⚠️ 這一段要加在 `_issue_access_token` 之後、`logger.warning(...)` 之前；確保在同一個 session 內、commit 前。但因為 commit 已經發生（line 372 的 `session.commit()`），需要把這段移到 `session.commit()` **之前**。

正確修法是把 commit 之前的 sequence 重新排版。修改後的關鍵區段如下（找到 `guardian.user_id = user.id` 那一段，改寫到 commit）：

```python
        guardian.user_id = user.id
        user.last_login = _now()
        _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        session.commit()
        session.refresh(user)

        _clear_bind_failures(line_user_id)
        _clear_bind_token_cookie(response)
        _issue_access_token(response, user)
```

- [ ] **Step 4: 跑全 parent_auth 測試確保不退化**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py -q
```
Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py tests/test_parent_auth_liff.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): bind 成功時發 refresh token cookie

新建/補綁家長都在同 transaction 內寫 ParentRefreshToken。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 新增 `/refresh` 端點（happy path，TDD）

**Files:**
- Create: `tests/test_parent_refresh_token.py`
- Modify: `api/parent_portal/auth.py`

- [ ] **Step 1: 建測試骨架 + happy path**

新建 `tests/test_parent_refresh_token.py`：

```python
"""家長 refresh token 端點測試。

涵蓋：
- happy path: rotation 後舊 token 失效、新 token 可 refresh
- reuse detection: 重用 used token 觸發 family revoke
- race window: 5 秒內 race 不誤判
- expired / revoked / missing 情境
- multi-device family 隔離
- logout 只踢當前 family
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import (
    parent_router as parent_portal_router,
    init_parent_line_service,
)
from api.parent_portal.auth import _bind_failures
from models.database import (
    Base,
    ParentRefreshToken,
    User,
)


class FakeLineLoginService:
    def __init__(self, sub_map):
        self.sub_map = sub_map

    def is_configured(self):
        return True

    def verify_id_token(self, id_token):
        if id_token in self.sub_map:
            return {"sub": self.sub_map[id_token], "aud": "test", "name": "x"}
        raise HTTPException(status_code=401, detail="invalid")


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "refresh.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = SessionLocal

    Base.metadata.create_all(engine)
    _bind_failures.clear()

    init_parent_line_service(
        FakeLineLoginService({"token-A": "U_A", "token-B": "U_B"})
    )

    app = FastAPI()
    app.include_router(parent_portal_router)

    with TestClient(app) as client:
        yield client, SessionLocal

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def _make_parent(session, line_user_id):
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        must_change_password=False,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, session_factory, id_token, line_user_id):
    """走 liff-login 拿到 (access_cookie, refresh_cookie)。"""
    with session_factory() as s:
        _make_parent(s, line_user_id)
        s.commit()
    resp = client.post("/api/parent/auth/liff-login", json={"id_token": id_token})
    assert resp.status_code == 200
    return resp.cookies["access_token"], resp.cookies["parent_refresh_token"]


# ── Happy path: rotation ────────────────────────────────────────────────


def test_refresh_rotates_token_and_old_token_fails(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")

    resp = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert resp.status_code == 200
    new_refresh = resp.cookies.get("parent_refresh_token")
    assert new_refresh is not None
    assert new_refresh != old_refresh

    # DB 有兩筆，舊筆 used_at 填上、parent_token_id 串上
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).order_by(ParentRefreshToken.id).all()
        assert len(rows) == 2
        assert rows[0].used_at is not None
        assert rows[1].used_at is None
        assert rows[1].parent_token_id == rows[0].id
        assert rows[1].family_id == rows[0].family_id

    # 用舊 refresh 再次 refresh → 在 race window 外應 401（reuse）
    # 為避開 5 秒寬容窗，sleep 6 秒
    time.sleep(6)
    resp2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert resp2.status_code == 401
```

- [ ] **Step 2: 跑測試確認 happy path 失敗（端點不存在）**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py::test_refresh_rotates_token_and_old_token_fails -v
```
Expected: FAIL — 404 或 405 之類。

- [ ] **Step 3: 實作 `/refresh` 端點（happy path 含 reuse）**

在 `api/parent_portal/auth.py` 結尾加：

```python
@router.post("/refresh")
def parent_refresh(request: Request, response: Response):
    """以家長 refresh token 換發新 access + 新 refresh（rotation）。

    狀態碼：
    - 200：rotation 成功
    - 401：cookie 缺、token 不存在、過期、family revoked、超出 race window 的 reuse
    - 409：5 秒內同 token 並發 race（前端應重打原請求）
    """
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="未提供 refresh token")
    token_hash = _hash_refresh(raw)

    session = get_session()
    try:
        # postgres FOR UPDATE；sqlite no-op 但測試覆蓋夠
        row = (
            session.query(ParentRefreshToken)
            .filter(ParentRefreshToken.token_hash == token_hash)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=401, detail="refresh token 不存在")
        if row.revoked_at is not None:
            raise HTTPException(status_code=401, detail="refresh token 已撤銷")
        if row.expires_at < _now():
            raise HTTPException(status_code=401, detail="refresh token 已過期")

        if row.used_at is not None:
            # race window 容忍：5 秒內視為合法雙請求
            elapsed = (_now() - row.used_at).total_seconds()
            if 0 <= elapsed <= _REFRESH_RACE_TOLERANCE_SECONDS:
                logger.debug(
                    "[parent-refresh] race-tolerated user_id=%s family_id=%s elapsed=%.2fs",
                    row.user_id, row.family_id, elapsed,
                )
                # 不撤、不發新 token；前端應重打原請求
                raise HTTPException(status_code=409, detail="rotation in progress, please retry")
            # 超過 race window 仍拿 used token 來 refresh → reuse → 撤整個 family
            session.query(ParentRefreshToken).filter(
                ParentRefreshToken.family_id == row.family_id,
                ParentRefreshToken.revoked_at.is_(None),
            ).update(
                {"revoked_at": _now()},
                synchronize_session=False,
            )
            user = session.query(User).filter(User.id == row.user_id).first()
            if user is not None:
                user.token_version = (user.token_version or 0) + 1
            session.commit()
            logger.warning(
                "[parent-refresh] REUSE detected user_id=%s family_id=%s",
                row.user_id, row.family_id,
            )
            raise HTTPException(status_code=401, detail="refresh token 重用，整批已撤銷")

        # 正常 rotation
        user = session.query(User).filter(
            User.id == row.user_id, User.is_active == True  # noqa: E712
        ).first()
        if user is None:
            raise HTTPException(status_code=401, detail="使用者已停用")

        row.used_at = _now()
        new_row = _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            family_id=row.family_id,
            parent_token_id=row.id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        user.last_login = _now()
        _issue_access_token(response, user)
        session.commit()

        return {
            "status": "ok",
            "user": {
                "user_id": user.id,
                "name": user.username,
                "role": "parent",
            },
        }
    finally:
        session.close()
```

- [ ] **Step 4: 跑 happy path 測試**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py::test_refresh_rotates_token_and_old_token_fails -v
```
Expected: PASS。

- [ ] **Step 5: 跑全 parent auth 套件確認不退化**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py tests/test_parent_refresh_token.py -q
```
Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py tests/test_parent_refresh_token.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): 新增 POST /api/parent/auth/refresh 端點

rotation：舊 token 標 used_at、新 token 同 family_id、parent_token_id 串接。
reuse > 5s：family 全 revoke、user.token_version+=1、回 401。
reuse ≤ 5s：視為並發 race、回 409 不撤、前端重打原請求。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: refresh 邊界測試

**Files:**
- Modify: `tests/test_parent_refresh_token.py`

> Task 6 的實作已支援 reuse / race / expired / revoked，本 task 只補測試。

- [ ] **Step 1: 加 reuse detection 測試（race window 外）**

在 `tests/test_parent_refresh_token.py` 結尾加：

```python
def test_refresh_reuse_outside_race_window_revokes_family(parent_client):
    """超過 5 秒後拿 used token → 整 family revoke、token_version bump。"""
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")

    # 第一次 rotation 成功，得到 new1
    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r1.status_code == 200
    new1 = r1.cookies["parent_refresh_token"]

    # 等 6 秒（避開 race window），用 old_refresh 再 refresh → reuse
    time.sleep(6)
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r2.status_code == 401

    # family 全 revoked、token_version 升
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).all()
        assert all(r.revoked_at is not None for r in rows)
        user = s.query(User).filter(User.line_user_id == "U_A").first()
        assert user.token_version == 1

    # 連 new1 也用不了
    r3 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": new1},
    )
    assert r3.status_code == 401


def test_refresh_race_within_window_returns_409(parent_client):
    """5 秒內 race：第二個請求收 409，不誤觸 reuse。"""
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")

    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r1.status_code == 200

    # 立刻（< 5s）再用 old_refresh
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r2.status_code == 409

    # family 不應被 revoke
    with session_factory() as s:
        revoked = (
            s.query(ParentRefreshToken)
            .filter(ParentRefreshToken.revoked_at.isnot(None))
            .count()
        )
        assert revoked == 0


def test_refresh_missing_cookie_returns_401(parent_client):
    client, _ = parent_client
    r = client.post("/api/parent/auth/refresh")
    assert r.status_code == 401


def test_refresh_unknown_token_returns_401(parent_client):
    client, _ = parent_client
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": "never-issued-by-server"},
    )
    assert r.status_code == 401


def test_refresh_expired_token_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")
    # 直接 SQL 把 expires_at 推到過去
    with session_factory() as s:
        row = s.query(ParentRefreshToken).first()
        row.expires_at = datetime.now() - timedelta(hours=1)
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_refresh_revoked_token_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")
    with session_factory() as s:
        row = s.query(ParentRefreshToken).first()
        row.revoked_at = datetime.now()
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_refresh_disabled_user_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-A", "U_A")
    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        u.is_active = False
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_multi_device_family_isolation(parent_client):
    """裝置 A reuse 觸發後，裝置 B 的 family 不受影響。"""
    client, session_factory = parent_client

    # 裝置 A 登入（會發 family A）
    _, refresh_A = _login(client, session_factory, "token-A", "U_A")

    # 同 user 模擬第二裝置：直接呼叫 issue_refresh_token 寫一筆新 family
    from api.parent_portal.auth import (
        _issue_refresh_token,
        _gen_refresh_raw,
        _hash_refresh,
    )
    import uuid
    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        raw_B = _gen_refresh_raw()
        row_B = ParentRefreshToken(
            user_id=u.id,
            family_id=str(uuid.uuid4()),
            token_hash=_hash_refresh(raw_B),
            expires_at=datetime.now() + timedelta(days=30),
        )
        s.add(row_B)
        s.commit()

    # 把 family A 的 refresh 用一次（rotation），然後等 6s 再 reuse
    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": refresh_A},
    )
    assert r1.status_code == 200
    time.sleep(6)
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": refresh_A},
    )
    assert r2.status_code == 401  # reuse 觸發

    # ⚠ family A 被 revoke + user.token_version+=1，但 family B 也仍在 DB
    # 不過實際上 token_version bump 後，access token 全廢；refresh token 本身
    # 不檢查 token_version。所以 family B 的 refresh 仍可用：
    r3 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": raw_B},
    )
    assert r3.status_code == 200
```

- [ ] **Step 2: 跑全部 refresh 測試**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py -v
```
Expected: 全綠（8 tests）。如果 multi-device test 失敗，看是否 token_version bump 真的不影響 refresh — 是的話應綠；若 caller 對 family B 用 refresh 卻 401，就需要 debug。

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/ivy-backend
git add tests/test_parent_refresh_token.py
git commit -m "$(cat <<'EOF'
test(parent-auth): refresh 端點邊界測試

涵蓋 reuse-outside-window / race-within-window / missing / unknown /
expired / revoked / disabled-user / multi-device-family 八種情境。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: logout 撤銷當前 family（TDD）

**Files:**
- Test: `tests/test_parent_refresh_token.py`
- Modify: `api/parent_portal/auth.py`（parent_logout 函式）

- [ ] **Step 1: 加 logout 行為測試**

在 `tests/test_parent_refresh_token.py` 加：

```python
def test_logout_revokes_current_family_only(parent_client):
    """logout 應 revoke 當前 family + 清 cookie；其他裝置 family 不受影響。"""
    client, session_factory = parent_client
    access_A, refresh_A = _login(client, session_factory, "token-A", "U_A")

    # 第二裝置（family B）
    from api.parent_portal.auth import _gen_refresh_raw, _hash_refresh
    import uuid
    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        raw_B = _gen_refresh_raw()
        row_B = ParentRefreshToken(
            user_id=u.id,
            family_id=str(uuid.uuid4()),
            token_hash=_hash_refresh(raw_B),
            expires_at=datetime.now() + timedelta(days=30),
        )
        s.add(row_B)
        s.commit()
        family_B = row_B.family_id

    # 登出（帶 access_A + refresh_A）
    r = client.post(
        "/api/parent/auth/logout",
        cookies={"access_token": access_A, "parent_refresh_token": refresh_A},
    )
    assert r.status_code == 204

    # family A 全 revoked、family B 不變
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).all()
        for row in rows:
            if row.family_id == family_B:
                assert row.revoked_at is None
            else:
                assert row.revoked_at is not None

    # cookie 已清（response 寫了清除指令；測試端 cookies jar 可能仍保留舊值
    # 因此用 family B 的 raw 跨裝置去 refresh 應仍 OK）
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": raw_B},
    )
    assert r2.status_code == 200
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py::test_logout_revokes_current_family_only -v
```
Expected: FAIL — family A 沒被 revoke。

- [ ] **Step 3: 實作：parent_logout 內 revoke family**

打開 `api/parent_portal/auth.py`，找到 `def parent_logout(...)` 函式（檔尾）。把整個函式改寫為：

```python
@router.post("/logout", status_code=204)
def parent_logout(
    request: Request,
    response: Response,
    current_user: dict = Depends(require_parent_role()),
):
    """登出：清 cookie + bump token_version + 撤銷當前 refresh family。"""
    user_id = current_user["user_id"]
    raw_refresh = request.cookies.get(_REFRESH_COOKIE)

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if user:
            user.token_version = (user.token_version or 0) + 1

        if raw_refresh:
            token_hash = _hash_refresh(raw_refresh)
            row = (
                session.query(ParentRefreshToken)
                .filter(ParentRefreshToken.token_hash == token_hash)
                .first()
            )
            if row is not None and row.revoked_at is None:
                session.query(ParentRefreshToken).filter(
                    ParentRefreshToken.family_id == row.family_id,
                    ParentRefreshToken.revoked_at.is_(None),
                ).update(
                    {"revoked_at": _now()},
                    synchronize_session=False,
                )

        session.commit()
    finally:
        session.close()

    clear_access_token_cookie(response)
    _clear_bind_token_cookie(response)
    _clear_refresh_cookie(response)
    return Response(status_code=204)
```

- [ ] **Step 4: 跑 logout 測試 + 全套 parent**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py tests/test_parent_refresh_token.py -q
```
Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py tests/test_parent_refresh_token.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): logout 撤銷當前裝置 family（多裝置並存策略）

只 revoke 當前 refresh token 對應的 family；其他裝置不受影響。
保留既有 token_version bump，使當前 access token 立即失效。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: GC 過期 token（保留 7 天稽核窗）

**Files:**
- Modify: `api/parent_portal/auth.py`（加 helper）
- Test: `tests/test_parent_refresh_token.py`

> 不接 cron，先做純函式 + 測試；之後接哪個排程器另議。

- [ ] **Step 1: 加 GC helper + 測試**

在 `tests/test_parent_refresh_token.py` 加：

```python
def test_gc_purges_tokens_expired_more_than_7_days(parent_client):
    from api.parent_portal.auth import gc_expired_refresh_tokens
    client, session_factory = parent_client
    _, _ = _login(client, session_factory, "token-A", "U_A")

    with session_factory() as s:
        rows = s.query(ParentRefreshToken).all()
        # 標一筆 8 天前過期、另一筆 1 天前過期
        rows[0].expires_at = datetime.now() - timedelta(days=8)
        s.commit()
        # 再造一筆剛過期的（保留窗內）
        from api.parent_portal.auth import _gen_refresh_raw, _hash_refresh
        import uuid
        s.add(ParentRefreshToken(
            user_id=rows[0].user_id,
            family_id=str(uuid.uuid4()),
            token_hash=_hash_refresh(_gen_refresh_raw()),
            expires_at=datetime.now() - timedelta(days=1),
        ))
        s.commit()

    # 跑 GC
    with session_factory() as s:
        n = gc_expired_refresh_tokens(s, retention_days=7)
        s.commit()
        assert n == 1  # 只清 8 天前那筆
        remaining = s.query(ParentRefreshToken).count()
        assert remaining == 1
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py::test_gc_purges_tokens_expired_more_than_7_days -v
```
Expected: FAIL — `gc_expired_refresh_tokens` 不存在。

- [ ] **Step 3: 實作 GC**

在 `api/parent_portal/auth.py` 加（建議放在 `_issue_refresh_token` 之後）：

```python
def gc_expired_refresh_tokens(session, *, retention_days: int = 7) -> int:
    """刪除 expires_at < now - retention_days 的 token row；回傳刪除筆數。

    保留 retention_days 天供事後稽核。caller 負責 commit。
    """
    cutoff = _now() - timedelta(days=retention_days)
    result = session.execute(
        ParentRefreshToken.__table__.delete().where(
            ParentRefreshToken.expires_at < cutoff
        )
    )
    return int(result.rowcount or 0)
```

需要在頂部 import `from sqlalchemy import update` 已存在；新增 delete 用 `__table__.delete()` 不需要新 import。

- [ ] **Step 4: 跑測試**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_refresh_token.py::test_gc_purges_tokens_expired_more_than_7_days -v
```
Expected: PASS。

- [ ] **Step 5: 全 parent suite 不退化**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_auth_liff.py tests/test_parent_refresh_token.py -q
```
Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/auth.py tests/test_parent_refresh_token.py
git commit -m "$(cat <<'EOF'
feat(parent-auth): gc_expired_refresh_tokens helper（7 天稽核保留窗）

純函式，未接排程器；caller 負責 commit。
之後接 cron 或 admin 端 endpoint 都可。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10：後端整合驗證

**Files:** 無

- [ ] **Step 1: 跑全部後端測試確認沒退化任何模組**

```bash
cd ~/Desktop/ivy-backend && pytest -q 2>&1 | tail -30
```
Expected: 全綠。若有別模組失敗，需確認是否本次改動造成（通常不會，但 `models/database.py` 多 import 一個模型有時會踩到 metadata 清除順序）。

- [ ] **Step 2: 推分支**

```bash
cd ~/Desktop/ivy-backend && git push -u origin feat/parent-refresh-token-v1
```

- [ ] **Step 3: 列出本支 commit 供 PR 描述用**

```bash
cd ~/Desktop/ivy-backend && git log main..HEAD --oneline
```
記下這些 commit hash 和訊息，前端做完一起開兩個 PR。

---

## Phase 2：前端

### Task 11：前端建分支 + interceptor refresh URL 改家長端

**Files:**
- Modify: `src/parent/api/index.js:54-60, 76-79`

- [ ] **Step 1: 建分支**

```bash
cd ~/Desktop/ivy-frontend && git checkout -b feat/parent-refresh-token-v1
```

- [ ] **Step 2: 改 `_doRefresh` 的 URL**

打開 `src/parent/api/index.js`，找到 `function _doRefresh()`（約 line 54）。把：

```js
function _doRefresh() {
  return axios
    .post('/api/auth/refresh', null, { withCredentials: true, timeout: 10000 })
    .then(() => true)
}
```

改為：

```js
function _doRefresh() {
  return axios
    .post('/api/parent/auth/refresh', null, { withCredentials: true, timeout: 10000 })
    .then(() => true)
}
```

- [ ] **Step 3: `isAuthEndpoint` 白名單也跟著換**

同一檔案約 line 76–79，把：

```js
    const isAuthEndpoint =
      url.includes('/parent/auth/liff-login') ||
      url.includes('/parent/auth/bind') ||
      url.includes('/auth/refresh')
```

改為：

```js
    const isAuthEndpoint =
      url.includes('/parent/auth/liff-login') ||
      url.includes('/parent/auth/bind') ||
      url.includes('/parent/auth/refresh')
```

> `/parent/auth/refresh` 是子字串，會同時涵蓋家長端 refresh 自身；員工端 `/auth/refresh` 不在家長端 axios 內被呼叫。

- [ ] **Step 4: build 確認語法 ok**

```bash
cd ~/Desktop/ivy-frontend && npx vite build --mode development 2>&1 | tail -15
```
Expected: build success，無 TypeScript / parse error。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/parent/api/index.js
git commit -m "$(cat <<'EOF'
feat(parent-app): refresh interceptor 改打家長端 /api/parent/auth/refresh

對應後端新增的家長 30 天 refresh token 端點；員工端 /api/auth/refresh
僅員工 axios 使用，家長 axios 不再共用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12：interceptor 處理 409 RACE_PLEASE_RETRY（TDD）

**Files:**
- Create: `tests/unit/parent/api.refresh.test.js`
- Modify: `src/parent/api/index.js`

- [ ] **Step 1: 加失敗測試**

新建 `tests/unit/parent/api.refresh.test.js`：

```js
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import MockAdapter from 'axios-mock-adapter'
import axios from 'axios'

// 注意：此 test 攔截 api 模組頂部 axios 實例 + 全域 axios（refresh 用全域）
import api from '@/parent/api/index'

describe('parent api refresh interceptor', () => {
  let mockApi
  let mockGlobal

  beforeEach(() => {
    mockApi = new MockAdapter(api)
    mockGlobal = new MockAdapter(axios)
  })

  afterEach(() => {
    mockApi.restore()
    mockGlobal.restore()
  })

  it('retries original request once after refresh succeeds', async () => {
    // 第一次 401 → refresh 200 → 原請求 200
    mockApi.onGet('/some-endpoint').replyOnce(401)
    mockGlobal.onPost('/api/parent/auth/refresh').replyOnce(200, { ok: true })
    mockApi.onGet('/some-endpoint').replyOnce(200, { hello: 'world' })

    const resp = await api.get('/some-endpoint')
    expect(resp.data).toEqual({ hello: 'world' })
  })

  it('retries original request once on 409 RACE response from refresh', async () => {
    // 401 → refresh 409 → 預期原請求被重打一次（cookie 已被 first refresh 寫入）
    mockApi.onGet('/some-endpoint').replyOnce(401)
    mockGlobal.onPost('/api/parent/auth/refresh').replyOnce(409, {
      detail: 'rotation in progress, please retry',
    })
    mockApi.onGet('/some-endpoint').replyOnce(200, { ok: true })

    const resp = await api.get('/some-endpoint')
    expect(resp.data).toEqual({ ok: true })
  })

  it('does not loop refresh when /parent/auth/refresh itself returns 401', async () => {
    // refresh 屬於 isAuthEndpoint 白名單；自己 401 應直接 reject、不再 refresh
    let globalRefreshCalls = 0
    mockGlobal.onPost('/api/parent/auth/refresh').reply(() => {
      globalRefreshCalls += 1
      return [200]
    })
    // 透過 api 實例打到 /parent/auth/refresh（baseURL /api 會合成 /api/parent/auth/refresh）
    mockApi.onPost('/parent/auth/refresh').replyOnce(401)

    await expect(
      api.post('/parent/auth/refresh'),
    ).rejects.toMatchObject({ response: { status: 401 } })

    // global axios 上的 _doRefresh 一次都不應被呼叫
    expect(globalRefreshCalls).toBe(0)
  })
})
```

> 若 `axios-mock-adapter` 未安裝，先 `npm i -D axios-mock-adapter` 並 commit lockfile。

- [ ] **Step 2: 跑測試確認失敗（409 retry 還沒實作）**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run tests/unit/parent/api.refresh.test.js 2>&1 | tail -25
```
Expected: 第二個 test FAIL（409 沒被當成重試）。

- [ ] **Step 3: 實作 409 retry**

設計：refresh 自己回 409（5 秒內 race）時，**不**視為失敗；直接重打原請求一次（兄弟 rotation 已寫好新 cookie）。實作方式是把 401 retry 區段的 catch 加判斷。

打開 `src/parent/api/index.js`，把現有的 401 retry 區塊（約 line 91–109）：

```js
    if (
      error.response?.status === 401 &&
      !isAuthEndpoint &&
      !originalRequest._retried
    ) {
      originalRequest._retried = true
      try {
        if (!_refreshing) {
          _refreshing = _doRefresh().finally(() => {
            _refreshing = null
          })
        }
        await _refreshing
        return api(originalRequest)
      } catch {
        _redirectToLogin()
        return Promise.reject(error)
      }
    }
```

整段替換為：

```js
    if (
      error.response?.status === 401 &&
      !isAuthEndpoint &&
      !originalRequest._retried
    ) {
      originalRequest._retried = true
      try {
        if (!_refreshing) {
          _refreshing = _doRefresh().finally(() => {
            _refreshing = null
          })
        }
        await _refreshing
        return api(originalRequest)
      } catch (refreshErr) {
        // refresh 自己回 409 RACE：兄弟請求已完成 rotation 並寫入新 cookie，
        // 此分支直接重打原請求即可恢復；不重導登入
        if (refreshErr?.response?.status === 409) {
          return api(originalRequest)
        }
        _redirectToLogin()
        return Promise.reject(error)
      }
    }
```

`_doRefresh()` 在 409 時 axios 預設會 reject，因此 catch 內 `refreshErr.response.status === 409` 是可達的判斷。

- [ ] **Step 4: 跑測試直到全綠**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run tests/unit/parent/api.refresh.test.js 2>&1 | tail -20
```
Expected: 全綠（3 tests）。

- [ ] **Step 5: 跑全部既有 parent vitest 確認不退化**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run tests/unit/parent/ 2>&1 | tail -10
```
Expected: 既有 `liff.test.js` 仍 fail（pre-existing tech debt，本次不修）；其餘全綠。

> ⚠️ `tests/unit/parent/liff.test.js` 在 main 分支即已 fail（引用了不存在的 `forceLiffReloginOnce` 等），與本次改動無關。不要去動它。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/parent/api/index.js tests/unit/parent/api.refresh.test.js
# 若安裝了 axios-mock-adapter
git add package.json package-lock.json
git commit -m "$(cat <<'EOF'
feat(parent-app): 處理 refresh 409 RACE 並補 interceptor vitest

- 新增：refresh 回 409（並發 race）時直接重打原請求
- 不更動既有 401 → refresh → retry 主路徑
- 補 vitest 驗證 refresh 成功 / 409 RACE / 自身 401 不遞迴

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13：整合驗證

**Files:** 無

- [ ] **Step 1: 啟動前後端，手動跑一次 LIFF 登入**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

開瀏覽器到家長 LIFF App（外部瀏覽器走 `withLoginOnExternalBrowser`），登入後：

- 開 DevTools → Application → Cookies → 確認 `parent_refresh_token` 存在、Path=/api/parent/auth、HttpOnly=true
- 確認 `access_token` 也仍存在（Path=/api）

- [ ] **Step 2: 手動觸發 refresh**

DevTools console 執行：

```js
fetch('/api/parent/auth/refresh', { method: 'POST', credentials: 'include' })
  .then(r => r.json())
  .then(console.log)
```

Expected: `{ status: 'ok', user: {...} }`，且 cookies 內 `parent_refresh_token` 的值已換新（rotation）。

- [ ] **Step 3: 模擬 access token 過期觸發自動 refresh**

DevTools 內手動把 `access_token` cookie 刪掉（refresh 留著），重整頁面或觸發任意 API call。Network 應看到：

- 原 API 401 → axios interceptor 自動打 `/api/parent/auth/refresh` 200 → 重打原 API 200

無使用者操作即恢復。

- [ ] **Step 4: 推前端分支**

```bash
cd ~/Desktop/ivy-frontend && git push -u origin feat/parent-refresh-token-v1
```

- [ ] **Step 5: 開兩個 PR（後端 + 前端）**

```bash
cd ~/Desktop/ivy-backend && gh pr create --title "feat(parent-auth): 家長端 30 天 refresh token + rotation/reuse detection" --body "$(cat <<'EOF'
## Summary
- 新增 ParentRefreshToken 模型 + alembic migration
- 新端點 POST /api/parent/auth/refresh：rotation + reuse detection + 5s race tolerance
- liff-login / bind 成功時多發 refresh token；logout 撤銷當前 family
- 員工端 /api/auth/refresh 完全不動

Spec: docs/superpowers/specs/2026-05-03-parent-line-refresh-token-design.md

## Test plan
- [x] pytest tests/test_parent_auth_liff.py tests/test_parent_refresh_token.py
- [x] alembic upgrade head 後表結構正確
- [x] 全套後端 pytest 不退化

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"

cd ~/Desktop/ivy-frontend && gh pr create --title "feat(parent-app): refresh interceptor 改家長端端點 + 409 RACE 重試" --body "$(cat <<'EOF'
## Summary
- axios interceptor 401 → 改打 /api/parent/auth/refresh
- 處理 refresh 409 RACE：直接重打原請求（兄弟 rotation 已完成）
- 新增 vitest 覆蓋三種情境

對應後端 PR：ivy-backend feat/parent-refresh-token-v1

## Test plan
- [x] vitest tests/unit/parent/api.refresh.test.js
- [x] 手動 LIFF 登入 + 觀察 parent_refresh_token cookie

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review Checklist

執行者實作完畢後，自我檢查：

- [ ] 全部 13 task 都打勾
- [ ] 後端 `pytest` 全綠（除既有無關 fail）
- [ ] 前端 `npx vitest run tests/unit/parent/` 除 `liff.test.js` 之外全綠
- [ ] 兩個 PR 都已開、CI 都綠
- [ ] 手動測：LIFF 登入後 DevTools 看到兩個 cookie；模擬 access token 過期能自動 refresh

如有任何 task 失敗、跳過、或留 TODO，**不可** 標 plan 完成；應回報具體 task 編號 + 失敗原因。
