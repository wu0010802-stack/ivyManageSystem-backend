"""家長端才藝課登入版測試（Batch 7）。

涵蓋：
- list courses 含 enrolled_count / is_full
- register happy path：parent_phone 從 Guardian 自動帶、match_status='manual'
- register 額滿 → waitlist
- register 額滿且不允候補 → 400
- 同學期重複報名 → 400
- 非自己小孩報名 → 403
- my-registrations 僅列自己小孩
- payments：不揭露 operator
- confirm-promotion happy path
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.activity import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivityRegistrationSettings,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "activity.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import (
        make_sqlite_parent_db_override,
        register_sqlite_parent_rls_udfs,
    )

    # Phase 1f activity.py uses func.public_count_enrolled(course_id) which is
    # a Postgres SECURITY DEFINER function. Provide an inline SQLite UDF so
    # SQLite-backed tests can run the same code path.
    register_sqlite_parent_rls_udfs(db_engine)

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(
    session, *, line_user_id="UA", student_name="阿活", classroom_name="活力班"
):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()
    student = Student(
        student_id=f"S_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        phone="0911000111",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _create_course(
    session,
    *,
    name="繪畫",
    price=2000,
    capacity=2,
    school_year=115,
    semester=1,
    allow_waitlist=True,
):
    course = ActivityCourse(
        name=name,
        price=price,
        capacity=capacity,
        school_year=school_year,
        semester=semester,
        allow_waitlist=allow_waitlist,
        is_active=True,
    )
    session.add(course)
    session.flush()
    return course


def _create_supply(
    session,
    *,
    name="畫具",
    price=300,
    school_year=115,
    semester=1,
):
    supply = ActivitySupply(
        name=name,
        price=price,
        school_year=school_year,
        semester=semester,
        is_active=True,
    )
    session.add(supply)
    session.flush()
    return supply


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": user.token_version or 0,
        }
    )


class TestListCourses:
    def test_list_courses_with_enrolled_count(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, _, _ = _setup_family(session)
            _create_course(session, name="繪畫", capacity=2)
            _create_course(session, name="音樂", capacity=10)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert {i["name"] for i in items} == {"繪畫", "音樂"}
        for i in items:
            assert i["enrolled_count"] == 0
            assert i["is_full"] is False

    def test_list_courses_defaults_to_current_term(self, activity_client):
        # F2：不帶學期參數時應只回「當前學期」active 課程，而非跨所有學期，
        # 否則前端用第一筆課程決定報名學期可能被帶去報舊學期。對齊全模組
        # resolve_academic_term_filters 慣例。
        from utils.academic import resolve_current_academic_term

        cur_sy, cur_sem = resolve_current_academic_term()
        old_sy = cur_sy - 1
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, _, _ = _setup_family(session)
            _create_course(session, name="當期課", school_year=cur_sy, semester=cur_sem)
            _create_course(session, name="舊期課", school_year=old_sy, semester=cur_sem)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        names = {i["name"] for i in resp.json()["items"]}
        assert names == {"當期課"}


class TestRegister:
    def test_register_happy_path(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            session.commit()
            token = _parent_token(user)
            student_id = student.id
            course_id = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["match_status"] == "manual"
        assert data["pending_review"] is False
        assert len(data["courses"]) == 1
        assert data["courses"][0]["status"] == "enrolled"
        with session_factory() as session:
            reg = session.query(ActivityRegistration).first()
            assert reg.parent_phone == "0911000111"  # 從 Guardian 自動帶入
            assert reg.classroom_id is not None

    def test_register_when_full_goes_waitlist(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            user_b, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            user_c, _, student_c, _ = _setup_family(
                session, line_user_id="UC", student_name="C", classroom_name="C班"
            )
            course = _create_course(
                session, name="熱門課", capacity=2, allow_waitlist=True
            )
            session.commit()
            tokens = [_parent_token(u) for u in (user_a, user_b, user_c)]
            student_ids = [student_a.id, student_b.id, student_c.id]
            course_id = course.id

        for token, sid in zip(tokens, student_ids):
            resp = client.post(
                "/api/parent/activity/register",
                json={
                    "student_id": sid,
                    "school_year": 115,
                    "semester": 1,
                    "course_ids": [course_id],
                    "supply_ids": [],
                },
                cookies={"access_token": token},
            )
            assert resp.status_code == 201

        with session_factory() as session:
            statuses = sorted(
                rc.status for rc in session.query(RegistrationCourse).all()
            )
            assert statuses == ["enrolled", "enrolled", "waitlist"]

    def test_register_when_full_and_no_waitlist_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            user_b, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            course = _create_course(
                session, name="不可候補", capacity=1, allow_waitlist=False
            )
            session.commit()
            token_a = _parent_token(user_a)
            token_b = _parent_token(user_b)
            student_a_id = student_a.id
            student_b_id = student_b.id
            course_id = course.id

        client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_a_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_a},
        )
        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_b_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 400

    def test_register_other_child_returns_403(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            course = _create_course(session, name="繪畫")
            session.commit()
            token_a = _parent_token(user_a)
            student_b_id = student_b.id
            course_id = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_b_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_register_duplicate_in_term_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            c1 = _create_course(session, name="繪畫")
            c2 = _create_course(session, name="音樂")
            session.commit()
            token = _parent_token(user)
            student_id = student.id
            c1_id = c1.id
            c2_id = c2.id

        client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [c1_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [c2_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_register_rejects_cross_term_course(self, activity_client):
        # F1：報名 payload 學期與課程學期不符時應 400，不可把舊學期 active 課程
        # 掛到新學期報名（混入跨學期項目 + 用舊學期價虛灌應收）。對齊公開/後台端。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            old_course = _create_course(
                session, name="舊課", school_year=114, semester=2
            )
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = old_course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_register_rejects_cross_term_supply(self, activity_client):
        # F1：用品學期與 payload 學期不符時應 400。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            old_supply = _create_supply(
                session, name="舊用品", school_year=114, semester=2
            )
            session.commit()
            token = _parent_token(user)
            sid = student.id
            sup_id = old_supply.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [],
                "supply_ids": [sup_id],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_register_triggers_dashboard_cache_invalidation(
        self, activity_client, monkeypatch
    ):
        # F4：家長登入版報名原本完全不清任何 activity dashboard cache（連 summary 都沒清），
        # 導致招生達成率儀表板統計陳舊。報名後須觸發 dashboard 快取失效。
        # 註：家長端走 RLS session、handler 內不可 commit（get_parent_db 擁有交易，commit 會
        # 掉 SET LOCAL）；SQLite 測試下未提交寫鎖會讓真正的 DELETE fail-soft（生產環境 Postgres
        # 為獨立連線、正常清除），故此處以 spy 驗證「報名有觸發 invalidate」這個行為。
        from services.activity_service import activity_service

        calls = []
        orig = activity_service.invalidate_dashboard_caches

        def spy(session):
            calls.append(True)
            try:
                return orig(session)
            except Exception:
                return 0

        monkeypatch.setattr(activity_service, "invalidate_dashboard_caches", spy)

        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        assert calls, "家長端報名應觸發 dashboard 快取失效"

    def test_register_empty_courses_and_supplies_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_register_blocked_when_registration_closed(self, activity_client):
        # ② 登入家長報名也須受報名開放時間限制（比照公開端 _check_registration_open）。
        # 後台關閉報名（is_open=False）時，登入家長直接打 API 應被擋（400），
        # 不可繞過時段。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            session.add(ActivityRegistrationSettings(is_open=False))
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400
        assert "報名" in resp.json()["detail"]

    def test_register_locks_course_rows_for_update(self, activity_client, monkeypatch):
        # ① 家長報名須對課程列加行鎖（with_for_update），與公開端 public_register
        # 相同的鎖定策略，避免並發報名同時讀到最後名額都寫成 enrolled 造成超賣。
        # SQLite 下 FOR UPDATE 為 no-op（與 public_register 測試同），故此處以
        # spy 驗證「報名路徑確實對 ActivityCourse 請求行鎖」這個行為；真正的
        # 序列化由 PostgreSQL 行鎖提供（同 public_register）。
        from sqlalchemy.orm import Query

        locked_entities = []
        orig_with_for_update = Query.with_for_update

        def spy(self, *args, **kwargs):
            for desc in self.column_descriptions:
                entity = desc.get("entity")
                if entity is not None:
                    locked_entities.append(entity)
            return orig_with_for_update(self, *args, **kwargs)

        monkeypatch.setattr(Query, "with_for_update", spy)

        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        assert (
            ActivityCourse in locked_entities
        ), "家長報名應對 ActivityCourse 加 with_for_update 行鎖防超賣"

    def test_register_acquires_registration_advisory_lock(
        self, activity_client, monkeypatch
    ):
        # L2：同學生同學期唯一報名為 check-then-insert，DB partial unique 鍵含
        # parent_phone 不含 student_id，兩位不同 Guardian 並發替同生報名可雙雙
        # 通過。比照 admin match 以 acquire_activity_registration_lock 序列化
        # （SQLite no-op，故 spy 驗證「報名路徑確實取得報名 advisory lock」）。
        import api.parent_portal.activity as pa
        from services.activity_service import activity_service

        # dashboard 快取失效在 SQLite 測試下會與 RLS 未提交寫鎖搶連線（見
        # test_register_triggers_dashboard_cache_invalidation 註解）；本測試聚焦
        # advisory lock 行為，故把失效設為 no-op 避免無關的 DB-locked 噪音。
        monkeypatch.setattr(
            activity_service, "invalidate_dashboard_caches", lambda session: 0
        )

        calls = []

        def spy(session, **kwargs):
            calls.append(kwargs)

        monkeypatch.setattr(
            pa, "acquire_activity_registration_lock", spy, raising=False
        )

        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            sname = student.name  # 取在 commit 前，避免 expire_on_commit 觸發 refresh
            session.commit()
            token = _parent_token(user)
            sid, cid = student.id, course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        assert calls, "家長報名應取得同學生同學期報名 advisory lock（防並發重複報名）"
        assert calls[0]["student_name"] == sname
        assert calls[0]["school_year"] == 115
        assert calls[0]["semester"] == 1

    def test_register_response_includes_payment_fields(self, activity_client):
        # ④ 家長端報名 response 須直接回傳 total_amount / outstanding_amount /
        # payment_status，前端不再自行加總（避免漏扣已繳、誤計候補課程、漏算用品）。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫", price=2000)
            supply = _create_supply(session, name="畫具", price=300)
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = course.id
            sup_id = supply.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [sup_id],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        data = resp.json()
        # 應繳 = enrolled 課程 2000 + 用品 300；未繳
        assert data["total_amount"] == 2300
        assert data["paid_amount"] == 0
        assert data["outstanding_amount"] == 2300
        assert data["payment_status"] == "unpaid"

    def test_register_response_payment_fields_no_fee_for_waitlist(
        self, activity_client
    ):
        # ④ 全候補（無 enrolled 課程、無用品）→ 應繳為 0 → payment_status=no_fee、
        # outstanding_amount=0，前端據此顯示「免繳」而非誤標「未繳費」。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            # capacity=0 + 允許候補 → 報名直接進候補（不佔 enrolled，total=0）
            course = _create_course(
                session, name="候補課", capacity=0, allow_waitlist=True
            )
            session.commit()
            token = _parent_token(user)
            sid = student.id
            cid = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [cid],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["courses"][0]["status"] == "waitlist"
        assert data["total_amount"] == 0
        assert data["outstanding_amount"] == 0
        assert data["payment_status"] == "no_fee"


class TestMyRegistrationsAndPayments:
    def test_my_registrations_only_owned(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            session.add(
                ActivityRegistration(
                    student_name="A",
                    is_active=True,
                    school_year=115,
                    semester=1,
                    student_id=student_a.id,
                    parent_phone="0911",
                    pending_review=False,
                    match_status="manual",
                )
            )
            session.add(
                ActivityRegistration(
                    student_name="B",
                    is_active=True,
                    school_year=115,
                    semester=1,
                    student_id=student_b.id,
                    parent_phone="0922",
                    pending_review=False,
                    match_status="manual",
                )
            )
            session.commit()
            token = _parent_token(user_a)

        resp = client.get(
            "/api/parent/activity/my-registrations",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["student_name"] == "A"

    def test_payments_does_not_leak_operator(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=2000,
                    payment_date=date(2026, 4, 1),
                    payment_method="現金",
                    operator="財務人員",
                    receipt_no="POS-20260401-XYZ",
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id

        resp = client.get(
            f"/api/parent/activity/registrations/{reg_id}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["receipt_no"] == "POS-20260401-XYZ"
        assert "operator" not in items[0]

    def test_payments_visible_for_own_inactive_registration(self, activity_client):
        # code review（2026-06-24）：報名被軟刪（is_active=False，常見於退課/刪除後
        # 自動沖帳）後，家長仍須能查到自己的退費/付款歷史——與 admin 端
        # test_activity_inactive_accounting 的稽核需求一致。原本硬篩 is_active=True
        # 會讓家長對自己已軟刪的報名拿到 403，付款/退費紀錄在家長端憑空消失。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=False,  # 已軟刪
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="refund",
                    amount=1500,
                    payment_date=date(2026, 4, 2),
                    payment_method="系統補齊",
                    operator="財務人員",
                    receipt_no="REFUND-20260402-ABC",
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id

        resp = client.get(
            f"/api/parent/activity/registrations/{reg_id}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["type"] == "refund"
        assert items[0]["amount"] == 1500
        assert items[0]["receipt_no"] == "REFUND-20260402-ABC"

    def test_payments_other_family_inactive_registration_blocked(self, activity_client):
        # 放寬 is_active 篩選不可削弱枚舉防護：查別人家小孩的（軟刪）報名仍須
        # 回 generic 403（與「報名不存在」同樣的訊息），不得洩漏存在性。
        client, session_factory = activity_client
        with session_factory() as session:
            _attacker, _, _, _ = _setup_family(
                session, line_user_id="UB", student_name="攻擊者小孩"
            )
            victim_user, _, victim_student, _ = _setup_family(
                session,
                line_user_id="UV",
                student_name="受害者小孩",
                classroom_name="受害者班",
            )
            reg = ActivityRegistration(
                student_name=victim_student.name,
                is_active=False,
                school_year=115,
                semester=1,
                student_id=victim_student.id,
                parent_phone="0922",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="refund",
                    amount=999,
                    payment_date=date(2026, 4, 3),
                    payment_method="系統補齊",
                    operator="財務人員",
                    receipt_no="REFUND-SECRET",
                )
            )
            session.commit()
            attacker_token = _parent_token(_attacker)
            reg_id = reg.id

        resp = client.get(
            f"/api/parent/activity/registrations/{reg_id}/payments",
            cookies={"access_token": attacker_token},
        )
        assert resp.status_code == 403


class TestConfirmPromotion:
    def test_confirm_promotion_happy_path(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="promoted_pending",
                    price_snapshot=course.price,
                    promoted_at=datetime.now(),
                    confirm_deadline=datetime.now() + timedelta(hours=24),
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id
            course_id = course.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        with session_factory() as session:
            rc = session.query(RegistrationCourse).first()
            assert rc.status == "enrolled"

    def test_confirm_promotion_expired_releases_and_promotes_next(
        self, activity_client
    ):
        """Finding #3（2026-06-29 audit）：家長端確認撞到逾期時，須以獨立主庫特權
        session 同步釋出名額 + 遞補下一位候補（家長 RLS session 無權跨家庭寫入），
        回 410。完成 F3 留的缺口——不再依賴預設停用的 sweeper，避免名額被逾期
        pending 永久卡住。"""
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(
                session,
                line_user_id="UEXP",
                student_name="逾期娃",
                classroom_name="逾期班",
            )
            _, _, student_w, _ = _setup_family(
                session,
                line_user_id="UWAIT",
                student_name="候補娃",
                classroom_name="候補班",
            )
            course = _create_course(session, name="陶土", capacity=1)
            # 家長自己的逾期 promoted_pending
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="promoted_pending",
                    price_snapshot=course.price,
                    promoted_at=datetime.now() - timedelta(hours=50),
                    confirm_deadline=datetime.now() - timedelta(hours=1),  # 已逾期
                )
            )
            # 下一位候補（不同家庭）
            reg_w = ActivityRegistration(
                student_name=student_w.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student_w.id,
                parent_phone="0922",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg_w)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg_w.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=course.price,
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id
            course_id = course.id
            reg_w_id = reg_w.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token},
        )
        assert resp.status_code == 410, resp.text
        with session_factory() as session:
            # 逾期者已被釋出（刪除）
            rc_expired = (
                session.query(RegistrationCourse)
                .filter_by(registration_id=reg_id, course_id=course_id)
                .first()
            )
            assert rc_expired is None, "逾期 promoted_pending 應被同步釋出（刪除）"
            # 下一位遞補為 promoted_pending
            rc_next = (
                session.query(RegistrationCourse)
                .filter_by(registration_id=reg_w_id, course_id=course_id)
                .first()
            )
            assert (
                rc_next is not None and rc_next.status == "promoted_pending"
            ), "下一位候補應遞補為 promoted_pending"

    def test_confirm_promotion_other_child_returns_403(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            course = _create_course(session, name="X")
            reg_b = ActivityRegistration(
                student_name="B",
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student_b.id,
                parent_phone="0922",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg_b)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg_b.id,
                    course_id=course.id,
                    status="promoted_pending",
                )
            )
            session.commit()
            token_a = _parent_token(user_a)
            reg_b_id = reg_b.id
            course_id = course.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_b_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_confirm_promotion_terminal_student_returns_403(self, activity_client):
        """Finding (P1)：家長對已離校子女確認候補轉正 → 403（service 守衛
        STUDENT_TERMINAL 映射）。對齊直接報名的終態寫入守衛，且不長出幽靈
        enrolled（佔容量卻不出現在點名名冊）。"""
        from models.classroom import LIFECYCLE_WITHDRAWN

        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            # 子女已退學（終態）：is_active=False + lifecycle_status=withdrawn
            student.is_active = False
            student.lifecycle_status = LIFECYCLE_WITHDRAWN
            course = _create_course(session, name="繪畫")
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="promoted_pending",
                    price_snapshot=course.price,
                    promoted_at=datetime.now(),
                    confirm_deadline=datetime.now() + timedelta(hours=24),
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id
            course_id = course.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token},
        )
        assert resp.status_code == 403, resp.text
        with session_factory() as session:
            rc = session.query(RegistrationCourse).first()
            assert rc.status == "promoted_pending", "終態守衛須在改 status 前生效"


class TestRegisterPayloadDedupesIds:
    """LIFF 報名 payload 內重複 course_ids / supply_ids 在逐筆 insert 時會撞
    (registration_id, course_id/supply_id) 唯一鍵 → 裸 500。schema 層去重保序擋住。"""

    def test_dedupes_course_and_supply_ids_preserving_order(self):
        from api.parent_portal.activity import RegisterPayload

        p = RegisterPayload(
            student_id=1,
            school_year=114,
            semester=1,
            course_ids=[5, 5, 3, 5],
            supply_ids=[2, 2],
        )
        assert p.course_ids == [5, 3]  # 去重且保序
        assert p.supply_ids == [2]

    def test_no_dedup_needed_keeps_input(self):
        from api.parent_portal.activity import RegisterPayload

        p = RegisterPayload(
            student_id=1,
            school_year=114,
            semester=1,
            course_ids=[7, 8],
            supply_ids=[],
        )
        assert p.course_ids == [7, 8]
        assert p.supply_ids == []


class TestCapacityAndCountFixes:
    """Finding 4/6（2026-06-22）：家長端容量計數與 NULL 容量處理。"""

    def test_null_capacity_course_not_full(self, activity_client):
        """Finding 6 + Finding 5：capacity=NULL 的歷史課程應視為 30（其餘端口徑），
        不可用 `capacity or 0` 把 NULL→0 而全顯額滿。

        Finding 5（2026-06-23）：回傳的 capacity 亦改為 effective 值（NULL→30），
        與 is_full 口徑一致；原本只修 is_full、capacity 仍回 raw NULL → 前端顯示
        "enrolled/null"。故此處 capacity 斷言由 None 改為 30。"""
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, _, _ = _setup_family(session)
            course = _create_course(session, name="無上限課", capacity=30)
            # 模型 default=30 只在 INSERT 套用；既 flush 後改 None 再 commit
            # 會發 UPDATE SET capacity=NULL，重現歷史 NULL 資料。
            course.capacity = None
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        course = next(i for i in resp.json()["items"] if i["name"] == "無上限課")
        assert course["capacity"] == 30, "NULL capacity 回 effective 值（Finding 5）"
        assert course["is_full"] is False

    def test_effective_capacity_treats_null_as_default(self):
        """Finding 6 + 2026-06-23 口徑收斂：NULL→30（對齊全模組慣例），
        明確 0 維持 0（真的不開放名額），其餘原值。家長端報名與 is_full 共用
        utils.activity_constants.effective_capacity 單一來源（物件簽名）。"""
        from types import SimpleNamespace
        from api.parent_portal.activity import effective_capacity

        assert effective_capacity(SimpleNamespace(capacity=None)) == 30
        assert effective_capacity(SimpleNamespace(capacity=0)) == 0
        assert effective_capacity(SimpleNamespace(capacity=5)) == 5

    def test_rejected_registration_not_counted_as_enrolled(self, activity_client):
        """Finding 4：被拒絕（is_active=False）報名的 RC 仍是 enrolled，
        但 public_count_enrolled 不應計入，否則家長端誤判額滿並錯放候補。"""
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, _, _ = _setup_family(session)
            course = _create_course(
                session, name="容量一", capacity=1, allow_waitlist=True
            )
            session.flush()
            cid = course.id
            # 一筆被拒絕報名：is_active=False，但 RC 狀態仍是 enrolled
            rejected = ActivityRegistration(
                student_name="離園生",
                birthday="2020-01-01",
                is_active=False,
                match_status="rejected",
                school_year=115,
                semester=1,
            )
            session.add(rejected)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=rejected.id,
                    course_id=cid,
                    status="enrolled",
                    price_snapshot=2000,
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        course = next(i for i in resp.json()["items"] if i["name"] == "容量一")
        assert course["enrolled_count"] == 0, "被拒絕報名不應計入佔位"
        assert course["is_full"] is False


class TestActivityBootstrap:
    """GET /parent/activity/bootstrap 聚合：4 區塊須與對應單支端點輸出一致。"""

    def test_bootstrap_aggregates_match_single_endpoints(self, activity_client):
        from utils.academic import resolve_current_academic_term

        cur_sy, cur_sem = resolve_current_academic_term()
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(
                session, name="美術", school_year=cur_sy, semester=cur_sem
            )
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=cur_sy,
                semester=cur_sem,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=course.price,
                )
            )
            session.commit()
            token = _parent_token(user)

        cookies = {"access_token": token}
        boot = client.get("/api/parent/activity/bootstrap", cookies=cookies)
        assert boot.status_code == 200, boot.text
        data = boot.json()
        assert set(data) == {
            "registration_time",
            "courses",
            "registrations",
            "upcoming_sessions",
        }

        # 各區塊與對應單支端點完全一致（bootstrap 直接重用同 handler 邏輯）
        courses = client.get("/api/parent/activity/courses", cookies=cookies)
        regs = client.get("/api/parent/activity/my-registrations", cookies=cookies)
        upcoming = client.get("/api/parent/activity/upcoming-sessions", cookies=cookies)
        assert data["courses"] == courses.json()
        assert data["registrations"] == regs.json()
        assert data["upcoming_sessions"] == upcoming.json()

        # 實際有資料 + registration_time 為公開設定 payload dict
        assert data["courses"]["total"] >= 1
        assert data["registrations"]["total"] == 1
        assert isinstance(data["registration_time"], dict)
