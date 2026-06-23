"""P3 顯示一致性修復測試：outstanding_count 與 totals.outstanding 一致性。

問題：第一輪迴圈對 outstanding>0 的 record 累加 outstanding_count，
      折抵迴圈只更新 bucket["outstanding"] 未回調計數，
      導致全額折抵後 totals.outstanding==0 但 outstanding_count 仍>0。

此測試確認修復後兩者同源一致。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal.fees import compute_fees_summary
from models.database import Base, Classroom, Guardian, Student, User
from models.fees import StudentFeeAdjustment, StudentFeeRecord


@pytest.fixture
def session_for_count(tmp_path):
    db_path = tmp_path / "fees_count.sqlite"
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

    with session_factory() as session:
        yield session

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_student(session, name="測試生", classroom_name="星星班"):
    classroom = Classroom(name=classroom_name, is_active=True)
    session.add(classroom)
    session.flush()

    user = User(
        username=f"parent_{name}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id=f"LINE_{name}",
        token_version=0,
    )
    session.add(user)
    session.flush()

    student = Student(
        student_id=f"S_{name}",
        name=name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return student


class TestOutstandingCountConsistency:
    def test_full_adjustment_zeroes_both_outstanding_and_count(self, session_for_count):
        """全額折抵後 totals.outstanding==0 且 outstanding_count==0（修前 count 仍為 1）。

        設定：
        - 學生一筆欠款 outstanding=500，學期 "114-1"
        - 同 (student_id, period) 折抵 amount=500

        修前行為（RED）：totals.outstanding==0 但 outstanding_count==1（計數未回調）
        修後行為（GREEN）：totals.outstanding==0 且 outstanding_count==0
        """
        session = session_for_count
        student = _setup_student(session, name="全額折抵生")

        # 一筆欠款 500，尚未繳費
        record = StudentFeeRecord(
            student_id=student.id,
            student_name=student.name,
            classroom_name="星星班",
            fee_item_name="學費",
            amount_due=500,
            amount_paid=0,
            status="unpaid",
            period="114-1",
            due_date=None,
        )
        session.add(record)
        session.flush()

        # 同 (student_id, period) 折抵 500 → 全額沖銷
        adj = StudentFeeAdjustment(
            student_id=student.id,
            period="114-1",
            adjustment_type="prepayment",
            amount=500,
            reason="預繳全額沖銷",
        )
        session.add(adj)
        session.commit()

        result = compute_fees_summary(session, [student.id])
        totals = result["totals"]

        # 折抵後應收為 0
        assert (
            totals["outstanding"] == 0
        ), f"expected outstanding==0, got {totals['outstanding']}"
        # 折抵後未繳筆數也應為 0（修前此處為 1）
        assert totals["outstanding_count"] == 0, (
            f"expected outstanding_count==0, got {totals['outstanding_count']}  "
            f"(bug: count not recalculated after adjustment)"
        )

    def test_partial_adjustment_keeps_count(self, session_for_count):
        """部分折抵後 outstanding_count 仍保留 bucket 仍有欠款的計數。

        設定：
        - 學生一筆欠款 outstanding=1000，學期 "114-1"
        - 同 (student_id, period) 折抵 amount=400（部分折抵）

        期望：totals.outstanding==600 且 outstanding_count==1
        """
        session = session_for_count
        student = _setup_student(session, name="部分折抵生", classroom_name="月亮班")

        record = StudentFeeRecord(
            student_id=student.id,
            student_name=student.name,
            classroom_name="月亮班",
            fee_item_name="材料費",
            amount_due=1000,
            amount_paid=0,
            status="unpaid",
            period="114-1",
            due_date=None,
        )
        session.add(record)
        session.flush()

        adj = StudentFeeAdjustment(
            student_id=student.id,
            period="114-1",
            adjustment_type="prepayment",
            amount=400,
            reason="部分預繳",
        )
        session.add(adj)
        session.commit()

        result = compute_fees_summary(session, [student.id])
        totals = result["totals"]

        assert (
            totals["outstanding"] == 600
        ), f"expected outstanding==600, got {totals['outstanding']}"
        assert (
            totals["outstanding_count"] == 1
        ), f"expected outstanding_count==1, got {totals['outstanding_count']}"

    def test_no_adjustment_count_matches_record_count(self, session_for_count):
        """無折抵時，outstanding_count 等於有欠款的 (student, period) bucket 數量。

        設定：同一學生兩筆不同學期各一筆欠款，無折抵
        期望：outstanding_count==2（每個 bucket 各一個未繳）
        """
        session = session_for_count
        student = _setup_student(session, name="兩學期生", classroom_name="太陽班")

        for period in ("114-1", "114-2"):
            record = StudentFeeRecord(
                student_id=student.id,
                student_name=student.name,
                classroom_name="太陽班",
                fee_item_name="學費",
                amount_due=500,
                amount_paid=0,
                status="unpaid",
                period=period,
                due_date=None,
            )
            session.add(record)
        session.commit()

        result = compute_fees_summary(session, [student.id])
        totals = result["totals"]

        assert totals["outstanding"] == 1000
        assert totals["outstanding_count"] == 2

    def test_all_paid_outstanding_count_is_zero(self, session_for_count):
        """全部繳清時，outstanding_count 應為 0。"""
        session = session_for_count
        student = _setup_student(session, name="全繳清生", classroom_name="彩虹班")

        record = StudentFeeRecord(
            student_id=student.id,
            student_name=student.name,
            classroom_name="彩虹班",
            fee_item_name="學費",
            amount_due=800,
            amount_paid=800,
            status="paid",
            period="114-1",
            due_date=None,
        )
        session.add(record)
        session.commit()

        result = compute_fees_summary(session, [student.id])
        totals = result["totals"]

        assert totals["outstanding"] == 0
        assert totals["outstanding_count"] == 0
