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
    JSON,
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
    code = Column(
        Text, nullable=False, unique=True, comment="權限識別字串（如 EMPLOYEES_READ）"
    )
    label = Column(Text, nullable=False, comment="中文顯示名（如「員工管理 (檢視)」）")
    description = Column(
        Text, nullable=True, comment="詳細說明（admin 可標『此權限為...』）"
    )
    group_name = Column(Text, nullable=False, server_default="自訂", comment="前端分組")
    is_core = Column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="core 為 alembic seed 的 57 條，admin 不可刪",
    )
    # OPS-1：permscope01 已 add_column 此欄到 DB，但 ORM 原本漏宣告，導致
    # get_permissions_definition 永不回傳 scope_options → 前端角色編輯器的
    # 『僅自班/全園』scope radio 永不渲染、admin 只能授 bare＝全園。
    # 型別對齊 permscope01（PG=ARRAY(Text)，其餘 dialect=JSON）。
    scope_options = Column(
        JSON().with_variant(ARRAY(Text), "postgresql"),
        nullable=True,
        comment="scope-aware 權限的可選 scope（如 ['own_class','all']）；NULL=非 scope-aware",
    )
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_permission_definitions_group", "group_name"),)


class Role(Base):
    """角色表：取代 ROLE_TEMPLATES + ROLE_LABELS + ROLE_DESCRIPTIONS in-code dict。

    is_core=True：對應 (a) 之 7 個 ROLE_TEMPLATES（admin/principal/supervisor/hr/
    accountant/teacher/parent），由 alembic seed，admin 可改 label/description 但不可
    改 code / 不可改 permissions / 不可刪。
    """

    __tablename__ = "roles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(
        Text,
        nullable=False,
        unique=True,
        comment="對應 users.role 字串（如 admin / hr）",
    )
    label = Column(Text, nullable=False, comment="中文顯示名")
    description = Column(Text, nullable=True, comment="適用對象 / 一句話")
    permissions = Column(
        JSON().with_variant(ARRAY(Text), "postgresql"),
        nullable=False,
        default=list,
        comment="角色預設權限；['*'] = wildcard；與 users.permission_names 同 shape",
    )
    is_core = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now()
    )
