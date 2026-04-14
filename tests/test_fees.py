"""
tests/test_fees.py — 學費管理邏輯單元測試

使用 SQLite in-memory 資料庫，測試：
- 批次產生：同一 student + fee_item 不重複建立
- 繳費狀態更新
- summary 計算正確性
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.fees import _apply_fee_record_filters
from models.base import Base
from models.classroom import Classroom, Student
from models.fees import FeeItem, StudentFeeRecord


@pytest.fixture
def session():
    """SQLite in-memory session，每個測試獨立。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 輔助建立資料
# ---------------------------------------------------------------------------

def _add_classroom(session, name="大班A") -> Classroom:
    cls = Classroom(name=name, school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    return cls


def _add_student(session, name="王小明", classroom_id=None) -> Student:
    import random
    sid = f"S{random.randint(10000, 99999)}"
    s = Student(student_id=sid, name=name, is_active=True, classroom_id=classroom_id)
    session.add(s)
    session.flush()
    return s


def _add_fee_item(session, name="學費", amount=3000, period="2025-1", classroom_id=None) -> FeeItem:
    item = FeeItem(name=name, amount=amount, period=period, classroom_id=classroom_id, is_active=True)
    session.add(item)
    session.flush()
    return item


def _add_record(session, student, fee_item) -> StudentFeeRecord:
    r = StudentFeeRecord(
        student_id=student.id,
        student_name=student.name,
        classroom_name="",
        fee_item_id=fee_item.id,
        fee_item_name=fee_item.name,
        amount_due=fee_item.amount,
        amount_paid=0,
        status="unpaid",
        period=fee_item.period,
    )
    session.add(r)
    session.flush()
    return r


# ---------------------------------------------------------------------------
# 批次產生邏輯：不重複
# ---------------------------------------------------------------------------

class TestGenerateFeeRecords:
    def test_generate_creates_records_for_active_students(self, session):
        """在校學生應各建立一筆費用記錄"""
        cls = _add_classroom(session)
        s1 = _add_student(session, "學生甲", cls.id)
        s2 = _add_student(session, "學生乙", cls.id)
        item = _add_fee_item(session)
        session.commit()

        students = session.query(Student).filter(Student.is_active == True).all()
        existing = {r.student_id for r in session.query(StudentFeeRecord.student_id).filter(
            StudentFeeRecord.fee_item_id == item.id
        ).all()}

        created = 0
        for s in students:
            if s.id not in existing:
                session.add(StudentFeeRecord(
                    student_id=s.id, student_name=s.name, classroom_name="",
                    fee_item_id=item.id, fee_item_name=item.name,
                    amount_due=item.amount, amount_paid=0, status="unpaid", period=item.period,
                ))
                created += 1
        session.commit()

        assert created == 2
        assert session.query(StudentFeeRecord).count() == 2

    def test_generate_skips_existing_records(self, session):
        """同一 student+fee_item 組合若已存在，應跳過不重複建立"""
        cls = _add_classroom(session)
        s1 = _add_student(session, "學生甲", cls.id)
        item = _add_fee_item(session)
        _add_record(session, s1, item)
        session.commit()

        existing = {r.student_id for r in session.query(StudentFeeRecord.student_id).filter(
            StudentFeeRecord.fee_item_id == item.id
        ).all()}

        created = 0
        skipped = 0
        for s in session.query(Student).filter(Student.is_active == True).all():
            if s.id in existing:
                skipped += 1
            else:
                session.add(StudentFeeRecord(
                    student_id=s.id, student_name=s.name, classroom_name="",
                    fee_item_id=item.id, fee_item_name=item.name,
                    amount_due=item.amount, amount_paid=0, status="unpaid", period=item.period,
                ))
                created += 1
        session.commit()

        assert created == 0
        assert skipped == 1
        assert session.query(StudentFeeRecord).count() == 1

    def test_generate_mixed_existing_and_new(self, session):
        """部分已存在、部分新學生：只新增未存在的"""
        cls = _add_classroom(session)
        s1 = _add_student(session, "已有記錄學生", cls.id)
        s2 = _add_student(session, "新學生", cls.id)
        item = _add_fee_item(session)
        _add_record(session, s1, item)
        session.commit()

        existing = {r.student_id for r in session.query(StudentFeeRecord.student_id).filter(
            StudentFeeRecord.fee_item_id == item.id
        ).all()}

        created = skipped = 0
        for s in session.query(Student).filter(Student.is_active == True).all():
            if s.id in existing:
                skipped += 1
            else:
                session.add(StudentFeeRecord(
                    student_id=s.id, student_name=s.name, classroom_name="",
                    fee_item_id=item.id, fee_item_name=item.name,
                    amount_due=item.amount, amount_paid=0, status="unpaid", period=item.period,
                ))
                created += 1
        session.commit()

        assert created == 1
        assert skipped == 1
        assert session.query(StudentFeeRecord).count() == 2


# ---------------------------------------------------------------------------
# 繳費狀態更新
# ---------------------------------------------------------------------------

class TestPayFeeRecord:
    def test_pay_updates_status_to_paid(self, session):
        """登記繳費後狀態應為 paid"""
        cls = _add_classroom(session)
        s = _add_student(session, "王小明", cls.id)
        item = _add_fee_item(session, amount=3000)
        record = _add_record(session, s, item)
        session.commit()

        record.status = "paid"
        record.amount_paid = 3000
        record.payment_date = date(2025, 9, 1)
        record.payment_method = "現金"
        session.commit()

        updated = session.query(StudentFeeRecord).filter(StudentFeeRecord.id == record.id).first()
        assert updated.status == "paid"
        assert updated.amount_paid == 3000
        assert updated.payment_method == "現金"

    def test_pay_partial_amount(self, session):
        """允許部分繳費（amount_paid < amount_due），status 仍改為 paid"""
        cls = _add_classroom(session)
        s = _add_student(session, "李小華", cls.id)
        item = _add_fee_item(session, amount=5000)
        record = _add_record(session, s, item)
        session.commit()

        record.status = "paid"
        record.amount_paid = 2500
        record.payment_date = date(2025, 9, 10)
        record.payment_method = "轉帳"
        session.commit()

        updated = session.query(StudentFeeRecord).filter(StudentFeeRecord.id == record.id).first()
        assert updated.status == "paid"
        assert updated.amount_paid == 2500


# ---------------------------------------------------------------------------
# Summary 計算
# ---------------------------------------------------------------------------

class TestFeeSummary:
    def test_summary_with_no_records(self, session):
        """無記錄時 summary 全為 0"""
        records = session.query(StudentFeeRecord).all()
        total_due = sum(r.amount_due for r in records)
        total_paid = sum(r.amount_paid for r in records)
        paid_count = sum(1 for r in records if r.status == "paid")

        assert total_due == 0
        assert total_paid == 0
        assert paid_count == 0

    def test_summary_counts_correctly(self, session):
        """有 3 筆記錄：2 筆已繳、1 筆未繳，驗證統計正確"""
        item = _add_fee_item(session, amount=3000)
        cls = _add_classroom(session)
        s1 = _add_student(session, "甲", cls.id)
        s2 = _add_student(session, "乙", cls.id)
        s3 = _add_student(session, "丙", cls.id)
        session.commit()

        r1 = _add_record(session, s1, item)
        r1.status = "paid"; r1.amount_paid = 3000
        r2 = _add_record(session, s2, item)
        r2.status = "paid"; r2.amount_paid = 3000
        r3 = _add_record(session, s3, item)  # unpaid
        session.commit()

        records = session.query(StudentFeeRecord).all()
        total_count = len(records)
        paid_count = sum(1 for r in records if r.status == "paid")
        unpaid_count = total_count - paid_count
        total_due = sum(r.amount_due for r in records)
        total_paid = sum(r.amount_paid for r in records)
        total_unpaid = total_due - total_paid

        assert total_count == 3
        assert paid_count == 2
        assert unpaid_count == 1
        assert total_due == 9000
        assert total_paid == 6000
        assert total_unpaid == 3000

    def test_summary_filtered_by_period(self, session):
        """依學期篩選 summary，只計算指定學期的記錄"""
        item_2025 = _add_fee_item(session, name="2025上學費", period="2025-1", amount=3000)
        item_2026 = _add_fee_item(session, name="2026上學費", period="2026-1", amount=4000)
        cls = _add_classroom(session)
        s = _add_student(session, "學生", cls.id)
        session.commit()

        r1 = _add_record(session, s, item_2025)
        r1.status = "paid"; r1.amount_paid = 3000

        # 直接為 s 建立 2026 記錄（不同 fee_item）
        r2 = StudentFeeRecord(
            student_id=s.id, student_name=s.name, classroom_name="",
            fee_item_id=item_2026.id, fee_item_name=item_2026.name,
            amount_due=4000, amount_paid=0, status="unpaid", period="2026-1",
        )
        session.add(r2)
        session.commit()

        records_2025 = session.query(StudentFeeRecord).filter(
            StudentFeeRecord.period == "2025-1"
        ).all()
        assert len(records_2025) == 1
        assert sum(r.amount_due for r in records_2025) == 3000


# ---------------------------------------------------------------------------
# NV8 回歸測試：student_id FK 應為 RESTRICT，刪除學生時不可級聯刪除繳費歷史
# ---------------------------------------------------------------------------

class TestStudentFeeRecordFKRestrict:
    def test_fee_record_has_restrict_fk(self):
        """驗證 StudentFeeRecord.student_id 的 ondelete 行為設定為 RESTRICT（模型層驗證）。"""
        from sqlalchemy import inspect as sa_inspect
        from models.fees import StudentFeeRecord

        mapper = sa_inspect(StudentFeeRecord)
        col = mapper.columns["student_id"]
        fk = list(col.foreign_keys)[0]
        # ondelete 屬性應為 RESTRICT 而非 CASCADE
        assert fk.ondelete == "RESTRICT", (
            f"student_fee_records.student_id FK ondelete 應為 RESTRICT，實際為 {fk.ondelete!r}。"
            "NV8 修復：防止刪除學生時靜默刪除財務記錄。"
        )

    def test_cascade_delete_blocked_at_db_level(self):
        """SQLite in-memory + 啟用 FK 強制：刪除有費用記錄的學生應引發 IntegrityError。"""
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.exc import IntegrityError

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

        # SQLite 需手動啟用 FK 強制（PRAGMA foreign_keys = ON）
        # 注意：此處 conn 為原始 DBAPI 連線（sqlite3.Connection），須用字串 API
        @event.listens_for(engine, "connect")
        def _enable_fk(conn, _):
            conn.execute("PRAGMA foreign_keys = ON")

        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            cls = _add_classroom(s)
            stu = _add_student(s, "待刪學生", cls.id)
            item = _add_fee_item(s)
            _add_record(s, stu, item)
            s.commit()

            # 嘗試刪除學生（應被 RESTRICT 阻擋）
            student_to_delete = s.query(Student).filter(Student.id == stu.id).first()
            s.delete(student_to_delete)
            with pytest.raises(IntegrityError):
                s.flush()
        finally:
            s.close()
            engine.dispose()


# ---------------------------------------------------------------------------
# fee_summary SQL 聚合正確性驗證
# ---------------------------------------------------------------------------

class TestFeeSummarySQLAggregation:
    """驗證 fee_summary 改用 SQL 聚合後，結果與逐筆 Python 計算一致。"""

    def _sql_summary(self, session, period=None, classroom_name=None, status=None, fee_item_id=None):
        """複製 fees.py fee_summary 的 SQL 聚合邏輯。"""
        from sqlalchemy import func, case
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            fee_item_id=fee_item_id,
        )

        agg_q = q.with_entities(
            func.count(StudentFeeRecord.id).label("total_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "paid", 1), else_=0)), 0
            ).label("paid_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "partial", 1), else_=0)), 0
            ).label("partial_count"),
            func.coalesce(func.sum(StudentFeeRecord.amount_due), 0).label("total_due"),
            func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0).label("total_paid"),
        )
        row = agg_q.one()
        total_count = row.total_count or 0
        paid_count = int(row.paid_count or 0)
        partial_count = int(row.partial_count or 0)
        total_due = int(row.total_due or 0)
        total_paid = int(row.total_paid or 0)
        return {
            "total_count": total_count,
            "paid_count": paid_count,
            "partial_count": partial_count,
            "unpaid_count": total_count - paid_count - partial_count,
            "total_due": total_due,
            "total_paid": total_paid,
            "total_unpaid": total_due - total_paid,
        }

    def _python_summary(self, session, period=None, classroom_name=None, status=None, fee_item_id=None):
        """原始 Python 端計算（作為比對基準）。"""
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            fee_item_id=fee_item_id,
        )
        records = q.all()
        total_count = len(records)
        paid_count = sum(1 for r in records if r.status == "paid")
        partial_count = sum(1 for r in records if r.status == "partial")
        total_due = sum(r.amount_due for r in records)
        total_paid = sum(r.amount_paid for r in records)
        return {
            "total_count": total_count,
            "paid_count": paid_count,
            "partial_count": partial_count,
            "unpaid_count": total_count - paid_count - partial_count,
            "total_due": total_due,
            "total_paid": total_paid,
            "total_unpaid": total_due - total_paid,
        }

    def test_empty_db_all_zero(self, session):
        """無記錄時 SQL 聚合回傳全 0"""
        result = self._sql_summary(session)
        assert result == {
            "total_count": 0,
            "paid_count": 0,
            "partial_count": 0,
            "unpaid_count": 0,
            "total_due": 0,
            "total_paid": 0,
            "total_unpaid": 0,
        }

    def test_sql_matches_python_mixed_status(self, session):
        """paid / unpaid / partial 混合狀態：SQL 聚合與 Python 計算結果一致"""
        item = _add_fee_item(session, amount=5000)
        cls = _add_classroom(session)
        s1 = _add_student(session, "甲", cls.id)
        s2 = _add_student(session, "乙", cls.id)
        s3 = _add_student(session, "丙", cls.id)
        session.commit()

        r1 = _add_record(session, s1, item)
        r1.status = "paid"; r1.amount_paid = 5000
        r2 = _add_record(session, s2, item)
        r2.status = "partial"; r2.amount_paid = 2000
        r3 = _add_record(session, s3, item)  # unpaid, amount_paid=0
        session.commit()

        sql_result = self._sql_summary(session)
        py_result = self._python_summary(session)
        assert sql_result == py_result

    def test_sql_matches_python_filtered_by_period(self, session):
        """依 period 篩選：SQL 聚合與 Python 計算一致"""
        item_a = _add_fee_item(session, period="2025-1", amount=3000)
        item_b = _add_fee_item(session, period="2025-2", amount=4000)
        cls = _add_classroom(session)
        s1 = _add_student(session, "甲", cls.id)
        s2 = _add_student(session, "乙", cls.id)
        session.commit()

        r1 = _add_record(session, s1, item_a)
        r1.status = "paid"; r1.amount_paid = 3000
        r2 = _add_record(session, s2, item_b)
        session.commit()

        sql_result = self._sql_summary(session, period="2025-1")
        py_result = self._python_summary(session, period="2025-1")
        assert sql_result == py_result
        assert sql_result["total_count"] == 1
        assert sql_result["paid_count"] == 1

    def test_rejected_record_not_counted_as_paid(self, session):
        """status 為 'unpaid' 的記錄不應計入 paid_count"""
        item = _add_fee_item(session, amount=1000)
        cls = _add_classroom(session)
        s = _add_student(session, "戊", cls.id)
        session.commit()
        r = _add_record(session, s, item)  # status = "unpaid"
        session.commit()

        result = self._sql_summary(session)
        assert result["paid_count"] == 0
        assert result["unpaid_count"] == 1

    def test_summary_can_filter_by_partial_status(self, session):
        item = _add_fee_item(session, amount=2500)
        cls = _add_classroom(session)
        s1 = _add_student(session, "甲", cls.id)
        s2 = _add_student(session, "乙", cls.id)
        session.commit()

        partial_record = _add_record(session, s1, item)
        partial_record.status = "partial"
        partial_record.amount_paid = 1000

        paid_record = _add_record(session, s2, item)
        paid_record.status = "paid"
        paid_record.amount_paid = 2500
        session.commit()

        sql_result = self._sql_summary(session, status="partial")
        py_result = self._python_summary(session, status="partial")

        assert sql_result == py_result
        assert sql_result["total_count"] == 1
        assert sql_result["partial_count"] == 1
        assert sql_result["paid_count"] == 0


class TestFeeRecordFilterHelper:
    def test_filter_supports_partial_status(self, session):
        cls = _add_classroom(session)
        s1 = _add_student(session, "全繳學生", cls.id)
        s2 = _add_student(session, "部分學生", cls.id)
        item = _add_fee_item(session, amount=4000)
        session.commit()

        paid_record = _add_record(session, s1, item)
        paid_record.status = "paid"
        paid_record.amount_paid = 4000

        partial_record = _add_record(session, s2, item)
        partial_record.status = "partial"
        partial_record.amount_paid = 1000
        session.commit()

        rows = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            status="partial",
        ).all()

        assert [row.student_name for row in rows] == ["部分學生"]

    def test_filter_supports_student_name_keyword(self, session):
        cls = _add_classroom(session)
        item = _add_fee_item(session, amount=3500)
        target = _add_student(session, "王小明", cls.id)
        other = _add_student(session, "李小華", cls.id)
        session.commit()

        _add_record(session, target, item)
        _add_record(session, other, item)
        session.commit()

        rows = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            student_name="小明",
        ).all()

        assert len(rows) == 1
        assert rows[0].student_name == "王小明"
