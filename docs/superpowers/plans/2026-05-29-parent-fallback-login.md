# 家長端 fallback 登入（無 LINE 裝置登入碼）後端實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為無法/不用 LINE 的家長補一條登入路徑：staff 簽發一次性設定碼 → 家長在一般瀏覽器頁輸入一次 → 兌換成裝置 refresh token（30 天 rolling），之後自動登入。

**Architecture:** 重用既有家長端 `ParentRefreshToken`（family rotation）+ `_issue_refresh_token` / `_issue_access_token` / `resolve_parent_display_name`。新表 `parent_device_setup_codes` 與 `guardian_binding_codes` 語意分離（後者走 LINE LIFF `/bind`），避免污染既有 `/bind` claim 邏輯。新增無 LINE 的 parent User 建立 helper（`line_user_id=None`、username=`parent_device_<guardian_id>`）。device-setup 端點 ungated → 高熵 12 碼 + IP 限流嘗試鎖 + 通用錯誤防枚舉。

**Tech Stack:** FastAPI + SQLAlchemy + PostgreSQL（測試 SQLite in-memory）+ alembic + pytest。spec：`docs/superpowers/specs/2026-05-29-parent-fallback-login-design.md`。

**只做後端**；前端（後台發碼/撤銷按鈕 + 無 LINE 登入頁）為後端 merge + OpenAPI codegen 後的另一份 plan。

---

### Task 1: 資料模型 `ParentDeviceSetupCode` + 中央註冊 + migration

**Files:**
- Modify: `models/parent_binding.py`（在 `GuardianBindingCode` 後新增 class）
- Modify: `models/database.py:34`（import）、`models/database.py:165` 附近（`__all__`）
- Create: `alembic/versions/pdevsetup01_parent_device_setup_codes.py`
- Test: `tests/test_parent_device_setup.py`

- [ ] **Step 1: 寫 failing test（model 建表 + 欄位）**

於 `tests/test_parent_device_setup.py` 新建：

```python
"""家長端無 LINE 裝置登入（設定碼）測試。"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base, Guardian, ParentDeviceSetupCode, Student, User
from utils.taipei_time import now_taipei_naive


def test_model_creates_with_expected_columns():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    stu = Student(student_id="S1", name="小明", is_active=True)
    s.add(stu)
    s.flush()
    g = Guardian(student_id=stu.id, name="王大明", is_primary=True)
    s.add(g)
    s.flush()
    u = User(username="staff1", password_hash="x", role="teacher", token_version=0)
    s.add(u)
    s.flush()
    code = ParentDeviceSetupCode(
        guardian_id=g.id,
        code_hash="a" * 64,
        expires_at=now_taipei_naive() + timedelta(hours=24),
        created_by=u.id,
    )
    s.add(code)
    s.commit()
    row = s.query(ParentDeviceSetupCode).one()
    assert row.guardian_id == g.id
    assert row.used_at is None
    assert row.used_by_user_id is None
    s.close()
    engine.dispose()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python -m pytest tests/test_parent_device_setup.py::test_model_creates_with_expected_columns -q`
Expected: FAIL — `ImportError: cannot import name 'ParentDeviceSetupCode'`

- [ ] **Step 3: 新增 model（`models/parent_binding.py` 檔尾）**

```python
class ParentDeviceSetupCode(Base):
    """無 LINE 家長裝置登入：一次性設定碼。

    行政對特定 Guardian 簽發；無 LINE 家長在一般瀏覽器頁輸入明碼，兌換成裝置
    refresh token（passwordless device-trust）。與 GuardianBindingCode 語意分離
    （後者走 LINE LIFF /bind），不共用資料表以免污染既有 claim 邏輯。
    """

    __tablename__ = "parent_device_setup_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guardian_id = Column(
        Integer,
        ForeignKey("guardians.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = Column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(明碼) hex；明碼僅回傳簽發者一次",
    )
    expires_at = Column(DateTime, nullable=False, comment="預設 24h 過期")
    used_at = Column(
        DateTime, nullable=True, comment="兌換成功時間；non-null 即視為已用"
    )
    used_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="兌換該碼的家長 User",
    )
    created_by = Column(
        Integer, ForeignKey("users.id"), nullable=False, comment="簽發此碼的行政 User"
    )
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)

    __table_args__ = (
        Index(
            "ix_parent_device_setup_expires_unused",
            "expires_at",
            "used_at",
        ),
    )
```

- [ ] **Step 4: 中央註冊（`models/database.py`）**

第 34 行 import 改為：

```python
from models.parent_binding import GuardianBindingCode, ParentDeviceSetupCode
```

`__all__`（約 165 行 `"GuardianBindingCode",` 之後）加一行：

```python
    "ParentDeviceSetupCode",
```

- [ ] **Step 5: 跑測試確認 pass**

Run: `python -m pytest tests/test_parent_device_setup.py::test_model_creates_with_expected_columns -q`
Expected: PASS

- [ ] **Step 6: 寫 alembic migration**

Create `alembic/versions/pdevsetup01_parent_device_setup_codes.py`：

```python
"""parent_device_setup_codes：無 LINE 家長裝置登入設定碼

Revision ID: pdevsetup01
Revises: eb0d4cf88f26
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision = "pdevsetup01"
down_revision = "eb0d4cf88f26"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "parent_device_setup_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "guardian_id",
            sa.Integer(),
            sa.ForeignKey("guardians.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "used_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_parent_device_setup_codes_guardian_id",
        "parent_device_setup_codes",
        ["guardian_id"],
    )
    op.create_index(
        "ix_parent_device_setup_expires_unused",
        "parent_device_setup_codes",
        ["expires_at", "used_at"],
    )


def downgrade():
    op.drop_index(
        "ix_parent_device_setup_expires_unused",
        table_name="parent_device_setup_codes",
    )
    op.drop_index(
        "ix_parent_device_setup_codes_guardian_id",
        table_name="parent_device_setup_codes",
    )
    op.drop_table("parent_device_setup_codes")
```

> 註：CI 測試走 `Base.metadata.create_all` + `alembic stamp heads`（見專案慣例），**不跑** `alembic upgrade`，故 migration 僅供 prod；務必與 model 欄位完全一致。確認 single head：`down_revision = "eb0d4cf88f26"`（撰寫時的 head）。

- [ ] **Step 7: ruff/black + commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-fallback-login-2026-05-29
ruff check models/parent_binding.py models/database.py alembic/versions/pdevsetup01_parent_device_setup_codes.py tests/test_parent_device_setup.py
black models/parent_binding.py models/database.py alembic/versions/pdevsetup01_parent_device_setup_codes.py tests/test_parent_device_setup.py
git add models/parent_binding.py models/database.py alembic/versions/pdevsetup01_parent_device_setup_codes.py tests/test_parent_device_setup.py
git commit -m "feat(parent-auth): ParentDeviceSetupCode model + migration (pdevsetup01)"
```

---

### Task 2: 回應 schema `DeviceSetupOut`

**Files:**
- Modify: `schemas/parent_portal_auth.py`（沿用既有 `ParentUserInfo`）

- [ ] **Step 1: 新增 schema（`schemas/parent_portal_auth.py`，於 `BindFirstChildOut` 之後）**

```python
class DeviceSetupOut(IvyBaseModel):
    """POST /auth/device-setup 兌換設定碼成功回傳（無 LINE 家長裝置登入）。"""

    status: Literal["ok"]
    user: ParentUserInfo
```

- [ ] **Step 2: commit**

```bash
black schemas/parent_portal_auth.py && ruff check schemas/parent_portal_auth.py
git add schemas/parent_portal_auth.py
git commit -m "feat(parent-auth): DeviceSetupOut 回應 schema"
```

---

### Task 3: 後端 helper（claim / 無 LINE User / IP 鎖）

**Files:**
- Modify: `api/parent_portal/auth.py`（import 加 `ParentDeviceSetupCode`；新增 4 個 helper）
- Test: `tests/test_parent_device_setup.py`

- [ ] **Step 1: 寫 failing test（claim atomic 單次 + 無 LINE User 建立）**

於 `tests/test_parent_device_setup.py` 加：

```python
def _mk_engine_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _seed_guardian(session, *, with_user=False):
    stu = Student(student_id="S1", name="小明", is_active=True)
    session.add(stu)
    session.flush()
    g = Guardian(student_id=stu.id, name="王大明", is_primary=True)
    session.add(g)
    session.flush()
    return g


class TestDeviceSetupHelpers:
    def test_claim_atomic_single_use(self):
        from api.parent_portal import auth as pauth
        engine, s = _mk_engine_session()
        g = _seed_guardian(s)
        staff = User(username="staff", password_hash="x", role="teacher", token_version=0)
        s.add(staff)
        s.flush()
        s.add(ParentDeviceSetupCode(
            guardian_id=g.id, code_hash=pauth._hash_code("CODE123"),
            expires_at=now_taipei_naive() + timedelta(hours=24), created_by=staff.id,
        ))
        s.commit()
        first = pauth._claim_device_setup_code_atomic(s, pauth._hash_code("CODE123"))
        assert first is not None and first.used_at is not None
        second = pauth._claim_device_setup_code_atomic(s, pauth._hash_code("CODE123"))
        assert second is None  # 已用 → 不可再 claim
        s.close()
        engine.dispose()

    def test_create_parent_user_for_device_no_line(self):
        from api.parent_portal import auth as pauth
        engine, s = _mk_engine_session()
        g = _seed_guardian(s)
        s.commit()
        u = pauth._create_parent_user_for_device(s, g)
        s.commit()
        assert u.role == "parent"
        assert u.line_user_id is None
        assert u.username == f"parent_device_{g.id}"
        assert u.display_name == "王大明"  # 取 Guardian.name
        s.close()
        engine.dispose()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python -m pytest tests/test_parent_device_setup.py::TestDeviceSetupHelpers -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_claim_device_setup_code_atomic'`

- [ ] **Step 3: 實作 helper（`api/parent_portal/auth.py`）**

import 區塊把 `ParentDeviceSetupCode` 加入 `from models.database import (...)`：

```python
from models.database import (
    Guardian,
    GuardianBindingCode,
    ParentDeviceSetupCode,
    ParentRefreshToken,
    User,
    get_session,
)
```

於 `_diagnose_binding_failure` / `_BIND_FAILURE_MESSAGES` 之後新增：

```python
# ── 無 LINE 裝置登入（device-setup）─────────────────────────────────────────
_DEVICE_SETUP_SCOPE = "parent_device_setup"


def _claim_device_setup_code_atomic(session, code_hash: str):
    """atomic 單次 claim parent_device_setup_codes（used_at IS NULL 且未過期）。

    rowcount==1 才成功；回更新後 ORM 物件，失敗回 None（已用 / 過期 / 不存在）。
    """
    now = _now()
    stmt = (
        update(ParentDeviceSetupCode)
        .where(
            ParentDeviceSetupCode.code_hash == code_hash,
            ParentDeviceSetupCode.used_at.is_(None),
            ParentDeviceSetupCode.expires_at > now,
        )
        .values(used_at=now)
    )
    if session.execute(stmt).rowcount != 1:
        return None
    return (
        session.query(ParentDeviceSetupCode)
        .filter(ParentDeviceSetupCode.code_hash == code_hash)
        .first()
    )


def _username_for_device(guardian_id: int) -> str:
    """無 LINE 家長 User 的 username：parent_device_<guardian_id>（唯一）。"""
    return f"parent_device_{guardian_id}"


def _create_parent_user_for_device(session, guardian) -> User:
    """建立無 LINE 的 role='parent' User。

    line_user_id=None（欄位 nullable+unique，多筆 NULL 允許）；display_name 取
    Guardian.name（無 LINE 暱稱可用）；password_hash sentinel 同 LINE 家長。
    """
    user = User(
        employee_id=None,
        username=_username_for_device(guardian.id),
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        must_change_password=False,
        line_user_id=None,
        display_name=_clean_display_name(guardian.name),
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _check_device_setup_lockout(ip: str) -> None:
    """device-setup 為 ungated 入口 → 以 IP 為 key 做失敗鎖（連 5 次鎖 15 分）。"""
    from utils.rate_limit_db import count_recent_attempts

    count = count_recent_attempts(
        _DEVICE_SETUP_SCOPE, ip, within_seconds=_BIND_FAIL_LOCKOUT
    )
    if count >= _BIND_FAIL_THRESHOLD:
        logger.warning("device-setup 失敗過多，ip=%s 已鎖 (failures=%d)", ip, count)
        raise HTTPException(status_code=429, detail="嘗試次數過多，請稍後再試")


def _record_device_setup_failure(ip: str) -> None:
    from utils.rate_limit_db import record_attempt

    record_attempt(_DEVICE_SETUP_SCOPE, ip, window_seconds=_BIND_FAIL_LOCKOUT)
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python -m pytest tests/test_parent_device_setup.py::TestDeviceSetupHelpers -q`
Expected: PASS（2 passed）

- [ ] **Step 5: ruff/black + commit**

```bash
black api/parent_portal/auth.py tests/test_parent_device_setup.py && ruff check api/parent_portal/auth.py
git add api/parent_portal/auth.py tests/test_parent_device_setup.py
git commit -m "feat(parent-auth): device-setup helper（atomic claim / 無 LINE User / IP 鎖）"
```

---

### Task 4: `POST /api/parent/auth/device-setup` 端點

**Files:**
- Modify: `api/parent_portal/auth.py`（import `DeviceSetupOut`；新增 request model + 端點）
- Test: `tests/test_parent_device_setup.py`（新增 self-contained `parent_client` fixture + 端點測試）

- [ ] **Step 1: 寫 failing test（self-contained fixture + 成功/過期/已用/通用錯誤/多裝置）**

於 `tests/test_parent_device_setup.py` 加（fixture 仿 `tests/test_parent_auth_liff.py:parent_client`）：

```python
import hashlib
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.base as base_module
from api.auth import router as auth_router, _ip_attempts, _account_failures
from api.parent_portal import (
    admin_router as parent_admin_router,
    parent_router as parent_portal_router,
    init_parent_line_service,
)
from api.parent_portal.auth import _bind_failures
from utils.exception_handlers import register_exception_handlers


class _FakeLine:
    def is_configured(self):
        return True

    def verify_id_token(self, t):
        raise AssertionError("device-setup 不應呼叫 LINE")


@pytest.fixture
def pclient(tmp_path):
    db_path = tmp_path / "dev-setup.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    init_parent_line_service(_FakeLine())
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(parent_portal_router)
    app.include_router(parent_admin_router)
    with TestClient(app) as c:
        yield c, sf
    base_module._engine, base_module._SessionFactory = old_e, old_sf
    engine.dispose()


def _seed_code(sf, *, used=False, expired=False):
    from api.parent_portal import auth as pauth
    s = sf()
    stu = Student(student_id="S9", name="小華", is_active=True)
    s.add(stu)
    s.flush()
    g = Guardian(student_id=stu.id, name="陳媽媽", is_primary=True)
    s.add(g)
    s.flush()
    staff = User(username="adm", password_hash="x", role="admin", token_version=0)
    s.add(staff)
    s.flush()
    exp = now_taipei_naive() + (timedelta(hours=-1) if expired else timedelta(hours=24))
    code = ParentDeviceSetupCode(
        guardian_id=g.id, code_hash=pauth._hash_code("DEVCODE0001"),
        expires_at=exp, created_by=staff.id,
        used_at=(now_taipei_naive() if used else None),
    )
    s.add(code)
    s.commit()
    gid = g.id
    s.close()
    return gid


class TestDeviceSetupEndpoint:
    def test_success_creates_user_and_session(self, pclient):
        c, sf = pclient
        gid = _seed_code(sf)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["user"]["role"] == "parent"
        # guardian.user_id 已連結；refresh cookie 已下發
        assert "parent_refresh_token" in r.cookies
        s = sf()
        g = s.query(Guardian).filter(Guardian.id == gid).first()
        assert g.user_id is not None
        u = s.query(User).filter(User.id == g.user_id).first()
        assert u.line_user_id is None and u.role == "parent"
        s.close()

    def test_expired_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf, expired=True)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text  # 通用文案，不分流

    def test_used_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf, used=True)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text

    def test_unknown_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf)
        r = c.post("/api/parent/auth/device-setup", json={"code": "WRONGCODE99"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text

    def test_second_device_reuses_user_keeps_both(self, pclient):
        """同 guardian 第二張碼 → 重用 User（不新建），兩裝置 family 並存。"""
        c, sf = pclient
        gid = _seed_code(sf)
        r1 = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r1.status_code == 200
        # 再發一張碼給同 guardian
        from api.parent_portal import auth as pauth
        s = sf()
        staff = s.query(User).filter(User.role == "admin").first()
        s.add(ParentDeviceSetupCode(
            guardian_id=gid, code_hash=pauth._hash_code("DEVCODE0002"),
            expires_at=now_taipei_naive() + timedelta(hours=24), created_by=staff.id,
        ))
        s.commit()
        s.close()
        r2 = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0002"})
        assert r2.status_code == 200
        s = sf()
        g = s.query(Guardian).filter(Guardian.id == gid).first()
        from models.database import ParentRefreshToken
        n_users = s.query(User).filter(User.username == f"parent_device_{gid}").count()
        n_tokens = s.query(ParentRefreshToken).filter(
            ParentRefreshToken.user_id == g.user_id,
            ParentRefreshToken.revoked_at.is_(None),
        ).count()
        assert n_users == 1  # 沒重複建 User
        assert n_tokens == 2  # 兩裝置 family 並存
        s.close()
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python -m pytest tests/test_parent_device_setup.py::TestDeviceSetupEndpoint -q`
Expected: FAIL — 404（端點尚未存在）

- [ ] **Step 3: 實作端點（`api/parent_portal/auth.py`）**

import schema：

```python
from schemas.parent_portal_auth import (
    BindAdditionalChildOut,
    BindFirstChildOut,
    DeviceSetupOut,
    LiffLoginOut,
    ParentRefreshOut,
)
```

於 `bind_additional_child` 端點之後新增：

```python
class DeviceSetupRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=20)


@router.post("/device-setup", response_model=DeviceSetupOut)
def device_setup(
    payload: DeviceSetupRequest,
    request: Request,
    response: Response,
):
    """無 LINE 家長以 staff 簽發的設定碼兌換裝置登入 session（passwordless）。

    成功 → 找/建 parent User（link guardian.user_id）+ 發 access + 30d refresh
    （裝置記憶）。失敗一律回通用錯誤，避免碼枚舉。
    """
    ip = get_client_ip(request) or "unknown"
    _check_ip_rate_limit(ip)
    _check_device_setup_lockout(ip)

    code_hash = _hash_code(payload.code)
    session = get_session()
    try:
        binding = _claim_device_setup_code_atomic(session, code_hash)
        if binding is None:
            session.rollback()
            _record_device_setup_failure(ip)
            raise BusinessError(
                code="DEVICE_SETUP_CODE_INVALID",
                message="設定碼無效或已過期，請向園所索取新碼",
                http_status=400,
            )

        guardian = (
            session.query(Guardian).filter(Guardian.id == binding.guardian_id).first()
        )
        if guardian is None or guardian.deleted_at is not None:
            session.rollback()
            raise HTTPException(status_code=400, detail="此設定碼對應的監護人已不存在")

        # 解析 parent User：已綁定者重用（保留多子女/歷史），否則建無 LINE User
        if guardian.user_id:
            user = session.query(User).filter(User.id == guardian.user_id).first()
            if user is None:
                user = _create_parent_user_for_device(session, guardian)
                guardian.user_id = user.id
        else:
            user = _create_parent_user_for_device(session, guardian)
            guardian.user_id = user.id

        binding.used_by_user_id = user.id
        user.last_login = _now()
        # 不撤銷既有 family：容許家長 + 代養者多裝置並存（撤銷走 revoke-devices）
        _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            user_agent=request.headers.get("user-agent"),
            ip=get_client_ip(request),
        )
        session.commit()
        session.refresh(user)
        _issue_access_token(response, user)

        logger.warning(
            "[device-setup] guardian_id=%s user_id=%s ip=%s",
            guardian.id,
            user.id,
            ip,
        )
        return {
            "status": "ok",
            "user": {
                "user_id": user.id,
                "name": resolve_parent_display_name(session, user),
                "role": "parent",
            },
        }
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python -m pytest tests/test_parent_device_setup.py::TestDeviceSetupEndpoint -q`
Expected: PASS（5 passed）

- [ ] **Step 5: ruff/black + commit**

```bash
black api/parent_portal/auth.py tests/test_parent_device_setup.py && ruff check api/parent_portal/auth.py
git add api/parent_portal/auth.py tests/test_parent_device_setup.py
git commit -m "feat(parent-auth): POST /parent/auth/device-setup 端點（無 LINE 裝置登入）"
```

---

### Task 5: staff 簽發設定碼 `POST /api/guardians/{guardian_id}/device-setup-code`

**Files:**
- Modify: `api/guardians_admin.py`（import `ParentDeviceSetupCode`；新增端點，鏡像 `create_binding_code`）
- Test: `tests/test_parent_device_setup.py`

- [ ] **Step 1: 寫 failing test（權限/明碼一次/active cap/audit）**

加入 `tests/test_parent_device_setup.py`：

```python
def _staff_token(sf):
    from utils.auth import create_access_token
    s = sf()
    u = User(username="adm2", password_hash="x", role="admin",
             permission_names=["GUARDIANS_WRITE"], token_version=0)
    s.add(u)
    s.flush()
    uid = u.id
    s.commit()
    s.close()
    return create_access_token({
        "user_id": uid, "employee_id": None, "role": "admin",
        "name": "adm2", "permission_names": ["GUARDIANS_WRITE"], "token_version": 0,
    })


def _make_guardian(sf):
    s = sf()
    stu = Student(student_id="S7", name="小光", is_active=True)
    s.add(stu)
    s.flush()
    g = Guardian(student_id=stu.id, name="林爸爸", is_primary=True)
    s.add(g)
    s.flush()
    gid = g.id
    s.commit()
    s.close()
    return gid


class TestStaffIssueDeviceCode:
    def test_issue_returns_plain_once_and_stores_hash(self, pclient):
        c, sf = pclient
        gid = _make_guardian(sf)
        tok = _staff_token(sf)
        r = c.post(
            f"/api/guardians/{gid}/device-setup-code",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        plain = r.json()["code"]
        assert len(plain) == 12
        s = sf()
        row = s.query(ParentDeviceSetupCode).filter(
            ParentDeviceSetupCode.guardian_id == gid
        ).one()
        assert row.code_hash != plain  # 只存 hash
        from api.parent_portal import auth as pauth
        assert row.code_hash == pauth._hash_code(plain)
        s.close()

    def test_requires_guardians_write(self, pclient):
        c, sf = pclient
        gid = _make_guardian(sf)
        from utils.auth import create_access_token
        weak = create_access_token({
            "user_id": 999, "employee_id": None, "role": "teacher",
            "name": "t", "permission_names": [], "token_version": 0,
        })
        r = c.post(
            f"/api/guardians/{gid}/device-setup-code",
            headers={"Authorization": f"Bearer {weak}"},
        )
        assert r.status_code in (401, 403)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python -m pytest tests/test_parent_device_setup.py::TestStaffIssueDeviceCode -q`
Expected: FAIL — 404（端點未存在）

- [ ] **Step 3: 實作端點（`api/guardians_admin.py`）**

import 改為：

```python
from models.database import (
    AuditLog,
    Guardian,
    GuardianBindingCode,
    ParentDeviceSetupCode,
    get_session,
)
```

於 `create_binding_code` 之後新增：

```python
@router.post("/{guardian_id}/device-setup-code")
def create_device_setup_code(
    guardian_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """為指定 Guardian 簽發無 LINE 裝置登入碼（明碼僅此次回傳）。"""
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="找不到監護人")

        now = now_taipei_naive()
        active_count = (
            session.query(ParentDeviceSetupCode)
            .filter(
                ParentDeviceSetupCode.guardian_id == guardian.id,
                ParentDeviceSetupCode.used_at.is_(None),
                ParentDeviceSetupCode.expires_at > now,
            )
            .count()
        )
        if active_count >= _MAX_ACTIVE_CODES_PER_GUARDIAN:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此監護人已有 {active_count} 個未使用的裝置登入碼，"
                    f"請先讓家長使用既有碼或等失效後再簽發"
                ),
            )

        plain_code = _generate_plain_code()
        code_hash = _hash_code(plain_code)
        expires_at = now_taipei_naive() + timedelta(hours=_CODE_TTL_HOURS)

        session.add(
            ParentDeviceSetupCode(
                guardian_id=guardian.id,
                code_hash=code_hash,
                expires_at=expires_at,
                created_by=current_user["user_id"],
            )
        )
        ip = get_client_ip(request)
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="CREATE",
                entity_type="parent_device_setup",
                entity_id=str(guardian.id),
                summary="簽發無 LINE 裝置登入碼",
                ip_address=ip,
                created_at=now_taipei_naive(),
            )
        )
        session.commit()
        logger.warning(
            "[device-setup-code] guardian_id=%s created_by=%s",
            guardian.id,
            current_user["user_id"],
        )
        return {"code": plain_code, "expires_at": expires_at.isoformat()}
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python -m pytest tests/test_parent_device_setup.py::TestStaffIssueDeviceCode -q`
Expected: PASS（2 passed）

- [ ] **Step 5: ruff/black + commit**

```bash
black api/guardians_admin.py tests/test_parent_device_setup.py && ruff check api/guardians_admin.py
git add api/guardians_admin.py tests/test_parent_device_setup.py
git commit -m "feat(parent-auth): staff 簽發無 LINE 裝置登入碼端點"
```

---

### Task 6: staff 撤銷裝置 `POST /api/guardians/{guardian_id}/revoke-devices`

**Files:**
- Modify: `api/guardians_admin.py`（import `ParentRefreshToken`；新增端點）
- Test: `tests/test_parent_device_setup.py`

- [ ] **Step 1: 寫 failing test（撤銷後該 family /refresh → 401；無 user → 0）**

```python
class TestRevokeDevices:
    def test_revoke_invalidates_device(self, pclient):
        c, sf = pclient
        gid = _make_guardian(sf)
        # 先發碼 + 兌換建立裝置
        from api.parent_portal import auth as pauth
        s = sf()
        staff = User(username="adm3", password_hash="x", role="admin", token_version=0)
        s.add(staff)
        s.flush()
        s.add(ParentDeviceSetupCode(
            guardian_id=gid, code_hash=pauth._hash_code("REVCODE0001"),
            expires_at=now_taipei_naive() + timedelta(hours=24), created_by=staff.id,
        ))
        s.commit()
        s.close()
        r = c.post("/api/parent/auth/device-setup", json={"code": "REVCODE0001"})
        assert r.status_code == 200
        # refresh 應可用
        assert c.post("/api/parent/auth/refresh").status_code == 200
        # staff 撤銷
        tok = _staff_token(sf)
        rv = c.post(
            f"/api/guardians/{gid}/revoke-devices",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert rv.status_code == 200
        assert rv.json()["revoked"] >= 1
        # 撤銷後 refresh 應 401
        assert c.post("/api/parent/auth/refresh").status_code == 401

    def test_revoke_guardian_without_user_returns_zero(self, pclient):
        c, sf = pclient
        gid = _make_guardian(sf)
        tok = _staff_token(sf)
        rv = c.post(
            f"/api/guardians/{gid}/revoke-devices",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert rv.status_code == 200
        assert rv.json()["revoked"] == 0
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `python -m pytest tests/test_parent_device_setup.py::TestRevokeDevices -q`
Expected: FAIL — 404

- [ ] **Step 3: 實作端點（`api/guardians_admin.py`）**

import 補 `ParentRefreshToken`：

```python
from models.database import (
    AuditLog,
    Guardian,
    GuardianBindingCode,
    ParentDeviceSetupCode,
    ParentRefreshToken,
    get_session,
)
```

新增端點：

```python
@router.post("/{guardian_id}/revoke-devices")
def revoke_guardian_devices(
    guardian_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """撤銷此 Guardian 對應家長 User 的所有未撤銷裝置（遺失/被盜裝置）。

    撤銷後該家長所有裝置下次 /refresh 即 401，需重新以新設定碼設定。
    （含 LINE 裝置一併撤銷——「全撤」為安全動作，over-revoke 安全。）
    """
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="找不到監護人")
        if guardian.user_id is None:
            return {"revoked": 0}

        n = (
            session.query(ParentRefreshToken)
            .filter(
                ParentRefreshToken.user_id == guardian.user_id,
                ParentRefreshToken.revoked_at.is_(None),
            )
            .update({"revoked_at": now_taipei_naive()}, synchronize_session=False)
        )
        ip = get_client_ip(request)
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="UPDATE",
                entity_type="parent_device_setup",
                entity_id=str(guardian.id),
                summary=f"撤銷家長裝置（{int(n or 0)} 個 session）",
                ip_address=ip,
                created_at=now_taipei_naive(),
            )
        )
        session.commit()
        logger.warning(
            "[revoke-devices] guardian_id=%s user_id=%s revoked=%s",
            guardian.id,
            guardian.user_id,
            n,
        )
        return {"revoked": int(n or 0)}
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `python -m pytest tests/test_parent_device_setup.py::TestRevokeDevices -q`
Expected: PASS（2 passed）

- [ ] **Step 5: ruff/black + commit**

```bash
black api/guardians_admin.py tests/test_parent_device_setup.py && ruff check api/guardians_admin.py
git add api/guardians_admin.py tests/test_parent_device_setup.py
git commit -m "feat(parent-auth): staff 撤銷家長裝置端點"
```

---

### Task 7: 全檔測試 + 鄰近回歸 + 收尾

- [ ] **Step 1: 跑本功能全測試**

Run: `python -m pytest tests/test_parent_device_setup.py -q`
Expected: PASS（全綠，約 11+ tests）

- [ ] **Step 2: 跑鄰近家長 auth 回歸（確保未動到既有流程）**

Run: `python -m pytest tests/test_parent_auth_liff.py tests/test_line_binding.py tests/test_parent_bind_takeover_guard.py -q`
Expected: PASS（零新增 fail）

- [ ] **Step 3: 全檔 ruff/black 最終確認**

Run: `ruff check api/parent_portal/auth.py api/guardians_admin.py models/parent_binding.py models/database.py schemas/parent_portal_auth.py && black --check api/parent_portal/auth.py api/guardians_admin.py models/parent_binding.py models/database.py schemas/parent_portal_auth.py tests/test_parent_device_setup.py`
Expected: All checks passed / would be left unchanged

- [ ] **Step 4: 確認 router 已掛載（無需改 main.py）**

`/api/guardians/...`（guardians_admin）與 `/api/parent/auth/...`（parent_portal.auth）兩 router 既有端點已上線，本 plan 僅在既有 router **新增端點**，不需動 `main.py` 註冊。若 Task 4-6 整合測試 200 即已證實掛載正確。

---

## 自我審查（plan vs spec）

- **spec §4 資料模型** → Task 1 ✓
- **spec §5.1 staff 發碼** → Task 5 ✓
- **spec §5.2 device-setup 兌換（通用錯誤防枚舉）** → Task 4（3 個 generic-error 測試）✓
- **spec §5.3 撤銷裝置** → Task 6 ✓
- **spec §6 安全（IP 鎖 / 高熵 12 碼 / 單次 / 通用錯誤）** → Task 3 lockout + Task 5 沿用 `_generate_plain_code`（12 碼）+ Task 4 generic error ✓
- **spec §3 重用 _issue_refresh_token / resolve_parent_display_name / guardian.user_id 解析** → Task 4 ✓
- **spec §5.2 find-vs-create（guardian.user_id 已存在重用）** → Task 4 邏輯 + `test_second_device_reuses_user_keeps_both` ✓
- **型別一致**：`_hash_code` / `_claim_device_setup_code_atomic` / `_create_parent_user_for_device` / `DeviceSetupOut` 跨 Task 命名一致 ✓
- **無 placeholder**：每步含實際 test/impl code 與指令 ✓

## 後端 merge 後的 follow-up（另開 plan）

- 前端：後台 guardian 區「產生無 LINE 裝置登入碼 / 撤銷裝置」按鈕 + 家長無 LINE 登入頁（一般瀏覽器 URL，非 LIFF）。
- OpenAPI codegen（`dump_openapi.py` → `gen:api`）下放新端點型別。
- 文件：CLAUDE.md #9 家長端註記新增「無 LINE fallback 登入」一句。
