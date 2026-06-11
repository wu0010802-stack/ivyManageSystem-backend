"""P0 回歸：列出「permission_names 含某權限」的 active user。

`User.permission_names` 是 `JSON().with_variant(ARRAY(Text), "postgresql")`。
PostgreSQL **不可**用 `.contains()`——該欄基底型別 JSON 的 comparator 會生成畸形
`permission_names LIKE '%' || ARRAY[...]::TEXT[] || '%'`，真實 PG 報
`malformed array literal` 並中止整個交易（2026-06-06 教師 portal 送假/加班/
補打卡全 500 的根因；pytest 跑 SQLite 走另一條 app-layer 分支故照不到）。
"""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base, User
from utils.auth import hash_password
from utils.permissions import (
    _permission_names_contains,
    list_active_user_ids_with_permission,
)


def test_pg_permission_filter_uses_array_containment_not_like():
    """對 PostgreSQL dialect 編譯時必須生成 array 包含運算子 @>，而非畸形 LIKE。

    這是唯一能在 SQLite 測試環境抓到此 PG 專屬 bug 的方式：直接斷言對
    PostgreSQL dialect 編譯出的 SQL。
    """
    expr = _permission_names_contains("LEAVES_WRITE")
    sql = str(expr.compile(dialect=postgresql.dialect()))
    assert "@>" in sql, f"應生成 array 包含運算子 @>，實際：{sql}"
    assert "LIKE" not in sql.upper(), f"不可生成畸形 LIKE，實際：{sql}"


def _make_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_list_active_user_ids_matches_only_explicit_permission():
    """語意：僅匹配顯式列出該權限的 active 帳號；NULL-perm 與 inactive 不算。"""
    session = _make_session()
    session.add_all(
        [
            User(
                username="u_has",
                password_hash=hash_password("x"),
                role="hr",
                permission_names=["LEAVES_WRITE", "OVERTIME_READ"],
                is_active=True,
            ),
            User(
                username="u_other",
                password_hash=hash_password("x"),
                role="hr",
                permission_names=["OVERTIME_READ"],
                is_active=True,
            ),
            User(
                username="u_null",
                password_hash=hash_password("x"),
                role="hr",
                permission_names=None,
                is_active=True,
            ),
            User(
                username="u_inactive",
                password_hash=hash_password("x"),
                role="hr",
                permission_names=["LEAVES_WRITE"],
                is_active=False,
            ),
        ]
    )
    session.commit()

    ids = list_active_user_ids_with_permission(session, "LEAVES_WRITE")
    names = {session.get(User, i).username for i in ids}
    assert names == {"u_has"}
    session.close()
