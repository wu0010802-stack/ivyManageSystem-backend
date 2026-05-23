"""tests/test_recruitment_student_id.py

驗證學號自動產生 next_student_id_code：
- 單執行緒：用 SQLite in-memory（與 test_recruitment_conversion.py 同 pattern）
- 並發：50-thread on Postgres（pg_advisory_xact_lock 必須真 lock），用 env var 控管。
"""

import os
import sys
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student
from services.recruitment_funnel import next_student_id_code


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _add_student(session, sid: str):
    """最小新增 — 只填 NOT NULL 必要欄位。"""
    s = Student(
        student_id=sid,
        name=f"測試-{sid}",
        lifecycle_status="enrolled",
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


class TestNextStudentIdCodeSingleThread:
    def test_empty_pool_returns_01(self, session):
        code = next_student_id_code(session, school_year=115, class_code="A")
        assert code == "115-A-01"

    def test_increments_within_same_year_class(self, session):
        _add_student(session, "115-A-01")
        _add_student(session, "115-A-02")
        assert (
            next_student_id_code(session, school_year=115, class_code="A") == "115-A-03"
        )

    def test_independent_streams_per_class(self, session):
        _add_student(session, "115-A-01")
        _add_student(session, "115-A-02")
        assert (
            next_student_id_code(session, school_year=115, class_code="B") == "115-B-01"
        )

    def test_year_boundary_resets(self, session):
        _add_student(session, "115-A-05")
        assert (
            next_student_id_code(session, school_year=116, class_code="A") == "116-A-01"
        )

    def test_ignores_unrelated_format(self, session):
        # 既有非 NN 結尾的學號（legacy）不應影響流水計算
        _add_student(session, "115-A-LEGACY")
        assert (
            next_student_id_code(session, school_year=115, class_code="A") == "115-A-01"
        )


# ── 並發測試（需 real Postgres test DB） ────────────────────────────────────
PG_TEST_DSN = os.environ.get(
    "PG_TEST_DSN"
)  # 例：postgresql://yilunwu@localhost:5432/ivymanagement_test
pg_only = pytest.mark.skipif(
    not PG_TEST_DSN,
    reason="PG_TEST_DSN not set — 並發測試需 real Postgres",
)


@pg_only
def test_no_duplicate_under_50_threads():
    """50 thread 並發拿同 (year, class) 學號 + insert，必須 1..50 連續無重複。

    需 pg_advisory_xact_lock 真有效。
    """
    engine = create_engine(PG_TEST_DSN)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)

    # 清除測試資料避免污染
    cleanup_sess = Sess()
    cleanup_sess.query(Student).filter(Student.student_id.like("116-Z-%")).delete()
    cleanup_sess.commit()
    cleanup_sess.close()

    results: list[str] = []
    lock = threading.Lock()

    def worker(idx: int):
        sess = Sess()
        try:
            code = next_student_id_code(sess, school_year=116, class_code="Z")
            stu = Student(
                student_id=code,
                name=f"並發測試-{idx}",
                lifecycle_status="enrolled",
                is_active=True,
            )
            sess.add(stu)
            sess.commit()
            with lock:
                results.append(code)
        except Exception as e:
            sess.rollback()
            with lock:
                results.append(f"ERROR: {e}")
        finally:
            sess.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 清理測試資料
    cleanup_sess = Sess()
    cleanup_sess.query(Student).filter(Student.student_id.like("116-Z-%")).delete()
    cleanup_sess.commit()
    cleanup_sess.close()

    errors = [r for r in results if r.startswith("ERROR")]
    assert not errors, f"並發錯誤：{errors[:3]}"
    assert (
        len(set(results)) == 50
    ), f"有重複學號：{[r for r in results if results.count(r) > 1][:3]}"
    expected = {f"116-Z-{i:02d}" for i in range(1, 51)}
    assert set(results) == expected
