"""renderer 純函式測試。"""

from services.notification.renderers import Rendered, render, RENDERERS
from services.notification.event_types import NOTIFICATION_EVENT_TYPES


def test_all_event_types_have_renderer():
    """每個 event_type 必須有 renderer。"""
    missing = NOTIFICATION_EVENT_TYPES - set(RENDERERS.keys())
    assert not missing, f"缺 renderer: {missing}"


def test_render_leave_approved_happy_path():
    ctx = {
        "reviewer_name": "張主任",
        "leave_type": "事假",
        "start": "2026-06-01",
        "end": "2026-06-02",
        "leave_id": 42,
    }
    r = render("leave.approved", ctx)
    assert "張主任" in r.title
    assert "核准" in r.title
    assert "事假" in r.body
    assert r.deep_link == "/portal/leaves/42"


def test_render_unknown_event_type_fallback():
    """未註冊 event_type → 不拋例外，回 placeholder Rendered。"""
    r = render("unknown.event", {})
    assert r.title.startswith("(")
    assert "unknown.event" in r.title


def test_render_function_raises_returns_failure_placeholder():
    """renderer 函式內部炸 → render() catch + 回 (渲染失敗)。"""
    # ctx 缺必要 key → leave.approved renderer KeyError
    r = render("leave.approved", {})
    assert r.title == "(渲染失敗)"
    assert "leave.approved" in r.body
    assert r.deep_link is None


def test_render_parent_message_received_happy_path():
    ctx = {
        "teacher_name": "王老師",
        "student_name": "小明",
        "body_preview": "今天小明很乖",
        "thread_id": 7,
    }
    r = render("parent.message_received", ctx)
    assert "王老師" in r.title
    assert "小明" in r.title or "小明" in r.body
    assert r.deep_link is not None


def test_render_punch_correction_submitted_happy_path():
    ctx = {
        "submitter_name": "李老師",
        "target_date": "2026-06-01",
        "correction_id": 88,
    }
    r = render("punch_correction.submitted", ctx)
    assert "李老師" in r.title
    assert "補打卡" in r.title
    assert "2026-06-01" in r.body
    assert r.deep_link == "/approvals/punch-corrections/88"
