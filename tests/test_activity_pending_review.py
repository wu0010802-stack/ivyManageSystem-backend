"""才藝報名「待審核佇列 + 隱私契約 + 人工審核」整合測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Student,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def pending_client(tmp_path):
    db_path = tmp_path / "pending.sqlite"
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
    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_admin(session, username="admin", password="TempPass123"):
    session.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            # F-027：/students/search 同時要求 STUDENTS_READ 才能拉學生目錄，
            # 媒合測試需要這個 bit 才能跑通完整流程。
            permissions=(
                Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.STUDENTS_READ
            ),
            is_active=True,
        )
    )
    session.flush()


def _login(client, username="admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return r


def _seed_base(
    session,
    *,
    with_student=True,
    classroom_name="大象班",
    phone="0912345678",
    student_name="王小明",
    birthday=date(2020, 5, 10),
):
    """建立 admin + 活躍班級 + 課程，依參數可建立對應學生。回傳 classroom_id。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    classroom = Classroom(
        name=classroom_name, is_active=True, school_year=sy, semester=sem
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
    if with_student:
        session.add(
            Student(
                student_id="S001",
                name=student_name,
                birthday=birthday,
                classroom_id=classroom.id,
                parent_phone=phone,
                is_active=True,
            )
        )
    session.commit()
    return classroom.id


def _public_register_payload(
    *,
    name="王小明",
    birthday="2020-05-10",
    phone="0912345678",
    class_="大象班",
):
    return {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": class_,
        "courses": [{"name": "圍棋", "price": "1"}],
        "supplies": [],
    }


class TestPublicRegisterMatching:
    def test_matched_writes_classroom_id_and_overrides_class_name(self, pending_client):
        """家長自選班級與真實班級不符時，匹配成功後應覆蓋為真實班級。"""
        client, sf = pending_client
        with sf() as s:
            classroom_id = _seed_base(s, classroom_name="大象班")
            # 再新增一個家長可能誤選的班級
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            s.add(
                Classroom(name="長頸鹿班", is_active=True, school_year=sy, semester=sem)
            )
            s.commit()

        res = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(class_="長頸鹿班"),
        )
        assert res.status_code == 201

        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.student_id is not None
            assert reg.classroom_id == classroom_id
            assert reg.class_name == "大象班"
            assert reg.match_status == "matched"
            assert reg.pending_review is False

    def test_unmatched_goes_to_pending_review(self, pending_client):
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)

        res = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(),
        )
        assert res.status_code == 201

        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.pending_review is True
            assert reg.match_status == "pending"
            assert reg.student_id is None
            assert reg.classroom_id is None
            # 保留家長輸入以供人工審核參考
            assert reg.class_name == "大象班"
            assert reg.parent_phone == "0912345678"

    def test_phone_mismatch_goes_to_pending(self, pending_client):
        """三欄中 phone 錯誤時也應走 pending 流程。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)

        res = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0999999999"),
        )
        assert res.status_code == 201
        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.pending_review is True
            assert reg.student_id is None

    def test_response_does_not_leak_match_status(self, pending_client):
        """隱私契約：公開 API response 絕不可回傳匹配結果欄位。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)

        res = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(),
        )
        body = res.json()
        for forbidden in (
            "match_status",
            "pending_review",
            "student_id",
            "classroom_id",
        ):
            assert forbidden not in body, f"response leaked {forbidden}"

    def test_soft_dedup_blocks_duplicate_pending_same_phone(self, pending_client):
        """同 parent_phone + 同學期已有 pending 時，第二筆不應落 DB（soft dedup）。

        F-030 修補後，未驗證身分（with_student=False → unmatched）的潛在攻擊者
        路徑改回 silent-success（201 + 中性訊息）以避免存在性 oracle，但 dedup
        保證 DB 仍只有一筆 pending（不能讓攻擊者 / 錯字家長堆出大量 pending）。
        """
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)

        r1 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(),
        )
        assert r1.status_code == 201
        # 改個名字但用同手機 → 觸發 pending_dup soft-dedup。
        # 未驗證身分 → silent-success（201），DB 不應多寫一筆。
        r2 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(name="王大明"),
        )
        assert r2.status_code == 201
        with sf() as s:
            count = s.query(ActivityRegistration).count()
        assert count == 1, f"soft dedup 應保留只有 1 筆，實際 {count}"


class TestPublicQueryPrivacy:
    def test_query_requires_all_three_fields(self, pending_client):
        """三欄不齊的 query 應直接 422（Pydantic 擋下）。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)

        # 缺 parent_phone
        res = client.get(
            "/api/activity/public/query",
            params={"name": "王小明", "birthday": "2020-05-10"},
        )
        assert res.status_code == 422

    def test_query_generic_error_on_phone_mismatch(self, pending_client):
        """phone 錯一位數一律回 404（不透露哪一欄錯）。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        assert r1.status_code == 201

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0999999999",
            },
        )
        assert res.status_code == 404
        assert "請確認" in res.json()["detail"]

    def test_query_field_state_confirmed_for_matched_registration(self, pending_client):
        """匹配成功的報名 → field_state 顯示班級唯讀 + 已確認。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        assert r1.status_code == 201

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert "field_state" in body
        fs = body["field_state"]
        assert fs == {
            "class_source": "student_record",
            "class_editable": False,
            "review_state": "confirmed",
        }

    def test_query_field_state_review_for_pending_registration(self, pending_client):
        """待審核（未比對成功）的報名 → field_state 顯示班級可編 + 校方審核中。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)  # 無學生 → pending
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        assert r1.status_code == 201

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 200
        body = res.json()
        fs = body["field_state"]
        assert fs == {
            "class_source": "submitted",
            "class_editable": True,
            "review_state": "school_review",
        }

    def test_query_response_does_not_leak_match_internals(self, pending_client):
        """隱私契約：query 回傳即使新增 field_state，也不能洩漏 student_id 等 raw 欄位。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s)
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        assert r1.status_code == 201

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        body = res.json()
        for forbidden in (
            "match_status",
            "pending_review",
            "student_id",
            "classroom_id",
        ):
            assert forbidden not in body, f"query response leaked {forbidden}"


class TestAdminApprovalWorkflow:
    def test_pending_list_returns_only_pending_rows(self, pending_client):
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)  # 不建學生 → 報名會走 pending

        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        assert r_reg.status_code == 201

        _login(client)
        res = client.get("/api/activity/registrations/pending")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["pending_review"] is True
        assert body["items"][0]["match_status"] == "pending"

    def test_match_api_binds_student_and_sets_manual_status(self, pending_client):
        client, sf = pending_client
        with sf() as s:
            classroom_id = _seed_base(s)
            # 家長填錯名字 → pending
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(name="王小銘"),  # 名字錯 → 不匹配
        )
        assert r_reg.status_code == 201

        _login(client)
        # 取 pending 列表 + 找 student
        pending_list = client.get("/api/activity/registrations/pending").json()
        reg_id = pending_list["items"][0]["id"]

        search = client.get(
            "/api/activity/students/search", params={"q": "王小明"}
        ).json()
        assert len(search["items"]) == 1
        sid = search["items"][0]["id"]

        res = client.post(
            f"/api/activity/registrations/{reg_id}/match",
            json={"student_id": sid},
        )
        assert res.status_code == 200

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.student_id == sid
            assert reg.classroom_id == classroom_id
            assert reg.match_status == "manual"
            assert reg.pending_review is False
            assert reg.reviewed_at is not None
            assert reg.class_name == "大象班"

    def test_reject_api_soft_deletes_and_marks_rejected(self, pending_client):
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "校外生"},
        )
        assert res.status_code == 200

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is False
            assert reg.match_status == "rejected"
            assert reg.pending_review is False
            assert "校外生" in (reg.remark or "")
            assert reg.reviewed_at is not None

    def test_reject_requires_non_empty_reason(self, pending_client):
        """拒絕原因必填（≥ 2 字），用於事後稽核追溯。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        reg_id = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        ).json()["id"]

        _login(client)
        # 空字串：422
        assert (
            client.post(
                f"/api/activity/registrations/{reg_id}/reject", json={"reason": ""}
            ).status_code
            == 422
        )
        # 只有 1 字：422
        assert (
            client.post(
                f"/api/activity/registrations/{reg_id}/reject", json={"reason": "錯"}
            ).status_code
            == 422
        )
        # 僅空白：422
        assert (
            client.post(
                f"/api/activity/registrations/{reg_id}/reject", json={"reason": "   "}
            ).status_code
            == 422
        )
        # 合法：200
        assert (
            client.post(
                f"/api/activity/registrations/{reg_id}/reject",
                json={"reason": "資料錯誤"},
            ).status_code
            == 200
        )

    def test_rematch_picks_up_updated_phone(self, pending_client):
        """家長/校方修正資料後，後台 rematch 自動脫離 pending。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, phone="0912345678")
        # 先用錯 phone 送出 → pending
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        reg_id = r_reg.json()["id"]

        # 模擬校方直接在 DB 修正 phone
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            reg.parent_phone = "0912345678"
            s.commit()

        _login(client)
        res = client.post(f"/api/activity/registrations/{reg_id}/rematch")
        assert res.status_code == 200
        assert res.json()["matched"] is True

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.pending_review is False
            assert reg.match_status == "matched"
            assert reg.student_id is not None

    def test_rematch_edits_phone_and_matches(self, pending_client):
        """rematch body 內直接修 parent_phone，一次完成編輯+比對。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, phone="0912345678")
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/rematch",
            json={"parent_phone": "0912345678"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["matched"] is True
        assert data["field_changed"] is True

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.parent_phone == "0912345678"
            assert reg.match_status == "matched"
            assert reg.pending_review is False

    def test_rematch_keeps_edits_when_still_unmatched(self, pending_client):
        """比對失敗也要保留編輯後的欄位，不讓校方白打。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/rematch",
            json={"parent_phone": "0922222222", "birthday": "2020-05-11"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["matched"] is False
        assert data["field_changed"] is True

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.parent_phone == "0922222222"
            assert reg.birthday == "2020-05-11"
            assert reg.pending_review is True

    def test_rematch_rejects_invalid_phone_format(self, pending_client):
        """家長手機格式錯誤應被 Pydantic 攔截。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/rematch",
            json={"parent_phone": "123"},
        )
        assert res.status_code == 422

    def test_rematch_blocks_duplicate_name_birthday_in_same_term(self, pending_client):
        """若編輯後的 name+birthday 與同學期另一筆有效 reg 重複，應回 400。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        # 第一筆：王小明 2020-05-10
        r1 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        # 第二筆：李小美 2021-01-01 同學期
        r2 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(
                name="李小美", birthday="2021-01-01", phone="0922222222"
            ),
        )
        reg2_id = r2.json()["id"]

        _login(client)
        # 試圖把第二筆改成和第一筆一樣的 name+birthday
        res = client.post(
            f"/api/activity/registrations/{reg2_id}/rematch",
            json={"name": "王小明", "birthday": "2020-05-10"},
        )
        assert res.status_code == 400
        assert "重複" in res.json()["detail"]

    def test_pending_list_rejected_status_returns_rejected_items(self, pending_client):
        """status=rejected 應只回已拒絕（is_active=False, rejected）的報名。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        client.post(
            f"/api/activity/registrations/{reg_id}/reject", json={"reason": "校外生"}
        )

        res_pending = client.get(
            "/api/activity/registrations/pending", params={"status": "pending"}
        )
        assert all(it["id"] != reg_id for it in res_pending.json()["items"])

        res_rejected = client.get(
            "/api/activity/registrations/pending", params={"status": "rejected"}
        )
        ids = [it["id"] for it in res_rejected.json()["items"]]
        assert reg_id in ids
        assert res_rejected.json()["status"] == "rejected"

    def test_restore_rejected_returns_to_pending(self, pending_client):
        """restore 把已拒絕的報名復原為待審核，可再被 rematch。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "測試用拒絕原因"},
        )

        res = client.post(f"/api/activity/registrations/{reg_id}/restore")
        assert res.status_code == 200

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is True
            assert reg.match_status == "pending"
            assert reg.pending_review is True
            assert "已還原" in (reg.remark or "")

    def test_restore_rejects_non_rejected_registration(self, pending_client):
        """非 rejected 狀態的 reg 呼叫 restore 應回 400。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(f"/api/activity/registrations/{reg_id}/restore")
        assert res.status_code == 400

    def test_force_accept_inserts_with_forced_status(self, pending_client):
        """強行收件：跳過比對直接進正式列表，標記 match_status=forced。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(f"/api/activity/registrations/{reg_id}/force-accept")
        assert res.status_code == 200
        data = res.json()
        assert data["forced"] is True
        assert data["matched"] is False

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.is_active is True
            assert reg.pending_review is False
            assert reg.match_status == "forced"
            assert reg.student_id is None
            assert "強行收件" in (reg.remark or "")

    def test_force_accept_with_field_edits_saves_changes(self, pending_client):
        """強行收件同時修正欄位，應保留修改。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r_reg = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        reg_id = r_reg.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/force-accept",
            json={"parent_phone": "0922222222"},
        )
        assert res.status_code == 200
        assert res.json()["field_changed"] is True

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.parent_phone == "0922222222"
            assert reg.match_status == "forced"

    def test_force_accept_blocks_duplicate_name_birthday(self, pending_client):
        """強行收件仍需擋同學期重複的 name+birthday。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        r2 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(
                name="李小美", birthday="2021-01-01", phone="0922222222"
            ),
        )
        reg2_id = r2.json()["id"]

        _login(client)
        res = client.post(
            f"/api/activity/registrations/{reg2_id}/force-accept",
            json={"name": "王小明", "birthday": "2020-05-10"},
        )
        assert res.status_code == 400

    def test_pending_list_all_status_merges_pending_and_rejected(self, pending_client):
        """status=all 應同時包含 pending 與 rejected 筆數。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        # 兩筆 pending
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        r2 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(name="李小美", phone="0922222222"),
        )
        _login(client)
        # 拒絕其中一筆
        client.post(
            f"/api/activity/registrations/{r1.json()['id']}/reject",
            json={"reason": "測試用拒絕原因"},
        )

        res = client.get("/api/activity/registrations/pending")  # 預設 all
        ids = {it["id"] for it in res.json()["items"]}
        assert r1.json()["id"] in ids
        assert r2.json()["id"] in ids

    def test_restore_blocks_when_duplicate_active_exists(self, pending_client):
        """拒絕後又另建了同 name+birthday 的新 reg，不能 restore 避免衝突。"""
        client, sf = pending_client
        with sf() as s:
            _seed_base(s, with_student=False)
        r1 = client.post(
            "/api/activity/public/register", json=_public_register_payload()
        )
        reg1_id = r1.json()["id"]

        _login(client)
        client.post(
            f"/api/activity/registrations/{reg1_id}/reject",
            json={"reason": "測試用拒絕原因"},
        )

        # 拒絕後又送一筆同 name+birthday
        r2 = client.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0922222222"),
        )
        assert r2.status_code == 201

        res = client.post(f"/api/activity/registrations/{reg1_id}/restore")
        assert res.status_code == 400
