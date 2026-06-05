"""scripts/seed/child_development.py — 「幼兒發展」模組 114 學年示範資料。

灌入三張表的示範資料供前端展示/手測:
- student_growth_reports（期末成長/發展報告，每生每學期一份）
- student_observations（教師日常正向觀察紀錄，跨學年多筆）
- student_milestones（結構化發展里程碑，部分學生補上）

設計原則:
- **冪等**:每筆插入前先 exists 查;重跑必須新增 0 筆、不刪改任何現有資料。
- **不生未來**:所有日期落在 2025-08-01 ~ 2026-06-05（TODAY）之間。
- **provenance**:
  - growth_report.generated_by / milestone.created_by → employees.id
    （取學生所屬班級的 head_teacher_id，缺則回退 admin.employee_id）
  - observation.recorded_by → users.id（取 admin user）

只新增本檔,不修改任何其他 seed 檔。執行:
    python3 -m scripts.seed.child_development
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime

from scripts.seed._common import (  # noqa: F401
    session_scope,
    get_active_students,
    get_admin_user,
    TERM1,
    TERM2,
    TODAY,
)
from models.classroom import Classroom, Student
from models.portfolio import (
    StudentGrowthReport,
    StudentObservation,
    StudentMilestone,
    REPORT_STATUS_READY,
    MILESTONE_TYPE_CUSTOM,
    MILESTONE_SOURCE_MANUAL,
)

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

# 取樣的學生數（每人每學期一份報告 + 跨學年 2-4 筆觀察 + 部分里程碑）
SAMPLE_SIZE = 30

# ── 學期期末/期中日期錨點（皆 ≤ TODAY，TERM2 下學期上限為 TODAY=2026-06-05）──
TERM1_START, TERM1_END = TERM1  # (2025-08-01, 2026-01-20)
TERM2_START, TERM2_END = TERM2  # (2026-02-01, 2026-06-05)

# 兩學期報告的期間標籤與期末日（報告日期落各學期末）
GROWTH_PERIODS = [
    {
        "period_label": "114學年上學期",
        "period_start": TERM1_START,
        "period_end": TERM1_END,
        "generated_on": date(2026, 1, 16),  # 上學期末
    },
    {
        "period_label": "114學年下學期",
        "period_start": TERM2_START,
        "period_end": TERM2_END,
        "generated_on": date(2026, 6, 4),  # 下學期末（≤ TODAY）
    },
]

# 各領域的繁中發展評語素材（涵蓋身體動作/認知/語言/社會情緒/美感等）
DOMAIN_NARRATIVES = {
    "身體動作與健康": [
        "大肌肉活動發展良好，能穩定跑跳、單腳站立，平衡感佳。",
        "精細動作進步明顯，能自行使用剪刀沿線剪紙、扣鈕扣。",
        "用餐與如廁自理能力佳，能獨立完成日常生活自理。",
    ],
    "語文": [
        "口語表達流暢，能用完整句子描述今天發生的事。",
        "喜愛聽故事並能複述情節，詞彙量持續增加。",
        "對符號與文字產生興趣，能辨認自己的名字。",
    ],
    "認知": [
        "邏輯推理能力提升，能完成簡單的分類與排序活動。",
        "對數與量的概念逐漸建立，能正確點數到二十。",
        "觀察力敏銳，能發現生活中事物的異同並提出疑問。",
    ],
    "社會": [
        "樂於與同儕合作，在團體遊戲中能輪流並遵守規則。",
        "願意分享玩具與工具，展現良好的人際互動。",
        "能主動關心他人，協助同學收拾與整理。",
    ],
    "情緒": [
        "情緒調節能力進步，遇到挫折時能在引導下緩和情緒。",
        "能用語言表達自己的感受，較少以哭鬧處理問題。",
        "對新環境適應良好，展現安定與自信的情緒狀態。",
    ],
    "美感": [
        "喜愛塗鴉與創作，能運用多種媒材表現想法。",
        "對音樂與律動充滿熱情，能隨節奏擺動身體。",
        "欣賞作品時能表達感受，展現豐富的想像力。",
    ],
    "綜合": [
        "本學期整體發展均衡，學習動機強且樂於探索。",
        "各領域表現穩定成長，是個活潑開朗的孩子。",
        "持續鼓勵深化興趣，建議家庭延續多元探索活動。",
    ],
}

# 觀察紀錄（日常亮點）素材，依領域分類
OBSERVATION_NARRATIVES = {
    "身體動作與健康": [
        "今天在戶外遊戲時主動挑戰攀爬架，完成後露出滿足的笑容。",
        "午餐時間能自己使用湯匙與筷子，把飯菜吃得乾乾淨淨。",
    ],
    "語文": [
        "在分享時間自願上台，清楚地說出週末和家人出遊的經過。",
        "聽完繪本後主動舉手提問，展現對故事的高度投入。",
    ],
    "認知": [
        "在益智角專注完成拼圖，並開心地向老師展示成果。",
        "玩數字遊戲時能正確點數教室裡的椅子數量。",
    ],
    "社會": [
        "看到同學跌倒時主動上前扶起並安慰，展現同理心。",
        "在團體積木建構中與同伴分工合作，完成一座大城堡。",
    ],
    "情緒": [
        "想念家人而難過時，能在老師陪伴下逐漸平復情緒。",
        "比賽輸了仍能微笑為同學加油，情緒管理進步許多。",
    ],
    "美感": [
        "用色彩鮮豔的水彩畫出全家福，並細心介紹畫中的每個人。",
        "在音樂課跟著旋律自創動作，帶動全班一起律動。",
    ],
    "綜合": [
        "整天保持愉快的學習情緒，主動參與每一項活動。",
        "對新的科學探索活動充滿好奇，提出許多有趣的問題。",
    ],
}

# 里程碑（自理/認知/社交等具體成就）素材
MILESTONE_ITEMS = [
    ("能自行穿鞋", "今天第一次不需協助就把鞋子穿好並黏好魔鬼氈。", "👟"),
    ("會數到二十", "能正確且流暢地從一數到二十，數量概念建立。", "🔢"),
    ("第一次自己上廁所", "已能完全獨立完成如廁，自理能力再進一步。", "🚽"),
    ("學會自己扣鈕扣", "精細動作發展成熟，能自行扣上外套的鈕扣。", "🧥"),
    ("能完整背誦一首兒歌", "在團體前完整唱完一首兒歌，自信心大增。", "🎵"),
    ("第一次主動交朋友", "主動邀請新同學一起遊戲，踏出社交的一大步。", "🤝"),
    ("會用筷子吃飯", "午餐時間能熟練使用筷子，用餐自理更進步。", "🥢"),
    ("第一次完整說一個故事", "看圖說故事，能有頭有尾地說完整段內容。", "📖"),
]


def _homeroom_employee_id(student: Student, classroom_head: dict, fallback_emp_id):
    """取學生所屬班級的班導 employee id；缺則回退 fallback（admin.employee_id）。"""
    emp_id = classroom_head.get(student.classroom_id)
    return emp_id if emp_id is not None else fallback_emp_id


def _rng_date(rng: random.Random, a: date, b: date) -> date:
    """以本地確定性 rng 取 [a, b] 間一天。

    刻意不用 _common.rand_date_between（其走 module-global random，
    每次跑結果不同 → 破壞以日期為冪等鍵的 exists 查）。本檔所有日期
    皆走此函式由固定 seed 的 rng 推導，重跑得到完全相同日期。
    """
    from datetime import timedelta

    span = (b - a).days
    if span <= 0:
        return a
    return a + timedelta(days=rng.randint(0, span))


def step():
    logger.info("=== 幼兒發展模組 seed（成長報告 / 觀察 / 里程碑）===")
    with session_scope() as session:
        admin = get_admin_user(session)
        admin_user_id = admin.id if admin else None
        admin_emp_id = admin.employee_id if admin else None

        # 班級 → 班導 employee id 對照（provenance）
        classroom_head = {
            c.id: c.head_teacher_id
            for c in session.query(Classroom)
            .filter(Classroom.is_active == True)  # noqa: E712
            .all()
        }

        students = get_active_students(session)
        if not students:
            logger.warning("無 active 學生，略過。")
            return

        sample = students[: min(SAMPLE_SIZE, len(students))]

        n_reports = 0
        n_observations = 0
        n_milestones = 0

        # ── 1) 成長/發展報告（每生每學期 1 筆）────────────────────────────
        for stu in sample:
            gen_emp_id = _homeroom_employee_id(stu, classroom_head, admin_emp_id)
            # 每生獨立 rng：內容為 student_id 的純函式，與迴圈順序/已存在 skip 解耦
            # → 重跑時即使整段被 skip，亦不影響其他迴圈的隨機序列（保證冪等）。
            r_rng = random.Random(f"growth:{stu.id}")
            for period in GROWTH_PERIODS:
                # 冪等鍵對齊 model partial unique:(student, label, start, end)
                exists = (
                    session.query(StudentGrowthReport)
                    .filter(
                        StudentGrowthReport.student_id == stu.id,
                        StudentGrowthReport.period_label == period["period_label"],
                        StudentGrowthReport.period_start == period["period_start"],
                        StudentGrowthReport.period_end == period["period_end"],
                    )
                    .first()
                )
                if exists:
                    continue

                # 期末綜合評語：每領域各取一句，組成跨領域的發展總評
                lines = [
                    f"【{dom}】{r_rng.choice(DOMAIN_NARRATIVES[dom])}"
                    for dom in (
                        "身體動作與健康",
                        "認知",
                        "語文",
                        "社會",
                        "情緒",
                        "美感",
                    )
                ]
                narrative = (
                    f"{stu.name} {period['period_label']}發展總評：\n"
                    + "\n".join(lines)
                    + "\n【綜合】"
                    + r_rng.choice(DOMAIN_NARRATIVES["綜合"])
                )
                generated_on = period["generated_on"]
                session.add(
                    StudentGrowthReport(
                        student_id=stu.id,
                        period_label=period["period_label"],
                        period_start=period["period_start"],
                        period_end=period["period_end"],
                        status=REPORT_STATUS_READY,
                        teacher_narrative=narrative,
                        generated_by=gen_emp_id,
                        generated_at=datetime(
                            generated_on.year,
                            generated_on.month,
                            generated_on.day,
                            15,
                            0,
                        ),
                    )
                )
                n_reports += 1

        # ── 2) 教師觀察紀錄（每生跨學年 2-4 筆）──────────────────────────
        for stu in sample:
            o_rng = random.Random(f"observation:{stu.id}")
            n_obs = o_rng.randint(2, 4)
            # 同生每筆觀察的領域/評語不重複，確保 (student, narrative) 唯一 →
            # 冪等鍵穩定，不會因撞日期 narrative 相同被誤判存在。
            domains = list(OBSERVATION_NARRATIVES.keys())
            o_rng.shuffle(domains)
            for i in range(n_obs):
                # 每筆取不同領域（已 shuffle 且 n_obs<=4<7），故 narrative 必不同 →
                # (student, narrative) 唯一。領域內兩句由 o_rng 擇一增加變化。
                # ⚠ 冪等關鍵：所有 rng 取值都在 exists 判斷「之前」完成，確保重跑
                #   被 skip 的筆數不會少消耗 rng → 後續筆的隨機序列不偏移。
                domain = domains[i % len(domains)]
                pool = OBSERVATION_NARRATIVES[domain]
                narrative = pool[o_rng.randrange(len(pool))]
                obs_date = _rng_date(o_rng, TERM1_START, TODAY)  # ≤ TODAY
                rating = o_rng.choice([4, 4, 5, 5, 3])
                is_highlight = o_rng.random() < 0.4
                # 冪等鍵:(student, narrative) 唯一辨識本批資料（同生不重複句）
                exists = (
                    session.query(StudentObservation)
                    .filter(
                        StudentObservation.student_id == stu.id,
                        StudentObservation.narrative == narrative,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    StudentObservation(
                        student_id=stu.id,
                        observation_date=obs_date,
                        domain=domain,
                        narrative=narrative,
                        rating=rating,
                        is_highlight=is_highlight,
                        recorded_by=admin_user_id,
                    )
                )
                n_observations += 1

        # ── 3) 發展里程碑（部分學生補上 1-2 筆 custom 里程碑）────────────
        # 取樣前半數學生補里程碑，避免每位都有
        milestone_students = sample[: max(1, len(sample) // 2)]
        for stu in milestone_students:
            crt_emp_id = _homeroom_employee_id(stu, classroom_head, admin_emp_id)
            m_rng = random.Random(f"milestone:{stu.id}")
            n_mil = m_rng.randint(1, 2)
            chosen = m_rng.sample(MILESTONE_ITEMS, n_mil)
            for title, desc, icon in chosen:
                achieved_on = _rng_date(m_rng, TERM1_START, TODAY)
                # 冪等鍵對齊 auto-trigger 唯一語意:(student, type, date, source, title)
                exists = (
                    session.query(StudentMilestone)
                    .filter(
                        StudentMilestone.student_id == stu.id,
                        StudentMilestone.milestone_type == MILESTONE_TYPE_CUSTOM,
                        StudentMilestone.title == title,
                        StudentMilestone.source_type == MILESTONE_SOURCE_MANUAL,
                    )
                    .first()
                )
                if exists:
                    continue
                session.add(
                    StudentMilestone(
                        student_id=stu.id,
                        milestone_type=MILESTONE_TYPE_CUSTOM,
                        achieved_on=achieved_on,
                        title=title,
                        description=desc,
                        icon=icon,
                        source_type=MILESTONE_SOURCE_MANUAL,
                        created_by=crt_emp_id,
                    )
                )
                n_milestones += 1

        logger.info(
            "新增筆數 → 成長報告: %d / 觀察紀錄: %d / 里程碑: %d",
            n_reports,
            n_observations,
            n_milestones,
        )


if __name__ == "__main__":
    step()
