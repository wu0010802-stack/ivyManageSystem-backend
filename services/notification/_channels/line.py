"""LINE channel adapter — dispatch._fan_out 對 LINE channel 走 LINE_HANDLERS dict
查 event-specific handler 構訊息（含 Flex/quick-reply）。

Phase 4 Section 1 (2026-05-26)：19 個 event_type handler 註冊完成；dispatch 對註冊
event 走專屬 handler 取代 PR-A/B/C 的 fallback push_text，恢復家長 quick-reply
postback 等互動 UI。未註冊 event 仍 fallback 純文字 push_to_user。

evt.recipient_user_id 在 LINE adapter call 前已被 _fan_out._resolve_line_user_id
解析為 LINE user_id (str)，handler 直接用此值呼叫 line_service._push_to_user。

Phase 4 Section 2 將支援 group_id mode（dismissal hybrid 收尾）；本檔暫不接 group
推送，dismissal 事件未在 LINE_HANDLERS 註冊（caller 仍走 _line_service._notify_*
hybrid path）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

from services.line_service import (
    build_leave_message,
    build_overtime_message,
    build_leave_result_message,
    build_overtime_result_message,
    build_salary_batch_message,
    build_activity_waitlist_promoted_message,
    build_activity_waitlist_promotion_reminder_message,
    build_activity_waitlist_promotion_expired_message,
    build_activity_waitlist_final_reminder_message,
    _build_parent_leave_result_message,
)

logger = logging.getLogger(__name__)


def _parse_date(value) -> date | None:
    """Context 內 date 欄位可能是 isoformat str 或 date object；統一回 date。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.split("T", 1)[0])
        except ValueError:
            return None
    return None


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# ── 員工域 handlers ─────────────────────────────────────────────────────────


def _h_leave_submitted(ls, evt, rendered) -> None:
    """員工送出請假 → per-reviewer LINE 個人推送。"""
    ctx = evt.context
    start = _parse_date(ctx.get("start"))
    end = _parse_date(ctx.get("end"))
    text = build_leave_message(
        name=ctx.get("submitter_name", ""),
        leave_type=ctx.get("leave_type", ""),
        start=start,
        end=end,
        hours=ctx.get("leave_hours", 0),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_leave_approved(ls, evt, rendered) -> None:
    ctx = evt.context
    start = _parse_date(ctx.get("start"))
    end = _parse_date(ctx.get("end"))
    text = build_leave_result_message(
        name=ctx.get("submitter_name", ""),
        leave_type=ctx.get("leave_type", ""),
        start=start,
        end=end,
        approved=True,
        reason=None,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_leave_rejected(ls, evt, rendered) -> None:
    ctx = evt.context
    start = _parse_date(ctx.get("start"))
    end = _parse_date(ctx.get("end"))
    text = build_leave_result_message(
        name=ctx.get("submitter_name", ""),
        leave_type=ctx.get("leave_type", ""),
        start=start,
        end=end,
        approved=False,
        reason=ctx.get("rejection_reason"),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_overtime_submitted(ls, evt, rendered) -> None:
    ctx = evt.context
    ot_date = _parse_date(ctx.get("ot_date"))
    text = build_overtime_message(
        name=ctx.get("submitter_name", ""),
        ot_date=ot_date,
        ot_type=ctx.get("ot_type", ""),
        hours=ctx.get("hours", 0),
        use_comp=ctx.get("use_comp", False),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_overtime_approved(ls, evt, rendered) -> None:
    ctx = evt.context
    ot_date = _parse_date(ctx.get("ot_date"))
    text = build_overtime_result_message(
        name=ctx.get("submitter_name", ""),
        ot_date=ot_date,
        ot_type=ctx.get("ot_type", ""),
        approved=True,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_overtime_rejected(ls, evt, rendered) -> None:
    ctx = evt.context
    ot_date = _parse_date(ctx.get("ot_date"))
    text = build_overtime_result_message(
        name=ctx.get("submitter_name", ""),
        ot_date=ot_date,
        ot_type=ctx.get("ot_type", ""),
        approved=False,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_punch_correction_submitted(ls, evt, rendered) -> None:
    """員工送出補打卡 → per-reviewer LINE 個人推送。"""
    ctx = evt.context
    target = _parse_date(ctx.get("target_date"))
    name = ctx.get("submitter_name", "")
    target_str = target.isoformat() if target else ctx.get("target_date", "")
    text = f"【補打卡待審】{name} 送出 {target_str} 的補打卡申請，待核准"
    ls._push_to_user(evt.recipient_user_id, text)


def _h_punch_correction_approved(ls, evt, rendered) -> None:
    ctx = evt.context
    target = _parse_date(ctx.get("target_date"))
    status = "✅ 已核准"
    name = ctx.get("submitter_name", "")
    target_str = target.isoformat() if target else ctx.get("target_date", "")
    text = f"【補打卡審核結果】{name} {target_str} 的補打卡{status}"
    ls._push_to_user(evt.recipient_user_id, text)


def _h_punch_correction_rejected(ls, evt, rendered) -> None:
    ctx = evt.context
    target = _parse_date(ctx.get("target_date"))
    name = ctx.get("submitter_name", "")
    target_str = target.isoformat() if target else ctx.get("target_date", "")
    reason = ctx.get("rejection_reason")
    suffix = f"\n原因：{reason}" if reason else ""
    text = f"【補打卡審核結果】{name} {target_str} 的補打卡❌ 已駁回{suffix}"
    ls._push_to_user(evt.recipient_user_id, text)


def _h_salary_batch_completed(ls, evt, rendered) -> None:
    ctx = evt.context
    text = build_salary_batch_message(
        year=ctx.get("year", 0),
        month=ctx.get("month", 0),
        count=ctx.get("count", 0),
        total_net=ctx.get("total_net", 0),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_activity_waitlist_promoted(ls, evt, rendered) -> None:
    ctx = evt.context
    deadline = _parse_datetime(ctx.get("deadline"))
    text = build_activity_waitlist_promoted_message(
        student_name=ctx.get("student_name", ""),
        course_name=ctx.get("course_name", ""),
        deadline=deadline,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_activity_waitlist_reminder(ls, evt, rendered) -> None:
    ctx = evt.context
    deadline = _parse_datetime(ctx.get("deadline"))
    if deadline is None:
        ls._push_to_user(evt.recipient_user_id, rendered.title + "\n" + rendered.body)
        return
    text = build_activity_waitlist_promotion_reminder_message(
        student_name=ctx.get("student_name", ""),
        course_name=ctx.get("course_name", ""),
        deadline=deadline,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_activity_waitlist_final_reminder(ls, evt, rendered) -> None:
    ctx = evt.context
    deadline = _parse_datetime(ctx.get("deadline"))
    if deadline is None:
        ls._push_to_user(evt.recipient_user_id, rendered.title + "\n" + rendered.body)
        return
    from zoneinfo import ZoneInfo

    try:
        now = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)
        delta_seconds = (deadline - now).total_seconds()
        hours_left = max(1, int(delta_seconds // 3600))
    except Exception:
        hours_left = 6
    text = build_activity_waitlist_final_reminder_message(
        student_name=ctx.get("student_name", ""),
        course_name=ctx.get("course_name", ""),
        hours_left=hours_left,
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_activity_waitlist_expired(ls, evt, rendered) -> None:
    ctx = evt.context
    text = build_activity_waitlist_promotion_expired_message(
        student_name=ctx.get("student_name", ""),
        course_name=ctx.get("course_name", ""),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_pos_unlock_requested(ls, evt, rendered) -> None:
    ctx = evt.context
    target = _parse_date(ctx.get("target_date"))
    target_str = target.isoformat() if target else ctx.get("target_date", "")
    label = "管理員 override 解鎖" if ctx.get("is_override") else "解鎖"
    text = (
        f"📝 POS 日結{label}通知\n"
        f"日期：{target_str}\n"
        f"原簽核人：{ctx.get('original_approver', '')}（您）\n"
        f"解鎖人：{ctx.get('requester_name', '')}\n"
        f"原因：{ctx.get('reason', '')}\n\n"
        "請至後台確認異常稽核軌跡。"
    )
    ls._push_to_user(evt.recipient_user_id, text)


# ── 家長域 handlers ─────────────────────────────────────────────────────────


def _h_parent_message_received(ls, evt, rendered) -> None:
    """家長端：老師訊息推送，帶 quick-reply postback（thread_id 給定時）。"""
    ctx = evt.context
    snippet = (ctx.get("body_preview") or "").strip() or "(附件)"
    if len(snippet) > 60:
        snippet = snippet[:60] + "…"
    teacher_name = ctx.get("teacher_name", "老師")
    student_name = ctx.get("student_name")
    prefix = f"💬 {teacher_name} 傳了新訊息"
    if student_name:
        prefix += f"（{student_name}）"
    text = f"{prefix}：\n{snippet}\n\n可直接回覆此訊息或開啟家長 App。"

    thread_id = ctx.get("thread_id")
    if thread_id is not None:
        quick_reply = {
            "items": [
                {
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": "💬 回覆此訊息",
                        "data": f"thread_id={thread_id}",
                        "displayText": "回覆此訊息",
                    },
                }
            ]
        }
        ls._push_to_user_with_quick_reply(evt.recipient_user_id, text, quick_reply)
    else:
        ls._push_to_user(evt.recipient_user_id, text)


def _h_parent_announcement(ls, evt, rendered) -> None:
    """家長公告推播：含影像附件時推 flex bubble + hero；否則純文字。"""
    ctx = evt.context
    title = ctx.get("title", "")
    preview = ctx.get("preview")
    text = f"【園所公告】\n{title}"
    if preview:
        snippet = preview.strip()
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        text += f"\n{snippet}"
    text += "\n請開啟家長 App 查看詳情。"

    hero_url = getattr(rendered, "hero_url", None)
    if hero_url:
        flex = {
            "type": "bubble",
            "hero": {
                "type": "image",
                "url": hero_url,
                "size": "full",
                "aspectRatio": "20:13",
                "aspectMode": "cover",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "📣 園所公告",
                        "weight": "bold",
                        "size": "sm",
                        "color": "#888888",
                    },
                    {
                        "type": "text",
                        "text": title,
                        "weight": "bold",
                        "size": "md",
                        "wrap": True,
                    },
                ],
            },
        }
        if preview:
            snippet = preview.strip()
            if len(snippet) > 80:
                snippet = snippet[:80] + "…"
            flex["body"]["contents"].append(
                {
                    "type": "text",
                    "text": snippet,
                    "size": "sm",
                    "color": "#555555",
                    "wrap": True,
                    "margin": "md",
                }
            )
        try:
            ls.push_flex_to_user(
                evt.recipient_user_id,
                flex,
                alt_text=f"園所公告：{title}",
            )
            return
        except Exception:
            # flex 推播失敗 fallback 文字
            pass

    ls._push_to_user(evt.recipient_user_id, text)


def _h_parent_event_ack_required(ls, evt, rendered) -> None:
    ctx = evt.context
    title = ctx.get("event_title", "")
    deadline = _parse_date(ctx.get("deadline"))
    body = f"【需簽閱】\n事件：{title}"
    if deadline:
        body += f"\n簽閱截止：{deadline.isoformat()}"
    body += "\n請開啟家長 App 完成簽閱。"
    ls._push_to_user(evt.recipient_user_id, body)


def _h_parent_fee_due(ls, evt, rendered) -> None:
    ctx = evt.context
    due = _parse_date(ctx.get("due_date"))
    due_str = due.isoformat() if due else ctx.get("due_date", "")
    text = (
        f"【繳費提醒】\n"
        f"學生：{ctx.get('student_name', '')}\n"
        f"項目：{ctx.get('item_name', '')}\n"
        f"未繳金額：${ctx.get('amount', 0)}\n"
        f"繳費期限：{due_str}"
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_parent_leave_result(ls, evt, rendered) -> None:
    ctx = evt.context
    start = _parse_date(ctx.get("start"))
    end = _parse_date(ctx.get("end"))
    text = _build_parent_leave_result_message(
        student_name=ctx.get("student_name", ""),
        leave_type=ctx.get("leave_type", ""),
        start=start,
        end=end,
        approved=ctx.get("approved", True),
        review_note=ctx.get("review_note"),
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_parent_attendance_alert(ls, evt, rendered) -> None:
    ctx = evt.context
    target = _parse_date(ctx.get("target_date"))
    target_str = target.isoformat() if target else ctx.get("target_date", "")
    text = (
        f"【出席提醒】\n"
        f"學生：{ctx.get('student_name', '')}\n"
        f"日期：{target_str}\n"
        f"狀態：{ctx.get('status', ctx.get('detail', ''))}\n"
        f"如有誤請聯絡老師。"
    )
    ls._push_to_user(evt.recipient_user_id, text)


def _h_parent_contact_book_published(ls, evt, rendered) -> None:
    ctx = evt.context
    log_date = _parse_date(ctx.get("date"))
    date_str = log_date.isoformat() if log_date else ctx.get("date", "")
    teacher_note = ctx.get("teacher_note_preview")
    photo_count = ctx.get("photo_count", 0)
    body = (
        f"【今日聯絡簿】\n" f"學生：{ctx.get('student_name', '')}\n" f"日期：{date_str}"
    )
    if teacher_note:
        snippet = teacher_note.strip()
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        body += f"\n老師留言：{snippet}"
    if photo_count > 0:
        body += f"\n附 {photo_count} 張照片"
    body += "\n請開啟家長 App 查看完整內容。"
    ls._push_to_user(evt.recipient_user_id, body)


def _h_growth_report_published(ls, evt, rendered) -> bool:
    """Growth Report 推家長。Phase 4 Section 3 後 reports.py send-line 走
    dispatch.send_to_line_user_sync 觸發此 handler；支援 caller 傳
    `custom_message` 覆蓋預設模板（admin 推送時可指定）。

    本 handler 回 bool（push API 結果）讓 send_to_line_user_sync 拿真實 ACK；
    其他 handler 沿用 -> None signature（fan-out caller 不 propagate）。
    """
    ctx = evt.context
    custom = ctx.get("custom_message")
    if custom:
        text = custom
    else:
        period = ctx.get("period", "")
        student_name = ctx.get("student_name", "")
        text = f"📊 {student_name} {period} 成長報告已備好\n請至家長 App 查看下載。"
    return ls._push_to_user(evt.recipient_user_id, text)


# ── 群組推送 handlers (Phase 4 Section 2) ───────────────────────────────────


def _h_dismissal_created(ls, evt, rendered) -> None:
    """接送通知建立 → LINE 群組推送。

    Section 2: 走 evt.line_group_id（caller 從 settings 或 context 帶入）+
    line_service.push_text_to_group。Caller (api/dismissal_calls.py) 從
    dispatch.enqueue(line_group_id=...) 傳入；dispatch._fan_out 對 line_group_id
    設值的 event 跳過 _resolve_line_user_id 直接走本 handler。
    """
    ctx = evt.context
    from services.line_service import build_dismissal_message

    text = build_dismissal_message(
        student_name=ctx.get("student_name", ""),
        classroom_name=ctx.get("classroom_name", ""),
        note=ctx.get("note"),
    )
    group_id = evt.line_group_id or ls._target_id
    if not group_id:
        logger.warning(
            "dismissal.created LINE push 略過：line_group_id 與 line_service._target_id 都空"
        )
        return
    ls.push_text_to_group(group_id, text)


# event_type → handler(line_service, evt, rendered)
LINE_HANDLERS: dict[str, Callable] = {
    # 員工域 (12)
    "leave.submitted": _h_leave_submitted,
    "leave.approved": _h_leave_approved,
    "leave.rejected": _h_leave_rejected,
    "overtime.submitted": _h_overtime_submitted,
    "overtime.approved": _h_overtime_approved,
    "overtime.rejected": _h_overtime_rejected,
    "punch_correction.submitted": _h_punch_correction_submitted,
    "punch_correction.approved": _h_punch_correction_approved,
    "punch_correction.rejected": _h_punch_correction_rejected,
    "salary.batch_completed": _h_salary_batch_completed,
    "activity.waitlist_promoted": _h_activity_waitlist_promoted,
    "pos.unlock_requested": _h_pos_unlock_requested,
    # 家長域 (7)
    "parent.message_received": _h_parent_message_received,
    "parent.announcement": _h_parent_announcement,
    "parent.event_ack_required": _h_parent_event_ack_required,
    "parent.fee_due": _h_parent_fee_due,
    "parent.leave_result": _h_parent_leave_result,
    "parent.attendance_alert": _h_parent_attendance_alert,
    "parent.contact_book_published": _h_parent_contact_book_published,
    # 才藝家長域 (3)
    "activity.waitlist_reminder": _h_activity_waitlist_reminder,
    "activity.waitlist_final_reminder": _h_activity_waitlist_final_reminder,
    "activity.waitlist_expired": _h_activity_waitlist_expired,
    # 家長 Growth Report (1)
    "growth_report.published": _h_growth_report_published,
    # 群組推送 (Section 2)
    "dismissal.created": _h_dismissal_created,
}


class LineAdapter:
    def __init__(self, line_service):
        self._ls = line_service

    def send(self, evt, rendered, *, log_id: int) -> None:
        # log_id 留作 Section 3 push receipt 追蹤；v1 不用
        # dispatch 情境：LINE push HTTP 失敗須 raise（非靜默誤記送達），讓
        # dispatch._fan_out / retry_scheduler 偵測失敗並排重試。webhook reply
        # 等不經此 adapter，維持 bool 回傳不受影響。
        from services.line_service import dispatch_delivery_strict

        with dispatch_delivery_strict():
            handler = LINE_HANDLERS.get(evt.event_type)
            if handler is None:
                # group mode 走專屬 handler；個人 mode 需 str recipient_user_id
                # （_fan_out 已 pre-resolve）；其他情境是 caller 錯
                if evt.line_group_id is None and not isinstance(
                    evt.recipient_user_id, str
                ):
                    raise ValueError(
                        f"LINE adapter 收到非 str recipient_user_id={evt.recipient_user_id!r}; "
                        "_fan_out 應先呼叫 _resolve_line_user_id"
                    )
                text = (rendered.title or "") + (
                    "\n" + rendered.body if rendered.body else ""
                )
                self._ls.push_text_to_user(evt.recipient_user_id, text)
                return
            handler(self._ls, evt, rendered)
