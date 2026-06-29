"""每 event_type 對應一個 (title, body, deep_link) 純函式 renderer。

新增 event_type 必須在此檔加一個 @renderer(...) 裝飾的函式，否則 _fan_out
fallback 為 placeholder title（不會拋例外，但通知中心顯示「(event_type)」很醜）。

renderer 內部炸例外時 render() 會 catch + 回 (渲染失敗)，log row 仍會寫入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    deep_link: str | None
    hero_url: str | None = None


RENDERERS: dict[str, Callable[[dict], Rendered]] = {}


def renderer(event_type: str):
    def deco(fn: Callable[[dict], Rendered]) -> Callable[[dict], Rendered]:
        RENDERERS[event_type] = fn
        return fn

    return deco


def render(event_type: str, ctx: dict) -> Rendered:
    fn = RENDERERS.get(event_type)
    if fn is None:
        return Rendered(title=f"({event_type})", body="", deep_link=None)
    try:
        return fn(ctx)
    except Exception:
        logger.exception("renderer 失敗 event=%s", event_type)
        return Rendered(
            title="(渲染失敗)", body=f"event_type={event_type}", deep_link=None
        )


# ────────────────────── 員工域 ──────────────────────


@renderer("leave.submitted")
def _r_leave_submitted(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['submitter_name']} 送出請假申請",
        body=f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}",
        deep_link=f"/approvals/leaves/{ctx['leave_id']}",
    )


@renderer("leave.approved")
def _r_leave_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的請假",
        body=f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}",
        deep_link=f"/portal/leaves/{ctx['leave_id']}",
    )


@renderer("leave.rejected")
def _r_leave_rejected(ctx: dict) -> Rendered:
    body = f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}"
    if ctx.get("rejection_reason"):
        body += f"\n原因：{ctx['rejection_reason']}"
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的請假",
        body=body,
        deep_link=f"/portal/leaves/{ctx['leave_id']}",
    )


@renderer("overtime.submitted")
def _r_overtime_submitted(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['submitter_name']} 送出加班申請",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/approvals/overtimes/{ctx['overtime_id']}",
    )


@renderer("overtime.approved")
def _r_overtime_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的加班",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/portal/overtimes/{ctx['overtime_id']}",
    )


@renderer("overtime.rejected")
def _r_overtime_rejected(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的加班",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/portal/overtimes/{ctx['overtime_id']}",
    )


@renderer("punch_correction.submitted")
def _r_punch_corr_submitted(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['submitter_name']} 送出補打卡申請",
        body=f"日期：{ctx['target_date']}",
        deep_link=f"/approvals/punch-corrections/{ctx['correction_id']}",
    )


@renderer("punch_correction.approved")
def _r_punch_corr_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的補打卡",
        body=f"日期：{ctx['target_date']}",
        deep_link=f"/portal/punch-corrections/{ctx['correction_id']}",
    )


@renderer("punch_correction.rejected")
def _r_punch_corr_rejected(ctx: dict) -> Rendered:
    body = f"日期：{ctx['target_date']}"
    if ctx.get("rejection_reason"):
        body += f"\n原因：{ctx['rejection_reason']}"
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的補打卡",
        body=body,
        deep_link=f"/portal/punch-corrections/{ctx['correction_id']}",
    )


@renderer("salary.batch_completed")
def _r_salary_batch(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['year']}/{ctx['month']:02d} 薪資批次已完成",
        body=f"共 {ctx.get('count', 0)} 筆",
        deep_link=f"/salary/{ctx['year']}/{ctx['month']}",
    )


@renderer("activity.waitlist_promoted")
def _r_activity_waitlist(ctx: dict) -> Rendered:
    body = f"學生：{ctx.get('student_name', '')}"
    # F1（2026-06-29 audit）：被升候補無家長 App/LINE 通知管道時，提醒承辦 staff
    # 改以電話主動外撥通知確認（否則 48h 確認時鐘到期家長毫無所悉 → 靜默失位）。
    if ctx.get("no_parent_channel"):
        phone = ctx.get("parent_phone") or ""
        body += f"\n⚠ 此候補無家長 App/LINE 通知管道，請以電話 {phone} 主動通知確認"
    return Rendered(
        title=f"候補轉正：{ctx['course_name']}",
        body=body,
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("pos.unlock_requested")
def _r_pos_unlock(ctx: dict) -> Rendered:
    return Rendered(
        title=f"POS 解鎖請求：{ctx['requester_name']}",
        body=ctx.get("reason", ""),
        deep_link=f"/pos/unlock-requests/{ctx['request_id']}",
    )


@renderer("dismissal.created")
def _r_dismissal_created(ctx: dict) -> Rendered:
    body = f"班級：{ctx['classroom_name']}"
    if ctx.get("note"):
        body += f"\n備註：{ctx['note']}"
    return Rendered(
        title=f"接送通知：{ctx['student_name']}",
        body=body,
        deep_link=None,  # 群組推播，無個人深連結
    )


# ────────────────────── 家長域 ──────────────────────


@renderer("parent.message_received")
def _r_parent_message(ctx: dict) -> Rendered:
    snippet = (ctx.get("body_preview") or "").strip() or "(附件)"
    if len(snippet) > 60:
        snippet = snippet[:60] + "…"
    title = f"💬 {ctx['teacher_name']} 傳了新訊息"
    if ctx.get("student_name"):
        title += f"（{ctx['student_name']}）"
    return Rendered(
        title=title,
        body=snippet,
        deep_link=(
            f"/parent/messages/{ctx['thread_id']}"
            if ctx.get("thread_id")
            else "/parent/messages"
        ),
    )


@renderer("parent.announcement")
def _r_parent_announcement(ctx: dict) -> Rendered:
    attachments = ctx.get("attachments") or []
    first_image = next(
        (
            a
            for a in attachments
            if (a.get("mime_type") or "").startswith("image/") and a.get("thumb_url")
        ),
        None,
    )
    hero_url: str | None = None
    if first_image:
        from urllib.parse import urljoin

        try:
            from config import get_settings

            base = getattr(get_settings().misc, "ivy_api_base_url", None) or ""
        except Exception:
            base = ""
        if base:
            hero_url = urljoin(base, first_image["thumb_url"])
    return Rendered(
        title=f"📣 園所公告：{ctx['title']}",
        body=ctx.get("preview", "")[:80],
        deep_link=f"/parent/announcements/{ctx['announcement_id']}",
        hero_url=hero_url,
    )


@renderer("parent.event_ack_required")
def _r_parent_event_ack(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📋 待簽事件：{ctx['event_title']}",
        body=f"請於 {ctx.get('deadline', '盡快')} 前完成簽核",
        deep_link=f"/parent/event-ack/{ctx['event_id']}",
    )


@renderer("parent.fee_due")
def _r_parent_fee_due(ctx: dict) -> Rendered:
    return Rendered(
        title=f"💰 學費到期：{ctx['amount']} 元",
        body=f"繳費期限：{ctx['due_date']}",
        deep_link="/parent/fees",
    )


@renderer("parent.leave_result")
def _r_parent_leave_result(ctx: dict) -> Rendered:
    verb = "已核准" if ctx["approved"] else "已駁回"
    body = f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}"
    if not ctx["approved"] and ctx.get("review_note"):
        body += f"\n原因：{ctx['review_note']}"
    return Rendered(
        title=f"{ctx['student_name']} 的請假 {verb}",
        body=body,
        deep_link="/parent/leaves",
    )


@renderer("parent.attendance_alert")
def _r_parent_attendance(ctx: dict) -> Rendered:
    return Rendered(
        title=f"⚠️ {ctx['student_name']} 出席異常",
        body=ctx.get("detail", ""),
        deep_link="/parent/attendance",
    )


@renderer("parent.contact_book_published")
def _r_parent_contact_book(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📖 {ctx['student_name']} 今日聯絡簿已發布",
        body=f"日期：{ctx['date']}",
        deep_link=f"/parent/contact-book/{ctx['date']}",
    )


# ────────────────────── 才藝家長域（PR-C） ──────────────────────


@renderer("activity.waitlist_reminder")
def _r_activity_waitlist_reminder(ctx: dict) -> Rendered:
    return Rendered(
        title=f"⏰ 候補提醒：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}，請於 {ctx.get('deadline', '近日')} 前確認",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("activity.waitlist_final_reminder")
def _r_activity_waitlist_final(ctx: dict) -> Rendered:
    return Rendered(
        title=f"🚨 候補最後提醒：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}，候補名額即將釋出",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("activity.waitlist_expired")
def _r_activity_waitlist_expired(ctx: dict) -> Rendered:
    return Rendered(
        title=f"❌ 候補名額已過期：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


# ────────────────────── 家長 Growth Report（PR-C） ──────────────────────


@renderer("growth_report.published")
def _r_growth_report(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📊 {ctx['student_name']} {ctx.get('period', '')} 成長報告已發布",
        body=ctx.get("summary", ""),
        deep_link=f"/parent/growth-reports/{ctx['report_id']}",
    )
