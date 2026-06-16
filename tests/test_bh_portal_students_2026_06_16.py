"""tests/test_bh_portal_students_2026_06_16.py

bh-portal 修補批次回歸測試（2026-06-16），四個 bug 各一組：

- #16 public/update 用品改 diff：保留未變更用品原 price_snapshot，
  不以「當前 DB 價」全刪重建（破壞差異化保價、靜默改寫已報名總額）。
- #18 class_hub 五個待辦計數補 lifecycle_status == active：
  休學（on_leave，is_active 仍為 True）學生不應被算成待點名/待觀察/待聯絡簿/待用藥。
- #19 終態離校（graduate/delete）標記薪資 needs_recalc（節慶/超額在籍人數漂移）。
- #12 家長端費用總覽折抵 scope 到實際納入彙總的 (student_id, period)，
  避免跨期把已繳清學期折抵移轉到他期 outstanding，低報未繳。

所有會寫 DB 的測試一律走自建 SQLite session/engine（tmp 隔離），不碰 dev PG。
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    Base,
    Classroom,
    RegistrationSupply,
    Student,
)
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ON_LEAVE,
)


# ---------------------------------------------------------------------------
# 共用 in-memory session fixture（純函式 / service 層測試用）
# ---------------------------------------------------------------------------
@pytest.fixture
def mem_session(tmp_path):
    db_path = tmp_path / "bh_portal.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    sess = sf()
    yield sess
    sess.close()
    engine.dispose()


# ===========================================================================
# #18 — class_hub 五個待辦計數遺漏 lifecycle_status 過濾
# ===========================================================================
class TestClassHubExcludesOnLeave:
    """休學生（on_leave，is_active 仍 True）不應計入任何待辦計數。"""

    def _two_students(self, sess):
        c = Classroom(name="向日葵班", is_active=True)
        sess.add(c)
        sess.flush()
        active = Student(
            student_id="ACT",
            name="在學生",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        on_leave = Student(
            student_id="LVE",
            name="休學生",
            classroom_id=c.id,
            is_active=True,  # 休學仍 is_active=True，這正是 bug 的觸發點
            lifecycle_status=LIFECYCLE_ON_LEAVE,
        )
        sess.add_all([active, on_leave])
        sess.flush()
        return c, active, on_leave

    def test_attendance_pending_excludes_on_leave(self, mem_session):
        from services.portal_class_hub_service import count_attendance_pending

        c, _active, _on_leave = self._two_students(mem_session)
        # 無人點名 → 只有 1 位在學生待點名（休學生不算）
        assert (
            count_attendance_pending(
                mem_session, classroom_id=c.id, today=date(2026, 5, 4)
            )
            == 1
        )

    def test_observation_pending_excludes_on_leave(self, mem_session):
        from services.portal_class_hub_service import count_observation_pending

        c, _active, _on_leave = self._two_students(mem_session)
        assert (
            count_observation_pending(
                mem_session, classroom_id=c.id, today=date(2026, 5, 4)
            )
            == 1
        )

    def test_contact_book_pending_excludes_on_leave(self, mem_session):
        from services.portal_class_hub_service import count_contact_book_pending

        c, _active, _on_leave = self._two_students(mem_session)
        assert (
            count_contact_book_pending(
                mem_session, classroom_id=c.id, today=date(2026, 5, 4)
            )
            == 1
        )

    def test_incidents_today_excludes_on_leave(self, mem_session):
        from services.portal_class_hub_service import count_incidents_today
        from models.classroom import StudentIncident

        c, active, on_leave = self._two_students(mem_session)
        # 兩位學生今天各 1 筆事件 → 只應計在學生那 1 筆
        for stu in (active, on_leave):
            mem_session.add(
                StudentIncident(
                    student_id=stu.id,
                    incident_type="行為觀察",
                    occurred_at=datetime(2026, 5, 4, 9, 0),
                    description="x",
                )
            )
        mem_session.flush()
        assert (
            count_incidents_today(
                mem_session, classroom_id=c.id, today=date(2026, 5, 4)
            )
            == 1
        )

    def test_pending_medications_excludes_on_leave(self, mem_session):
        from services.portal_class_hub_service import list_pending_medications
        from models.portfolio import StudentMedicationOrder, StudentMedicationLog

        c, active, on_leave = self._two_students(mem_session)
        today = date(2026, 5, 4)
        for stu in (active, on_leave):
            order = StudentMedicationOrder(
                student_id=stu.id,
                order_date=today,
                medication_name="退燒藥",
                dose="5ml",
                time_slots=["10:00"],
            )
            mem_session.add(order)
            mem_session.flush()
            mem_session.add(
                StudentMedicationLog(
                    order_id=order.id,
                    scheduled_time="10:00",
                    administered_at=None,
                    skipped=False,
                    correction_of=None,
                )
            )
        mem_session.flush()
        result = list_pending_medications(mem_session, classroom_id=c.id, today=today)
        # 只有在學生的用藥需執行
        assert len(result) == 1
        assert result[0]["student_id"] == active.id


# ===========================================================================
# #12 — 家長端費用總覽折抵跨學期錯誤扣抵
# ===========================================================================
class TestParentFeesSummaryAdjustmentScope:
    def _student(self, sess, sid="S1"):
        c = Classroom(name="海豚班", is_active=True)
        sess.add(c)
        sess.flush()
        stu = Student(
            student_id=sid,
            name="小明",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(stu)
        sess.flush()
        return stu

    def _record(self, sess, student_id, period, due, paid):
        from models.fees import StudentFeeRecord

        rec = StudentFeeRecord(
            student_id=student_id,
            student_name="小明",
            classroom_name="海豚班",
            fee_item_name="月費",
            amount_due=due,
            amount_paid=paid,
            status="paid" if paid >= due else ("partial" if paid > 0 else "unpaid"),
            period=period,
        )
        sess.add(rec)
        sess.flush()
        return rec

    def _adjustment(self, sess, student_id, period, amount):
        from models.fees import StudentFeeAdjustment

        adj = StudentFeeAdjustment(
            student_id=student_id,
            period=period,
            adjustment_type="prepayment",
            amount=amount,
        )
        sess.add(adj)
        sess.flush()
        return adj

    def test_adjustment_does_not_leak_across_periods(self, mem_session):
        """已繳清學期(114-1)的折抵不應移轉去抵另一學期(114-2)的未繳。

        場景：
        - 114-1：應繳 1000、已繳 1000（繳清，outstanding=0），但仍掛一筆 500 折抵
                 （例如事後同胞優惠補登）。
        - 114-2：應繳 1000、已繳 0（全未繳，outstanding=1000）。

        正確：114-1 的 500 折抵只能抵 114-1（已 0，無可抵）→ 114-2 outstanding 仍 1000。
        舊 bug：折抵按 student 匯總跨期扣抵 → 114-2 被誤抵成 500，低報未繳。
        """
        from api.parent_portal.fees import compute_fees_summary

        stu = self._student(mem_session)
        self._record(mem_session, stu.id, "114-1", due=1000, paid=1000)
        self._record(mem_session, stu.id, "114-2", due=1000, paid=0)
        self._adjustment(mem_session, stu.id, "114-1", amount=500)

        summary = compute_fees_summary(mem_session, [stu.id])
        # 折抵僅限 114-1（已繳清），不得移轉抵 114-2
        assert summary["totals"]["outstanding"] == 1000

    def test_adjustment_applies_within_same_period(self, mem_session):
        """同學期內的折抵仍正常抵減該期 outstanding。"""
        from api.parent_portal.fees import compute_fees_summary

        stu = self._student(mem_session)
        self._record(mem_session, stu.id, "114-2", due=1000, paid=0)
        self._adjustment(mem_session, stu.id, "114-2", amount=300)

        summary = compute_fees_summary(mem_session, [stu.id])
        # 同期折抵 300 → outstanding 由 1000 降為 700
        assert summary["totals"]["outstanding"] == 700


# ===========================================================================
# #19 — 終態離校未標記薪資 needs_recalc
# ===========================================================================
class TestTerminalLifecycleMarksSalaryStale:
    """graduate / delete 後，受影響發放月的未封存薪資應被標 needs_recalc。"""

    def _seed_salary(self, sess, *, year, month, finalized):
        from models.database import SalaryRecord, Employee

        emp = Employee(
            employee_id=f"E{year}{month}{int(finalized)}",
            name="班導師",
            is_active=True,
        )
        sess.add(emp)
        sess.flush()
        rec = SalaryRecord(
            employee_id=emp.id,
            salary_year=year,
            salary_month=month,
            is_finalized=finalized,
            needs_recalc=False,
        )
        sess.add(rec)
        sess.flush()
        return rec

    def test_graduate_marks_distribution_month_stale(self, mem_session):
        from services.salary.utils import mark_salary_stale_for_enrollment_event

        # 一筆未封存的發放月薪資（事件日 5 月 → 下一發放月 6 月）
        rec = self._seed_salary(mem_session, year=2026, month=6, finalized=False)
        count = mark_salary_stale_for_enrollment_event(mem_session, date(2026, 5, 20))
        mem_session.refresh(rec)
        assert count == 1
        assert rec.needs_recalc is True

    def test_finalized_record_not_marked(self, mem_session):
        from services.salary.utils import mark_salary_stale_for_enrollment_event

        rec = self._seed_salary(mem_session, year=2026, month=6, finalized=True)
        mark_salary_stale_for_enrollment_event(mem_session, date(2026, 5, 20))
        mem_session.refresh(rec)
        # 已封存不動
        assert rec.needs_recalc is False

    def test_graduate_endpoint_marks_salary_stale(self, mem_session, monkeypatch):
        """端到端：呼叫 graduate_student 後，發放月未封存薪資被標 stale。

        直呼 async endpoint（避免起整個 app），用 monkeypatch 把 get_session 指到
        本測試 SQLite session，並把 WS 廣播打成 no-op。
        """
        import asyncio
        import api.students as students_api
        from models.database import SalaryRecord

        sess = mem_session
        # 在學學生
        c = Classroom(name="畢業班", is_active=True)
        sess.add(c)
        sess.flush()
        stu = Student(
            student_id="GRAD1",
            name="畢業生",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            enrollment_date=date(2023, 9, 1),
        )
        sess.add(stu)
        # 畢業日 6/15 → next_distribution_month=（2026, 9），故受影響的是 9 月（含）
        # 之後的未封存發放月薪資。
        from models.database import Employee

        emp = Employee(employee_id="HT001", name="班導師", is_active=True)
        sess.add(emp)
        sess.flush()
        rec = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=9,
            is_finalized=False,
            needs_recalc=False,
        )
        sess.add(rec)
        sess.commit()
        rec_id = rec.id

        # get_session 回傳同一個 SQLite session；close 設為 no-op 以免提早關閉
        monkeypatch.setattr(students_api, "get_session", lambda: sess)
        monkeypatch.setattr(sess, "close", lambda: None)

        from api.students import StudentGraduate

        item = StudentGraduate(
            graduation_date="2026-06-15",
            status="已畢業",
            reason=None,
            notes=None,
        )
        current_user = {
            "user_id": 1,
            "username": "admin",
            "role": "admin",
            "permission_names": ["*"],
        }
        asyncio.run(students_api.graduate_student(stu.id, item, current_user))

        fresh = sess.query(SalaryRecord).filter(SalaryRecord.id == rec_id).first()
        assert fresh.needs_recalc is True


# ===========================================================================
# #16 — public/update 用品改 diff（保留原 price_snapshot）
# ===========================================================================
@pytest.fixture
def public_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.activity import router as activity_router
    from api.activity.public import (
        _public_query_limiter_instance,
        _public_register_limiter_instance,
    )

    db_path = tmp_path / "pub.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, session_factory
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


class TestPublicUpdateSupplyPriceSnapshotPreserved:
    def test_unchanged_supply_keeps_original_price_snapshot(self, public_client):
        """報名時用品價 200 → 校方漲價成 500 → 家長改報名（用品不變）
        應沿用原 price_snapshot=200，而非以當前 DB 價 500 重新快照。
        """
        client, sf = public_client
        sy, sem = _term()
        with sf() as s:
            classroom = Classroom(
                name="海豚班", is_active=True, school_year=sy, semester=sem
            )
            s.add(classroom)
            s.flush()
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1000,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.add(
                ActivityCourse(
                    name="畫畫", price=800, school_year=sy, semester=sem, is_active=True
                )
            )
            s.add(
                ActivitySupply(
                    name="畫具", price=200, school_year=sy, semester=sem, is_active=True
                )
            )
            s.add(
                Student(
                    student_id="S001",
                    name="王小明",
                    birthday=date(2020, 5, 10),
                    classroom_id=classroom.id,
                    parent_phone="0912345678",
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
            s.commit()

        # 報名：圍棋 + 畫具(200)
        r = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [{"name": "畫具", "price": "200"}],
            },
        )
        assert r.status_code == 201, r.text

        q = client.post(
            "/api/activity/public/query",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        ).json()
        reg_id = q["id"]

        # 校方把畫具漲價成 500（模擬調價）
        with sf() as s:
            sup = s.query(ActivitySupply).filter(ActivitySupply.name == "畫具").first()
            sup.price = 500
            s.commit()

        # 家長修改報名：加一門課，用品「畫具」維持不變
        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [
                    {"name": "圍棋", "price": "1000"},
                    {"name": "畫畫", "price": "800"},
                ],
                "supplies": [{"name": "畫具", "price": "200"}],
                "if_unmodified_since": q["updated_at"],
            },
        )
        assert res.status_code == 200, res.text

        # 未變更的「畫具」應沿用原 price_snapshot=200，而非被重抓成 500
        with sf() as s:
            rs = (
                s.query(RegistrationSupply)
                .filter(RegistrationSupply.registration_id == reg_id)
                .all()
            )
            assert len(rs) == 1
            assert rs[0].price_snapshot == 200
