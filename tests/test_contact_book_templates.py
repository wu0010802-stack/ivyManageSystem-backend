"""聯絡簿範本（ContactBookTemplate）CRUD 測試。

涵蓋：
- 教師建立 / 列出 / 編輯 / 刪除個人範本
- 教師不能建立 shared 範本（無 PORTFOLIO_PUBLISH）
- 主管可建立 shared 範本，亦可 promote 個人範本
- 個人範本只可 owner 編輯，他人不可
- 列表只看到自己 personal + 全部 shared
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from models.database import Base, Employee, User
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def tpl_client(tmp_path):
    db_path = tmp_path / "tpl.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as c:
        yield c, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _make_teacher(session, *, username: str, perms: int) -> tuple[User, Employee]:
    emp = Employee(
        employee_id=f"E_{username}",
        name=username,
        is_active=True,
        base_salary=30000,
    )
    session.add(emp)
    session.flush()
    u = User(
        username=username,
        password_hash="!",
        role="teacher",
        employee_id=emp.id,
        permissions=perms,
        is_active=True,
        token_version=0,
    )
    session.add(u)
    session.flush()
    return u, emp


def _token(user: User, employee_id: int, perms: int) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": employee_id,
            "role": "teacher",
            "name": user.username,
            "permissions": perms,
            "token_version": user.token_version or 0,
        }
    )


# ════════════════════════════════════════════════════════════════════════
# Personal template — 教師基本流
# ════════════════════════════════════════════════════════════════════════


class TestPersonalTemplate:
    def _setup_teacher(self, sf):
        write_perm = int(
            Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
        )
        with sf() as session:
            user, emp = _make_teacher(session, username="t1", perms=write_perm)
            session.commit()
            return _token(user, emp.id, write_perm), user.id

    def test_create_and_list_personal(self, tpl_client):
        client, sf = tpl_client
        tk, _ = self._setup_teacher(sf)
        rsp = client.post(
            "/api/portal/contact-book/templates",
            json={
                "name": "今日很棒",
                "scope": "personal",
                "fields": {
                    "mood": "happy",
                    "teacher_note": "今日表現良好",
                    "learning_highlight": "完成所有活動",
                },
            },
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 201, rsp.text
        body = rsp.json()
        assert body["scope"] == "personal"
        assert body["fields"]["mood"] == "happy"
        assert body["fields"]["teacher_note"] == "今日表現良好"
        assert body["is_archived"] is False

        lst = client.get(
            "/api/portal/contact-book/templates", cookies={"access_token": tk}
        )
        assert lst.status_code == 200
        items = lst.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "今日很棒"

    def test_teacher_cannot_create_shared(self, tpl_client):
        """無 PORTFOLIO_PUBLISH 不能建 shared。"""
        client, sf = tpl_client
        tk, _ = self._setup_teacher(sf)
        rsp = client.post(
            "/api/portal/contact-book/templates",
            json={
                "name": "通用",
                "scope": "shared",
                "fields": {"teacher_note": "x"},
            },
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403

    def test_update_own_template(self, tpl_client):
        client, sf = tpl_client
        tk, _ = self._setup_teacher(sf)
        created = client.post(
            "/api/portal/contact-book/templates",
            json={
                "name": "v1",
                "scope": "personal",
                "fields": {"teacher_note": "v1"},
            },
            cookies={"access_token": tk},
        ).json()
        rsp = client.patch(
            f"/api/portal/contact-book/templates/{created['id']}",
            json={
                "name": "v2",
                "fields": {"teacher_note": "v2", "mood": "happy"},
            },
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200
        body = rsp.json()
        assert body["name"] == "v2"
        assert body["fields"]["teacher_note"] == "v2"
        assert body["fields"]["mood"] == "happy"

    def test_other_teacher_cannot_modify(self, tpl_client):
        """另一個沒有 PUBLISH 權限的教師不可改我的範本。"""
        client, sf = tpl_client
        tk, _ = self._setup_teacher(sf)
        created = client.post(
            "/api/portal/contact-book/templates",
            json={
                "name": "私有",
                "scope": "personal",
                "fields": {"teacher_note": "x"},
            },
            cookies={"access_token": tk},
        ).json()

        # 另一個教師
        write_perm = int(
            Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
        )
        with sf() as session:
            u2, e2 = _make_teacher(session, username="t2", perms=write_perm)
            session.commit()
            tk2 = _token(u2, e2.id, write_perm)

        rsp = client.patch(
            f"/api/portal/contact-book/templates/{created['id']}",
            json={"name": "壞改"},
            cookies={"access_token": tk2},
        )
        assert rsp.status_code == 403

    def test_archive_template(self, tpl_client):
        client, sf = tpl_client
        tk, _ = self._setup_teacher(sf)
        created = client.post(
            "/api/portal/contact-book/templates",
            json={"name": "x", "scope": "personal", "fields": {}},
            cookies={"access_token": tk},
        ).json()
        d = client.delete(
            f"/api/portal/contact-book/templates/{created['id']}",
            cookies={"access_token": tk},
        )
        assert d.status_code == 200
        # 列表預設不含已封存
        lst = client.get(
            "/api/portal/contact-book/templates", cookies={"access_token": tk}
        ).json()
        assert lst["items"] == []
        # 帶 include_archived 看得到
        lst2 = client.get(
            "/api/portal/contact-book/templates?include_archived=true",
            cookies={"access_token": tk},
        ).json()
        assert len(lst2["items"]) == 1
        assert lst2["items"][0]["is_archived"] is True


# ════════════════════════════════════════════════════════════════════════
# Shared template — 主管權限
# ════════════════════════════════════════════════════════════════════════


class TestSharedTemplate:
    def _setup_supervisor(self, sf):
        sup_perm = int(
            Permission.PORTFOLIO_READ.value
            | Permission.PORTFOLIO_WRITE.value
            | Permission.PORTFOLIO_PUBLISH.value
        )
        with sf() as session:
            u, e = _make_teacher(session, username="sup", perms=sup_perm)
            session.commit()
            return _token(u, e.id, sup_perm), u.id

    def test_supervisor_can_create_shared(self, tpl_client):
        client, sf = tpl_client
        tk, _ = self._setup_supervisor(sf)
        rsp = client.post(
            "/api/portal/contact-book/templates",
            json={
                "name": "全園通用",
                "scope": "shared",
                "fields": {"teacher_note": "今日活動順利"},
            },
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 201
        body = rsp.json()
        assert body["scope"] == "shared"
        assert body["owner_user_id"] is None

    def test_promote_personal_to_shared(self, tpl_client):
        """主管可把任意個人範本 promote 為 shared。"""
        client, sf = tpl_client

        # 教師建個人
        tch_perm = int(
            Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
        )
        with sf() as session:
            tu, te = _make_teacher(session, username="tch", perms=tch_perm)
            session.commit()
            tch_tk = _token(tu, te.id, tch_perm)
        created = client.post(
            "/api/portal/contact-book/templates",
            json={"name": "好範本", "scope": "personal", "fields": {}},
            cookies={"access_token": tch_tk},
        ).json()

        # 主管 promote
        sup_tk, _ = self._setup_supervisor(sf)
        rsp = client.post(
            f"/api/portal/contact-book/templates/{created['id']}/promote",
            cookies={"access_token": sup_tk},
        )
        assert rsp.status_code == 200
        body = rsp.json()
        assert body["scope"] == "shared"
        assert body["owner_user_id"] is None

    def test_promote_already_shared_is_400(self, tpl_client):
        client, sf = tpl_client
        tk, _ = self._setup_supervisor(sf)
        created = client.post(
            "/api/portal/contact-book/templates",
            json={"name": "x", "scope": "shared", "fields": {}},
            cookies={"access_token": tk},
        ).json()
        rsp = client.post(
            f"/api/portal/contact-book/templates/{created['id']}/promote",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 400

    def test_teacher_sees_shared_templates_in_list(self, tpl_client):
        """教師列表看到自己 personal + 全部 shared。"""
        client, sf = tpl_client
        # 主管建 shared
        sup_tk, _ = self._setup_supervisor(sf)
        client.post(
            "/api/portal/contact-book/templates",
            json={"name": "全園", "scope": "shared", "fields": {}},
            cookies={"access_token": sup_tk},
        )

        # 教師建 personal + 看到 shared
        tch_perm = int(
            Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
        )
        with sf() as session:
            tu, te = _make_teacher(session, username="tch", perms=tch_perm)
            session.commit()
            tch_tk = _token(tu, te.id, tch_perm)
        client.post(
            "/api/portal/contact-book/templates",
            json={"name": "我的", "scope": "personal", "fields": {}},
            cookies={"access_token": tch_tk},
        )

        items = client.get(
            "/api/portal/contact-book/templates", cookies={"access_token": tch_tk}
        ).json()["items"]
        names = {i["name"] for i in items}
        assert "全園" in names
        assert "我的" in names
