# DB-Driven 自訂權限/角色 (b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `ROLE_TEMPLATES` / `ROLE_DESCRIPTIONS` / `PERMISSION_GROUPS` / `PERMISSION_LABELS` 搬到 DB 兩個 table（`permission_definitions` + `roles`），admin UI 可 runtime 新增自訂角色與權限定義，零 redeploy。

**Architecture:** alembic migration `rolesdb01` 建兩表並 seed in-code dict（標 `is_core=true`）；`get_permissions_definition()` 與 `get_role_default_permissions()` 改從 DB 拉；新 `api/permissions_admin.py` 7 個 CRUD endpoint 走 `ROLES_MANAGE` 守衛；新前端 `SettingsPermissionsTab.vue` 兩個 sub-tab（角色管理 + 權限定義）。Hot path（`require_permission` / `has_permission`）零變動，仍純字串比對。

**Tech Stack:** SQLAlchemy + Alembic + FastAPI + pytest（後端）；Vue 3 `<script setup lang="ts">` + Element Plus + vitest（前端）。

**Spec：** `ivy-backend/docs/superpowers/specs/2026-05-25-permission-db-driven-design.md`（同 worktree）

**Repo layout：** 建議用 worktree 隔離
- 後端：`feat/permission-db-driven-2026-05-25-backend`
- 前端：`feat/permission-db-driven-2026-05-25-frontend`

**Alembic head**：當前 head = `mergeheads02`；新 migration `rolesdb01` 的 `down_revision = "mergeheads02"`。

**前置依賴**：(a) 子專案已 merge local main（含 principal/accountant ROLE_TEMPLATES + ROLE_DESCRIPTIONS dict）。

---

## File Structure

**Created (8 files)：**
| 路徑 | 責任 |
|---|---|
| `ivy-backend/models/permission_models.py` | SQLAlchemy ORM `PermissionDefinition` + `Role` |
| `ivy-backend/api/permissions_admin.py` | 7 CRUD endpoint + Pydantic schemas |
| `ivy-backend/alembic/versions/20260525_rolesdb01_roles_and_permission_definitions.py` | 建兩表 + seed 57 perm + 7 role |
| `ivy-backend/tests/test_permission_db_seed.py` | alembic seed 結果驗證（5 條） |
| `ivy-backend/tests/test_permissions_admin.py` | 7 endpoint 整合測試（25 條） |
| `ivy-frontend/src/api/permissions_admin.ts` | API wrapper（getPermissionDefinitions / createPermissionDefinition / updatePermissionDefinition / deletePermissionDefinition / getRoles / createRole / updateRole / deleteRole） |
| `ivy-frontend/src/components/settings/SettingsPermissionsTab.vue` | admin UI（角色管理 + 權限定義 兩 sub-tab） |
| `ivy-frontend/src/components/settings/__tests__/SettingsPermissionsTab.test.ts` | 8 條 vitest |

**Modified (3 files)：**
| 路徑 | 變動 |
|---|---|
| `ivy-backend/utils/permissions.py` | 加 `Permission.ROLES_MANAGE`；`get_permissions_definition()` / `get_role_default_permissions()` 改簽章為 `(session, ...)` 並從 DB 拉；in-code dict 加 deprecated docstring |
| `ivy-backend/api/auth.py` | 3 處 `get_role_default_permissions(role)` 改傳 session（line 142 / 1036 / 1137） |
| `ivy-frontend/src/views/SettingsView.vue` | 加 `<el-tab-pane label="權限管理" name="permissions">` |

---

## Phase A：後端（`ivy-backend/`）

### Task 1: `Permission` enum 加 `ROLES_MANAGE`

**Files:**
- Modify: `ivy-backend/utils/permissions.py:12-89`（Permission str Enum）
- Test: `ivy-backend/tests/test_permissions_unit.py`

- [ ] **Step 1.1: 加 1 條 test 確認 enum 含 ROLES_MANAGE**

在 `tests/test_permissions_unit.py` 末尾加：

```python
def test_permission_enum_has_roles_manage():
    """ROLES_MANAGE 是 (b) 加的第 57 條 enum，守衛角色/權限定義 CRUD。"""
    assert Permission.ROLES_MANAGE.value == "ROLES_MANAGE"
```

- [ ] **Step 1.2: 跑 fail**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "roles_manage" -v
```

預期：FAIL with `AttributeError: ROLES_MANAGE`。

- [ ] **Step 1.3: Edit `utils/permissions.py`**

在 `Permission` enum 末尾（line 89 `VENDOR_PAYMENT_WRITE = "VENDOR_PAYMENT_WRITE"` 之後）加：

```python

    # DB-driven 自訂權限/角色 CRUD 守衛（(b) 子專案）
    ROLES_MANAGE = "ROLES_MANAGE"
```

- [ ] **Step 1.4: 跑 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "roles_manage" -v
```

預期：PASS。

---

### Task 2: SQLAlchemy ORM `PermissionDefinition` + `Role`

**Files:**
- Create: `ivy-backend/models/permission_models.py`
- Test: 跨入 Task 4 alembic seed 驗證

- [ ] **Step 2.1: 新建 `models/permission_models.py`**

```python
"""DB-driven 權限與角色定義（取代 utils/permissions.py 內的 in-code dict）。

由 alembic rolesdb01 建表 + seed；runtime 由 utils/permissions.get_permissions_definition()
與 get_role_default_permissions() 從本 model 查詢。
"""

from datetime import datetime
from typing import List

from sqlalchemy import (
    Column,
    BigInteger,
    Text,
    Boolean,
    TIMESTAMP,
    Index,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY

from models.base import Base


class PermissionDefinition(Base):
    """權限定義表：取代 PERMISSION_LABELS + PERMISSION_GROUPS in-code dict。

    is_core=True：對應 Permission enum + ROLES_MANAGE 共 57 條，由 alembic seed，
    admin 不可刪 / 不可改 code（label/description/group_name 可改）。
    """

    __tablename__ = "permission_definitions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(Text, nullable=False, unique=True, comment="權限識別字串（如 EMPLOYEES_READ）")
    label = Column(Text, nullable=False, comment="中文顯示名（如「員工管理 (檢視)」）")
    description = Column(Text, nullable=True, comment="詳細說明（admin 可標『此權限為...』）")
    group_name = Column(Text, nullable=False, server_default="自訂", comment="前端分組")
    is_core = Column(Boolean, nullable=False, server_default="false", comment="core 為 alembic seed 的 57 條，admin 不可刪")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_permission_definitions_group", "group_name"),
    )


class Role(Base):
    """角色表：取代 ROLE_TEMPLATES + ROLE_LABELS + ROLE_DESCRIPTIONS in-code dict。

    is_core=True：對應 (a) 之 7 個 ROLE_TEMPLATES（admin/principal/supervisor/hr/
    accountant/teacher/parent），由 alembic seed，admin 可改 label/description 但不可
    改 code / 不可改 permissions / 不可刪。
    """

    __tablename__ = "roles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(Text, nullable=False, unique=True, comment="對應 users.role 字串（如 admin / hr）")
    label = Column(Text, nullable=False, comment="中文顯示名")
    description = Column(Text, nullable=True, comment="適用對象 / 一句話")
    permissions = Column(
        ARRAY(Text),
        nullable=False,
        server_default="{}",
        comment="角色預設權限；['*'] = wildcard；與 users.permission_names 同 shape",
    )
    is_core = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())
```

- [ ] **Step 2.2: 確認 import 不爆**

```bash
cd ivy-backend && python -c "from models.permission_models import PermissionDefinition, Role; print('OK')"
```

預期：`OK`。

---

### Task 3: Alembic migration `rolesdb01`（建兩表 + seed）

**Files:**
- Create: `ivy-backend/alembic/versions/20260525_rolesdb01_roles_and_permission_definitions.py`

- [ ] **Step 3.1: 新建 migration 檔（用 alembic revision 自動建檔避免格式錯）**

```bash
cd ivy-backend && alembic revision -m "roles and permission_definitions tables; seed from in-code dicts" --rev-id=rolesdb01
```

這會生成 `alembic/versions/<timestamp>_rolesdb01_roles_and_permission_definitions.py`。檔名 timestamp 由 alembic 自動產生（無需改檔名）。

- [ ] **Step 3.2: 編輯生成的檔案，把內容整個取代為：**

```python
"""roles and permission_definitions tables; seed from in-code dicts

Revision ID: rolesdb01
Revises: mergeheads02
Create Date: 2026-05-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY


revision: str = "rolesdb01"
down_revision: Union[str, None] = "mergeheads02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _build_code_to_group_lookup(groups: list) -> dict:
    """反查 PERMISSION_GROUPS：把每個 code 對應的 group_name 拉出。"""
    lookup = {}
    for g in groups:
        name = g["name"]
        for code in g.get("permissions", []) or []:
            lookup[code] = name
        for sp in g.get("split_permissions", []) or []:
            lookup[sp["read"]] = name
            lookup[sp["write"]] = name
    return lookup


def upgrade() -> None:
    op.create_table(
        "permission_definitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=False, server_default="自訂"),
        sa.Column("is_core", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_permission_definitions_code"),
    )
    op.create_index("ix_permission_definitions_group", "permission_definitions", ["group_name"])

    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "permissions",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("is_core", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )

    # Seed permission_definitions（56 + 1 = 57 條 is_core=true）
    from utils.permissions import (
        PERMISSION_LABELS,
        PERMISSION_GROUPS,
        ROLE_TEMPLATES,
        ROLE_LABELS,
        ROLE_DESCRIPTIONS,
    )

    conn = op.get_bind()
    group_lookup = _build_code_to_group_lookup(PERMISSION_GROUPS)

    perm_rows = []
    for code, label in PERMISSION_LABELS.items():
        perm_rows.append({
            "code": code,
            "label": label,
            "description": None,
            "group_name": group_lookup.get(code, "其他"),
            "is_core": True,
        })
    # 第 57 條：ROLES_MANAGE
    perm_rows.append({
        "code": "ROLES_MANAGE",
        "label": "角色與權限管理",
        "description": "新增/編輯/刪除自訂角色與權限定義",
        "group_name": "系統",
        "is_core": True,
    })

    conn.execute(
        sa.text(
            "INSERT INTO permission_definitions (code, label, description, group_name, is_core) "
            "VALUES (:code, :label, :description, :group_name, :is_core)"
        ),
        perm_rows,
    )

    # Seed roles（7 條 is_core=true）
    role_rows = []
    for code, perms in ROLE_TEMPLATES.items():
        role_rows.append({
            "code": code,
            "label": ROLE_LABELS.get(code, code),
            "description": ROLE_DESCRIPTIONS.get(code, ""),
            "permissions": list(perms),
            "is_core": True,
        })

    conn.execute(
        sa.text(
            "INSERT INTO roles (code, label, description, permissions, is_core) "
            "VALUES (:code, :label, :description, :permissions, :is_core)"
        ),
        role_rows,
    )


def downgrade() -> None:
    # 注意：自訂角色與自訂權限資料將丟失（emergency rollback 接受）
    op.drop_table("roles")
    op.drop_index("ix_permission_definitions_group", table_name="permission_definitions")
    op.drop_table("permission_definitions")
```

- [ ] **Step 3.3: 確認 alembic chain 完整**

```bash
cd ivy-backend && alembic heads
```

預期：`rolesdb01 (head)`（取代原本 mergeheads02）。

- [ ] **Step 3.4: 跑 upgrade 與 downgrade roundtrip**

```bash
cd ivy-backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```

預期：三步都無錯。最終 head 在 rolesdb01。

---

### Task 4: Seed 結果驗證測試

**Files:**
- Create: `ivy-backend/tests/test_permission_db_seed.py`

- [ ] **Step 4.1: 新建測試檔**

```python
"""驗證 rolesdb01 alembic upgrade 之後 permission_definitions 與 roles 兩表的 seed 內容。"""

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base
from models.permission_models import PermissionDefinition, Role


@pytest.fixture
def session_with_seed(tmp_path):
    """建立隔離 SQLite + 跑 Base.metadata.create_all + 手動 seed（避免 alembic 跑 PG-specific）。"""
    db_path = tmp_path / "perm-db-seed.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    session = Session()

    # 模擬 rolesdb01 seed 內容（與 migration 內 in-code dict import 一致）
    from utils.permissions import (
        PERMISSION_LABELS,
        PERMISSION_GROUPS,
        ROLE_TEMPLATES,
        ROLE_LABELS,
        ROLE_DESCRIPTIONS,
    )

    # 反查 group lookup
    group_lookup = {}
    for g in PERMISSION_GROUPS:
        for code in g.get("permissions", []) or []:
            group_lookup[code] = g["name"]
        for sp in g.get("split_permissions", []) or []:
            group_lookup[sp["read"]] = g["name"]
            group_lookup[sp["write"]] = g["name"]

    for code, label in PERMISSION_LABELS.items():
        session.add(PermissionDefinition(
            code=code,
            label=label,
            description=None,
            group_name=group_lookup.get(code, "其他"),
            is_core=True,
        ))
    session.add(PermissionDefinition(
        code="ROLES_MANAGE",
        label="角色與權限管理",
        description="新增/編輯/刪除自訂角色與權限定義",
        group_name="系統",
        is_core=True,
    ))

    for code, perms in ROLE_TEMPLATES.items():
        session.add(Role(
            code=code,
            label=ROLE_LABELS.get(code, code),
            description=ROLE_DESCRIPTIONS.get(code, ""),
            permissions=list(perms),
            is_core=True,
        ))
    session.commit()

    yield session
    session.close()
    engine.dispose()


def test_seed_permission_definitions_count(session_with_seed):
    """seed 後 permission_definitions 應有 56 + 1 (ROLES_MANAGE) = 57 條 is_core=true。"""
    from utils.permissions import PERMISSION_LABELS
    count = session_with_seed.query(PermissionDefinition).filter_by(is_core=True).count()
    assert count == len(PERMISSION_LABELS) + 1


def test_seed_permission_definitions_roles_manage_exists(session_with_seed):
    pd = session_with_seed.query(PermissionDefinition).filter_by(code="ROLES_MANAGE").first()
    assert pd is not None
    assert pd.is_core is True
    assert pd.label == "角色與權限管理"
    assert pd.group_name == "系統"


def test_seed_roles_count(session_with_seed):
    """seed 後 roles 應有 7 條 is_core=true（含 (a) 加的 principal/accountant）。"""
    count = session_with_seed.query(Role).filter_by(is_core=True).count()
    assert count == 7


def test_seed_roles_admin_is_wildcard(session_with_seed):
    admin_role = session_with_seed.query(Role).filter_by(code="admin").first()
    assert admin_role.permissions == ["*"]


def test_seed_roles_principal_inherits_supervisor(session_with_seed):
    """principal seed 內容 = supervisor + SALARY_READ + AUDIT_LOGS + GOV_REPORTS_EXPORT。"""
    from utils.permissions import ROLE_TEMPLATES, Permission
    pri = set(session_with_seed.query(Role).filter_by(code="principal").first().permissions)
    sup = set(ROLE_TEMPLATES["supervisor"])
    assert sup.issubset(pri)
    extras = pri - sup
    assert extras == {
        Permission.SALARY_READ.value,
        Permission.AUDIT_LOGS.value,
        Permission.GOV_REPORTS_EXPORT.value,
    }
```

- [ ] **Step 4.2: 跑測試**

```bash
cd ivy-backend && pytest tests/test_permission_db_seed.py -v
```

預期：5 條 PASS。

---

### Task 5: `get_permissions_definition(session)` 改從 DB 拉

**Files:**
- Modify: `ivy-backend/utils/permissions.py:582-603`
- Test: `ivy-backend/tests/test_permission_db_seed.py`

- [ ] **Step 5.1: 在 `tests/test_permission_db_seed.py` 末尾加 4 條 test**

```python
def test_get_permissions_definition_from_db_returns_all_roles(session_with_seed):
    """get_permissions_definition(session) 應回 7 個 role，每個含 label/description/permissions/is_core。"""
    from utils.permissions import get_permissions_definition
    definition = get_permissions_definition(session_with_seed)
    roles = definition["roles"]
    assert set(roles.keys()) == {"admin", "principal", "supervisor", "hr", "accountant", "teacher", "parent"}
    for role_data in roles.values():
        assert "label" in role_data
        assert "description" in role_data
        assert "permissions" in role_data
        assert "is_core" in role_data
        assert role_data["is_core"] is True


def test_get_permissions_definition_from_db_returns_all_permissions(session_with_seed):
    """get_permissions_definition(session).permissions 應含 57 條（含 ROLES_MANAGE）。"""
    from utils.permissions import get_permissions_definition, PERMISSION_LABELS
    definition = get_permissions_definition(session_with_seed)
    perms = definition["permissions"]
    assert len(perms) == len(PERMISSION_LABELS) + 1
    assert "ROLES_MANAGE" in perms
    for perm_data in perms.values():
        assert "value" in perm_data
        assert "label" in perm_data
        assert "is_core" in perm_data


def test_get_permissions_definition_includes_groups(session_with_seed):
    """response 應含 groups（從 group_name 動態組）。"""
    from utils.permissions import get_permissions_definition
    definition = get_permissions_definition(session_with_seed)
    assert "groups" in definition
    assert len(definition["groups"]) > 0


def test_get_permissions_definition_includes_split_modules(session_with_seed):
    """split_modules 暫保 in-code，仍在 response 內。"""
    from utils.permissions import get_permissions_definition, SPLIT_MODULES
    definition = get_permissions_definition(session_with_seed)
    assert definition["split_modules"] == SPLIT_MODULES
```

- [ ] **Step 5.2: 跑 fail**

```bash
cd ivy-backend && pytest tests/test_permission_db_seed.py -k "get_permissions_definition" -v
```

預期：FAIL — `get_permissions_definition()` 還沒接受 session 參數。

- [ ] **Step 5.3: Edit `utils/permissions.py:582-603`**

把：

```python
def get_permissions_definition() -> Dict:
    """取得完整權限定義供前端使用。"""
    permissions = {
        perm.value: {
            "value": perm.value,
            "label": PERMISSION_LABELS.get(perm.value, perm.value),
        }
        for perm in Permission
    }
    roles = {
        role: {
            "permissions": perms,
            "label": ROLE_LABELS.get(role, role),
            "description": ROLE_DESCRIPTIONS.get(role, ""),
        }
        for role, perms in ROLE_TEMPLATES.items()
    }
    return {
        "permissions": permissions,
        "groups": PERMISSION_GROUPS,
        "roles": roles,
        "split_modules": SPLIT_MODULES,
    }
```

改為：

```python
def get_permissions_definition(session) -> Dict:
    """取得完整權限定義（從 DB 拉 permission_definitions + roles，取代 in-code dict）。

    runtime 從 DB 拉確保 admin runtime 改動立即生效。in-code dict 仍保留供 alembic
    rolesdb01 seed 用，但 runtime 不再參考。
    """
    from models.permission_models import PermissionDefinition, Role

    perm_defs = (
        session.query(PermissionDefinition)
        .order_by(PermissionDefinition.group_name, PermissionDefinition.code)
        .all()
    )
    role_defs = (
        session.query(Role)
        .order_by(Role.is_core.desc(), Role.code)
        .all()
    )

    permissions = {
        p.code: {"value": p.code, "label": p.label, "is_core": p.is_core}
        for p in perm_defs
    }

    # 動態組 groups：依 group_name 分群，對齊 SPLIT_MODULES 為 split_permissions
    groups_map = {}
    for p in perm_defs:
        if p.group_name not in groups_map:
            groups_map[p.group_name] = {"name": p.group_name, "permissions": [], "split_permissions": []}
        # 若該 code 是 SPLIT_MODULES 的 read 或 write，歸到 split_permissions
        is_split = any(
            p.code in (sp["read"], sp["write"]) for sp in SPLIT_MODULES.values()
        )
        if not is_split:
            groups_map[p.group_name]["permissions"].append(p.code)
    # 把 SPLIT_MODULES 的 read/write 配對加進對應 group
    for module_key, sp in SPLIT_MODULES.items():
        read_def = next((p for p in perm_defs if p.code == sp["read"]), None)
        if read_def and read_def.group_name in groups_map:
            module_label = PERMISSION_LABELS.get(sp["read"], sp["read"]).replace(" (檢視)", "")
            groups_map[read_def.group_name]["split_permissions"].append({
                "module": module_label,
                "read": sp["read"],
                "write": sp["write"],
            })
    groups = list(groups_map.values())

    roles = {
        r.code: {
            "label": r.label,
            "description": r.description or "",
            "permissions": list(r.permissions),
            "is_core": r.is_core,
        }
        for r in role_defs
    }

    return {
        "permissions": permissions,
        "groups": groups,
        "roles": roles,
        "split_modules": SPLIT_MODULES,
    }
```

- [ ] **Step 5.4: Edit `api/auth.py:1098`**

把：

```python
@router.get("/permissions")
def get_permissions():
    """取得權限定義（供前端渲染 UI）"""
    return get_permissions_definition()
```

改為：

```python
@router.get("/permissions")
def get_permissions(session: Session = Depends(get_session_dep)):
    """取得權限定義（供前端渲染 UI）— 從 DB 拉，admin runtime 改動立即生效。"""
    return get_permissions_definition(session)
```

注意：`get_session_dep` import 應在 `api/auth.py` 頂部，若無請先加：

```python
from models.database import get_session_dep
```

（先 grep 確認 `get_session_dep` 存在，可能是 `get_db` 或其他名稱）。

- [ ] **Step 5.5: 跑 pass**

```bash
cd ivy-backend && pytest tests/test_permission_db_seed.py -k "get_permissions_definition" -v
```

預期：4 條 PASS。

- [ ] **Step 5.6: 更新 in-code dict deprecated docstring**

在 `utils/permissions.py:200`（`ROLE_TEMPLATES` 字典定義之前）加 module-level 註解：

```python
# ---------------------------------------------------------------------------
# 以下 in-code dict 自 rolesdb01 (2026-05-25) 起 *僅供 alembic seed 與測試*；
# runtime 改由 `get_permissions_definition(session)` 從 DB permission_definitions
# 與 roles 兩表拉。新增 in-code 角色/權限定義不會影響 runtime——必須走 admin
# UI（設定 → 權限管理）或直接 INSERT 進 DB。
# ---------------------------------------------------------------------------
```

---

### Task 6: `get_role_default_permissions(session, code)` + `api/auth.py` 3 處 caller 改

**Files:**
- Modify: `ivy-backend/utils/permissions.py:534-555`（`get_role_default_permissions`）
- Modify: `ivy-backend/api/auth.py:142, 1036, 1137`（3 處 caller）
- Test: `ivy-backend/tests/test_permission_db_seed.py`

- [ ] **Step 6.1: 加 3 條 test**

```python
def test_get_role_default_permissions_returns_db_role(session_with_seed):
    from utils.permissions import get_role_default_permissions
    perms = get_role_default_permissions(session_with_seed, "principal")
    # principal = supervisor 全部 + SALARY_READ + AUDIT_LOGS + GOV_REPORTS_EXPORT
    assert "SALARY_READ" in perms
    assert "AUDIT_LOGS" in perms
    assert "GOV_REPORTS_EXPORT" in perms


def test_get_role_default_permissions_admin_wildcard(session_with_seed):
    from utils.permissions import get_role_default_permissions
    assert get_role_default_permissions(session_with_seed, "admin") == ["*"]


def test_get_role_default_permissions_unknown_role_falls_back_to_teacher(session_with_seed):
    """未知 role code 回傳 teacher 預設（既有行為）。"""
    from utils.permissions import get_role_default_permissions
    perms = get_role_default_permissions(session_with_seed, "nonexistent_role_xyz")
    teacher_perms = get_role_default_permissions(session_with_seed, "teacher")
    assert perms == teacher_perms
```

- [ ] **Step 6.2: 跑 fail**

```bash
cd ivy-backend && pytest tests/test_permission_db_seed.py -k "get_role_default_permissions" -v
```

預期：FAIL — `get_role_default_permissions(role)` 簽章不接受 session。

- [ ] **Step 6.3: Edit `utils/permissions.py`**

找到既有 `get_role_default_permissions(role)` 函式（約 line 534），整個取代為：

```python
def get_role_default_permissions(session, role_code: str) -> List[str]:
    """從 DB roles 表拉指定 role 的預設 permissions。

    fallback：未知 role 回 teacher 預設（既有行為）。
    """
    from models.permission_models import Role

    role = session.query(Role).filter_by(code=role_code).first()
    if role is None:
        teacher_role = session.query(Role).filter_by(code="teacher").first()
        return list(teacher_role.permissions) if teacher_role else []
    return list(role.permissions)
```

- [ ] **Step 6.4: Edit `api/auth.py` 3 處 caller**

3 處需改傳 session：

**Line 142**（POST /users 內）：找到：

```python
        final_perms = get_role_default_permissions(payload_role)
```

改為：

```python
        final_perms = get_role_default_permissions(session, payload_role)
```

**Line 1036**（POST /users 另一處或 UpdateUserRequest 處）：找到：

```python
            final_permission_names = get_role_default_permissions(data.role)
```

改為：

```python
            final_permission_names = get_role_default_permissions(session, data.role)
```

**Line 1137**：找到：

```python
                user.permission_names = get_role_default_permissions(data.role)
```

改為：

```python
                user.permission_names = get_role_default_permissions(session, data.role)
```

注意：3 處的 `session` 變數名要對應該 endpoint 內的 session 變數名（grep `session` 或 `db` 上下文確認）。

- [ ] **Step 6.5: 跑 pass + 跑 auth.py 既有 test 確認零回歸**

```bash
cd ivy-backend && pytest tests/test_permission_db_seed.py tests/test_auth.py tests/test_user_management_authz.py -v 2>&1 | tail -20
```

預期：所有 PASS（含新 3 條 + 既有 auth/user_management 全綠）。

---

### Task 7: `api/permissions_admin.py` — PermissionDefinition CRUD

**Files:**
- Create: `ivy-backend/api/permissions_admin.py`
- Modify: `ivy-backend/main.py`（include router）
- Test: `ivy-backend/tests/test_permissions_admin.py`

- [ ] **Step 7.1: 先建立空 test 檔骨架**

```python
"""api/permissions_admin.py 整合測試（DB-driven 自訂權限/角色 CRUD）。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, User
from models.permission_models import PermissionDefinition, Role
from utils.auth import hash_password


def _seed_core(session):
    """seed 1 個 admin user + 1 個無 ROLES_MANAGE 的 user + 3 個 is_core permission + 2 個 is_core role。"""
    session.add(PermissionDefinition(code="EMPLOYEES_READ", label="員工檢視", group_name="員工", is_core=True))
    session.add(PermissionDefinition(code="ROLES_MANAGE", label="角色與權限管理", group_name="系統", is_core=True))
    session.add(PermissionDefinition(code="DASHBOARD", label="儀表板", group_name="基礎", is_core=True))
    session.add(Role(code="admin", label="系統管理員", description="全部", permissions=["*"], is_core=True))
    session.add(Role(code="teacher", label="教師", description="基礎", permissions=["DASHBOARD"], is_core=True))
    session.add(User(username="admin_u", password_hash=hash_password("p"), role="admin", permission_names=["*"]))
    session.add(User(username="teacher_u", password_hash=hash_password("p"), role="teacher", permission_names=["DASHBOARD"]))
    session.commit()


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "perm-admin.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    from api.permissions_admin import router as perm_admin_router
    from api.auth import router as auth_router

    app = FastAPI()
    app.include_router(perm_admin_router)
    app.include_router(auth_router)

    session = session_factory()
    _seed_core(session)
    session.close()

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin_login(client):
    resp = client.post("/api/auth/login", json={"username": "admin_u", "password": "p"})
    assert resp.status_code == 200
    return resp


def _teacher_login(client):
    resp = client.post("/api/auth/login", json={"username": "teacher_u", "password": "p"})
    assert resp.status_code == 200
    return resp
```

- [ ] **Step 7.2: 加 10 條 PermissionDefinition CRUD test**

接續上面 test 檔末尾加：

```python
# ====================================================================
# PermissionDefinition CRUD
# ====================================================================

class TestPermissionDefinitionCRUD:
    def test_create_custom_definition_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/permissions/definitions", json={
            "code": "PARENT_SURVEY_WRITE",
            "label": "家長問卷編輯",
            "description": "編輯家長問卷模板",
            "group_name": "家園溝通",
        })
        assert resp.status_code == 200
        assert resp.json()["code"] == "PARENT_SURVEY_WRITE"
        assert resp.json()["is_core"] is False

    def test_create_duplicate_code_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/permissions/definitions", json={"code": "EMPLOYEES_READ", "label": "重複"})
        assert resp.status_code == 422

    def test_create_invalid_code_pattern_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        # lowercase 開頭、特殊符號等都該被 pattern 擋
        for bad in ["lowercase", "WITH-DASH", "123_LEAD", ""]:
            resp = c.post("/api/permissions/definitions", json={"code": bad, "label": "x"})
            assert resp.status_code == 422, f"bad code {bad!r} should 422"

    def test_create_requires_roles_manage(self, client):
        c, _ = client
        _teacher_login(c)  # teacher 無 ROLES_MANAGE
        resp = c.post("/api/permissions/definitions", json={"code": "NEW_PERM", "label": "x"})
        assert resp.status_code == 403

    def test_update_is_core_label_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/permissions/definitions/EMPLOYEES_READ", json={"label": "員工檢視（改）"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "員工檢視（改）"

    def test_update_nonexistent_code_returns_404(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/permissions/definitions/NONEXISTENT", json={"label": "x"})
        assert resp.status_code == 404

    def test_delete_is_core_returns_409(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.delete("/api/permissions/definitions/EMPLOYEES_READ")
        assert resp.status_code == 409
        assert "核心" in resp.json()["detail"]

    def test_delete_custom_definition_success(self, client):
        c, _ = client
        _admin_login(c)
        c.post("/api/permissions/definitions", json={"code": "TMP_PERM", "label": "暫"})
        resp = c.delete("/api/permissions/definitions/TMP_PERM")
        assert resp.status_code == 200

    def test_delete_custom_cascade_cleans_roles(self, client):
        c, sf = client
        _admin_login(c)
        # 加自訂權限 → 加自訂角色含此權限 → 刪權限應清掉 role 內 reference
        c.post("/api/permissions/definitions", json={"code": "TMP_X", "label": "x"})
        c.post("/api/roles", json={"code": "tmp_role", "label": "暫", "permissions": ["DASHBOARD", "TMP_X"]})
        c.delete("/api/permissions/definitions/TMP_X")
        # 驗證 role 內 TMP_X 已被清
        session = sf()
        role = session.query(Role).filter_by(code="tmp_role").first()
        assert "TMP_X" not in role.permissions
        assert "DASHBOARD" in role.permissions
        session.close()

    def test_delete_custom_cascade_cleans_users(self, client):
        c, sf = client
        _admin_login(c)
        c.post("/api/permissions/definitions", json={"code": "TMP_Y", "label": "y"})
        session = sf()
        from models.database import User
        u = session.query(User).filter_by(username="teacher_u").first()
        u.permission_names = ["DASHBOARD", "TMP_Y"]
        old_token_v = u.token_version
        session.commit()
        session.close()
        c.delete("/api/permissions/definitions/TMP_Y")
        session = sf()
        u = session.query(User).filter_by(username="teacher_u").first()
        assert "TMP_Y" not in u.permission_names
        assert u.token_version > old_token_v  # token bump
        session.close()
```

- [ ] **Step 7.3: 跑 fail（router 還沒建）**

```bash
cd ivy-backend && pytest tests/test_permissions_admin.py -v 2>&1 | tail -10
```

預期：FAIL 在 `from api.permissions_admin import router` 那行 ImportError。

- [ ] **Step 7.4: 新建 `api/permissions_admin.py`**

```python
"""DB-driven 自訂權限/角色 admin CRUD（(b) 子專案）。

7 endpoint，全部走 Permission.ROLES_MANAGE 守衛：
- POST   /api/permissions/definitions       新增自訂權限
- PUT    /api/permissions/definitions/{code}  改 label/description/group_name
- DELETE /api/permissions/definitions/{code}  刪自訂權限（cascade 清 roles+users）
- POST   /api/roles                          新增自訂角色
- PUT    /api/roles/{code}                    改 label/description/permissions
- DELETE /api/roles/{code}                    刪自訂角色
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from models.database import User, get_session_dep
from models.permission_models import PermissionDefinition, Role
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["permissions-admin"])


# ============================================================
# Pydantic schemas
# ============================================================

class PermissionDefinitionIn(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    label: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    group_name: str = Field("自訂", max_length=40)


class PermissionDefinitionUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    group_name: Optional[str] = Field(None, max_length=40)


class RoleIn(BaseModel):
    code: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    label: str = Field(..., min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: List[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: Optional[List[str]] = None


# ============================================================
# PermissionDefinition CRUD
# ============================================================

@router.post("/permissions/definitions")
def create_permission_definition(
    payload: PermissionDefinitionIn,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    existing = session.query(PermissionDefinition).filter_by(code=payload.code).first()
    if existing is not None:
        raise HTTPException(status_code=422, detail=f"權限 code 已存在：{payload.code}")
    pd = PermissionDefinition(
        code=payload.code,
        label=payload.label,
        description=payload.description,
        group_name=payload.group_name,
        is_core=False,
    )
    session.add(pd)
    session.commit()
    session.refresh(pd)
    return {
        "code": pd.code,
        "label": pd.label,
        "description": pd.description,
        "group_name": pd.group_name,
        "is_core": pd.is_core,
    }


@router.put("/permissions/definitions/{code}")
def update_permission_definition(
    code: str,
    payload: PermissionDefinitionUpdate,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    pd = session.query(PermissionDefinition).filter_by(code=code).first()
    if pd is None:
        raise HTTPException(status_code=404, detail="權限定義不存在")
    if payload.label is not None:
        pd.label = payload.label
    if payload.description is not None:
        pd.description = payload.description
    if payload.group_name is not None:
        pd.group_name = payload.group_name
    session.commit()
    session.refresh(pd)
    return {"code": pd.code, "label": pd.label, "description": pd.description, "group_name": pd.group_name, "is_core": pd.is_core}


@router.delete("/permissions/definitions/{code}")
def delete_permission_definition(
    code: str,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    pd = session.query(PermissionDefinition).filter_by(code=code).first()
    if pd is None:
        raise HTTPException(status_code=404, detail="權限定義不存在")
    if pd.is_core:
        raise HTTPException(status_code=409, detail="核心權限不可刪除")

    # 順序：先 bump token_version → 再 array_remove → 最後 delete
    # 1. 找出所有持有此 perm 的 user，bump token_version
    affected_users = session.query(User).filter(
        User.permission_names.contains([code])  # PG ARRAY contains
    ).all()
    for u in affected_users:
        u.token_version = (u.token_version or 0) + 1

    # 2. array_remove 清掉 roles 與 users 內 reference
    # 注意：SQLite 無 array_remove，需 app 層處理
    is_sqlite = session.bind.dialect.name == "sqlite"
    if is_sqlite:
        roles = session.query(Role).all()
        for r in roles:
            if code in r.permissions:
                r.permissions = [p for p in r.permissions if p != code]
        users = session.query(User).all()
        for u in users:
            if u.permission_names and code in u.permission_names:
                u.permission_names = [p for p in u.permission_names if p != code]
    else:
        session.execute(
            text("UPDATE roles SET permissions = array_remove(permissions, :c), updated_at = NOW() WHERE :c = ANY(permissions)"),
            {"c": code},
        )
        session.execute(
            text("UPDATE users SET permission_names = array_remove(permission_names, :c) WHERE :c = ANY(permission_names)"),
            {"c": code},
        )

    # 3. delete pd
    session.delete(pd)
    session.commit()
    logger.info("delete permission_definition code=%s cascade users=%d", code, len(affected_users))
    return {"ok": True}
```

（Role CRUD 在 Task 8 加進同一檔，這個 task 先 commit permission CRUD 部分驗證。）

- [ ] **Step 7.5: Edit `main.py` include router**

在 main.py 的 router include 區塊（grep `include_router`）加：

```python
from api.permissions_admin import router as permissions_admin_router
app.include_router(permissions_admin_router)
```

- [ ] **Step 7.6: 跑測試**

```bash
cd ivy-backend && pytest tests/test_permissions_admin.py -v -k "PermissionDefinitionCRUD" 2>&1 | tail -15
```

預期：10 條 PASS。

---

### Task 8: `api/permissions_admin.py` — Role CRUD

**Files:**
- Modify: `ivy-backend/api/permissions_admin.py`
- Test: `ivy-backend/tests/test_permissions_admin.py`

- [ ] **Step 8.1: 在 test 檔末尾加 11 條 Role CRUD test**

```python
# ====================================================================
# Role CRUD
# ====================================================================

class TestRoleCRUD:
    def test_create_role_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/roles", json={
            "code": "custom_principal",
            "label": "兼會計園長",
            "description": "principal + SALARY_WRITE",
            "permissions": ["DASHBOARD", "EMPLOYEES_READ"],
        })
        assert resp.status_code == 200
        assert resp.json()["code"] == "custom_principal"
        assert resp.json()["is_core"] is False

    def test_create_role_with_unknown_permission_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/roles", json={
            "code": "bad_role",
            "label": "x",
            "permissions": ["UNKNOWN_PERM_XYZ"],
        })
        assert resp.status_code == 422
        assert "UNKNOWN_PERM_XYZ" in resp.json()["detail"]

    def test_create_role_with_wildcard_allowed(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/roles", json={"code": "super", "label": "s", "permissions": ["*"]})
        assert resp.status_code == 200

    def test_create_duplicate_code_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/roles", json={"code": "admin", "label": "重複"})
        assert resp.status_code == 422

    def test_create_invalid_code_pattern_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        for bad in ["UPPERCASE", "with-dash", "123lead", ""]:
            resp = c.post("/api/roles", json={"code": bad, "label": "x"})
            assert resp.status_code == 422, f"bad role code {bad!r} should 422"

    def test_update_is_core_permissions_returns_409(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/roles/teacher", json={"permissions": ["DASHBOARD", "EMPLOYEES_READ"]})
        assert resp.status_code == 409
        assert "核心" in resp.json()["detail"]

    def test_update_is_core_label_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/roles/teacher", json={"label": "老師（改）"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "老師（改）"

    def test_update_custom_role_permissions_bumps_user_token_version(self, client):
        c, sf = client
        _admin_login(c)
        c.post("/api/roles", json={"code": "tmp_r", "label": "x", "permissions": ["DASHBOARD"]})
        # 建一個 user 用此 role 且 permission_names IS NULL（依角色預設）
        session = sf()
        from models.database import User
        from utils.auth import hash_password
        u = User(username="u_tmp", password_hash=hash_password("p"), role="tmp_r", permission_names=None)
        session.add(u)
        session.commit()
        old_token_v = u.token_version or 0
        session.close()
        # PUT permissions
        c.put("/api/roles/tmp_r", json={"permissions": ["DASHBOARD", "EMPLOYEES_READ"]})
        session = sf()
        u = session.query(User).filter_by(username="u_tmp").first()
        assert (u.token_version or 0) > old_token_v
        session.close()

    def test_delete_is_core_returns_409(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.delete("/api/roles/teacher")
        assert resp.status_code == 409

    def test_delete_role_with_user_reference_returns_409(self, client):
        c, sf = client
        _admin_login(c)
        c.post("/api/roles", json={"code": "tmp_used", "label": "x"})
        session = sf()
        from models.database import User
        from utils.auth import hash_password
        u = User(username="u_used", password_hash=hash_password("p"), role="tmp_used", permission_names=[])
        session.add(u)
        session.commit()
        session.close()
        resp = c.delete("/api/roles/tmp_used")
        assert resp.status_code == 409
        assert "1 個帳號" in resp.json()["detail"]

    def test_delete_custom_role_success(self, client):
        c, _ = client
        _admin_login(c)
        c.post("/api/roles", json={"code": "tmp_unused", "label": "x"})
        resp = c.delete("/api/roles/tmp_unused")
        assert resp.status_code == 200
```

- [ ] **Step 8.2: 跑 fail**

```bash
cd ivy-backend && pytest tests/test_permissions_admin.py -v -k "RoleCRUD" 2>&1 | tail -20
```

預期：11 條 FAIL（router 缺）。

- [ ] **Step 8.3: 在 `api/permissions_admin.py` 末尾加 Role CRUD 3 個 endpoint**

```python
# ============================================================
# Role CRUD
# ============================================================

@router.post("/roles")
def create_role(
    payload: RoleIn,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    existing = session.query(Role).filter_by(code=payload.code).first()
    if existing is not None:
        raise HTTPException(status_code=422, detail=f"角色 code 已存在：{payload.code}")

    # Validate permissions exist
    invalid = []
    for c in payload.permissions:
        if c == "*":
            continue
        if not session.query(PermissionDefinition.code).filter_by(code=c).first():
            invalid.append(c)
    if invalid:
        raise HTTPException(status_code=422, detail=f"以下 permission code 不存在：{invalid}")

    role = Role(
        code=payload.code,
        label=payload.label,
        description=payload.description,
        permissions=list(payload.permissions),
        is_core=False,
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return {
        "code": role.code,
        "label": role.label,
        "description": role.description,
        "permissions": list(role.permissions),
        "is_core": role.is_core,
    }


@router.put("/roles/{code}")
def update_role(
    code: str,
    payload: RoleUpdate,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    role = session.query(Role).filter_by(code=code).first()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    if payload.permissions is not None:
        if role.is_core:
            raise HTTPException(status_code=409, detail="核心角色的權限不可修改（僅可改 label/description）")
        invalid = []
        for c in payload.permissions:
            if c == "*":
                continue
            if not session.query(PermissionDefinition.code).filter_by(code=c).first():
                invalid.append(c)
        if invalid:
            raise HTTPException(status_code=422, detail=f"以下 permission code 不存在：{invalid}")
        role.permissions = list(payload.permissions)

        # bump token_version for users 依此 role 預設（permission_names IS NULL）
        # SQLite/PG 通用寫法：query users where role=code 然後 set token_version + 1
        affected = session.query(User).filter(User.role == code, User.permission_names.is_(None)).all()
        for u in affected:
            u.token_version = (u.token_version or 0) + 1

    if payload.label is not None:
        role.label = payload.label
    if payload.description is not None:
        role.description = payload.description

    session.commit()
    session.refresh(role)
    return {
        "code": role.code,
        "label": role.label,
        "description": role.description,
        "permissions": list(role.permissions),
        "is_core": role.is_core,
    }


@router.delete("/roles/{code}")
def delete_role(
    code: str,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    role = session.query(Role).filter_by(code=code).first()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.is_core:
        raise HTTPException(status_code=409, detail="核心角色不可刪除")

    user_count = session.query(User).filter_by(role=code).count()
    if user_count > 0:
        raise HTTPException(status_code=409, detail=f"尚有 {user_count} 個帳號使用此角色，請先變更帳號角色再刪除")

    session.delete(role)
    session.commit()
    return {"ok": True}
```

- [ ] **Step 8.4: 跑 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_admin.py -v 2>&1 | tail -25
```

預期：25 條全 PASS（10 PermissionDef CRUD + 11 Role CRUD + 4 既有 fixture seed 自驗）。

---

### Task 9: 全套 pytest + BE commit

- [ ] **Step 9.1: 跑全套後端測試確認零回歸**

```bash
cd ivy-backend && pytest --ignore=tests/test_salary_export.py --ignore=tests/test_jwt_rotation.py --ignore=tests/test_reports_drilldown.py --ignore=tests/test_rule_applier.py 2>&1 | tail -10
```

（pre-existing fail 已知排除）。預期：所有 pass。

- [ ] **Step 9.2: git status 確認改檔範圍**

```bash
cd ivy-backend && git status -s
```

應只有：
- `utils/permissions.py` (modified)
- `api/auth.py` (modified)
- `main.py` (modified)
- `models/permission_models.py` (new)
- `api/permissions_admin.py` (new)
- `alembic/versions/<timestamp>_rolesdb01_*.py` (new)
- `tests/test_permission_db_seed.py` (new)
- `tests/test_permissions_admin.py` (new)
- `tests/test_permissions_unit.py` (modified — 加 ROLES_MANAGE test)

- [ ] **Step 9.3: commit**

```bash
cd ivy-backend
git add utils/permissions.py api/auth.py main.py models/permission_models.py api/permissions_admin.py alembic/versions/*rolesdb01*.py tests/test_permission_db_seed.py tests/test_permissions_admin.py tests/test_permissions_unit.py
git status
git commit -m "$(cat <<'EOF'
feat(permissions): DB-driven roles + permission_definitions (b) — admin runtime self-serve

- alembic rolesdb01 建兩表（permission_definitions / roles）+ seed in-code dict 標 is_core=true
- Permission enum 加 ROLES_MANAGE（第 57 條，守衛 7 個 CRUD endpoint）
- get_permissions_definition(session) / get_role_default_permissions(session, code) 改從 DB 拉
- api/permissions_admin.py 新檔：6 個 CRUD endpoint（3 permission def + 3 role）+ ROLES_MANAGE 守衛
- 刪除 permission_definition cascade 清 roles + users + bump token_version
- 改 role.permissions 自動 bump 依此角色預設（permission_names IS NULL）的 user token_version
- hot path（require_permission / has_permission）零變動，仍純字串比對
- 既有 router 引用 Permission.XXX 一字不動，向後相容

26 條新 pytest（5 seed + 4 get_permissions_definition + 3 get_role_default + 10 PermissionDef CRUD + 11 Role CRUD）；in-code dict 保留供 alembic seed 與 IDE 自動完成，runtime 不再參考。

Spec: docs/superpowers/specs/2026-05-25-permission-db-driven-design.md
Plan: docs/superpowers/plans/2026-05-25-permission-db-driven.md
依賴：(a) 子專案 ROLE_TEMPLATES principal/accountant + ROLE_DESCRIPTIONS

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase B：前端（`ivy-frontend/`）

### Task 10: `src/api/permissions_admin.ts` API wrapper

**Files:**
- Create: `ivy-frontend/src/api/permissions_admin.ts`

- [ ] **Step 10.1: 新建檔案**

```ts
import api from './index'
import type { AxiosResponse } from 'axios'

export interface PermissionDefinition {
  code: string
  label: string
  description: string | null
  group_name: string
  is_core: boolean
}

export interface PermissionDefinitionIn {
  code: string
  label: string
  description?: string
  group_name?: string
}

export interface PermissionDefinitionUpdate {
  label?: string
  description?: string
  group_name?: string
}

export interface Role {
  code: string
  label: string
  description: string | null
  permissions: string[]
  is_core: boolean
}

export interface RoleIn {
  code: string
  label: string
  description?: string
  permissions: string[]
}

export interface RoleUpdate {
  label?: string
  description?: string
  permissions?: string[]
}

export function createPermissionDefinition(payload: PermissionDefinitionIn): Promise<AxiosResponse<PermissionDefinition>> {
  return api.post('/permissions/definitions', payload)
}

export function updatePermissionDefinition(code: string, payload: PermissionDefinitionUpdate): Promise<AxiosResponse<PermissionDefinition>> {
  return api.put(`/permissions/definitions/${encodeURIComponent(code)}`, payload)
}

export function deletePermissionDefinition(code: string): Promise<AxiosResponse<{ ok: boolean }>> {
  return api.delete(`/permissions/definitions/${encodeURIComponent(code)}`)
}

export function createRole(payload: RoleIn): Promise<AxiosResponse<Role>> {
  return api.post('/roles', payload)
}

export function updateRole(code: string, payload: RoleUpdate): Promise<AxiosResponse<Role>> {
  return api.put(`/roles/${encodeURIComponent(code)}`, payload)
}

export function deleteRole(code: string): Promise<AxiosResponse<{ ok: boolean }>> {
  return api.delete(`/roles/${encodeURIComponent(code)}`)
}
```

- [ ] **Step 10.2: typecheck**

```bash
cd ivy-frontend && npm run typecheck 2>&1 | tail -5
```

預期：0 error。

---

### Task 11: `SettingsPermissionsTab.test.ts` 8 條（先失敗版）

**Files:**
- Create: `ivy-frontend/src/components/settings/__tests__/SettingsPermissionsTab.test.ts`

- [ ] **Step 11.1: 新建測試檔**

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import ElementPlus from 'element-plus'

vi.mock('@/api/auth', () => {
  const mockPermissionDefinition = {
    permissions: {
      DASHBOARD: { value: 'DASHBOARD', label: '儀表板', is_core: true },
      EMPLOYEES_READ: { value: 'EMPLOYEES_READ', label: '員工檢視', is_core: true },
      EMPLOYEES_WRITE: { value: 'EMPLOYEES_WRITE', label: '員工編輯', is_core: true },
      ROLES_MANAGE: { value: 'ROLES_MANAGE', label: '角色與權限管理', is_core: true },
      CUSTOM_X: { value: 'CUSTOM_X', label: '自訂 X', is_core: false },
    },
    groups: [
      { name: '基礎', permissions: ['DASHBOARD', 'ROLES_MANAGE'], split_permissions: [] },
      { name: '員工', permissions: [], split_permissions: [{ module: '員工', read: 'EMPLOYEES_READ', write: 'EMPLOYEES_WRITE' }] },
      { name: '自訂', permissions: ['CUSTOM_X'], split_permissions: [] },
    ],
    roles: {
      admin: { label: '系統管理員', description: '全部', permissions: ['*'], is_core: true },
      teacher: { label: '教師', description: '基礎', permissions: ['DASHBOARD'], is_core: true },
      custom_pri: { label: '兼會計園長', description: 'p+s', permissions: ['DASHBOARD', 'EMPLOYEES_READ'], is_core: false },
    },
  }
  return {
    getPermissions: vi.fn().mockResolvedValue({ data: mockPermissionDefinition }),
  }
})

vi.mock('@/api/permissions_admin', () => ({
  createPermissionDefinition: vi.fn().mockResolvedValue({ data: { code: 'NEW', label: 'n', is_core: false } }),
  updatePermissionDefinition: vi.fn().mockResolvedValue({ data: {} }),
  deletePermissionDefinition: vi.fn().mockResolvedValue({ data: { ok: true } }),
  createRole: vi.fn().mockResolvedValue({ data: { code: 'new_r', label: 'r', permissions: [], is_core: false } }),
  updateRole: vi.fn().mockResolvedValue({ data: {} }),
  deleteRole: vi.fn().mockResolvedValue({ data: { ok: true } }),
}))

import SettingsPermissionsTab from '../SettingsPermissionsTab.vue'
import * as permsAdminApi from '@/api/permissions_admin'

describe('SettingsPermissionsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  async function mountTab() {
    const wrapper = mount(SettingsPermissionsTab, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders two sub-tabs: 角色管理 + 權限定義', async () => {
    const wrapper = await mountTab()
    const tabLabels = wrapper.findAll('.el-tabs__item').map((el) => el.text())
    expect(tabLabels).toContain('角色管理')
    expect(tabLabels).toContain('權限定義')
  })

  it('roles table renders all roles with is_core badge', async () => {
    const wrapper = await mountTab()
    // 角色管理是 default tab，table 應渲染 3 個 role
    const rows = document.querySelectorAll('.roles-table .el-table__row')
    expect(rows.length).toBe(3)
  })

  it('is_core role delete button is disabled', async () => {
    const wrapper = await mountTab()
    const adminRow = Array.from(document.querySelectorAll('.roles-table .el-table__row')).find((r) =>
      r.textContent?.includes('admin'),
    )
    const deleteBtn = adminRow?.querySelector('.delete-role-btn')
    expect(deleteBtn?.hasAttribute('disabled')).toBe(true)
  })

  it('clicking 新增角色 opens dialog', async () => {
    const wrapper = await mountTab()
    const addBtn = document.querySelector('.add-role-btn') as HTMLElement
    addBtn.click()
    await flushPromises()
    expect(document.querySelector('.role-edit-dialog')).not.toBeNull()
  })

  it('switching to 權限定義 tab shows warning callout', async () => {
    const wrapper = await mountTab()
    // 找「權限定義」tab 並點
    const permTabLabel = wrapper.findAll('.el-tabs__item').find((el) => el.text() === '權限定義')
    await permTabLabel?.trigger('click')
    await flushPromises()
    await nextTick()
    const callout = document.querySelector('.permission-warning-callout')
    expect(callout).not.toBeNull()
    expect(callout?.textContent).toContain('自訂權限僅可用於')
  })

  it('clicking 新增權限 opens dialog with code+label+description+group_name fields', async () => {
    const wrapper = await mountTab()
    const permTabLabel = wrapper.findAll('.el-tabs__item').find((el) => el.text() === '權限定義')
    await permTabLabel?.trigger('click')
    await flushPromises()
    const addBtn = document.querySelector('.add-permission-btn') as HTMLElement
    addBtn.click()
    await flushPromises()
    expect(document.querySelector('.permission-edit-dialog')).not.toBeNull()
    expect(document.querySelector('.permission-edit-dialog input[data-field="code"]')).not.toBeNull()
  })

  it('deleting custom role calls deleteRole API', async () => {
    const wrapper = await mountTab()
    const customRow = Array.from(document.querySelectorAll('.roles-table .el-table__row')).find((r) =>
      r.textContent?.includes('custom_pri'),
    )
    const deleteBtn = customRow?.querySelector('.delete-role-btn') as HTMLElement
    deleteBtn.click()
    await flushPromises()
    // 確認 dialog 出現後點確認
    const confirmBtn = document.querySelector('.el-message-box__btns .el-button--primary') as HTMLElement
    confirmBtn?.click()
    await flushPromises()
    expect(permsAdminApi.deleteRole).toHaveBeenCalledWith('custom_pri')
  })

  it('is_core permission delete button is disabled in 權限定義 tab', async () => {
    const wrapper = await mountTab()
    const permTabLabel = wrapper.findAll('.el-tabs__item').find((el) => el.text() === '權限定義')
    await permTabLabel?.trigger('click')
    await flushPromises()
    const dashboardRow = Array.from(document.querySelectorAll('.permissions-table .el-table__row')).find((r) =>
      r.textContent?.includes('DASHBOARD'),
    )
    const deleteBtn = dashboardRow?.querySelector('.delete-permission-btn')
    expect(deleteBtn?.hasAttribute('disabled')).toBe(true)
  })
})
```

- [ ] **Step 11.2: 跑 fail**

```bash
cd ivy-frontend && npm test -- src/components/settings/__tests__/SettingsPermissionsTab.test.ts --run 2>&1 | tail -10
```

預期：8 條 FAIL（SettingsPermissionsTab.vue 不存在）。

---

### Task 12: `SettingsPermissionsTab.vue` 實作 + `SettingsView.vue` tab entry

**Files:**
- Create: `ivy-frontend/src/components/settings/SettingsPermissionsTab.vue`
- Modify: `ivy-frontend/src/views/SettingsView.vue:1-40`

- [ ] **Step 12.1: 新建 `SettingsPermissionsTab.vue`**

```vue
<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getPermissions } from '@/api/auth'
import {
  createPermissionDefinition,
  updatePermissionDefinition,
  deletePermissionDefinition,
  createRole,
  updateRole,
  deleteRole,
  type PermissionDefinition,
  type Role,
} from '@/api/permissions_admin'
import { apiError } from '@/utils/error'

interface RoleDef {
  label: string
  description: string
  permissions: string[]
  is_core: boolean
}

interface PermDef {
  value: string
  label: string
  is_core: boolean
}

interface PermissionsResponse {
  permissions: Record<string, PermDef>
  groups: { name: string; permissions: string[]; split_permissions?: { module: string; read: string; write: string }[] }[]
  roles: Record<string, RoleDef>
}

const activeSubTab = ref<'roles' | 'definitions'>('roles')
const definition = ref<PermissionsResponse>({ permissions: {}, groups: [], roles: {} })
const loading = ref(false)

const roleRows = computed(() =>
  Object.entries(definition.value.roles).map(([code, r]) => ({
    code,
    label: r.label,
    description: r.description,
    permission_count: r.permissions.includes('*') ? '全部' : `${r.permissions.length} 條`,
    is_core: r.is_core,
    permissions: r.permissions,
  })),
)

const permissionRows = computed(() =>
  Object.entries(definition.value.permissions).map(([code, p]) => ({
    code,
    label: p.label,
    group_name: _findGroupName(code),
    is_core: p.is_core,
  })),
)

function _findGroupName(code: string): string {
  for (const g of definition.value.groups) {
    if ((g.permissions || []).includes(code)) return g.name
    for (const sp of g.split_permissions || []) {
      if (sp.read === code || sp.write === code) return g.name
    }
  }
  return '其他'
}

async function fetchDefinition() {
  loading.value = true
  try {
    const res = await getPermissions()
    definition.value = res.data
  } catch (e) {
    ElMessage.error('載入權限定義失敗')
  } finally {
    loading.value = false
  }
}

// Role dialog
const roleDialogVisible = ref(false)
const roleEditMode = ref<'create' | 'edit'>('create')
const roleForm = reactive<{ code: string; label: string; description: string; permissions: string[]; is_core: boolean }>({
  code: '',
  label: '',
  description: '',
  permissions: [],
  is_core: false,
})

function handleAddRole() {
  roleEditMode.value = 'create'
  Object.assign(roleForm, { code: '', label: '', description: '', permissions: [], is_core: false })
  roleDialogVisible.value = true
}

function handleEditRole(row: typeof roleRows.value[0]) {
  roleEditMode.value = 'edit'
  Object.assign(roleForm, {
    code: row.code,
    label: row.label,
    description: row.description,
    permissions: [...row.permissions],
    is_core: row.is_core,
  })
  roleDialogVisible.value = true
}

async function saveRole() {
  try {
    if (roleEditMode.value === 'create') {
      await createRole({
        code: roleForm.code,
        label: roleForm.label,
        description: roleForm.description || undefined,
        permissions: roleForm.permissions,
      })
      ElMessage.success('角色已新增')
    } else {
      const payload: Record<string, unknown> = { label: roleForm.label, description: roleForm.description }
      if (!roleForm.is_core) {
        payload.permissions = roleForm.permissions
      }
      await updateRole(roleForm.code, payload)
      ElMessage.success('角色已更新')
    }
    roleDialogVisible.value = false
    await fetchDefinition()
  } catch (e) {
    ElMessage.error(apiError(e, '儲存失敗'))
  }
}

function handleDeleteRole(row: typeof roleRows.value[0]) {
  ElMessageBox.confirm(`確定刪除角色「${row.label}」（code: ${row.code}）？`, '警告', { type: 'warning' })
    .then(async () => {
      try {
        await deleteRole(row.code)
        ElMessage.success('角色已刪除')
        await fetchDefinition()
      } catch (e) {
        ElMessage.error(apiError(e, '刪除失敗'))
      }
    })
    .catch(() => {})
}

// Permission dialog
const permDialogVisible = ref(false)
const permEditMode = ref<'create' | 'edit'>('create')
const permForm = reactive<{ code: string; label: string; description: string; group_name: string; is_core: boolean }>({
  code: '',
  label: '',
  description: '',
  group_name: '自訂',
  is_core: false,
})

const existingGroupNames = computed(() => Array.from(new Set(definition.value.groups.map((g) => g.name))))

function handleAddPermission() {
  permEditMode.value = 'create'
  Object.assign(permForm, { code: '', label: '', description: '', group_name: '自訂', is_core: false })
  permDialogVisible.value = true
}

function handleEditPermission(row: typeof permissionRows.value[0]) {
  permEditMode.value = 'edit'
  Object.assign(permForm, {
    code: row.code,
    label: row.label,
    description: '',
    group_name: row.group_name,
    is_core: row.is_core,
  })
  permDialogVisible.value = true
}

async function savePermission() {
  try {
    if (permEditMode.value === 'create') {
      await createPermissionDefinition({
        code: permForm.code,
        label: permForm.label,
        description: permForm.description || undefined,
        group_name: permForm.group_name,
      })
      ElMessage.success('權限已新增')
    } else {
      await updatePermissionDefinition(permForm.code, {
        label: permForm.label,
        description: permForm.description,
        group_name: permForm.group_name,
      })
      ElMessage.success('權限已更新')
    }
    permDialogVisible.value = false
    await fetchDefinition()
  } catch (e) {
    ElMessage.error(apiError(e, '儲存失敗'))
  }
}

function handleDeletePermission(row: typeof permissionRows.value[0]) {
  ElMessageBox.confirm(
    `確定刪除權限「${row.label}」（code: ${row.code}）？\n所有引用此權限的角色與帳號都會被清掉。`,
    '警告',
    { type: 'warning' },
  )
    .then(async () => {
      try {
        await deletePermissionDefinition(row.code)
        ElMessage.success('權限已刪除')
        await fetchDefinition()
      } catch (e) {
        ElMessage.error(apiError(e, '刪除失敗'))
      }
    })
    .catch(() => {})
}

onMounted(() => {
  fetchDefinition()
})
</script>

<template>
  <div class="settings-permissions-tab">
    <el-tabs v-model="activeSubTab" type="border-card">
      <el-tab-pane label="角色管理" name="roles">
        <div class="tab-header">
          <el-button class="add-role-btn" type="primary" @click="handleAddRole">新增角色</el-button>
        </div>
        <el-table :data="roleRows" v-loading="loading" class="roles-table">
          <el-table-column prop="code" label="code" width="180" />
          <el-table-column prop="label" label="名稱" width="180" />
          <el-table-column prop="description" label="說明" />
          <el-table-column prop="permission_count" label="權限數" width="100" />
          <el-table-column label="類型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.is_core ? 'info' : 'warning'" size="small">
                {{ row.is_core ? '核心' : '自訂' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <el-button link type="primary" @click="handleEditRole(row)">編輯</el-button>
              <el-button
                class="delete-role-btn"
                link
                type="danger"
                :disabled="row.is_core"
                :title="row.is_core ? '核心角色不可刪除' : ''"
                @click="handleDeleteRole(row)"
              >
                刪除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="權限定義" name="definitions">
        <el-alert
          class="permission-warning-callout"
          type="warning"
          :closable="false"
          show-icon
          title="自訂權限的範圍限制"
          description="自訂權限僅可用於『角色組合』與『前端條件渲染』；後端 API 守衛仍是 hardcoded enum，新增權限不會自動為任何 endpoint 加守衛。若需後端守衛新模組，請開 issue 走開發流程。"
        />
        <div class="tab-header" style="margin-top: 12px;">
          <el-button class="add-permission-btn" type="primary" @click="handleAddPermission">新增權限</el-button>
        </div>
        <el-table :data="permissionRows" v-loading="loading" class="permissions-table">
          <el-table-column prop="code" label="code" width="220" />
          <el-table-column prop="label" label="名稱" width="180" />
          <el-table-column prop="group_name" label="分組" width="120" />
          <el-table-column label="類型" width="80">
            <template #default="{ row }">
              <el-tag :type="row.is_core ? 'info' : 'warning'" size="small">
                {{ row.is_core ? '核心' : '自訂' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180">
            <template #default="{ row }">
              <el-button link type="primary" @click="handleEditPermission(row)">編輯</el-button>
              <el-button
                class="delete-permission-btn"
                link
                type="danger"
                :disabled="row.is_core"
                :title="row.is_core ? '核心權限不可刪除' : ''"
                @click="handleDeletePermission(row)"
              >
                刪除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- Role Edit Dialog -->
    <el-dialog v-model="roleDialogVisible" :title="roleEditMode === 'create' ? '新增角色' : '編輯角色'" width="640px" class="role-edit-dialog">
      <el-form :model="roleForm" label-width="100px">
        <el-form-item label="code">
          <el-input v-model="roleForm.code" :disabled="roleEditMode === 'edit'" placeholder="例：custom_principal" />
        </el-form-item>
        <el-form-item label="名稱">
          <el-input v-model="roleForm.label" placeholder="例：兼會計園長" />
        </el-form-item>
        <el-form-item label="說明">
          <el-input v-model="roleForm.description" type="textarea" :rows="2" placeholder="一句話描述適用對象" />
        </el-form-item>
        <el-form-item label="權限">
          <div v-if="roleForm.is_core" class="readonly-hint">核心角色的權限不可修改</div>
          <div v-else class="permission-checkboxes">
            <div v-for="group in definition.groups" :key="group.name" class="perm-group">
              <div class="perm-group-name">{{ group.name }}</div>
              <el-checkbox
                v-for="code in group.permissions"
                :key="code"
                :model-value="roleForm.permissions.includes(code)"
                @change="(v: boolean) => v ? roleForm.permissions.push(code) : roleForm.permissions.splice(roleForm.permissions.indexOf(code), 1)"
              >
                {{ definition.permissions[code]?.label || code }}
              </el-checkbox>
              <div v-for="sp in group.split_permissions" :key="sp.read" class="split-row">
                <span>{{ sp.module }}</span>
                <el-checkbox
                  :model-value="roleForm.permissions.includes(sp.read)"
                  @change="(v: boolean) => v ? roleForm.permissions.push(sp.read) : roleForm.permissions.splice(roleForm.permissions.indexOf(sp.read), 1)"
                >檢視</el-checkbox>
                <el-checkbox
                  :model-value="roleForm.permissions.includes(sp.write)"
                  @change="(v: boolean) => v ? roleForm.permissions.push(sp.write) : roleForm.permissions.splice(roleForm.permissions.indexOf(sp.write), 1)"
                >編輯</el-checkbox>
              </div>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRole">儲存</el-button>
      </template>
    </el-dialog>

    <!-- Permission Edit Dialog -->
    <el-dialog v-model="permDialogVisible" :title="permEditMode === 'create' ? '新增權限' : '編輯權限'" width="540px" class="permission-edit-dialog">
      <el-form :model="permForm" label-width="100px">
        <el-form-item label="code">
          <el-input v-model="permForm.code" data-field="code" :disabled="permEditMode === 'edit'" placeholder="例：PARENT_SURVEY_WRITE" />
        </el-form-item>
        <el-form-item label="名稱">
          <el-input v-model="permForm.label" placeholder="例：家長問卷編輯" />
        </el-form-item>
        <el-form-item label="說明">
          <el-input v-model="permForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="分組">
          <el-select v-model="permForm.group_name" filterable allow-create>
            <el-option v-for="g in existingGroupNames" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="permDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="savePermission">儲存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.settings-permissions-tab {
  padding: 8px;
}
.tab-header {
  margin-bottom: 12px;
}
.permission-checkboxes {
  width: 100%;
}
.perm-group {
  margin-bottom: 12px;
  padding: 8px;
  background: #f8f9fa;
  border-radius: 4px;
}
.perm-group-name {
  font-weight: 600;
  margin-bottom: 6px;
}
.split-row {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 4px 0;
}
.readonly-hint {
  color: var(--text-tertiary);
  padding: 6px 0;
}
</style>
```

- [ ] **Step 12.2: Edit `src/views/SettingsView.vue` 加 tab entry**

把：

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useShiftStore } from '@/stores/shift'
import SettingsShiftTab from '@/components/settings/SettingsShiftTab.vue'
import SettingsUsersTab from '@/components/settings/SettingsUsersTab.vue'
import SettingsApprovalTab from '@/components/settings/SettingsApprovalTab.vue'
import SettingsLineTab from '@/components/settings/SettingsLineTab.vue'
import SettingsAcademicTermsTab from '@/components/settings/SettingsAcademicTermsTab.vue'
```

改為（加 import）：

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useShiftStore } from '@/stores/shift'
import SettingsShiftTab from '@/components/settings/SettingsShiftTab.vue'
import SettingsUsersTab from '@/components/settings/SettingsUsersTab.vue'
import SettingsApprovalTab from '@/components/settings/SettingsApprovalTab.vue'
import SettingsLineTab from '@/components/settings/SettingsLineTab.vue'
import SettingsAcademicTermsTab from '@/components/settings/SettingsAcademicTermsTab.vue'
import SettingsPermissionsTab from '@/components/settings/SettingsPermissionsTab.vue'
```

把：

```vue
      <el-tab-pane label="帳號管理" name="accounts">
        <SettingsUsersTab v-if="activeTab === 'accounts'" />
      </el-tab-pane>
```

之後加入：

```vue
      <el-tab-pane label="權限管理" name="permissions">
        <SettingsPermissionsTab v-if="activeTab === 'permissions'" />
      </el-tab-pane>
```

- [ ] **Step 12.3: 跑 vitest 確認 8 條 PASS**

```bash
cd ivy-frontend && npm test -- src/components/settings/__tests__/SettingsPermissionsTab.test.ts --run 2>&1 | tail -20
```

預期：8 條 PASS。若 selector 找不到（例如 `.add-role-btn` / `.delete-role-btn` / `.permission-warning-callout` 等），檢查 template 對應 class 是否一致。

---

### Task 13: 跑全套 frontend 驗證

- [ ] **Step 13.1: 跑全套 vitest**

```bash
cd ivy-frontend && npm test 2>&1 | tail -10
```

預期：所有 pass（含新 8 條），零回歸。

- [ ] **Step 13.2: 跑 typecheck**

```bash
cd ivy-frontend && npm run typecheck 2>&1 | tail -5
```

預期：0 error。

- [ ] **Step 13.3: 跑 build**

```bash
cd ivy-frontend && npm run build 2>&1 | tail -10
```

預期：success。

---

### Task 14: 手動驗收（user 親跑 dev server）

**用 user 親手操作 — implementer 不可代勞。**

- [ ] **Step 14.1: 啟動 dev server**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

開 http://localhost:5173，admin 登入。

- [ ] **Step 14.2: 走 spec §Rollout 驗收清單**

依序確認：

- [ ] 設定 → 多了「權限管理」tab
- [ ] 角色管理 sub-tab：列出 7 個 is_core 角色（admin/principal/...）+ 任何已新增的自訂角色
- [ ] is_core 角色「刪除」disabled + tooltip
- [ ] 點「新增角色」→ dialog 開 → 輸入 code「test_role」label「測試」勾 DASHBOARD → 儲存 → 表格刷新出現
- [ ] 此 test_role 立即出現在「帳號管理」tab 新增帳號 dialog 的卡片區（無需 redeploy）
- [ ] 編輯既有 supervisor 角色 → 改 label → 儲存 → 表格刷新
- [ ] 嘗試編輯 supervisor 角色 permissions 區域應顯示「核心角色的權限不可修改」
- [ ] 嘗試刪除 test_role（無 user 引用）→ 成功
- [ ] 加 user 用 test_role → 嘗試刪 test_role → 顯示「尚有 1 個帳號使用此角色...」
- [ ] 切到「權限定義」sub-tab → 上方 warning callout 顯示
- [ ] 列出 57 個 is_core 權限 + 任何自訂
- [ ] 新增自訂權限「TEST_PERM」label「測試」分組「自訂」→ 儲存
- [ ] 把 TEST_PERM 加入 test_role 的 permissions → 儲存
- [ ] 刪 TEST_PERM → 確認 → 提示成功；test_role 內的 TEST_PERM reference 已被清

任一步驟異常，回頭 debug。

---

### Task 15: FE commit

- [ ] **Step 15.1: git status 確認改檔範圍**

```bash
cd ivy-frontend && git status
```

應只有：
- `src/api/permissions_admin.ts` (new)
- `src/components/settings/SettingsPermissionsTab.vue` (new)
- `src/components/settings/__tests__/SettingsPermissionsTab.test.ts` (new)
- `src/views/SettingsView.vue` (modified)

- [ ] **Step 15.2: commit**

```bash
cd ivy-frontend
git add src/api/permissions_admin.ts src/components/settings/SettingsPermissionsTab.vue src/components/settings/__tests__/SettingsPermissionsTab.test.ts src/views/SettingsView.vue
git commit -m "$(cat <<'EOF'
feat(settings): add 權限管理 tab — admin runtime self-serve roles/permissions

- 新增 SettingsPermissionsTab 含「角色管理」+「權限定義」兩 sub-tab
- 角色管理：列表 + 新增/編輯/刪除（核心角色 disable 刪除、permissions 編輯）
- 權限定義：列表 + 新增/編輯/刪除 + warning callout 提示自訂權限對 router 無用
- src/api/permissions_admin.ts wrapper 6 個 endpoint（含完整 TS 型別）
- SettingsView 加 tab entry

依賴 backend rolesdb01 + api/permissions_admin endpoint（ivy-backend feat/permission-db-driven-2026-05-25-backend）

零行為變化於既有 admin 帳號/權限編輯（向後相容）；8 條 vitest 全綠、typecheck 0 error、build success。

Spec: ivy-backend/docs/superpowers/specs/2026-05-25-permission-db-driven-design.md
Plan: ivy-backend/docs/superpowers/plans/2026-05-25-permission-db-driven.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 驗收完成

對齊 spec §Rollout：

- [ ] 後端 26 條新 pytest 全綠（5 seed + 4 get_permissions_definition + 3 get_role_default + 14 endpoint CRUD）
- [ ] `GET /api/permissions` 回傳含 `is_core` 欄位於每個 role 與 permission
- [ ] 無 ROLES_MANAGE 帳號打 CRUD endpoint 403
- [ ] alembic upgrade rolesdb01 → 兩表存在 + 57 perm / 7 role seed 完成
- [ ] alembic downgrade rolesdb01 → 兩表消失（自訂資料丟失接受）
- [ ] 前端 8 條新 vitest 全綠 + typecheck + build 零錯
- [ ] dev server 手測 §14.2 14 條清單全勾
- [ ] 既有 router 引用 `Permission.XXX` 行為不變（零回歸）

整個 plan 約 2-2.5 工作日（後端 1.5 / 前端 1.0）。回滾：alembic downgrade + revert 兩 commit（自訂資料丟失接受）。
