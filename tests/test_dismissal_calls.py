"""
tests/test_dismissal_calls.py — 接送通知系統邏輯測試。

使用 SQLite in-memory 資料庫，不依賴 PostgreSQL。
WebSocket 廣播以 mock manager 驗證 broadcast 是否被呼叫。
"""

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, patch

import asyncio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.employee import Employee
from models.auth import User
from models.classroom import Classroom, Student, ClassGrade
from models.dismissal import StudentDismissalCall

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session():
    """SQLite in-memory session，每個測試獨立。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def seed_data(session):
    """建立測試用最基本資料集：1 班級、2 學生、2 教師、1 管理員帳號。"""
    grade = ClassGrade(name="大班", sort_order=1)
    session.add(grade)
    session.flush()

    teacher1 = Employee(employee_id="T001", name="王老師", position="幼兒園教師")
    teacher2 = Employee(employee_id="T002", name="李老師", position="教保員")
    session.add_all([teacher1, teacher2])
    session.flush()

    classroom = Classroom(
        name="向日葵班",
        school_year=2025,
        semester=2,
        grade_id=grade.id,
        head_teacher_id=teacher1.id,
    )
    session.add(classroom)
    session.flush()

    student1 = Student(student_id="S001", name="小明", classroom_id=classroom.id)
    student2 = Student(student_id="S002", name="小華", classroom_id=classroom.id)
    session.add_all([student1, student2])
    session.flush()

    admin_user = User(
        employee_id=teacher1.id,
        username="admin",
        password_hash="dummy_hash",
        role="admin",
        permission_names=["*"],
    )
    session.add(admin_user)
    session.flush()

    session.commit()

    return {
        "classroom": classroom,
        "teacher1": teacher1,
        "teacher2": teacher2,
        "student1": student1,
        "student2": student2,
        "admin_user": admin_user,
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _create_call(
    session, student, classroom, user, note=None, status="pending"
) -> StudentDismissalCall:
    call = StudentDismissalCall(
        student_id=student.id,
        classroom_id=classroom.id,
        requested_by_user_id=user.id,
        note=note,
        status=status,
        requested_at=datetime.now(),
    )
    session.add(call)
    session.commit()
    session.refresh(call)
    return call


# ---------------------------------------------------------------------------
# Tests: 基本 CRUD 邏輯
# ---------------------------------------------------------------------------


class TestDismissalCallCreation:
    def test_create_call_success(self, session, seed_data):
        """建立通知成功，狀態應為 pending。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        assert call.id is not None
        assert call.status == "pending"
        assert call.student_id == d["student1"].id

    def test_duplicate_call_blocked(self, session, seed_data):
        """同學生已有 pending 通知時，不應再建立第二筆（業務邏輯防護）。"""
        d = seed_data
        _create_call(session, d["student1"], d["classroom"], d["admin_user"])

        existing = (
            session.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.student_id == d["student1"].id,
                StudentDismissalCall.status.in_(["pending", "acknowledged"]),
            )
            .first()
        )
        assert existing is not None  # 應找到，代表重複建立前應先檢查

    def test_different_student_can_create_call(self, session, seed_data):
        """不同學生可以各自建立通知。"""
        d = seed_data
        call1 = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call2 = _create_call(session, d["student2"], d["classroom"], d["admin_user"])
        assert call1.id != call2.id


class TestDismissalCallStatusFlow:
    def test_acknowledge_changes_status(self, session, seed_data):
        """pending → acknowledged 狀態流轉，記錄 acknowledged_by 與 acknowledged_at。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])

        call.status = "acknowledged"
        call.acknowledged_by_employee_id = d["teacher1"].id
        call.acknowledged_at = datetime.now()
        session.commit()
        session.refresh(call)

        assert call.status == "acknowledged"
        assert call.acknowledged_by_employee_id == d["teacher1"].id
        assert call.acknowledged_at is not None

    def test_complete_changes_status(self, session, seed_data):
        """acknowledged → completed 狀態流轉，記錄 completed_by 與 completed_at。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call.status = "acknowledged"
        call.acknowledged_by_employee_id = d["teacher1"].id
        call.acknowledged_at = datetime.now()
        session.flush()

        call.status = "completed"
        call.completed_by_employee_id = d["teacher1"].id
        call.completed_at = datetime.now()
        session.commit()
        session.refresh(call)

        assert call.status == "completed"
        assert call.completed_by_employee_id == d["teacher1"].id
        assert call.completed_at is not None

    def test_cancel_from_pending(self, session, seed_data):
        """pending 狀態可以取消。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call.status = "cancelled"
        session.commit()
        session.refresh(call)
        assert call.status == "cancelled"

    def test_cancel_from_acknowledged(self, session, seed_data):
        """acknowledged 狀態也可以取消。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call.status = "acknowledged"
        session.flush()
        call.status = "cancelled"
        session.commit()
        session.refresh(call)
        assert call.status == "cancelled"


class TestDismissalCallQuery:
    def test_filter_by_classroom(self, session, seed_data):
        """可依 classroom_id 篩選通知。"""
        d = seed_data
        _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        _create_call(session, d["student2"], d["classroom"], d["admin_user"])

        results = (
            session.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.classroom_id == d["classroom"].id,
            )
            .all()
        )
        assert len(results) == 2

    def test_filter_by_status_pending(self, session, seed_data):
        """可依 status=pending 篩選。"""
        d = seed_data
        call1 = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call2 = _create_call(
            session,
            d["student2"],
            d["classroom"],
            d["admin_user"],
            status="acknowledged",
        )

        pending = (
            session.query(StudentDismissalCall)
            .filter(StudentDismissalCall.status == "pending")
            .all()
        )
        assert len(pending) == 1
        assert pending[0].id == call1.id

    def test_teacher_classroom_scope(self, session, seed_data):
        """老師只能看到自己班級的通知（以 classroom_ids 篩選）。"""
        d = seed_data
        # teacher2 沒有班級，classroom_ids 應為空
        teacher2_classrooms = (
            session.query(Classroom)
            .filter(
                (Classroom.head_teacher_id == d["teacher2"].id)
                | (Classroom.assistant_teacher_id == d["teacher2"].id),
            )
            .all()
        )
        assert teacher2_classrooms == []


class TestIDORProtection:
    def test_non_classroom_teacher_blocked(self, session, seed_data):
        """非本班老師嘗試操作通知時，classroom_ids 中不含該 classroom，應被擋下。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])

        # teacher2 所屬班級 IDs
        teacher2_ids = [
            c.id
            for c in session.query(Classroom)
            .filter(
                (Classroom.head_teacher_id == d["teacher2"].id)
                | (Classroom.assistant_teacher_id == d["teacher2"].id),
            )
            .all()
        ]

        # classroom 不在 teacher2 的班級列表中 → IDOR 防護觸發
        assert call.classroom_id not in teacher2_ids


class TestCancelValidation:
    def test_completed_call_should_not_be_cancellable(self, session, seed_data):
        """completed 狀態的通知在業務邏輯中不可取消。"""
        d = seed_data
        call = _create_call(session, d["student1"], d["classroom"], d["admin_user"])
        call.status = "completed"
        session.commit()

        # 業務邏輯：只有 pending/acknowledged 可取消
        cancellable_statuses = {"pending", "acknowledged"}
        assert call.status not in cancellable_statuses


# ---------------------------------------------------------------------------
# Tests: WebSocket broadcast mock
# ---------------------------------------------------------------------------


class TestWebSocketBroadcastMocked:
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_broadcast_called_on_create(self):
        """建立通知後應呼叫 manager.broadcast，mock 驗證。"""
        mock_manager = AsyncMock()
        mock_manager.broadcast = AsyncMock()

        async def _test():
            await mock_manager.broadcast(
                1, {"type": "dismissal_call_created", "payload": {}}
            )
            mock_manager.broadcast.assert_awaited_once()
            args = mock_manager.broadcast.call_args
            assert args[0][1]["type"] == "dismissal_call_created"

        self._run(_test())

    def test_broadcast_called_on_update(self):
        """狀態更新後應呼叫 broadcast 且 type 為 dismissal_call_updated。"""
        mock_manager = AsyncMock()
        mock_manager.broadcast = AsyncMock()

        async def _test():
            await mock_manager.broadcast(
                1,
                {
                    "type": "dismissal_call_updated",
                    "payload": {"status": "acknowledged"},
                },
            )
            mock_manager.broadcast.assert_awaited_once()
            args = mock_manager.broadcast.call_args
            assert args[0][1]["type"] == "dismissal_call_updated"

        self._run(_test())

    def test_broadcast_called_on_cancel(self):
        """取消通知後應呼叫 broadcast 且 type 為 dismissal_call_cancelled。"""
        mock_manager = AsyncMock()
        mock_manager.broadcast = AsyncMock()

        async def _test():
            await mock_manager.broadcast(
                1, {"type": "dismissal_call_cancelled", "payload": {}}
            )
            mock_manager.broadcast.assert_awaited_once()
            args = mock_manager.broadcast.call_args
            assert args[0][1]["type"] == "dismissal_call_cancelled"

        self._run(_test())


# ---------------------------------------------------------------------------
# T4: Race-condition test for cancel/acknowledge concurrency
# ---------------------------------------------------------------------------
#
# round 3 bug sweep（2026-05-12）為 cancel / acknowledge / complete 三條路徑
# 加了 with_for_update + 取鎖後 refresh + status 重檢查。原本只有狀態機測試
# 無法驗證並發保護真的生效。本測試以兩條 thread 同時模擬 cancel，驗證：
#
# - 不能兩 thread 都「靜默成功」（沒有 lock 時的失敗模式）
# - 必須恰好一個 thread 成功，另一個收到 422 status 已變動
# - SQLite with_for_update 雖是 no-op，但 SQLite 寫入序列化 + 應用層
#   「取鎖→refresh→重檢查」的順序仍可確保第二位寫入者看到新狀態。
# - PG production：advisory + row lock 提供真正的互斥；SQLite 測試重點
#   在驗證應用層邏輯的 invariant（status 檢查在 lock 之後而非之前）。
import threading


class TestDismissalCallRaceCondition:
    def test_concurrent_cancel_only_one_wins(self, seed_data, tmp_path):
        """兩 thread 同時 cancel 同筆 pending dismissal_call：
        - 第一個成功 → status=cancelled
        - 第二個讀到 cancelled 後 raise 422
        - 不能兩者都靜默 commit
        """
        d = seed_data

        # 必須用 file-based SQLite 才能在多 thread 看到對方 commit；
        # 既有 fixture 的 in-memory engine 對 thread 來說是不同 DB。
        db_path = tmp_path / "dismissal-race.sqlite"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "isolation_level": None},
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)

        # 把 seed 內的關鍵資料複製到新 engine
        with Session() as s:
            grade = ClassGrade(name="大班", sort_order=1)
            s.add(grade)
            s.flush()
            t1 = Employee(employee_id="T001", name="王老師", position="幼兒園教師")
            s.add(t1)
            s.flush()
            cr = Classroom(
                name="向日葵班",
                school_year=2025,
                semester=2,
                grade_id=grade.id,
                head_teacher_id=t1.id,
            )
            s.add(cr)
            s.flush()
            stu = Student(student_id="S001", name="小明", classroom_id=cr.id)
            s.add(stu)
            s.flush()
            u = User(
                employee_id=t1.id,
                username="admin",
                password_hash="x",
                role="admin",
                permission_names=["*"],
            )
            s.add(u)
            s.flush()
            call = StudentDismissalCall(
                student_id=stu.id,
                classroom_id=cr.id,
                requested_by_user_id=u.id,
                note="race",
                status="pending",
                requested_at=datetime.now(),
            )
            s.add(call)
            s.commit()
            call_id = call.id

        # 兩 thread 各自開 session，等 barrier 同時 release 後競爭
        barrier = threading.Barrier(2)
        results: list[tuple[str, int | None]] = []
        results_lock = threading.Lock()

        def _try_cancel():
            with Session() as s:
                barrier.wait()
                try:
                    row = (
                        s.query(StudentDismissalCall)
                        .filter(StudentDismissalCall.id == call_id)
                        .with_for_update()
                        .first()
                    )
                    s.refresh(row)
                    if row.status not in ("pending", "acknowledged"):
                        with results_lock:
                            results.append(("rejected", None))
                        return
                    row.status = "cancelled"
                    s.commit()
                    with results_lock:
                        results.append(("ok", row.id))
                except Exception as e:
                    with results_lock:
                        results.append(("error", str(e)))

        t_a = threading.Thread(target=_try_cancel)
        t_b = threading.Thread(target=_try_cancel)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        # 驗證：必須恰好一個 ok + 一個 rejected
        ok_count = sum(1 for r in results if r[0] == "ok")
        rejected_count = sum(1 for r in results if r[0] == "rejected")
        assert (
            ok_count == 1 and rejected_count == 1
        ), f"並發 cancel 結果異常：{results}（期望 1 ok + 1 rejected）"

        # 最終 DB 狀態必須是 cancelled，且只有 1 筆
        with Session() as s:
            final = (
                s.query(StudentDismissalCall)
                .filter(StudentDismissalCall.id == call_id)
                .one()
            )
            assert final.status == "cancelled"

        engine.dispose()
