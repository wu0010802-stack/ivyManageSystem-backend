"""
LINE Messaging API 通知服務
"""

import logging
from datetime import date, datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"


# ── 訊息建構（純函式，方便測試）──────────────────────────────────────────────


def build_leave_message(
    name: str,
    leave_type: str,
    start: date,
    end: date,
    hours: float,
) -> str:
    """建構請假通知訊息文字"""
    if start == end:
        date_str = start.isoformat()
    else:
        date_str = f"{start.isoformat()} ～ {end.isoformat()}"
    return (
        f"【請假申請】\n"
        f"員工：{name}\n"
        f"假別：{leave_type}\n"
        f"日期：{date_str}\n"
        f"時數：{hours}h\n"
        f"狀態：待主管核准"
    )


def build_overtime_message(
    name: str,
    ot_date: date,
    ot_type: str,
    hours: float,
    use_comp: bool,
) -> str:
    """建構加班通知訊息文字"""
    tag = "補休申請" if use_comp else "加班申請"
    return (
        f"【{tag}】\n"
        f"員工：{name}\n"
        f"類型：{ot_type}\n"
        f"日期：{ot_date.isoformat()}\n"
        f"時數：{hours}h\n"
        f"狀態：待主管核准"
    )


def build_leave_result_message(
    name: str,
    leave_type: str,
    start: date,
    end: date,
    approved: bool,
    reason: Optional[str] = None,
) -> str:
    """建構請假審核結果訊息文字"""
    if start == end:
        date_str = start.isoformat()
    else:
        date_str = f"{start.isoformat()} ～ {end.isoformat()}"
    status = "✅ 已核准" if approved else "❌ 已駁回"
    msg = (
        f"【請假審核結果】\n"
        f"員工：{name}\n"
        f"假別：{leave_type}\n"
        f"日期：{date_str}\n"
        f"狀態：{status}"
    )
    if not approved and reason:
        msg += f"\n駁回原因：{reason}"
    return msg


def build_overtime_result_message(
    name: str,
    ot_date: date,
    ot_type: str,
    approved: bool,
) -> str:
    """建構加班審核結果訊息文字"""
    status = "✅ 已核准" if approved else "❌ 已駁回"
    return (
        f"【加班審核結果】\n"
        f"員工：{name}\n"
        f"類型：{ot_type}\n"
        f"日期：{ot_date.isoformat()}\n"
        f"狀態：{status}"
    )


def build_salary_batch_message(
    year: int, month: int, count: int, total_net: int
) -> str:
    """建構薪資批次計算完成訊息文字"""
    return (
        f"【薪資計算完成】\n"
        f"期間：{year} 年 {month} 月\n"
        f"人數：{count} 人\n"
        f"實領總額：${total_net:,}"
    )


def build_activity_waitlist_promoted_message(
    student_name: str,
    course_name: str,
    deadline: Optional[datetime] = None,
) -> str:
    """建構才藝候補升位通知訊息文字。

    deadline 不為 None 時表示「升為待確認」（家長須於期限前確認接受）。
    deadline 為 None 時表示「管理員直升」或既有舊行為（立即生效）。
    """
    base = f"🎨 才藝候補升位通知\n" f"學生：{student_name}\n" f"課程：{course_name}\n"
    if deadline is None:
        return base + "已自動升為正式報名！"
    deadline_str = deadline.strftime("%Y-%m-%d %H:%M")
    return (
        base
        + f"已遞補為正式名額，請於 {deadline_str} 前至報名查詢頁確認接受；\n"
        + "逾期未確認將自動放棄，由下一位候補遞補。"
    )


def build_activity_waitlist_promotion_reminder_message(
    student_name: str,
    course_name: str,
    deadline: datetime,
) -> str:
    """建構候補升正式「剩餘時間」提醒訊息文字。"""
    deadline_str = deadline.strftime("%Y-%m-%d %H:%M")
    return (
        f"⏰ 才藝候補轉正提醒\n"
        f"學生：{student_name}\n"
        f"課程：{course_name}\n"
        f"請於 {deadline_str} 前完成確認，以免逾期放棄名額。"
    )


def build_activity_waitlist_promotion_expired_message(
    student_name: str, course_name: str
) -> str:
    """建構候補升正式「逾期自動放棄」訊息文字。"""
    return (
        f"⚠️ 才藝候補名額已釋出\n"
        f"學生：{student_name}\n"
        f"課程：{course_name}\n"
        f"因未於期限內確認，名額已自動釋出給下一位候補。"
        f"若需重新報名，請聯繫校方或於公開頁面重新送件。"
    )


def build_dismissal_message(
    student_name: str,
    classroom_name: str,
    note: Optional[str] = None,
) -> str:
    """建構接送通知訊息文字"""
    msg = f"【接送通知】\n" f"學生：{student_name}\n" f"班級：{classroom_name}"
    if note:
        msg += f"\n備註：{note}"
    return msg


# ── Service ──────────────────────────────────────────────────────────────────


class LineService:
    """LINE 通知 Singleton 服務，支援熱更新設定"""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._target_id: Optional[str] = None
        self._enabled: bool = False
        self._channel_secret: Optional[str] = None

    def configure(
        self,
        token: str,
        target_id: str,
        enabled: bool,
        channel_secret: Optional[str] = None,
    ) -> None:
        """熱更新設定（不需重啟服務）"""
        self._token = token
        self._target_id = target_id
        self._enabled = enabled
        if channel_secret is not None:
            self._channel_secret = channel_secret

    def _push(self, text: str) -> bool:
        """推送純文字訊息到 LINE 群組，成功回傳 True，失敗回傳 False"""
        if not self._enabled or not self._token or not self._target_id:
            return False
        try:
            resp = requests.post(
                _LINE_PUSH_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "to": self._target_id,
                    "messages": [{"type": "text", "text": text}],
                },
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(
                    "LINE API 回傳非 200: %s %s", resp.status_code, resp.text
                )
                return False
            return True
        except Exception as exc:
            logger.warning("LINE 推送失敗: %s", exc)
            return False

    def _push_to_user(self, line_user_id: str, text: str) -> bool:
        """推送純文字訊息給個人 LINE 用戶，成功回傳 True，失敗回傳 False"""
        if not self._enabled or not self._token or not line_user_id:
            return False
        try:
            resp = requests.post(
                _LINE_PUSH_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "to": line_user_id,
                    "messages": [{"type": "text", "text": text}],
                },
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning("LINE 個人推播失敗: %s %s", resp.status_code, resp.text)
                return False
            return True
        except Exception as exc:
            logger.warning("LINE 個人推播失敗: %s", exc)
            return False

    def _reply(self, reply_token: str, text: str) -> bool:
        """使用 LINE Reply API 回覆 Webhook 訊息，成功回傳 True，失敗回傳 False"""
        if not self._token or not reply_token:
            return False
        try:
            resp = requests.post(
                _LINE_REPLY_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "replyToken": reply_token,
                    "messages": [{"type": "text", "text": text}],
                },
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(
                    "LINE Reply API 失敗: %s %s", resp.status_code, resp.text
                )
                return False
            return True
        except Exception as exc:
            logger.warning("LINE Reply 失敗: %s", exc)
            return False

    # ── 公開通知方法 ──────────────────────────────────────────────────────────

    def notify_leave_submitted(
        self,
        name: str,
        leave_type: str,
        start: date,
        end: date,
        hours: float,
    ) -> None:
        """請假申請送出後通知（失敗時 log warning，不拋出）"""
        text = build_leave_message(name, leave_type, start, end, hours)
        self._push(text)

    def notify_overtime_submitted(
        self,
        name: str,
        ot_date: date,
        ot_type: str,
        hours: float,
        use_comp: bool,
    ) -> None:
        """加班申請送出後通知（失敗時 log warning，不拋出）"""
        text = build_overtime_message(name, ot_date, ot_type, hours, use_comp)
        self._push(text)

    def notify_leave_result(
        self,
        line_user_id: str,
        name: str,
        leave_type: str,
        start: date,
        end: date,
        approved: bool,
        reason: Optional[str] = None,
    ) -> None:
        """請假審核結果個人推播（失敗時 log warning，不拋出）"""
        text = build_leave_result_message(
            name, leave_type, start, end, approved, reason
        )
        self._push_to_user(line_user_id, text)

    def notify_overtime_result(
        self,
        line_user_id: str,
        name: str,
        ot_date: date,
        ot_type: str,
        approved: bool,
    ) -> None:
        """加班審核結果個人推播（失敗時 log warning，不拋出）"""
        text = build_overtime_result_message(name, ot_date, ot_type, approved)
        self._push_to_user(line_user_id, text)

    def notify_salary_batch_complete(
        self,
        year: int,
        month: int,
        count: int,
        total_net: int,
    ) -> None:
        """薪資批次計算完成後群組推播（失敗時 log warning，不拋出）"""
        text = build_salary_batch_message(year, month, count, total_net)
        self._push(text)

    def notify_activity_waitlist_promoted(
        self,
        student_name: str,
        course_name: str,
        deadline: Optional[datetime] = None,
    ) -> None:
        """才藝候補升位後群組推播（失敗時 log warning，不拋出）。

        deadline 為 None：升為正式且立即生效（管理員直升）。
        deadline 有值：升為 promoted_pending，家長須於期限前確認。
        """
        text = build_activity_waitlist_promoted_message(
            student_name, course_name, deadline
        )
        self._push(text)

    def notify_activity_waitlist_promotion_reminder(
        self,
        student_name: str,
        course_name: str,
        deadline: datetime,
    ) -> None:
        """候補轉正剩餘時間提醒（failsafe log warning）。"""
        text = build_activity_waitlist_promotion_reminder_message(
            student_name, course_name, deadline
        )
        self._push(text)

    def notify_activity_waitlist_promotion_expired(
        self,
        student_name: str,
        course_name: str,
    ) -> None:
        """候補轉正逾期自動放棄通知（failsafe log warning）。"""
        text = build_activity_waitlist_promotion_expired_message(
            student_name, course_name
        )
        self._push(text)

    def notify_dismissal_created(
        self,
        student_name: str,
        classroom_name: str,
        note: Optional[str] = None,
    ) -> None:
        """接送通知建立後群組推播（失敗時 log warning，不拋出）"""
        text = build_dismissal_message(student_name, classroom_name, note)
        self._push(text)

    def handle_webhook_message(
        self,
        line_user_id: str,
        text: str,
        reply_token: str,
        session,
    ) -> None:
        """處理 Webhook 收到的文字訊息，依指令回覆"""
        from models.auth import User
        from models.database import SalaryRecord, LeaveRecord, Attendance

        user = session.query(User).filter(User.line_user_id == line_user_id).first()
        if not user:
            self._reply(reply_token, "請先至 Portal 完成 LINE 綁定。")
            return

        emp_id = user.employee_id
        cmd = (text or "").strip()

        if cmd == "我的薪資":
            record = (
                session.query(SalaryRecord)
                .filter(SalaryRecord.employee_id == emp_id)
                .order_by(
                    SalaryRecord.salary_year.desc(), SalaryRecord.salary_month.desc()
                )
                .first()
            )
            if not record:
                self._reply(reply_token, "查無薪資記錄。")
                return
            reply = (
                f"【薪資摘要】{record.salary_year}/{record.salary_month:02d}\n"
                f"應發：${record.gross_salary:,.0f}\n"
                f"扣款：${record.total_deduction:,.0f}\n"
                f"實領：${record.net_salary:,.0f}"
            )
            self._reply(reply_token, reply)

        elif cmd == "我的假單":
            records = (
                session.query(LeaveRecord)
                .filter(LeaveRecord.employee_id == emp_id)
                .order_by(LeaveRecord.start_date.desc())
                .limit(3)
                .all()
            )
            if not records:
                self._reply(reply_token, "查無請假記錄。")
                return
            lines = ["【最近假單】"]
            for r in records:
                status = (
                    "✅ 核准"
                    if r.is_approved is True
                    else ("❌ 駁回" if r.is_approved is False else "⏳ 待審")
                )
                lines.append(f"• {r.leave_type} {r.start_date} {status}")
            self._reply(reply_token, "\n".join(lines))

        elif cmd == "我的打卡":
            from datetime import date as _date
            import calendar

            today = _date.today()
            records = (
                session.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.work_date >= _date(today.year, today.month, 1),
                    Attendance.work_date <= today,
                )
                .all()
            )
            late = sum(1 for r in records if r.is_late)
            missing = sum(1 for r in records if r.is_missing_punch)
            reply = f"【本月打卡統計】\n" f"遲到：{late} 次\n" f"缺打：{missing} 次"
            self._reply(reply_token, reply)

        else:
            help_text = (
                "【指令說明】\n"
                "• 我的薪資 — 查詢最近一筆薪資\n"
                "• 我的假單 — 查詢最近 3 筆假單\n"
                "• 我的打卡 — 查詢本月遲到/缺打統計"
            )
            self._reply(reply_token, help_text)
