"""scripts/seed/contact_book.py — 聯絡簿模組 dev DB 示範資料（全 114 學年）。

灌入四張表的示範資料，供前端家長端 / 教師端聯絡簿功能手測：
- contact_book_templates：園所共用 + 個人範本（~3 筆）
- student_contact_book_entries：取樣 ~40 名學生，每人跨上下學期插 ~4-6 筆每日聯絡簿
- student_contact_book_replies：家長回覆（限有綁定家長 User 的學生）
- student_contact_book_acks：家長已讀回條（同上）

冪等契約：每筆插入前先 exists 查；重跑必新增 0 筆、不刪改現有資料。
  - entries：自然鍵 (student_id, log_date) UniqueConstraint
  - acks：自然鍵 (entry_id, guardian_user_id) UniqueConstraint
  - replies：無唯一鍵 → 用 client_request_id 當冪等鍵（f"seed-reply-{entry_id}"）
  - templates：無唯一鍵 → 用 name dedup

決定論：每位學生迴圈開頭 random.seed(stu.id)，使每次重跑挑相同的 log_date /
欄位值 / 回覆對象，exists 查才命中、重跑才會新增 0 筆。

日期界線：所有日期落在 2025-08-01 ~ 2026-06-05，絕不生未來日期。
  - entry.log_date 落在 TERM1 / TERM2 工作日範圍內
  - 有回覆/已讀的 entry 必為已發布（published_at 非 NULL）
  - reply.created_at / ack.read_at / published_at 皆 ≥ log_date 且 ≤ TODAY

家長 User 限制：dev DB 中僅 1 名 active 學生（student 1）有綁定 user_id 的
guardian（user 5），故 replies / acks 僅能掛在該生的已發布 entry 上（其餘學生
無家長 User 帳號 → 無從填 NOT NULL 的 guardian_user_id）。建立家長 User / 綁定
不在本模組範圍，故不擴充，僅就有效子集示範並於回報說明。
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta

from scripts.seed._common import (
    session_scope,
    get_active_students,
    get_admin_user,
    get_classrooms,
    rand_date_between,
    TERM1,
    TERM2,
    TODAY,
)
from models.classroom import Classroom, Student
from models.guardian import Guardian
from models.contact_book import (
    StudentContactBookEntry,
    StudentContactBookReply,
    StudentContactBookAck,
    ContactBookTemplate,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("seed_contact_book")

# 取樣學生數（每人 ~4-6 筆 entry）
SAMPLE_STUDENT_COUNT = 40

# 心情 / 大便狀態（與 models.contact_book.CONTACT_BOOK_MOODS / _BOWEL 對齊）
MOODS = ("happy", "normal", "tired", "sad", "sick")
BOWEL = ("none", "normal", "loose", "constipated")

# 台灣幼兒園聯絡簿口吻的老師留言（餐點 / 午睡 / 活動 / 情緒）
TEACHER_NOTES = [
    "今天午餐吃光光，胃口很好，還主動說要再添飯，真棒！",
    "午睡睡得很安穩，睡了快兩小時，起床後精神飽滿。",
    "今天情緒比較黏老師，可能有點想家，午休後就恢復活力了。",
    "點心時間有點挑食，紅蘿蔔留在碗裡，在家可多鼓勵嘗試蔬菜喔。",
    "今天和小朋友玩得很開心，分享玩具給同學，很有愛心。",
    "上午有點咳嗽，已多補充溫開水並留意，請家長在家觀察體溫。",
    "戶外活動時跑跳很有活力，回教室後喝了好多水。",
    "今天學會自己穿鞋子，獨立性進步很多，給他大大的鼓勵！",
    "午睡前有點不想睡，老師陪伴後很快入睡，下午狀態不錯。",
    "今天比較安靜，午餐量普通，傍晚精神有恢復，請家長留意作息。",
]

LEARNING_HIGHLIGHTS = [
    "今天認識了數字 1 到 5，會用手指比出來數一數。",
    "美勞課畫了全家福，畫得很用心，色彩很豐富。",
    "團體律動時跟著音樂擺動身體，節奏感很好。",
    "今天的繪本故事是《好餓的毛毛蟲》，能回答老師的提問。",
    "練習自己收拾玩具，物歸原位做得很確實。",
    "唱了新的兒歌〈兩隻老虎〉，記得大部分歌詞。",
    "學習區玩積木堆出高塔，空間概念進步中。",
    "今天練習排隊和輪流，學習等待，社會互動表現佳。",
]

# 家長回覆（口吻像家長簡短回應老師）
PARENT_REPLIES = [
    "謝謝老師細心照顧，回家會多注意他的飲食，辛苦了！",
    "收到，謝謝老師！在家也會多鼓勵他嘗試蔬菜。",
    "謝謝老師的回饋，看到他在學校這麼開心我們很放心。",
    "好的，我們會在家觀察體溫，有狀況再跟老師聯繫。",
    "謝謝老師用心紀錄，孩子回家也一直分享今天的活動呢！",
    "辛苦老師了，週末會帶他多運動，感謝您的照顧。",
]

# 範本（fields 為 StudentContactBookEntry 欄位子集的 JSON）
TEMPLATES = [
    {
        "name": "一般日常（共用）",
        "scope": "shared",
        "fields": {
            "mood": "happy",
            "meal_lunch": 3,
            "meal_snack": 2,
            "nap_minutes": 90,
            "bowel": "normal",
            "teacher_note": "今天在學校用餐正常、午睡安穩，活動參與度高。",
        },
    },
    {
        "name": "生病觀察（共用）",
        "scope": "shared",
        "fields": {
            "mood": "sick",
            "meal_lunch": 1,
            "meal_snack": 1,
            "nap_minutes": 120,
            "bowel": "normal",
            "temperature_c": 37.8,
            "teacher_note": "今天精神較差、食慾下降，已多補充水分並留意體溫，請家長在家觀察。",
        },
    },
    {
        "name": "活力滿滿（個人）",
        "scope": "personal",
        "fields": {
            "mood": "happy",
            "meal_lunch": 3,
            "meal_snack": 3,
            "nap_minutes": 75,
            "bowel": "normal",
            "teacher_note": "今天活力十足，戶外活動跑跳開心，和同學互動良好。",
            "learning_highlight": "團體活動踴躍舉手回答問題。",
        },
    },
]


def _rand_workday(term: tuple[date, date]) -> date:
    """在 term 範圍內取一個工作日（週一至週五），上限不超過 TODAY。"""
    lo, hi = term
    if hi > TODAY:
        hi = TODAY
    for _ in range(40):
        d = rand_date_between(lo, hi)
        if d.weekday() < 5:  # 0=Mon..4=Fri
            return d
    return lo  # 退路：理論上不會走到


def _entry_count_for(student_id: int) -> int:
    """決定該生要插幾筆 entry（4-6，依 seed 後的 random 決定）。"""
    return random.randint(4, 6)


def step() -> None:
    """灌入聯絡簿示範資料（冪等）。"""
    added_templates = 0
    added_entries = 0
    added_replies = 0
    added_acks = 0

    with session_scope() as session:
        admin = get_admin_user(session)
        if admin is None:
            logger.warning("找不到 admin user，跳過聯絡簿 seed")
            return

        # ---- 1) 範本 ----
        for tpl in TEMPLATES:
            exists = (
                session.query(ContactBookTemplate)
                .filter(ContactBookTemplate.name == tpl["name"])
                .first()
            )
            if exists:
                continue
            session.add(
                ContactBookTemplate(
                    name=tpl["name"],
                    scope=tpl["scope"],
                    # personal 範本掛 admin 為 owner；shared 不需 owner
                    owner_user_id=(admin.id if tpl["scope"] == "personal" else None),
                    classroom_id=None,
                    fields=tpl["fields"],
                    is_archived=False,
                )
            )
            added_templates += 1

        # ---- 取樣學生 ----
        # 全部 active 學生依 id 排序取前 N；確保 student 1（唯一有綁定家長 User 的
        # active 學生）一定在樣本內，這樣 replies / acks 才有對象。
        all_students = get_active_students(session)
        sample: list[Student] = list(all_students[:SAMPLE_STUDENT_COUNT])
        sample_ids = {s.id for s in sample}
        for s in all_students:
            if s.id == 1 and 1 not in sample_ids:
                sample.append(s)
                sample_ids.add(1)
                break

        # 班級 → 班導 employee 對照（created_by_employee_id 是 employee FK）
        classrooms = {c.id: c for c in get_classrooms(session)}

        # 預先撈出「有綁定 user_id 的 guardian」對照（student_id -> guardian_user_id）
        guardian_user_by_student: dict[int, int] = {}
        for g_student_id, g_user_id in (
            session.query(Guardian.student_id, Guardian.user_id)
            .filter(Guardian.user_id.isnot(None))
            .all()
        ):
            # 一生可能多筆，取第一筆即可（demo）
            guardian_user_by_student.setdefault(g_student_id, g_user_id)

        # ---- 2) entries（+ 部分 replies / acks）----
        for stu in sample:
            classroom = classrooms.get(stu.classroom_id)
            if classroom is None:
                # active 學生理應都有班級；保險跳過避免 NOT NULL classroom_id
                continue
            head_teacher_id = classroom.head_teacher_id  # employee FK（nullable）

            # 決定論：固定 seed 後，每次重跑挑相同日期 / 欄位 / 回覆對象
            random.seed(stu.id)

            n = _entry_count_for(stu.id)
            # 跨上下學期：前半上學期、後半下學期
            n_term1 = n // 2 + (n % 2)
            n_term2 = n - n_term1
            chosen_dates: set[date] = set()
            plan: list[date] = []
            for _ in range(n_term1):
                d = _rand_workday(TERM1)
                # 避免同一天重複（uq student_id+log_date）
                tries = 0
                while d in chosen_dates and tries < 10:
                    d = _rand_workday(TERM1)
                    tries += 1
                chosen_dates.add(d)
                plan.append(d)
            for _ in range(n_term2):
                d = _rand_workday(TERM2)
                tries = 0
                while d in chosen_dates and tries < 10:
                    d = _rand_workday(TERM2)
                    tries += 1
                chosen_dates.add(d)
                plan.append(d)

            guardian_user_id = guardian_user_by_student.get(stu.id)

            for idx, log_date in enumerate(plan):
                exists_entry = (
                    session.query(StudentContactBookEntry)
                    .filter(
                        StudentContactBookEntry.student_id == stu.id,
                        StudentContactBookEntry.log_date == log_date,
                    )
                    .first()
                )
                if exists_entry:
                    entry = exists_entry
                else:
                    # 已發布時間：log_date 當天傍晚（不超過 TODAY）
                    publish_dt = datetime(
                        log_date.year, log_date.month, log_date.day, 17, 0
                    )
                    # 約 80% 發布、20% 留草稿（更貼近真實）；但若該生有家長 User
                    # 且這筆要掛 reply/ack，則一定發布（下方再決定）。
                    will_have_reply = (
                        guardian_user_id is not None and idx % 2 == 0
                    )  # 約半數
                    is_published = will_have_reply or (random.random() < 0.8)

                    entry = StudentContactBookEntry(
                        student_id=stu.id,
                        classroom_id=classroom.id,
                        log_date=log_date,
                        mood=random.choice(MOODS),
                        meal_lunch=random.randint(0, 3),
                        meal_snack=random.randint(0, 3),
                        nap_minutes=random.choice([0, 45, 60, 75, 90, 105, 120]),
                        bowel=random.choice(BOWEL),
                        temperature_c=round(random.uniform(36.2, 37.6), 1),
                        teacher_note=random.choice(TEACHER_NOTES),
                        learning_highlight=(
                            random.choice(LEARNING_HIGHLIGHTS)
                            if random.random() < 0.6
                            else None
                        ),
                        created_by_employee_id=head_teacher_id,
                        published_at=(publish_dt if is_published else None),
                    )
                    session.add(entry)
                    session.flush()  # 取得 entry.id 供 reply/ack FK
                    added_entries += 1

                # ---- replies / acks（僅限有綁定家長 User 的學生）----
                if guardian_user_id is None:
                    continue
                # 約半數 entry 補 reply + ack；且該 entry 必為已發布
                want_reply = idx % 2 == 0
                if not want_reply:
                    continue
                # 確保已發布（理論上 will_have_reply 已保證；保險再補）
                if entry.published_at is None:
                    entry.published_at = datetime(
                        log_date.year, log_date.month, log_date.day, 17, 0
                    )

                # reply 時間：發布後 1-2 天傍晚（不超過 TODAY）
                reply_d = log_date + timedelta(days=random.randint(0, 2))
                if reply_d > TODAY:
                    reply_d = TODAY
                reply_dt = datetime(reply_d.year, reply_d.month, reply_d.day, 19, 30)

                # reply 冪等鍵：client_request_id
                req_id = f"seed-reply-{entry.id}"
                exists_reply = (
                    session.query(StudentContactBookReply)
                    .filter(StudentContactBookReply.client_request_id == req_id)
                    .first()
                )
                if not exists_reply:
                    session.add(
                        StudentContactBookReply(
                            entry_id=entry.id,
                            guardian_user_id=guardian_user_id,
                            body=random.choice(PARENT_REPLIES),
                            client_request_id=req_id,
                            created_at=reply_dt,
                        )
                    )
                    added_replies += 1

                # ack 冪等鍵：自然鍵 (entry_id, guardian_user_id)
                exists_ack = (
                    session.query(StudentContactBookAck)
                    .filter(
                        StudentContactBookAck.entry_id == entry.id,
                        StudentContactBookAck.guardian_user_id == guardian_user_id,
                    )
                    .first()
                )
                if not exists_ack:
                    session.add(
                        StudentContactBookAck(
                            entry_id=entry.id,
                            guardian_user_id=guardian_user_id,
                            read_at=reply_dt,
                        )
                    )
                    added_acks += 1

    logger.info(
        "聯絡簿 seed 完成：templates +%d, entries +%d, replies +%d, acks +%d",
        added_templates,
        added_entries,
        added_replies,
        added_acks,
    )


if __name__ == "__main__":
    step()
