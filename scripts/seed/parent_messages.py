"""scripts/seed/parent_messages.py — 親師訊息（家長 ↔ 教師私訊）dev DB 示範資料。

灌入兩張表的示範資料，供前端家長端 / 教師端「親師訊息」功能手測：
- parent_message_threads：家長 ↔ 班導 1對1 對話串（三元組 UNIQUE）
- parent_messages        ：append-only 訊息（sender_role='parent' / 'teacher' 交錯）

冪等契約：每筆插入前先 exists 查；重跑必新增 0 筆、不刪改現有資料。
  - thread：自然鍵 = UNIQUE(parent_user_id, teacher_user_id, student_id)
  - message：自然鍵 = UNIQUE(thread_id, client_request_id)，client_request_id
            用決定論字串 f"seed-pmsg-{thread_id}-{idx}" → 重跑命中、新增 0 筆

⚠ 家長身分限制（這是「可建 thread 學生數」少的原因，非 bug）：
  thread 三元組要求 parent_user_id 與 teacher_user_id 都是有效 users.id，且
  service 層（services/parent_message_service.assert_teacher_is_homeroom）規定
  thread 只能由「該生班導」發起。dev DB 中：
    - parent role User 僅 4 個，其中 3 個（99401/99402/99403）是 phase1e 測試殘留
      （student 不存在或未分班）；
    - 真正可用的只有 student 1：guardian.user_id=5（parent）、classroom 1 的
      head_teacher=employee 59、employee 59 對應 users.id=2（teacher 側）。
  故三元組唯一可建：(parent_user_id=5, teacher_user_id=2, student_id=1)
  → 全 dev DB 僅 1 條 thread。所有示範深度集中在這條 thread 的訊息串裡。
  建立家長 User / 綁定不在本模組範圍，故不擴充，僅就有效子集示範並於回報說明。

日期界線：所有訊息 created_at 落在 2025-08-01 ~ 2026-06-05，絕不生未來。
  每則訊息明確帶遞增的 naive datetime（不依賴 model 的 now_taipei_naive 預設，
  否則全部會蓋成今天、失去時序與歷史散布）。thread.last_message_at 對齊最後一則。

已讀 / 未讀（對齊 service 的未讀 query 語意）：
  count_unread_for_parent 數 sender_role='teacher' 且 created_at > parent_last_read_at；
  count_unread_for_teacher 數 sender_role='parent' 且 created_at > teacher_last_read_at。
  本 seed 設：
    - teacher_last_read_at = 最後一則 parent 訊息之後 → 教師端 0 未讀（已讀完）
    - parent_last_read_at  = 最後一則 teacher 訊息之前 → 家長端尚有 1 則未讀
  以單一 thread 同時呈現「部分已讀、部分未讀」兩種狀態供前端手測。
"""

from __future__ import annotations

import logging
from datetime import datetime

from scripts.seed._common import session_scope
from models.guardian import Guardian
from models.classroom import Classroom, Student
from models.auth import User
from models.parent_message import ParentMessage, ParentMessageThread

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("seed_parent_messages")


# 一條 thread 的訊息腳本（家長提問 ↔ 老師回覆交錯，繁中、台灣幼兒園口吻）。
# role：'parent'(sender_user_id=parent) / 'teacher'(sender_user_id=teacher)
# when：明確 naive datetime，必須落在 2025-08-01 ~ 2026-06-05 且遞增。
# 主題：上學期請假詢問 → 孩子狀況關心 → 下學期活動報名問題。
_MESSAGE_SCRIPT: list[tuple[str, str, datetime]] = [
    (
        "parent",
        "老師您好，小寶這兩天有點輕微感冒咳嗽，明天想請一天假在家休息，"
        "請問需要補請假單嗎？謝謝老師。",
        datetime(2025, 9, 18, 20, 12),
    ),
    (
        "teacher",
        "媽媽您好，收到了，明天的假我這邊先幫小寶登記，假單在家長端 App"
        "送出即可，不用急著補。讓他在家多休息、多喝溫開水，祝早日康復！",
        datetime(2025, 9, 18, 21, 5),
    ),
    (
        "parent",
        "好的，謝謝老師這麼貼心！那他回學校後飲食上需要注意什麼嗎？",
        datetime(2025, 9, 19, 8, 40),
    ),
    (
        "teacher",
        "回來後先觀察一兩天，餐點我們會留意是否吃得下，若有咳嗽會提醒他多喝水。"
        "今天小寶在家還好嗎？",
        datetime(2025, 9, 19, 16, 30),
    ),
    (
        "parent",
        "今天精神好很多了，已經退燒，謝謝老師關心，明天就讓他正常上學！",
        datetime(2025, 9, 19, 19, 55),
    ),
    (
        "teacher",
        "太好了！那我們明天見。另外提醒一下，下個月園所有親子運動會，"
        "報名表這週會發下去，到時候歡迎家長一起來同樂喔～",
        datetime(2025, 10, 2, 17, 20),
    ),
]


def step() -> None:
    """灌入親師訊息示範資料（冪等）。"""
    added_threads = 0
    added_messages = 0

    with session_scope() as session:
        # ---- 解析唯一可建的三元組 (parent_user_id, teacher_user_id, student_id) ----
        # 取有綁定 parent User 的學生，且該生已分班、班級有班導、班導對應到一個 User。
        viable: list[tuple[int, int, int]] = (
            []
        )  # (parent_user_id, teacher_user_id, student_id)
        rows = (
            session.query(Guardian.student_id, Guardian.user_id)
            .filter(Guardian.user_id.isnot(None))
            .all()
        )
        seen_students: set[int] = set()
        for student_id, parent_user_id in rows:
            if student_id in seen_students:
                continue
            seen_students.add(student_id)

            parent_user = (
                session.query(User)
                .filter(User.id == parent_user_id, User.role == "parent")
                .first()
            )
            if parent_user is None:
                continue

            student = (
                session.query(Student)
                .filter(Student.id == student_id, Student.is_active.is_(True))
                .first()
            )
            if student is None or not student.classroom_id:
                # 學生不存在 / 未分班（典型為 phase1e 測試殘留）→ 無法掛 thread
                continue

            classroom = (
                session.query(Classroom)
                .filter(Classroom.id == student.classroom_id)
                .first()
            )
            if classroom is None or not classroom.head_teacher_id:
                continue

            # 班導 employee → 對應 User（teacher 側必須是 users.id）
            teacher_user = (
                session.query(User)
                .filter(User.employee_id == classroom.head_teacher_id)
                .order_by(User.id)
                .first()
            )
            if teacher_user is None:
                continue

            viable.append((parent_user.id, teacher_user.id, student_id))

        if not viable:
            logger.warning(
                "找不到任何「有 parent User + 已分班 + 班導有對應 User」的學生，"
                "跳過親師訊息 seed（dev DB 家長 User 不足）"
            )
            return

        logger.info("可建 thread 的學生數（受 parent User 限制）：%d", len(viable))

        # ---- 為每個可用三元組建立 1 條 thread + 訊息串 ----
        for parent_user_id, teacher_user_id, student_id in viable:
            # thread 冪等：UNIQUE(parent_user_id, teacher_user_id, student_id)
            thread = (
                session.query(ParentMessageThread)
                .filter(
                    ParentMessageThread.parent_user_id == parent_user_id,
                    ParentMessageThread.teacher_user_id == teacher_user_id,
                    ParentMessageThread.student_id == student_id,
                )
                .first()
            )
            if thread is None:
                first_when = _MESSAGE_SCRIPT[0][2]
                thread = ParentMessageThread(
                    parent_user_id=parent_user_id,
                    teacher_user_id=teacher_user_id,
                    student_id=student_id,
                    created_at=first_when,
                    updated_at=first_when,
                )
                session.add(thread)
                session.flush()  # 取得 thread.id 供訊息 FK
                added_threads += 1

            # ---- 訊息串（決定論 client_request_id 做冪等鍵）----
            last_parent_when: datetime | None = None
            last_when: datetime | None = None
            for idx, (role, body, when) in enumerate(_MESSAGE_SCRIPT):
                req_id = f"seed-pmsg-{thread.id}-{idx}"
                exists_msg = (
                    session.query(ParentMessage)
                    .filter(
                        ParentMessage.thread_id == thread.id,
                        ParentMessage.client_request_id == req_id,
                    )
                    .first()
                )
                sender_user_id = parent_user_id if role == "parent" else teacher_user_id
                if exists_msg is None:
                    session.add(
                        ParentMessage(
                            thread_id=thread.id,
                            sender_user_id=sender_user_id,
                            sender_role=role,
                            body=body,
                            client_request_id=req_id,
                            source="app",
                            created_at=when,
                        )
                    )
                    added_messages += 1

                if role == "parent":
                    last_parent_when = when
                last_when = when

            # ---- thread 時間 / 已讀指標（對齊 service 未讀 query 語意）----
            # last_message_at 永遠對齊最後一則（UI 依此排序 thread）。
            if last_when is not None:
                thread.last_message_at = last_when
            # 教師端：讀到最後一則 parent 訊息之後 → 0 未讀（已讀完家長提問）。
            if last_parent_when is not None:
                thread.teacher_last_read_at = last_parent_when
            # 家長端：停在最後一則 teacher 訊息「之前」→ 尚有最後 1 則未讀。
            # 取倒數第二則的時間當讀取點（最後一則是 teacher 的活動通知，未讀）。
            if len(_MESSAGE_SCRIPT) >= 2:
                thread.parent_last_read_at = _MESSAGE_SCRIPT[-2][2]

    logger.info(
        "親師訊息 seed 完成：threads +%d, messages +%d",
        added_threads,
        added_messages,
    )


if __name__ == "__main__":
    step()
