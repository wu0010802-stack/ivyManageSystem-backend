"""2026-05-11 leaves/overtimes 14 條 bug batch 修補的回歸測試。

每個測試對應一條 finding；測試命名格式：
    test_p<priority>_<n>_<short_description>

修補完成後此檔須全綠。所有測試共用 app_client fixture（in-memory SQLite + mini app）。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    OvertimeRecord,
    SalaryRecord,
    User,
)
from utils.auth import hash_password

# ────────────────────────────────────────────────────────────────────────────
# 共用 fixture
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """In-memory SQLite + mini FastAPI app（含 auth/leaves/overtimes router）。"""
    db_path = tmp_path / "bug-batch.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)
    monkeypatch.setattr(leaves_module, "_line_service", None)
    monkeypatch.setattr(overtimes_module, "_line_service", None)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)

    with TestClient(app) as client:
        yield client, session_factory, monkeypatch

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str, name: str, is_active: bool = True) -> Employee:
    e = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=is_active,
    )
    session.add(e)
    session.flush()
    return e


def _admin(session, *, employee=None, username: str = "hr_admin") -> User:
    """純管理員（預設無 employee_id，避免觸發自我核准守衛）。"""
    u = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(
    client: TestClient, username: str = "hr_admin", password: str = "AdminPass123"
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _pending_leave(
    session,
    employee_id: int,
    *,
    start: date = date(2026, 6, 1),
    end: date | None = None,
    leave_type: str = "personal",
    leave_hours: float = 8.0,
    start_time: str | None = None,
    end_time: str | None = None,
) -> LeaveRecord:
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start,
        end_date=end or start,
        leave_hours=leave_hours,
        start_time=start_time,
        end_time=end_time,
        is_approved=None,
        is_deductible=True,
        deduction_ratio=1.0,
    )
    session.add(lv)
    session.flush()
    return lv


def _ot_dt(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def _approved_overtime(
    session,
    employee_id: int,
    *,
    overtime_date: date = date(2026, 6, 10),
    start_time: str = "18:00",
    end_time: str = "20:00",
    hours: float = 2.0,
    use_comp_leave: bool = False,
    is_approved: bool | None = True,
) -> OvertimeRecord:
    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=overtime_date,
        start_time=_ot_dt(overtime_date, start_time),
        end_time=_ot_dt(overtime_date, end_time),
        hours=hours,
        overtime_type="weekday",
        is_approved=is_approved,
        use_comp_leave=use_comp_leave,
        comp_leave_granted=use_comp_leave and is_approved is True,
    )
    session.add(ot)
    session.flush()
    return ot


# ────────────────────────────────────────────────────────────────────────────
# Task A — P0-1: batch_approve two-pass 驗證
# ────────────────────────────────────────────────────────────────────────────


class TestP0_1BatchApproveTwoPass:
    """Phase 1 catch-all rollback 不應抹掉同 batch 其他驗證通過條目的 setattr/log。

    修補前的行為：
      第二筆驗證階段拋出非 HTTPException → session.rollback() + expire_all()
      → 第一筆已 setattr 的 is_approved=True 被 rollback；Phase 2 commit 變 no-op
      → 但 succeeded 仍含第一筆，回傳體與 DB 脫鉤（silent data loss）

    修補後（two-pass）：
      Pass 1 純驗證收集 validated_ids，setattr 全部移到 Pass 2 → catch-all rollback
      時 Pass 1 無 dirty state，前面條目不受影響。
    """

    def test_partial_validation_failure_does_not_silently_succeed(self, app_client):
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp1 = _emp(session, "B001", "員工一")
            emp2 = _emp(session, "B002", "員工二")
            _admin(session)
            lv1 = _pending_leave(session, emp1.id)
            lv2 = _pending_leave(session, emp2.id, start=date(2026, 6, 5))
            session.commit()
            lv1_id, lv2_id = lv1.id, lv2.id

        assert _login(client).status_code == 200

        # 讓 _write_approval_log 第二筆觸發 RuntimeError；第一筆正常
        original_write = leaves_module._write_approval_log
        seen_ids: list[int] = []

        def fake_write(
            *, session, doc_type, doc_id, action, approver, comment=None, metadata=None
        ):
            seen_ids.append(doc_id)
            if doc_id == lv2_id:
                raise RuntimeError("simulated unexpected failure")
            return original_write(
                session=session,
                doc_type=doc_type,
                doc_id=doc_id,
                action=action,
                approver=approver,
                comment=comment,
                metadata=metadata,
            )

        mp.setattr(leaves_module, "_write_approval_log", fake_write)

        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [lv1_id, lv2_id], "approved": True},
        )
        # 不論 HTTP status，回傳體應一致：
        body = res.json()
        succeeded = set(body.get("succeeded") or [])
        failed_ids = {f.get("id") for f in (body.get("failed") or [])}

        # 重新讀 DB 確認 lv1 狀態
        with session_factory() as session:
            lv1_db = session.get(LeaveRecord, lv1_id)
            lv2_db = session.get(LeaveRecord, lv2_id)

        # 核心斷言：lv1 若在 succeeded，DB 必須真的核准；不允許 succeeded 與 DB 脫鉤
        if lv1_id in succeeded:
            assert (
                lv1_db.is_approved is True
            ), f"silent data loss: lv1 在 succeeded 但 DB is_approved={lv1_db.is_approved}"
        # lv2 必定 failed
        assert lv2_id in failed_ids, f"lv2 應被視為失敗；body={body}"
        # lv2 不應被部分套用
        assert (
            lv2_db.is_approved is None
        ), f"lv2 失敗但 is_approved={lv2_db.is_approved}（部分套用）"

    def test_overtime_batch_partial_failure_consistent(self, app_client):
        """overtimes 同 pattern 的 batch_approve 也須一致行為。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp1 = _emp(session, "B011", "OT 員工一")
            emp2 = _emp(session, "B012", "OT 員工二")
            _admin(session)
            ot1 = _approved_overtime(
                session, emp1.id, overtime_date=date(2026, 6, 1), is_approved=None
            )
            ot2 = _approved_overtime(
                session, emp2.id, overtime_date=date(2026, 6, 2), is_approved=None
            )
            session.commit()
            ot1_id, ot2_id = ot1.id, ot2.id

        assert _login(client).status_code == 200

        original_write = overtimes_module._write_approval_log

        def fake_write(
            *, session, doc_type, doc_id, action, approver, comment=None, metadata=None
        ):
            if doc_id == ot2_id:
                raise RuntimeError("simulated")
            return original_write(
                session=session,
                doc_type=doc_type,
                doc_id=doc_id,
                action=action,
                approver=approver,
                comment=comment,
                metadata=metadata,
            )

        mp.setattr(overtimes_module, "_write_approval_log", fake_write)

        res = client.post(
            "/api/overtimes/batch-approve",
            json={"ids": [ot1_id, ot2_id], "approved": True},
        )
        body = res.json()
        succeeded = set(body.get("succeeded") or [])
        failed_ids = {f.get("id") for f in (body.get("failed") or [])}

        with session_factory() as session:
            ot1_db = session.get(OvertimeRecord, ot1_id)
            ot2_db = session.get(OvertimeRecord, ot2_id)

        if ot1_id in succeeded:
            assert (
                ot1_db.is_approved is True
            ), f"silent data loss: ot1 succeeded 但 DB is_approved={ot1_db.is_approved}"
        assert ot2_id in failed_ids
        assert ot2_db.is_approved is None


# ────────────────────────────────────────────────────────────────────────────
# Task H — P2-12: batch LINE 推播挪到薪資重算成功後
#         P2-13: _calc_shift_hours 與 _calc_bounded_shift_hours 對午休扣除統一
# ────────────────────────────────────────────────────────────────────────────


class TestHLinePushAndShiftHours:
    def test_p2_12_batch_line_push_skipped_when_recalc_fails(
        self, app_client, monkeypatch
    ):
        """薪資重算失敗的條目不應收到「已核准」LINE 推播。"""
        from unittest.mock import MagicMock as _MM

        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "H001", "員工")
            _admin(session)
            # 建立員工自己的 User 帳號 + line_user_id（LINE push 走到 emp_user.line_user_id 才會推）
            emp_user = User(
                employee_id=emp.id,
                username="emp_h001",
                password_hash=hash_password("AdminPass123"),
                role="teacher",
                permissions=0,
                is_active=True,
                must_change_password=False,
                line_user_id="UTEST_LINE_ID_H001",
            )
            session.add(emp_user)
            lv = _pending_leave(session, emp.id, start=date(2026, 10, 1))
            session.commit()
            lv_id = lv.id

        # 假 LINE service 記錄推播
        notify_calls = []
        fake_line = _MM()
        fake_line.notify_leave_result = lambda *a, **kw: notify_calls.append(a)
        # 假薪資 engine 拋例外
        fake_engine = _MM()
        fake_engine.process_salary_calculation = _MM(
            side_effect=RuntimeError("simulated recalc fail")
        )
        mp.setattr(leaves_module, "_line_service", fake_line)
        mp.setattr(leaves_module, "_salary_engine", fake_engine)

        assert _login(client).status_code == 200

        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [lv_id], "approved": True},
        )
        # 修補前：LINE 已推給員工但薪資重算失敗 → 員工收到「已核准」與 DB 矛盾
        # 修補後：薪資重算失敗時不推 LINE
        body = res.json()
        failed_ids = {f.get("id") for f in (body.get("failed") or [])}
        assert lv_id in failed_ids, f"薪資重算失敗應計入 failed; body={body}"
        assert (
            len(notify_calls) == 0
        ), f"薪資重算失敗條目不應推 LINE；實際 {len(notify_calls)} 次"

    def test_p2_13_calc_shift_hours_consistent_with_bounded(self):
        """_calc_shift_hours 與 _calc_bounded_shift_hours(work_start, work_end, None, None)
        對 5h 邊界班的午休處理應一致。"""
        from api.leaves_workday import (
            _calc_shift_hours,
            _calc_bounded_shift_hours,
        )

        # 08:00-13:00：含 12:00-13:00 一段午休
        unbounded = _calc_shift_hours("08:00", "13:00")
        bounded = _calc_bounded_shift_hours("08:00", "13:00", None, None)
        assert unbounded == bounded, (
            f"_calc_shift_hours({unbounded}) vs _calc_bounded_shift_hours({bounded}) "
            "兩者午休扣除規則應一致（08:00-13:00 5h 邊界班）"
        )

        # 08:00-17:00：完整工作日
        u2 = _calc_shift_hours("08:00", "17:00")
        b2 = _calc_bounded_shift_hours("08:00", "17:00", None, None)
        assert u2 == b2, f"_calc_shift_hours({u2}) vs _calc_bounded_shift_hours({b2})"

        # 09:00-11:00：早上短班，午休不在範圍
        u3 = _calc_shift_hours("09:00", "11:00")
        b3 = _calc_bounded_shift_hours("09:00", "11:00", None, None)
        assert u3 == b3, f"_calc_shift_hours({u3}) vs _calc_bounded_shift_hours({b3})"


# ────────────────────────────────────────────────────────────────────────────
# Task G — 代理人衝突精細化
#   P1-9: 代理人 OT 衝突改用時段比對（不要只比日期）
#   P1-10: 代理人 is_active=False 不可被指定
# ────────────────────────────────────────────────────────────────────────────


class TestGSubstituteConflictRefinement:
    def test_p1_9_substitute_ot_different_time_no_conflict_on_approve(self, app_client):
        """半日假 08:00-12:00 + 代理人 OT 18:00-20:00，approve 不應因 OT 不同時段被擋。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            applicant = _emp(session, "G001", "申請人")
            substitute = _emp(session, "G002", "代理人")
            _admin(session)
            _approved_overtime(
                session,
                substitute.id,
                overtime_date=date(2026, 9, 15),
                start_time="18:00",
                end_time="20:00",
                hours=2.0,
            )
            lv = LeaveRecord(
                employee_id=applicant.id,
                leave_type="personal",
                start_date=date(2026, 9, 15),
                end_date=date(2026, 9, 15),
                start_time="08:00",
                end_time="12:00",
                leave_hours=4.0,
                is_approved=None,
                is_deductible=True,
                deduction_ratio=1.0,
                substitute_employee_id=substitute.id,
            )
            session.add(lv)
            session.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        res = client.put(f"/api/leaves/{lv_id}/approve", json={"approved": True})
        assert (
            res.status_code == 200
        ), f"代理人晚間 OT 不應與上午半日假衝突；body={res.json()}"

    def test_p1_10_inactive_substitute_blocked_on_approve(self, app_client):
        """代理人離職 is_active=False，approve 假單應被擋。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            applicant = _emp(session, "G003", "申請人")
            inactive_sub = _emp(session, "G004", "離職代理人", is_active=False)
            _admin(session)
            lv = LeaveRecord(
                employee_id=applicant.id,
                leave_type="personal",
                start_date=date(2026, 9, 16),
                end_date=date(2026, 9, 16),
                leave_hours=8.0,
                is_approved=None,
                is_deductible=True,
                deduction_ratio=1.0,
                substitute_employee_id=inactive_sub.id,
            )
            session.add(lv)
            session.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        res = client.put(f"/api/leaves/{lv_id}/approve", json={"approved": True})
        assert res.status_code in (
            400,
            403,
            409,
        ), f"離職代理人不應通過 approve；status={res.status_code} body={res.json()}"


# ────────────────────────────────────────────────────────────────────────────
# Task F — update 路徑硬化
#   P1-7: start_time/end_time 允許 null 清空（半日↔全日）
#   P1-8: _revoke_comp_leave_grant 自動駁回 linked_pending 補寫 ApprovalLog
#   P1-11: update_overtime 改 hours 縮減守衛（linked_approved 已使用）
#   P2-14: OvertimeUpdate 拒絕 use_comp_leave 翻轉
# ────────────────────────────────────────────────────────────────────────────


class TestFUpdatePathHardening:
    def test_p1_7_update_leave_can_clear_start_end_time(self, app_client):
        """半日假改全日：傳 start_time/end_time = null 應清空欄位。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "F001", "員工")
            _admin(session)
            lv = LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 1),
                start_time="09:00",
                end_time="12:00",
                leave_hours=3.0,
                is_approved=None,
                is_deductible=True,
                deduction_ratio=1.0,
            )
            session.add(lv)
            session.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/leaves/{lv_id}",
            json={
                "start_time": None,
                "end_time": None,
            },
        )
        assert res.status_code == 200, f"body={res.json()}"

        with session_factory() as session:
            lv_db = session.get(LeaveRecord, lv_id)
            assert (
                lv_db.start_time is None
            ), f"start_time 應被清空；實際={lv_db.start_time}"
            assert lv_db.end_time is None, f"end_time 應被清空；實際={lv_db.end_time}"

    def test_p1_8_revoke_comp_leave_writes_approval_log(self, app_client):
        """delete 已核准補休 OT 時，linked_pending 自動駁回必須寫 ApprovalLog。"""
        from models.database import ApprovalLog

        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "F002", "員工二")
            _admin(session)
            # 已核准補休模式 OT（comp_leave_granted=True）+ LeaveQuota
            ot = _approved_overtime(
                session,
                emp.id,
                overtime_date=date(2026, 9, 5),
                hours=4.0,
                use_comp_leave=True,
            )
            from models.database import LeaveQuota

            quota = LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="compensatory",
                total_hours=4.0,
            )
            session.add(quota)
            # pending 補休假單關聯該 OT
            pending_leave = LeaveRecord(
                employee_id=emp.id,
                leave_type="compensatory",
                start_date=date(2026, 9, 10),
                end_date=date(2026, 9, 10),
                leave_hours=2.0,
                is_approved=None,
                is_deductible=False,
                deduction_ratio=0.0,
                source_overtime_id=ot.id,
            )
            session.add(pending_leave)
            session.commit()
            ot_id = ot.id
            leave_id = pending_leave.id

        assert _login(client).status_code == 200

        res = client.delete(f"/api/overtimes/{ot_id}")
        assert res.status_code == 200, f"刪除應成功；body={res.json()}"

        with session_factory() as session:
            lv_db = session.get(LeaveRecord, leave_id)
            assert lv_db.is_approved is False, "linked_pending 應被自動駁回"
            log = (
                session.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "leave",
                    ApprovalLog.doc_id == leave_id,
                )
                .order_by(ApprovalLog.id.desc())
                .first()
            )
            assert log is not None, "自動駁回必須留 ApprovalLog（修補 P1-8）"
            assert log.action == "rejected"

    def test_p1_11_update_overtime_hours_shrink_blocked_when_used(self, app_client):
        """補休 OT 已核准 4h、linked_approved 用了 3h，再把 hours 改 2h 應 409。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "F003", "員工三")
            _admin(session)
            ot = _approved_overtime(
                session,
                emp.id,
                overtime_date=date(2026, 9, 6),
                hours=4.0,
                use_comp_leave=True,
            )
            from models.database import LeaveQuota

            quota = LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="compensatory",
                total_hours=4.0,
            )
            session.add(quota)
            # 已核准 3h 補休假
            used_leave = LeaveRecord(
                employee_id=emp.id,
                leave_type="compensatory",
                start_date=date(2026, 9, 12),
                end_date=date(2026, 9, 12),
                leave_hours=3.0,
                is_approved=True,
                is_deductible=False,
                deduction_ratio=0.0,
                source_overtime_id=ot.id,
            )
            session.add(used_leave)
            session.commit()
            ot_id = ot.id

        assert _login(client).status_code == 200

        # update_overtime 試圖把 hours 縮減到 2.0
        res = client.put(
            f"/api/overtimes/{ot_id}",
            json={"hours": 2.0},
        )
        assert (
            res.status_code == 409
        ), f"已用 3h、縮減至 2h 應被擋；實際 status={res.status_code} body={res.json()}"

    def test_p2_14_overtime_update_rejects_use_comp_leave_flip(self, app_client):
        """OvertimeUpdate 不應接受 use_comp_leave 翻轉。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "F004", "員工四")
            _admin(session)
            ot = _approved_overtime(
                session,
                emp.id,
                overtime_date=date(2026, 9, 7),
                hours=2.0,
                use_comp_leave=True,
            )
            session.commit()
            ot_id = ot.id

        assert _login(client).status_code == 200

        # 試圖把 use_comp_leave 翻為 False
        res = client.put(
            f"/api/overtimes/{ot_id}",
            json={"use_comp_leave": False},
        )
        assert res.status_code in (
            400,
            422,
        ), f"use_comp_leave 翻轉應 422/400；實際 status={res.status_code}"


# ────────────────────────────────────────────────────────────────────────────
# Task E — P1-6: approve_overtime body schema（向後相容）
# ────────────────────────────────────────────────────────────────────────────


class TestP1_6ApproveOvertimeBodySchema:
    """approve_overtime 必須支援 body schema 接 rejection_reason（不洩漏個資到 URL log）。
    保留 query param fallback 不破壞既有前端。approved_by 強制 current_user.username。
    """

    def test_approve_overtime_accepts_body_rejection_reason(self, app_client):
        """body 內的 approved=False + rejection_reason 必須被讀取並落 ApprovalLog。"""
        from models.database import ApprovalLog

        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "E001", "員工")
            _admin(session)
            ot = _approved_overtime(
                session,
                emp.id,
                is_approved=None,
                overtime_date=date(2026, 8, 1),
            )
            session.commit()
            ot_id = ot.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/overtimes/{ot_id}/approve",
            json={"approved": False, "rejection_reason": "時數不符實際工作"},
        )
        assert res.status_code == 200, f"body 路徑應通過；body={res.json()}"

        with session_factory() as session:
            ot_db = session.get(OvertimeRecord, ot_id)
            assert (
                ot_db.is_approved is False
            ), f"body 內 approved=False 應被讀取；實際 is_approved={ot_db.is_approved}"
            log = (
                session.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "overtime",
                    ApprovalLog.doc_id == ot_id,
                )
                .order_by(ApprovalLog.id.desc())
                .first()
            )
            assert log is not None and "時數不符實際工作" in (
                log.comment or ""
            ), f"rejection_reason 必須落 ApprovalLog.comment；log={log.comment if log else None}"

    def test_approve_overtime_query_fallback_still_works(self, app_client):
        """向後相容：舊前端用 query param 應仍可運作。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "E002", "員工二")
            _admin(session)
            ot = _approved_overtime(
                session,
                emp.id,
                is_approved=None,
                overtime_date=date(2026, 8, 2),
            )
            session.commit()
            ot_id = ot.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/overtimes/{ot_id}/approve?approved=false&rejection_reason=tooshort",
        )
        assert res.status_code == 200, f"query fallback 應通過；body={res.json()}"

    def test_approved_by_uses_current_user_not_query_param(self, app_client):
        """approved_by 應強制取 current_user.username，不接受外部 query 輸入。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "E003", "員工三")
            _admin(session, username="real_admin")
            ot = _approved_overtime(
                session,
                emp.id,
                is_approved=None,
                overtime_date=date(2026, 8, 3),
            )
            session.commit()
            ot_id = ot.id

        assert _login(client, username="real_admin").status_code == 200

        res = client.put(
            f"/api/overtimes/{ot_id}/approve?approved=true&approved_by=spoofed_name",
        )
        assert res.status_code == 200

        with session_factory() as session:
            ot_db = session.get(OvertimeRecord, ot_id)
            assert (
                ot_db.approved_by == "real_admin"
            ), f"approved_by 應取 current_user，不應為 spoofed_name；實際={ot_db.approved_by}"


# ────────────────────────────────────────────────────────────────────────────
# Task D — P1-4: import_leaves 補 _check_overlap
#         P1-5: leave ↔ overtime 跨類自我重疊偵測
# ────────────────────────────────────────────────────────────────────────────


class TestP1_4_5LeaveOvertimeCrossOverlap:
    """import_leaves 必須擋同員工同日多筆 pending；create_leave/create_overtime
    必須交叉檢查同員工同時段是否有對方類型的紀錄。"""

    def test_create_overtime_blocked_by_existing_approved_leave_same_day(
        self, app_client
    ):
        """申請人同日已有 approved 全日假，再申請 OT 應被擋"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "D001", "員工")
            _admin(session)
            # 已核准 全日 personal 假
            lv = LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 1),
                leave_hours=8.0,
                is_approved=True,
                is_deductible=True,
                deduction_ratio=1.0,
            )
            session.add(lv)
            session.commit()
            emp_id = emp.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/overtimes",
            json={
                "employee_id": emp_id,
                "overtime_date": "2026-07-01",
                "overtime_type": "weekday",
                "start_time": "18:00",
                "end_time": "20:00",
                "hours": 2.0,
            },
        )
        assert res.status_code in (
            400,
            409,
        ), f"OT 與既有 leave 同日重疊應 400/409；實際 {res.status_code} body={res.json()}"
        assert "請假" in res.json().get("detail", "")

    def test_create_leave_blocked_by_existing_approved_overtime_same_day(
        self, app_client
    ):
        """申請人同日已有 approved OT 全日，再申請全日假應被擋"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "D002", "員工二")
            _admin(session)
            _approved_overtime(
                session,
                emp.id,
                overtime_date=date(2026, 7, 2),
                start_time="08:00",
                end_time="17:00",
                hours=8.0,
            )
            session.commit()
            emp_id = emp.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/leaves",
            json={
                "employee_id": emp_id,
                "leave_type": "personal",
                "start_date": "2026-07-02",
                "end_date": "2026-07-02",
                "leave_hours": 8.0,
            },
        )
        assert res.status_code in (
            400,
            409,
        ), f"leave 與既有 OT 同日重疊應 400/409；實際 {res.status_code} body={res.json()}"
        assert "加班" in res.json().get("detail", "")

    def test_import_leaves_blocks_duplicate_pending_same_day(self, app_client):
        """import 兩筆同員工同日 leave，第二筆應 failed（avoid duplicate pending）"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp = _emp(session, "D003", "員工三")
            _admin(session)
            # 先建立一筆 pending leave（模擬 import 第一筆已落地）
            lv = LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=date(2026, 7, 3),
                end_date=date(2026, 7, 3),
                leave_hours=8.0,
                is_approved=None,
                is_deductible=True,
                deduction_ratio=1.0,
            )
            session.add(lv)
            session.commit()
            emp_id = emp.id

        assert _login(client).status_code == 200

        # 用 _find_overlapping_leave include_pending=True 應該偵測到既有 pending
        from api.leaves import _find_overlapping_leave

        with session_factory() as session:
            conflict = _find_overlapping_leave(
                session,
                emp_id,
                date(2026, 7, 3),
                date(2026, 7, 3),
                include_pending=True,
            )
        assert (
            conflict is not None
        ), "預先建立的 pending leave 應被 _find_overlapping_leave 偵測"

        # 直接 inspect import_leaves source 確認有 _check_overlap 或 _find_overlapping_leave 呼叫
        import inspect
        from api import leaves as m

        src = inspect.getsource(m.import_leaves)
        assert (
            "_check_overlap" in src or "_find_overlapping_leave" in src
        ), "import_leaves 必須呼叫 overlap 檢查（include_pending=True）"


# ────────────────────────────────────────────────────────────────────────────
# Task C — P0-3: update/delete 缺 with_for_update 列鎖
# ────────────────────────────────────────────────────────────────────────────


class TestP0_3UpdateDeleteRowLock:
    """leaves/overtimes 的 update/delete 路徑必須與 approve 一樣用 with_for_update()，
    否則並發 update+approve 會 lost update（補休配額負數、重複退還等）。

    用 source inspection 驗證 invariant；真實 race 留 staging 演練。
    """

    def test_update_leave_uses_for_update(self):
        import inspect
        from api import leaves as m

        src = inspect.getsource(m.update_leave)
        assert (
            "with_for_update" in src
        ), "update_leave 的 LeaveRecord SELECT 缺 with_for_update()"

    def test_delete_leave_uses_for_update(self):
        import inspect
        from api import leaves as m

        src = inspect.getsource(m.delete_leave)
        assert (
            "with_for_update" in src
        ), "delete_leave 的 LeaveRecord SELECT 缺 with_for_update()"

    def test_update_overtime_uses_for_update(self):
        import inspect
        from api import overtimes as m

        src = inspect.getsource(m.update_overtime)
        assert (
            "with_for_update" in src
        ), "update_overtime 的 OvertimeRecord SELECT 缺 with_for_update()"

    def test_delete_overtime_uses_for_update(self):
        import inspect
        from api import overtimes as m

        src = inspect.getsource(m.delete_overtime)
        assert (
            "with_for_update" in src
        ), "delete_overtime 的 OvertimeRecord SELECT 缺 with_for_update()"


# ────────────────────────────────────────────────────────────────────────────
# Task B — P0-2: Portal 病假繞過勞基雙配額
# ────────────────────────────────────────────────────────────────────────────


import types as _types
from unittest.mock import patch as _patch


def _portal_emp():
    e = _types.SimpleNamespace()
    e.id = 99
    e.name = "Portal 教師"
    e.base_salary = 30000
    e.hire_date = date(2020, 1, 1)
    return e


class TestP0_2PortalSickStatutoryCap:
    """Portal sick 必須走 _guard_leave_quota（呼叫 assert_sick_leave_within_statutory_caps）。

    修補前：portal/leaves.py:312-326 sick 分支只走 _check_quota（看 LeaveQuota 總量），
    LeaveQuota 未初始化時直接 return，雙桶（未住院 240h / 住院 2080h / 合計 2080h）
    完全繞過。
    """

    def _build_payload(
        self, *, leave_type: str = "sick", hours: float = 4.0, is_hosp: bool = False
    ):
        from api.portal._shared import LeaveCreatePortal

        return LeaveCreatePortal(
            leave_type=leave_type,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            leave_hours=hours,
            reason="生病",
            is_hospitalized=is_hosp,
        )

    def _common_patches(self, emp):
        from api.portal import leaves as portal_lv

        session = MagicMock()
        return session, [
            _patch.object(portal_lv, "get_session", return_value=session),
            _patch.object(portal_lv, "_get_employee", return_value=emp),
            _patch.object(portal_lv, "_check_overlap", return_value=None),
            _patch.object(portal_lv, "_check_substitute_leave_conflict"),
            _patch.object(portal_lv, "validate_leave_hours_against_schedule"),
            _patch.object(portal_lv, "_check_leave_limits"),
            _patch.object(portal_lv, "validate_portal_leave_rules"),
        ]

    def test_sick_dispatched_to_guard_leave_quota(self):
        """portal sick 必須呼叫 _guard_leave_quota（觸發雙桶檢查）"""
        from api.portal import leaves as portal_lv

        emp = _portal_emp()
        session, patches = self._common_patches(emp)
        for p in patches:
            p.start()
        try:
            with (
                _patch.object(portal_lv, "_guard_leave_quota") as mock_guard,
                _patch.object(portal_lv, "_check_quota") as mock_quota,
            ):
                try:
                    portal_lv.create_my_leave(
                        data=self._build_payload(),
                        request=MagicMock(),
                        current_user={"username": "t", "employee_id": 99},
                    )
                except Exception:
                    pass
            assert mock_guard.called, "portal sick 必須走 _guard_leave_quota"
        finally:
            for p in patches:
                p.stop()

    def test_sick_outpatient_241h_blocked_by_statutory_cap(self):
        """未住院 sick 已用 240h，再申請 1h 應被擋（勞工請假規則第 4 條）"""
        from api.portal import leaves as portal_lv

        emp = _portal_emp()
        session, patches = self._common_patches(emp)
        for p in patches:
            p.start()
        try:
            # 已用 240h 未住院 sick，年度上限 240h
            with (
                _patch(
                    "api.leaves._get_sick_committed_hours",
                    side_effect=lambda s, eid, year, is_hospitalized, exclude_id=None: (
                        240.0 if not is_hospitalized else 0.0
                    ),
                ),
            ):
                with pytest.raises(HTTPException) as exc:
                    portal_lv.create_my_leave(
                        data=self._build_payload(hours=1.0, is_hosp=False),
                        request=MagicMock(),
                        current_user={"username": "t", "employee_id": 99},
                    )
            assert exc.value.status_code == 400
            assert "勞工請假規則" in exc.value.detail or "未住院" in exc.value.detail
        finally:
            for p in patches:
                p.stop()
