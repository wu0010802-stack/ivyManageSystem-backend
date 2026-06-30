import random
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from api.fees._helpers import compute_fee_summary
from models.base import Base
from models.classroom import Classroom, Student
from models.fees import StudentFeeAdjustment, StudentFeeRecord


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _stu(session, name):
    cls = Classroom(name=name + "班", school_year=2025, semester=1)
    session.add(cls); session.flush()
    sid = f"S{random.randint(10000, 99999)}"
    st = Student(student_id=sid, name=name, is_active=True, classroom_id=cls.id)
    session.add(st); session.flush()
    return st, cls.name


def _rec(session, st, cname, item, due, paid, status, period="114-1"):
    session.add(StudentFeeRecord(student_id=st.id, student_name=st.name,
        classroom_name=cname, fee_item_name=item, amount_due=due,
        amount_paid=paid, status=status, period=period))


def test_probe(session):
    st, cname = _stu(session, "甲")
    _rec(session, st, cname, "學費", 10000, 10000, "paid")
    _rec(session, st, cname, "雜費", 5000, 0, "unpaid")
    session.add(StudentFeeAdjustment(student_id=st.id, period="114-1",
        adjustment_type="sibling_discount", amount=3000))
    session.flush()
    print("\n[adj=3000]")
    print("  status=unpaid:", compute_fee_summary(session, status="unpaid"))
    print("  period whole :", compute_fee_summary(session, period="114-1"))
    print("  status=paid  :", compute_fee_summary(session, status="paid"))

    st2, cname2 = _stu(session, "乙")
    _rec(session, st2, cname2, "學費", 10000, 10000, "paid")
    _rec(session, st2, cname2, "雜費", 5000, 0, "unpaid")
    session.add(StudentFeeAdjustment(student_id=st2.id, period="114-1",
        adjustment_type="prepayment", amount=8000))
    session.flush()
    print("\n[adj=8000 > unpaid due 5000]")
    print("  status=unpaid:", compute_fee_summary(session, status="unpaid", student_name="乙"))
    print("  period whole :", compute_fee_summary(session, period="114-1", student_name="乙"))
    assert True
