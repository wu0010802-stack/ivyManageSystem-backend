"""驗證 notify_approval 統一入口：dispatch 正確、failure log 不拋、reason 帶入駁回。"""

from datetime import date
from unittest.mock import MagicMock

from services.notification.approval_notifier import notify_approval


def test_notify_approval_leave_approved_dispatches_to_leave_result():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={
            "leave_type": "事假",
            "start": date(2026, 5, 15),
            "end": date(2026, 5, 16),
        },
    )
    mock_line.notify_leave_result.assert_called_once_with(
        "U123",
        "王小明",
        "事假",
        date(2026, 5, 15),
        date(2026, 5, 16),
        True,
        None,
    )


def test_notify_approval_leave_rejected_passes_reason():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="reject",
        line_user_id="U123",
        name="王小明",
        context={
            "leave_type": "事假",
            "start": date(2026, 5, 15),
            "end": date(2026, 5, 16),
        },
        rejection_reason="證明不足",
    )
    mock_line.notify_leave_result.assert_called_once_with(
        "U123",
        "王小明",
        "事假",
        date(2026, 5, 15),
        date(2026, 5, 16),
        False,
        "證明不足",
    )


def test_notify_approval_overtime_dispatches_to_overtime_result():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="overtime",
        action="approve",
        line_user_id="U123",
        name="李小華",
        context={"ot_date": date(2026, 5, 10), "ot_type": "平日"},
    )
    mock_line.notify_overtime_result.assert_called_once_with(
        "U123",
        "李小華",
        date(2026, 5, 10),
        "平日",
        True,
    )


def test_notify_approval_punch_correction_dispatches():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="punch_correction",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={"target_date": date(2026, 5, 8)},
    )
    mock_line.notify_punch_correction_result.assert_called_once_with(
        "U123",
        "王小明",
        date(2026, 5, 8),
        True,
        None,
    )


def test_notify_approval_no_line_service_is_noop():
    """line_service=None 時靜默跳過（dev 環境 LIFF 未設）"""
    notify_approval(
        line_service=None,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={
            "leave_type": "事假",
            "start": date(2026, 5, 15),
            "end": date(2026, 5, 15),
        },
    )  # 不拋例外即可


def test_notify_approval_no_line_user_id_is_noop():
    """line_user_id 為空時靜默跳過（員工未綁 LINE）"""
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="approve",
        line_user_id=None,
        name="王小明",
        context={
            "leave_type": "事假",
            "start": date(2026, 5, 15),
            "end": date(2026, 5, 15),
        },
    )
    mock_line.notify_leave_result.assert_not_called()


def test_notify_approval_line_service_failure_is_swallowed():
    """LineService 內部失敗（API down）應 log 不拋"""
    mock_line = MagicMock()
    mock_line.notify_leave_result.side_effect = RuntimeError("LINE API down")
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={
            "leave_type": "事假",
            "start": date(2026, 5, 15),
            "end": date(2026, 5, 15),
        },
    )  # 不拋例外即可
