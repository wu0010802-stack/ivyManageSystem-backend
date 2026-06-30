"""後台全域搜尋 /api/search 測試。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.search import router as search_router
from models.database import Base, Employee, User
from utils.auth import hash_password, create_access_token


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "search.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(search_router)
    try:
        with TestClient(app) as client:
            yield client, session_factory
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf


def _make_user(session_factory, *, username, role, permission_names):
    """建立 User（附一個最小 Employee），回傳 user_id 與 employee_id。"""
    s = session_factory()
    try:
        emp = Employee(employee_id=f"E-{username}", name=username, is_active=True)
        s.add(emp)
        s.flush()
        u = User(
            username=username,
            password_hash=hash_password("pw123456"),
            role=role,
            employee_id=emp.id,
            permission_names=permission_names,
            is_active=True,
        )
        s.add(u)
        s.flush()
        uid = u.id
        eid = emp.id
        s.commit()
        return uid, eid
    finally:
        s.close()


def _login(uid, eid, *, role, permission_names, username=""):
    """直接 mint JWT（仿 test_portal_search.py），繞過登入流程守衛。"""
    token = create_access_token(
        {
            "user_id": uid,
            "employee_id": eid,
            "role": role,
            "name": username,
            "permission_names": permission_names,
            "token_version": 0,
        }
    )
    return {"Authorization": f"Bearer {token}"}


def test_short_query_returns_empty(client_with_db):
    client, sf = client_with_db
    uid, eid = _make_user(sf, username="admin1", role="admin", permission_names=["*"])
    headers = _login(uid, eid, role="admin", permission_names=["*"])
    r = client.get("/api/search", params={"q": "a"}, headers=headers)  # < 2 字
    assert r.status_code == 200
    body = r.json()
    assert body["q"] == "a"
    assert body["students"] == [] and body["employees"] == []


def test_teacher_and_parent_are_forbidden(client_with_db):
    client, sf = client_with_db
    # teacher 走 portal/search、parent 走家長端，兩者都不可撞後台 /api/search
    for uname, role, perms in [
        ("teach1", "teacher", ["STUDENTS_READ:own_class"]),
        ("par1", "parent", []),
    ]:
        uid, eid = _make_user(sf, username=uname, role=role, permission_names=perms)
        headers = _login(uid, eid, role=role, permission_names=perms)
        r = client.get("/api/search", params={"q": "abc"}, headers=headers)
        assert r.status_code == 403, f"{role} 應被擋下 (got {r.status_code})"


def test_students_section_and_permission_gate(client_with_db):
    client, sf = client_with_db
    # 建班級 + 兩個學生
    from models.database import Classroom, Student

    s = sf()
    cr = Classroom(name="向日葵班", school_year=114, semester=1, is_active=True)
    s.add(cr)
    s.flush()
    s.add(
        Student(
            name="王小明",
            student_id="S001",
            classroom_id=cr.id,
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.add(
        Student(
            name="李大華",
            student_id="S002",
            classroom_id=cr.id,
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.commit()
    s.close()

    # 有 STUDENTS_READ → 搜得到
    uid, eid = _make_user(
        sf, username="reader", role="supervisor", permission_names=["STUDENTS_READ"]
    )
    h = _login(uid, eid, role="supervisor", permission_names=["STUDENTS_READ"])
    r = client.get("/api/search", params={"q": "王小"}, headers=h)
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["students"]]
    assert "王小明" in names and "李大華" not in names

    # 無 STUDENTS_READ → 學生區塊空
    uid2, eid2 = _make_user(
        sf, username="noread", role="accountant", permission_names=["FEES_READ"]
    )
    h2 = _login(uid2, eid2, role="accountant", permission_names=["FEES_READ"])
    r2 = client.get("/api/search", params={"q": "王小"}, headers=h2)
    assert r2.json()["students"] == []


def test_employees_section(client_with_db):
    client, sf = client_with_db
    from models.database import Employee

    s = sf()
    s.add(Employee(employee_id="E100", name="陳老師", is_active=True, title="教師"))
    s.add(Employee(employee_id="E200", name="林主任", is_active=False))  # 離職不出現
    s.commit()
    s.close()
    uid, eid = _make_user(
        sf, username="hr1", role="hr", permission_names=["EMPLOYEES_READ"]
    )
    h = _login(uid, eid, role="hr", permission_names=["EMPLOYEES_READ"])
    # 注意：單一 CJK 字（如「陳」）len==1，會被 MIN_QUERY_LEN=2 門檻擋掉，用 2 字
    r = client.get("/api/search", params={"q": "陳老"}, headers=h)
    rows = r.json()["employees"]
    assert any(x["name"] == "陳老師" and x["title"] == "教師" for x in rows)
    # 無 EMPLOYEES_READ → 空
    uid0, eid0 = _make_user(
        sf, username="hr0", role="supervisor", permission_names=["STUDENTS_READ"]
    )
    h0 = _login(uid0, eid0, role="supervisor", permission_names=["STUDENTS_READ"])
    assert (
        client.get("/api/search", params={"q": "陳老"}, headers=h0).json()["employees"]
        == []
    )


def test_guardians_section_masks_phone(client_with_db):
    client, sf = client_with_db
    from models.database import Classroom, Student, Guardian

    s = sf()
    cr = Classroom(name="A班", school_year=114, semester=1, is_active=True)
    s.add(cr)
    s.flush()
    stu = Student(
        name="王小明",
        student_id="S001",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status="active",
    )
    s.add(stu)
    s.flush()
    stu_id = stu.id  # session close 後 ORM 物件 detach，先取出 id
    s.add(
        Guardian(student_id=stu.id, name="王大華", phone="0912345678", is_primary=True)
    )
    s.commit()
    s.close()
    uid, eid = _make_user(
        sf, username="sup1", role="supervisor", permission_names=["GUARDIANS_READ"]
    )
    h = _login(uid, eid, role="supervisor", permission_names=["GUARDIANS_READ"])
    r = client.get("/api/search", params={"q": "王大"}, headers=h)
    rows = r.json()["guardians"]
    assert rows and rows[0]["name"] == "王大華"
    assert rows[0]["child_name"] == "王小明" and rows[0]["student_id"] == stu_id
    assert "0912345678" not in rows[0]["phone_masked"]  # 已遮罩


def test_classrooms_and_announcements_sections(client_with_db):
    client, sf = client_with_db
    from models.database import Classroom
    from models.event import Announcement

    # 先建 admin（取得 employee id 供 Announcement.created_by FK→employees.id 用）
    uid, eid = _make_user(sf, username="adm", role="admin", permission_names=["*"])
    s = sf()
    s.add(Classroom(name="彩虹班", school_year=114, semester=1, is_active=True))
    s.add(Announcement(title="彩虹班親師座談", content="內容", created_by=eid))
    s.commit()
    s.close()
    h = _login(uid, eid, role="admin", permission_names=["*"])
    r = client.get("/api/search", params={"q": "彩虹"}, headers=h)
    body = r.json()
    assert any(c["name"] == "彩虹班" for c in body["classrooms"])
    assert any(a["title"] == "彩虹班親師座談" for a in body["announcements"])


def test_fees_activity_recruitment_sections(client_with_db):
    client, sf = client_with_db
    from models.fees import StudentFeeRecord
    from models.activity import ActivityRegistration
    from models.recruitment import RecruitmentVisit

    s = sf()
    s.add(
        StudentFeeRecord(
            student_id=1,
            student_name="趙小妹",
            classroom_name="A班",
            fee_item_name="月費",
            period="114-1",
            status="unpaid",
            amount_due=5000,
        )
    )
    s.add(
        ActivityRegistration(
            student_name="趙小妹",
            class_name="A班",
            parent_phone="0911222333",
            is_active=True,
            match_status="matched",
        )
    )
    s.add(
        RecruitmentVisit(
            child_name="趙小寶",
            target_school_year=115,
            enrolled=False,
            month="115.03",
        )
    )
    s.commit()
    s.close()
    uid, eid = _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(uid, eid, role="admin", permission_names=["*"])
    rf = client.get("/api/search", params={"q": "趙小妹"}, headers=h).json()
    assert any(x["student_name"] == "趙小妹" for x in rf["fees"])
    assert any(x["student_name"] == "趙小妹" for x in rf["activity_registrations"])
    rr = client.get("/api/search", params={"q": "趙小寶"}, headers=h).json()
    assert any(x["child_name"] == "趙小寶" for x in rr["recruitment"])


def test_search_writes_read_audit(client_with_db):
    client, sf = client_with_db
    uid, eid = _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(uid, eid, role="admin", permission_names=["*"])
    r = client.get("/api/search", params={"q": "測試"}, headers=h)
    assert r.status_code == 200
    # 查 audit 表是否有一筆 admin_global_search READ
    from models.audit import AuditLog

    s = sf()
    try:
        rows = (
            s.query(AuditLog)
            .filter(AuditLog.entity_type == "admin_global_search")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].action == "READ"
    finally:
        s.close()


def test_student_scope_limits_to_own_class(client_with_db):
    """own_class scope 角色（非 admin/teacher）只看到自己擔任班級的學生。"""
    client, sf = client_with_db
    from models.database import Classroom, Student

    # 先建受限使用者（STUDENTS_READ:own_class，scope-aware），取得 employee id
    uid, eid = _make_user(
        sf,
        username="owncls",
        role="supervisor",
        permission_names=["STUDENTS_READ:own_class"],
    )
    s = sf()
    my_cr = Classroom(
        name="我的班", school_year=114, semester=1, is_active=True, head_teacher_id=eid
    )
    other_cr = Classroom(name="別人的班", school_year=114, semester=1, is_active=True)
    s.add(my_cr)
    s.add(other_cr)
    s.flush()
    s.add(
        Student(
            name="阿甲同學",
            student_id="SC1",
            classroom_id=my_cr.id,
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.add(
        Student(
            name="阿乙同學",
            student_id="SC2",
            classroom_id=other_cr.id,
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.commit()
    s.close()
    h = _login(
        uid, eid, role="supervisor", permission_names=["STUDENTS_READ:own_class"]
    )
    names = [
        x["name"]
        for x in client.get("/api/search", params={"q": "同學"}, headers=h).json()[
            "students"
        ]
    ]
    assert "阿甲同學" in names and "阿乙同學" not in names


def test_terminal_students_excluded(client_with_db):
    """畢業/退學/轉出（終態）學生不出現在全域搜尋（即使 admin）。"""
    client, sf = client_with_db
    from models.database import Classroom, Student

    s = sf()
    cr = Classroom(name="終態班", school_year=114, semester=1, is_active=True)
    s.add(cr)
    s.flush()
    s.add(
        Student(
            name="畢業生甲",
            student_id="GR1",
            classroom_id=cr.id,
            is_active=True,
            lifecycle_status="graduated",
        )
    )
    s.add(
        Student(
            name="在校生甲",
            student_id="AC1",
            classroom_id=cr.id,
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.commit()
    s.close()
    uid, eid = _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(uid, eid, role="admin", permission_names=["*"])
    names = [
        x["name"]
        for x in client.get("/api/search", params={"q": "生甲"}, headers=h).json()[
            "students"
        ]
    ]
    assert "在校生甲" in names and "畢業生甲" not in names


def test_per_category_limit(client_with_db):
    """同一類別最多回 SECTION_LIMIT(8) 筆。"""
    client, sf = client_with_db
    from models.database import Employee

    s = sf()
    for i in range(10):
        s.add(
            Employee(employee_id=f"EE{i:02d}", name=f"測試員工{i:02d}", is_active=True)
        )
    s.commit()
    s.close()
    uid, eid = _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(uid, eid, role="admin", permission_names=["*"])
    rows = client.get("/api/search", params={"q": "測試員工"}, headers=h).json()[
        "employees"
    ]
    assert len(rows) == 8


def test_activity_phone_reverse_lookup_blocked_without_guardians_read(client_with_db):
    """缺 GUARDIANS_READ：只有 ACTIVITY_READ 不應能用家長手機反查才藝報名學生。

    與才藝列表 PII policy（_build_registration_filter_query / GUARDIANS_READ）一致，
    全域搜尋的 _search_activity 同樣須把 parent_phone clause 收進 GUARDIANS_READ。
    """
    client, sf = client_with_db
    from models.activity import ActivityRegistration

    s = sf()
    # 姓名/班級皆不含手機字串，故命中只可能來自 parent_phone 比對（側信道反查）
    s.add(
        ActivityRegistration(
            student_name="林大華",
            class_name="大班",
            parent_phone="0988777666",
            is_active=True,
            match_status="matched",
        )
    )
    s.commit()
    s.close()
    uid, eid = _make_user(
        sf, username="actonly", role="hr", permission_names=["ACTIVITY_READ"]
    )
    h = _login(uid, eid, role="hr", permission_names=["ACTIVITY_READ"])
    rows = client.get("/api/search", params={"q": "0988"}, headers=h).json()[
        "activity_registrations"
    ]
    assert rows == [], "缺 GUARDIANS_READ 不應能以家長手機反查才藝報名學生"


def test_activity_phone_search_allowed_with_guardians_read(client_with_db):
    """持 GUARDIANS_READ：仍可用家長手機搜尋才藝報名（與才藝列表 PII policy 一致）。"""
    client, sf = client_with_db
    from models.activity import ActivityRegistration

    s = sf()
    s.add(
        ActivityRegistration(
            student_name="林大華",
            class_name="大班",
            parent_phone="0988777666",
            is_active=True,
            match_status="matched",
        )
    )
    s.commit()
    s.close()
    perms = ["ACTIVITY_READ", "GUARDIANS_READ"]
    uid, eid = _make_user(sf, username="actguard", role="hr", permission_names=perms)
    h = _login(uid, eid, role="hr", permission_names=perms)
    rows = client.get("/api/search", params={"q": "0988"}, headers=h).json()[
        "activity_registrations"
    ]
    assert (
        len(rows) == 1 and rows[0]["student_name"] == "林大華"
    ), "持 GUARDIANS_READ 仍可用家長手機搜尋才藝報名"


def test_activity_name_search_unaffected_by_guardian_perm(client_with_db):
    """姓名搜尋與 GUARDIANS_READ 無關（修法只移除手機 clause，不關閉整個才藝搜尋）。"""
    client, sf = client_with_db
    from models.activity import ActivityRegistration

    s = sf()
    s.add(
        ActivityRegistration(
            student_name="林大華",
            class_name="大班",
            parent_phone="0988777666",
            is_active=True,
            match_status="matched",
        )
    )
    s.commit()
    s.close()
    uid, eid = _make_user(
        sf, username="actname", role="hr", permission_names=["ACTIVITY_READ"]
    )
    h = _login(uid, eid, role="hr", permission_names=["ACTIVITY_READ"])
    rows = client.get("/api/search", params={"q": "林大華"}, headers=h).json()[
        "activity_registrations"
    ]
    assert (
        len(rows) == 1 and rows[0]["student_name"] == "林大華"
    ), "姓名搜尋不受 GUARDIANS_READ 影響"


# ── Task 2 新增：多關鍵字 AND + 相關性排序 ─────────────────────────────────────


def test_global_search_multi_token_and(client_with_db):
    """多關鍵字：『林 美』需 name 同時含『林』與『美』才命中。"""
    client, sf = client_with_db
    uid, eid = _make_user(sf, username="adm_mt", role="admin", permission_names=["*"])
    from models.database import Employee

    s = sf()
    s.add(Employee(employee_id="T001", name="林美麗", is_active=True))
    s.add(Employee(employee_id="T002", name="林大同", is_active=True))
    s.commit()
    s.close()
    h = _login(uid, eid, role="admin", permission_names=["*"])
    resp = client.get("/api/search", params={"q": "林 美"}, headers=h)
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["employees"]]
    assert "林美麗" in names
    assert "林大同" not in names


def test_global_search_relevance_order(client_with_db):
    """相關性：完全符合 < 前綴 < 包含。

    查 '王小'（2字元，≥ MIN_QUERY_LEN）：
      '王小'   → exact match  → relevance_key=0
      '王小明'  → prefix match → relevance_key=1
      '大王小'  → contains    → relevance_key=2
    DB order_by name.asc() 原本依 code point 排 ['大王小', '王小', '王小明']，
    _finalize 應覆蓋成相關性排序。
    """
    client, sf = client_with_db
    uid, eid = _make_user(sf, username="adm_rv", role="admin", permission_names=["*"])
    from models.database import Employee

    s = sf()
    s.add(Employee(employee_id="T101", name="王小", is_active=True))
    s.add(Employee(employee_id="T102", name="王小明", is_active=True))
    s.add(Employee(employee_id="T103", name="大王小", is_active=True))
    s.commit()
    s.close()
    h = _login(uid, eid, role="admin", permission_names=["*"])
    resp = client.get("/api/search", params={"q": "王小"}, headers=h)
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["employees"]]
    assert "王小" in names and "王小明" in names and "大王小" in names
    assert names.index("王小") < names.index("王小明") < names.index("大王小")


def test_global_search_wildcard_escaped(client_with_db):
    """escape 回歸：搜 '%%' 不應命中全部（% 被跳脫為字面字元）。"""
    client, sf = client_with_db
    uid, eid = _make_user(sf, username="adm_wc", role="admin", permission_names=["*"])
    from models.database import Employee

    s = sf()
    s.add(Employee(employee_id="T201", name="王小明", is_active=True))
    s.commit()
    s.close()
    h = _login(uid, eid, role="admin", permission_names=["*"])
    resp = client.get("/api/search", params={"q": "%%"}, headers=h)
    assert resp.status_code == 200
    # '%%' 兩字元 ≥ MIN_QUERY_LEN，但跳脫後不 match 不含字面 '%' 的姓名
    assert resp.json()["employees"] == []


def test_wildcard_query_is_escaped_not_match_all(client_with_db):
    """搜 `%%` / `__` 不得 match-all；萬用字元應視為字面字元（LIKE escape）。"""
    client, sf = client_with_db
    from models.database import Student

    s = sf()
    s.add(
        Student(
            name="王小明",
            student_id="S101",
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.add(
        Student(
            name="出席100%生",
            student_id="S102",
            is_active=True,
            lifecycle_status="active",
        )
    )
    s.commit()
    s.close()
    uid, eid = _make_user(
        sf, username="wild", role="admin", permission_names=["STUDENTS_READ"]
    )
    h = _login(uid, eid, role="admin", permission_names=["STUDENTS_READ"])

    # `%%`：資料中無「連續兩個字面 %」→ 必須零命中（match-all 即漏洞）
    r = client.get("/api/search", params={"q": "%%"}, headers=h)
    assert r.status_code == 200
    assert r.json()["students"] == [], "`%%` 被當 LIKE 萬用字元 match-all"

    # `__`：同理不得 match 任意兩字元
    r = client.get("/api/search", params={"q": "__"}, headers=h)
    assert r.json()["students"] == [], "`__` 被當 LIKE 萬用字元 match-all"

    # positive witness：字面含 % 的資料仍搜得到（escape 沒把功能弄壞）
    r = client.get("/api/search", params={"q": "100%"}, headers=h)
    names = [x["name"] for x in r.json()["students"]]
    assert names == ["出席100%生"]
