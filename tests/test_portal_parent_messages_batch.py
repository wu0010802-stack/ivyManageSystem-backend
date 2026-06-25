"""教師端討論串列表批次化（P2 N+1 修補）。

list_threads 對 page 內每個 thread 呼叫 _thread_summary，每串 4-5 查詢（Student、
parent User、last ParentMessage、unread aggregate、可能的 Guardian fallback）。
limit 預設 20、最大 100 → 教師收件匣單頁 80-100+ 查詢。

新增 _thread_summaries 批次入口：Student/parent 各一次 in_()；訊息一次撈回 Python
端算 last preview 與 per-thread unread（cutoff 為 per-thread）；缺 display_name 的
家長 Guardian 一次批次撈。語意與逐筆 _thread_summary 完全一致（本檔等價測試守護），
單筆 _thread_summary 仍供 get_thread 使用。
"""

from datetime import datetime

from sqlalchemy import event

from models.database import (
    Guardian,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
)
from api.portal.parent_messages import _thread_summaries, _thread_summary


class _SelectCounter:
    def __init__(self, engine):
        self._engine = engine
        self.count = 0
        self.statements: list[str] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on)
        return False


def _student(session, sid, name):
    s = Student(
        student_id=sid,
        name=name,
        is_active=True,
        enrollment_date=datetime(2025, 8, 1).date(),
        lifecycle_status="active",
    )
    session.add(s)
    session.flush()
    return s


def _parent(session, username, display_name=None):
    u = User(
        username=username,
        password_hash="x",
        role="parent",
        permission_names=[],
        is_active=True,
        display_name=display_name,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _thread(
    session, *, student_id, parent_id, teacher_id, last_read=None, last_at=None
):
    t = ParentMessageThread(
        student_id=student_id,
        parent_user_id=parent_id,
        teacher_user_id=teacher_id,
        teacher_last_read_at=last_read,
        last_message_at=last_at,
    )
    session.add(t)
    session.flush()
    return t


def _msg(session, *, thread_id, role, body, created_at, deleted_at=None, sender_id=1):
    m = ParentMessage(
        thread_id=thread_id,
        sender_user_id=sender_id,
        sender_role=role,
        body=body,
        created_at=created_at,
        deleted_at=deleted_at,
    )
    session.add(m)
    session.flush()
    return m


def _build_fixture(session):
    teacher_id = 999
    # Thread 1：家長有 display_name；1 則家長未讀（cutoff 後）+ 1 則教師訊息
    s1 = _student(session, "S001", "小明")
    p1 = _parent(session, "parent_line_a", display_name="家長甲")
    cutoff = datetime(2026, 6, 1, 9, 0)
    t1 = _thread(
        session,
        student_id=s1.id,
        parent_id=p1.id,
        teacher_id=teacher_id,
        last_read=cutoff,
        last_at=datetime(2026, 6, 1, 12, 0),
    )
    _msg(
        session,
        thread_id=t1.id,
        role="teacher",
        body="老師回覆",
        created_at=datetime(2026, 6, 1, 8, 0),
    )
    _msg(
        session,
        thread_id=t1.id,
        role="parent",
        body="家長最新訊息",
        created_at=datetime(2026, 6, 1, 12, 0),
    )

    # Thread 2：家長無 display_name 但有 Guardian（fallback 取 Guardian.name）；
    # 最後一則家長訊息已撤回（last_preview = "(已撤回)"）；cutoff=None → 全未讀
    s2 = _student(session, "S002", "小華")
    p2 = _parent(session, "parent_line_b", display_name=None)
    session.add(
        Guardian(student_id=s2.id, user_id=p2.id, name="王媽媽", is_primary=True)
    )
    session.flush()
    t2 = _thread(
        session,
        student_id=s2.id,
        parent_id=p2.id,
        teacher_id=teacher_id,
        last_read=None,
        last_at=datetime(2026, 6, 2, 10, 0),
    )
    _msg(
        session,
        thread_id=t2.id,
        role="parent",
        body="撤回前內容",
        created_at=datetime(2026, 6, 2, 10, 0),
        deleted_at=datetime(2026, 6, 2, 11, 0),
    )

    # Thread 3：家長無 display_name 無 Guardian（→「家長」）；無訊息
    s3 = _student(session, "S003", "小美")
    p3 = _parent(session, "parent_line_c", display_name=None)
    t3 = _thread(
        session,
        student_id=s3.id,
        parent_id=p3.id,
        teacher_id=teacher_id,
        last_read=None,
        last_at=None,
    )
    session.commit()
    return [t1, t2, t3]


def test_batch_matches_single_thread_summary(test_db_session):
    session = test_db_session
    threads = _build_fixture(session)

    batch = _thread_summaries(session, threads)
    single = [_thread_summary(session, t=t) for t in threads]

    assert batch == single

    # 顯式驗證關鍵欄位（不只比對兩實作）
    by_id = {row["id"]: row for row in batch}
    assert by_id[threads[0].id]["parent_name"] == "家長甲"
    assert by_id[threads[0].id]["unread_count"] == 1
    assert by_id[threads[0].id]["last_message_preview"] == "家長最新訊息"
    assert by_id[threads[1].id]["parent_name"] == "王媽媽"
    assert by_id[threads[1].id]["unread_count"] == 0  # 唯一一則已撤回（deleted）
    assert by_id[threads[1].id]["last_message_preview"] == "(已撤回)"
    assert by_id[threads[2].id]["parent_name"] == "家長"
    assert by_id[threads[2].id]["last_message_preview"] is None
    assert by_id[threads[2].id]["unread_count"] == 0


def test_batch_empty_returns_empty(test_db_session):
    assert _thread_summaries(test_db_session, []) == []


def test_batch_query_count_independent_of_thread_count(test_db_session):
    session = test_db_session
    teacher_id = 999
    threads = []
    for i in range(8):
        s = _student(session, f"Q{i:03d}", f"童{i}")
        p = _parent(session, f"parent_q{i}", display_name=f"家長{i}")
        t = _thread(
            session,
            student_id=s.id,
            parent_id=p.id,
            teacher_id=teacher_id,
            last_read=None,
            last_at=datetime(2026, 6, 1, 12, 0),
        )
        _msg(
            session,
            thread_id=t.id,
            role="parent",
            body=f"訊息{i}",
            created_at=datetime(2026, 6, 1, 12, 0),
        )
        threads.append(t)
    session.commit()

    # 對齊端點：先以一次 query 取回 threads（live in session，欄位已載入），
    # 再批次 summarize。否則 commit 後 thread 物件 expire，存取屬性會逐筆 reload
    # 而誤計入查詢數（那是測試 artifact，非端點真實行為）。
    fresh = (
        session.query(ParentMessageThread)
        .filter(ParentMessageThread.teacher_user_id == teacher_id)
        .all()
    )
    engine = session.get_bind()
    with _SelectCounter(engine) as ctr:
        rows = _thread_summaries(session, fresh)

    assert len(rows) == 8
    # Student in_ + parent in_ + 訊息 in_ ≈ 3 條（家長皆有 display_name 免 Guardian
    # fallback）。逐筆會是 8 × 4 ≈ 32，給足餘裕設 ≤ 5。
    assert (
        ctr.count <= 5
    ), f"討論串列表批次查詢數 {ctr.count} 超標（疑 N+1 回歸）：\n" + "\n".join(
        ctr.statements
    )
