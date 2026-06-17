"""活動模組：own_class 教師對終態(退/畢/轉)學生的 birthday/student_id/classroom_id 應遮罩。

#4（qa-loop 全掃 2026-06-17，業主裁示遮 birthday+FK 保留姓名/金額）：
api/activity 的 student_pii_row_visible 原只比對 classroom_id、不看 lifecycle_status，
而 ActivityRegistration 是 denormalized 快照（終態學生仍掛原班 classroom_id），故 own_class
教師可在報名清單/明細/POS 看到終態學生的 birthday/student_id/classroom_id（students.py /
search.py 對 teacher 都已用 _TEACHER_BLOCKED_LIFECYCLE 排除）。

can_see_student=False 的既有語意正好遮 birthday+student_id+classroom_id、保留 student_name/
class_name/金額（見 registrations.py 序列化），符合業主決定；故 scoped caller 遇終態學生時
student_pii_row_visible 應回 False。full-scope(admin/HR, allowed=None)不遮（對齊 students.py
對 admin 不排除終態）。
"""

from __future__ import annotations

from api.activity._shared import student_pii_row_visible


def test_scoped_caller_terminal_student_pii_masked():
    # own_class（管轄班級 {5}），列在班級 5，但學生為終態 → 遮（回 False）
    assert student_pii_row_visible(True, {5}, 5, student_terminal=True) is False


def test_scoped_caller_active_in_class_visible():
    assert student_pii_row_visible(True, {5}, 5, student_terminal=False) is True


def test_full_scope_terminal_not_masked():
    # admin/HR 全園可見（allowed=None）：終態學生不遮（與 students.py 對 admin 一致）
    assert student_pii_row_visible(True, None, 5, student_terminal=True) is True


def test_scoped_caller_out_of_class_still_masked():
    # 非管轄班級照舊遮（不論終態與否）
    assert student_pii_row_visible(True, {5}, 9, student_terminal=False) is False


def test_no_pii_permission_always_masked():
    assert student_pii_row_visible(False, None, 5, student_terminal=False) is False


def test_terminal_student_ids_in_empty_no_db():
    """空輸入（或全 None）不查 DB、回空集合（caller 對 full-scope 略過時用）。"""
    from api.activity._shared import terminal_student_ids_in

    assert terminal_student_ids_in(None, []) == set()
    assert terminal_student_ids_in(None, [None]) == set()


def test_terminal_student_ids_in_filters_terminal(test_db_session):
    from api.activity._shared import terminal_student_ids_in
    from models.classroom import Student

    session = test_db_session
    active = Student(
        student_id="A1", name="在學", lifecycle_status="active", is_active=True
    )
    grad = Student(
        student_id="G1", name="畢業", lifecycle_status="graduated", is_active=False
    )
    wd = Student(
        student_id="W1", name="退學", lifecycle_status="withdrawn", is_active=False
    )
    session.add_all([active, grad, wd])
    session.commit()

    result = terminal_student_ids_in(session, [active.id, grad.id, wd.id, 99999])
    assert result == {
        grad.id,
        wd.id,
    }, "只應回終態(畢業/退學/轉出)學生，排除在學與不存在 id"
