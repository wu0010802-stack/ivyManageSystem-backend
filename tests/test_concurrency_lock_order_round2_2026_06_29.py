"""Track F — qa-loop round2（2026-06-29）兩個 ABBA 鎖序缺口（1 P2 + 1 P3）。

ABBA 死鎖在 SQLite 無法重現（with_for_update / advisory lock 皆 no-op、無真實列鎖），
故以「鎖取得順序」做確定性驗證（與 PG 上的死鎖根因等價）。

P2 delete_registration(force_refund) 反轉 canonical 鎖序：API 先鎖 reg、service 再鎖 course
→ 與 withdraw_course / confirm/decline（course→reg）ABBA。修法：API 先鎖 course（依 id 排序）
再鎖 reg，與 service 同序。

P3 finalize_salary_month 以未排序 records 順序取 per-employee 鎖，與 bulk 重算 sorted(employee_ids)
不一致 → ABBA。修法：finalize 改 sorted({r.employee_id}) 取鎖。
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import api.salary as salary_api
import api.activity.registrations as reg_api
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
import utils.advisory_lock as advisory_lock

# ── P3：finalize 依 employee_id 排序取鎖 ─────────────────────────────────────


def test_finalize_acquires_per_employee_locks_in_sorted_order(monkeypatch):
    locked: list[int] = []

    def _spy_lock(session, *, employee_id=None, year=None, month=None):
        if employee_id is not None:
            locked.append(employee_id)

    monkeypatch.setattr(advisory_lock, "acquire_salary_lock", _spy_lock)

    # records 以非排序順序返回（模擬 DB 無 ORDER BY 的不確定順序）
    recs = [
        MagicMock(employee_id=3),
        MagicMock(employee_id=1),
        MagicMock(employee_id=2),
    ]
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.all.return_value = recs

    @contextmanager
    def _fake_scope():
        yield fake_session

    monkeypatch.setattr(salary_api, "session_scope", _fake_scope)

    data = salary_api.FinalizeMonthRequest(year=2026, month=4)
    try:
        salary_api.finalize_salary_month(data, MagicMock(), current_user={"user_id": 1})
    except Exception:
        # 鎖在前段已取得；後段封存對 MagicMock record 可能 raise，不影響鎖序斷言。
        pass

    assert locked == [1, 2, 3], f"per-employee 鎖應依 employee_id 排序，實際 {locked}"


# ── P2：delete_registration 先鎖 course 再鎖 reg ─────────────────────────────


def test_delete_registration_locks_course_before_reg(monkeypatch):
    query_order: list[str] = []

    reg_mock = MagicMock(is_active=True, paid_amount=0, student_name="測試生")

    def _query_side_effect(arg, *rest):
        # InstrumentedAttribute（如 RegistrationCourse.course_id）取 .class_，model 取 __name__
        model = getattr(arg, "class_", None)
        name = (
            model.__name__ if model is not None else getattr(arg, "__name__", str(arg))
        )
        query_order.append(name)
        q = MagicMock()
        q.filter.return_value = q
        q.order_by.return_value = q
        q.with_for_update.return_value = q
        q.all.return_value = []
        q.first.return_value = reg_mock
        q.scalar.return_value = None
        q.__iter__ = lambda self: iter([(10,)])  # RegistrationCourse.course_id 產生器
        return q

    fake_session = MagicMock()
    fake_session.query.side_effect = _query_side_effect

    # 隔離本端點 fix（API 的 course-prelock）：stub 掉 service、advisory、快取失效。
    monkeypatch.setattr(
        reg_api, "acquire_activity_daily_close_lock", lambda *a, **k: None
    )
    monkeypatch.setattr(
        reg_api.activity_service, "delete_registration", lambda *a, **k: None
    )
    monkeypatch.setattr(
        reg_api, "_invalidate_activity_dashboard_caches", lambda *a, **k: None
    )
    monkeypatch.setattr(reg_api, "get_session", lambda: fake_session)

    try:
        reg_api.delete_registration(
            registration_id=1,
            request=MagicMock(),
            force_refund=True,
            refund_reason=None,
            current_user={"username": "adm"},
        )
    except Exception:
        pass

    assert (
        "ActivityCourse" in query_order
    ), "force_refund 路徑應先鎖 ActivityCourse（prelock）"
    assert query_order.index("ActivityCourse") < query_order.index(
        "ActivityRegistration"
    ), f"course 鎖應在 reg 鎖之前，實際順序 {query_order}"
