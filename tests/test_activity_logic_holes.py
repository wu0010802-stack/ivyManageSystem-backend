"""本次才藝系統邏輯漏洞修復的回歸測試（2026-04-22）

覆蓋：
- M1: 同家長同學生同學期 is_active=TRUE 的 partial unique index
- M2: 學生離園自動沖帳會寫 RegistrationChange 軌跡
- L3: 生日格式/範圍驗證（Pydantic 層）
- L4: /public/update 換手機號時擋住與其他報名衝突
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationChange,
    Student,
)
from utils.academic import resolve_current_academic_term


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "logic-holes.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_term():
    return resolve_current_academic_term()


def _seed_basic(session, sy, sem, *, classroom_active=True):
    classroom = Classroom(
        name="大象班", is_active=classroom_active, school_year=sy, semester=sem
    )
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋",
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    session.commit()
    return classroom


def _public_register_payload(
    *,
    name="王小明",
    birthday="2020-05-10",
    phone="0912345678",
    class_="大象班",
    course_name="圍棋",
):
    return {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": class_,
        "courses": [{"name": course_name, "price": "1"}],
        "supplies": [],
    }


# ═══════════════════════════════════════════════════════════════════════════
# M1: partial unique index（防併發重複報名）
# ═══════════════════════════════════════════════════════════════════════════


class TestActiveRegistrationUniqueIndex:
    def test_db_blocks_duplicate_active_registration_same_family(self, client):
        """同家長同學生同學期直接 INSERT 兩筆 is_active=TRUE → DB 擋下第二筆。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add_all(
                [
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0912345678",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0912345678",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                s.commit()

    def test_db_allows_same_name_birthday_with_different_parent_phone(self, client):
        """不同家長但同姓同生日的兩個小孩（極端少見）在 DB 層仍可並存。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add_all(
                [
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0911111111",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0922222222",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                ]
            )
            s.commit()
            assert (
                s.query(ActivityRegistration)
                .filter(ActivityRegistration.is_active.is_(True))
                .count()
                == 2
            )

    def test_db_allows_reregister_after_soft_delete(self, client):
        """軟刪除後同家長可再建立新的有效報名（partial index WHERE is_active=1）。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add(
                ActivityRegistration(
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=False,  # 已軟刪
                )
            )
            s.add(
                ActivityRegistration(
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()  # 不應拋 IntegrityError

    def test_public_register_second_submit_silent_success_no_dup_row(self, client):
        """應用層：家長連送兩次相同資料且未匹配學生身分時，第二次走 silent-success
        （F-030 anti-enumeration），不再回 400；但 DB 仍只保留 1 筆，避免堆出大量重複報名。

        已驗證身分（matched）的家長第二次仍會看到 400 明確訊息，由 F-030 covered tests
        （test_misc_medium_authz）確認；此處覆蓋未驗證身分的 silent-success path。
        """
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r1 = c.post("/api/activity/public/register", json=_public_register_payload())
        assert r1.status_code == 201, r1.text
        r2 = c.post("/api/activity/public/register", json=_public_register_payload())
        assert r2.status_code == 201
        with sf() as s:
            count = (
                s.query(ActivityRegistration)
                .filter(ActivityRegistration.is_active.is_(True))
                .count()
            )
        assert count == 1, f"silent-success 應保留只有 1 筆，實際 {count}"

    def test_public_register_different_phone_same_name_not_swallowed(self, client):
        """P2-5：同名同生日但不同家長電話的第二個合法家庭，公開報名須真的寫入。

        app 層 existing dedup 原只比 name+birthday+term，與含 parent_phone 的 DB
        唯一索引不一致。第二個不同電話、未匹配在籍學生的家庭會走 silent-success
        被靜默吞掉（家長看到假成功、DB 沒寫入）。修法把 parent_phone 納入 existing
        比對後，第二筆應正常寫入。
        """
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r1 = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        assert r1.status_code == 201, r1.text
        # 同名同生日、不同電話的第二個家庭（未匹配在籍學生）
        r2 = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0922222222"),
        )
        assert r2.status_code == 201, r2.text
        with sf() as s:
            count = (
                s.query(ActivityRegistration)
                .filter(ActivityRegistration.is_active.is_(True))
                .count()
            )
        assert count == 2, f"不同電話的合法第二筆應寫入，實際 {count}"


# ═══════════════════════════════════════════════════════════════════════════
# M2: 學生離園自動沖帳 → log_change 軌跡
# ═══════════════════════════════════════════════════════════════════════════


class TestDeactivateAutoRefundLogged:
    def test_auto_refund_writes_registration_change(self, client):
        """sync_registrations_on_student_deactivate 自動寫退費時會留 RegistrationChange。"""
        _, sf = client
        sy, sem = _seed_term()
        from api.activity._shared import sync_registrations_on_student_deactivate

        with sf() as s:
            student = Student(
                student_id="S001",
                name="王小明",
                birthday=date(2020, 5, 10),
                is_active=True,
            )
            s.add(student)
            s.flush()
            s.add(
                ActivityRegistration(
                    student_id=student.id,
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                    paid_amount=1200,
                    is_paid=True,
                )
            )
            s.commit()

            sync_registrations_on_student_deactivate(s, student.id)
            s.commit()

            changes = (
                s.query(RegistrationChange)
                .filter(RegistrationChange.change_type == "學生離園自動沖帳")
                .all()
            )
            assert len(changes) == 1
            assert "NT$1200" in changes[0].description


# ═══════════════════════════════════════════════════════════════════════════
# L3: 生日格式/範圍
# ═══════════════════════════════════════════════════════════════════════════


class TestBirthdayRangeValidation:
    def test_rejects_future_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="2099-01-01"),
        )
        assert r.status_code == 422
        assert "未來" in r.text

    def test_rejects_too_old_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="1900-01-01"),
        )
        assert r.status_code == 422
        assert "合理範圍" in r.text

    def test_rejects_malformed_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="2020/05/10"),
        )
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# L4: /public/update 換手機號 —— 手足共用電話（F5，2026-06-29 業主裁示放寬）
# ═══════════════════════════════════════════════════════════════════════════


class TestPublicUpdatePhoneSiblingSharing:
    """2026-06-29 才藝點名稽核 F5：手足共用家長電話在「修改報名電話」時亦應成立。

    報名端（/public/register）自 2026-06-22 已移除 phone-only soft-dedup，手足
    （不同 name/birthday）可各自用同一支家長電話報名。但改號端（/public/update）
    原本擋「任何他筆 active 報名在用此號」→ 手足無法把既有報名改成家庭共用號。

    業主裁示（2026-06-29）：放寬，允許改成已存在號碼，與報名端一致。
    安全性不退：/public/query 需 name+birthday+phone「三欄精確全符」方可查得
    （見 test_query_still_requires_all_three_fields_after_phone_share），共用電話
    不造成跨家長外洩；移除此阻擋同時關閉原「200/400」pass-fail 枚舉 oracle。
    （取代原 TestPublicUpdatePhoneConflict——該守衛已隨業主裁示退場。）
    """

    def _seed_two_sharing_regs(self, c, sf):
        """A 先以自有電話報名；B（手足，不同 name+birthday）以家庭共用號報名。"""
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
            s.commit()
        r_a = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(
                name="王小明", birthday="2020-05-10", phone="0911111111"
            ),
        )
        assert r_a.status_code == 201, r_a.text
        r_b = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(
                name="王小華", birthday="2018-03-02", phone="0922222222"
            ),
        )
        assert r_b.status_code == 201, r_b.text
        return r_a, r_b

    def _change_a_to_shared(self, c, r_a):
        return c.post(
            "/api/activity/public/update",
            json={
                "id": r_a.json()["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0911111111",
                "new_parent_phone": "0922222222",
                "class": "大象班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
                "query_token": r_a.json()["query_token"],
            },
        )

    def test_can_change_to_phone_used_by_sibling_registration(self, client):
        """A 把電話改成手足 B 正在用的家庭共用號 → 放寬後成功（原本 400）。"""
        c, sf = client
        r_a, _r_b = self._seed_two_sharing_regs(c, sf)
        r_upd = self._change_a_to_shared(c, r_a)
        assert r_upd.status_code == 200, r_upd.text

    def test_query_still_requires_all_three_fields_after_phone_share(self, client):
        """共用電話後，/public/query 仍需三欄精確全符，不跨家長外洩（安全邊界）。"""
        c, sf = client
        r_a, r_b = self._seed_two_sharing_regs(c, sf)
        assert self._change_a_to_shared(c, r_a).status_code == 200

        # 共用號 + A 正確姓名生日 → 只查到 A
        q_a = c.post(
            "/api/activity/public/query",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0922222222",
            },
        )
        assert q_a.status_code == 200, q_a.text
        assert q_a.json()["id"] == r_a.json()["id"]

        # 共用號 + B 正確姓名生日 → 只查到 B（非 A）
        q_b = c.post(
            "/api/activity/public/query",
            json={
                "name": "王小華",
                "birthday": "2018-03-02",
                "parent_phone": "0922222222",
            },
        )
        assert q_b.status_code == 200, q_b.text
        assert q_b.json()["id"] == r_b.json()["id"]

        # 共用號 + 錯誤的姓名/生日組合 → 404（三欄缺一即查無，無跨家長外洩）
        q_wrong = c.post(
            "/api/activity/public/query",
            json={
                "name": "王小明",
                "birthday": "2018-03-02",
                "parent_phone": "0922222222",
            },
        )
        assert q_wrong.status_code == 404
