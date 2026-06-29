"""tests/test_parent_activity_no_phone_token_2026_06_29.py

第三輪才藝 review F3（中）：家長端報名允許 Guardian 電話為空/過短，但仍無條件
產生 query_token 並回傳 → 前端據此顯示「管理我的報名」公開連結。然而公開查詢
（/public/query、/public/query-by-token）的 parent_phone 強制 min_length=8，
故電話 <8 的報名其管理連結永久無法通過驗證（死連結）。

決策（業主）：後端不發 token。報名時 Guardian 無可用電話（strip 後長度 <8，
公開路徑本就要求 phone≥8）→ query_token 回 None、不寫 query_token_hash。
前端 `token ? buildPublicEditUrl(...) : ''` 自然隱藏連結。登入家長仍可於
portal「我的報名」分頁直接管理，能力不受影響；且公開 mutation 對此報名本就
因 phone<8 無法通過，故不發 token 不削弱安全（與舊報名無 token 同態）。
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.activity import ActivityRegistration
from models.database import Classroom, Guardian, Student, User
from tests.test_parent_activity_query_token import (  # noqa: F401
    activity_client,
    _create_course,
    _parent_token,
    _register,
)


def _setup_family_with_phone(session, *, phone, line_uid="UB"):
    """造一個家庭，Guardian.phone 可指定（含 None/過短）。"""
    user = User(
        username=f"parent_line_{line_uid}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id=line_uid,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = Classroom(name="活力班", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id=f"S_{line_uid}",
        name="阿活",
        birthday=date(2020, 3, 1),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        phone=phone,
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, student


def test_register_with_none_phone_returns_null_token(activity_client):
    """Guardian 電話為 None → response query_token 應為 None（不顯示死連結）。"""
    client, sf = activity_client
    with sf() as s:
        user, student = _setup_family_with_phone(s, phone=None)
        course = _create_course(s)
        s.commit()
        tok = _parent_token(user)
        sid, cid = student.id, course.id

    resp = _register(client, student_id=sid, course_id=cid, token=tok)
    assert resp.status_code == 201, resp.text
    assert resp.json().get("query_token") is None, resp.text


def test_register_with_short_phone_returns_null_token(activity_client):
    """Guardian 電話 strip 後 <8 碼 → 公開查詢本就要求 ≥8，連結無用 → token None。"""
    client, sf = activity_client
    with sf() as s:
        user, student = _setup_family_with_phone(s, phone="12345")
        course = _create_course(s)
        s.commit()
        tok = _parent_token(user)
        sid, cid = student.id, course.id

    resp = _register(client, student_id=sid, course_id=cid, token=tok)
    assert resp.status_code == 201, resp.text
    assert resp.json().get("query_token") is None, resp.text


def test_no_phone_registration_has_null_token_hash(activity_client):
    """不發 token 時 DB query_token_hash / issued_at 皆為 None（與舊報名同態）。"""
    client, sf = activity_client
    with sf() as s:
        user, student = _setup_family_with_phone(s, phone=None)
        course = _create_course(s)
        s.commit()
        tok = _parent_token(user)
        sid, cid = student.id, course.id

    _register(client, student_id=sid, course_id=cid, token=tok)
    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(student_id=sid).first()
        assert reg.query_token_hash is None
        assert reg.query_token_issued_at is None


def test_register_with_valid_phone_still_returns_token(activity_client):
    """反回歸：有可用電話（≥8）的報名仍照常發 token，能力不變。"""
    client, sf = activity_client
    with sf() as s:
        user, student = _setup_family_with_phone(s, phone="0911000222")
        course = _create_course(s)
        s.commit()
        tok = _parent_token(user)
        sid, cid = student.id, course.id

    resp = _register(client, student_id=sid, course_id=cid, token=tok)
    assert resp.status_code == 201, resp.text
    assert resp.json().get("query_token"), resp.text
    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(student_id=sid).first()
        assert reg.query_token_hash is not None
        assert reg.query_token_issued_at is not None
