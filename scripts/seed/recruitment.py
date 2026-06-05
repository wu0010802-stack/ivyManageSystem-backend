"""scripts/seed/recruitment.py — 招生模組示範資料（115 學年招生季）。

灌入招生漏斗示範資料，供 dev/手測使用：
- recruitment_periods：招生期（半年一筆，近年轉換彙整）
- recruitment_months：月份桶（2025-09 ~ 2026-03，民國 114.09 ~ 115.03）
- recruitment_visits：來訪/詢問 → 參觀 → 報名 → 預繳 的漏斗主表（~40 筆）
- competitor_school：附近競品幼兒園（5 筆合成資料，source_school_id 以 SEED- 前綴）
- parent_inquiries：家長詢問（電話/官網/LINE，~15 筆，繁中內容）

**冪等契約**：每筆插入前先 exists 查；重跑必新增 0 筆、不刪改現有資料。

關聯說明：recruitment_visits 與 month/period **無 FK**——唯一「關聯」是 `month`
字串（民國月份，如 "115.03"）。本腳本建立的 visit `month` 一律落在所建 month 桶內，
period_name 以 "114.09.16~115.03.15" 半年區間命名。漏斗 Kanban（GET /funnel/board）
直接抓所有 visit 推導 stage：未掛 Student 的 visit 只會落在 visited（未預繳）/
deposited（已預繳）兩欄；enrolled/active 需綁 Student.recruitment_visit_id（本腳本
不建學生，故不灌該兩欄）。報名/錄取/放棄語意改由 visit 欄位
（has_deposit / enrolled / transfer_term / no_deposit_reason）承載，餵 stats/records。

執行：python3 -m scripts.seed.recruitment（可重跑，第二次新增 0 筆）。
"""

from __future__ import annotations

import random
from datetime import date

from scripts.seed._common import (  # noqa: F401
    session_scope,
    get_admin_user,
    rand_date_between,
    _random_name,
    _random_phone,
    TODAY,
)
from models.recruitment import (
    RecruitmentVisit,
    RecruitmentMonth,
    RecruitmentPeriod,
    CompetitorSchool,
)
from models.activity import ParentInquiry
from utils.taipei_time import now_taipei_naive

# ===== 日期界線（絕不生未來）=====
RANGE_START = date(2025, 8, 1)
RANGE_END = TODAY  # 2026-06-05

# 招生月份桶（民國月份字串）：2025-09 ~ 2026-03
# 西元 → 民國：2025=114、2026=115
ROC_MONTHS: list[str] = [
    "114.09",
    "114.10",
    "114.11",
    "114.12",
    "115.01",
    "115.02",
    "115.03",
]


# 民國月份字串 → 該月西元起訖（給 visit_date 隨機落點用，上限不超過 TODAY）
def _roc_month_bounds(roc_month: str) -> tuple[date, date]:
    roc_y, mm = roc_month.split(".")
    year = int(roc_y) + 1911
    month = int(mm)
    start = date(year, month, 1)
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    # 月底 = 下月一號前一天
    from datetime import timedelta

    end = nxt - timedelta(days=1)
    # 夾在 [RANGE_START, RANGE_END] 內，絕不生未來
    start = max(start, RANGE_START)
    end = min(end, RANGE_END)
    if end < start:
        end = start
    return start, end


# ===== 真實類別值（取自 scripts/recruitment_seed_data.json 既有 255 筆分布）=====
SOURCES = [
    "自行蒞園",
    "附近",
    "朋友介紹",
    "在校生介紹",
    "在校生弟妹",
    "網路",
    "二人同行",
]
DISTRICTS = ["三民區", "鳳山區", "鳥松區", "苓雅區", "仁武區"]
GRADES = ["幼幼班", "小班", "中班", "大班"]
REFERRERS = ["Jocelyn", "Katrina", "Daisy", "Anny", "Tree", "Candy"]
COLLECTORS = ["Jocelyn", "Daisy", "Anny"]
NO_DEPOSIT_REASONS = ["考慮中", "費用考量", "離家較遠", "已決定他園", "尚未確定"]

# 漏斗階段分布（合理比例，共 40 筆）：
#   諮詢（純電訪/初訪，未參觀未預繳） 12
#   參觀（已到園參觀，未預繳）        12
#   報名/預繳（has_deposit）           10
#   錄取/報到（has_deposit + enrolled） 4
#   放棄（未預繳 + 退出，記 no_deposit_reason）2
# 未掛 Student，故 Kanban 只分 visited/deposited 兩欄；
# enrolled/transfer_term 旗標供 stats/records 統計用。
FUNNEL_PLAN = (
    [("諮詢", False, False, False)] * 12
    + [("參觀", False, False, False)] * 12
    + [("報名", True, False, False)] * 10
    + [("錄取", True, True, False)] * 4
    + [("放棄", False, False, True)] * 2
)


def _seed_periods(session, admin) -> int:
    """招生期（period_name unique）。兩筆：本期 + 前一期彙整。"""
    inserted = 0
    periods = [
        {
            "period_name": "114.09.16~115.03.15",
            "visit_count": 40,
            "deposit_count": 14,
            "enrolled_count": 4,
            # 對齊 visit 層級旗標：has_deposit=True 14 筆、transfer_term=True 2 筆
            "transfer_term_count": 2,
            "effective_deposit_count": 12,  # 14 - 2
            "not_enrolled_deposit": 0,
            "enrolled_after_school": 0,
            "notes": "115 學年招生季（示範資料）",
            "sort_order": 1,
        },
        {
            "period_name": "114.03.16~114.09.15",
            "visit_count": 35,
            "deposit_count": 12,
            "enrolled_count": 11,
            "transfer_term_count": 2,
            "effective_deposit_count": 10,
            "not_enrolled_deposit": 1,
            "enrolled_after_school": 0,
            "notes": "114 學年下半招生彙整（示範資料）",
            "sort_order": 2,
        },
    ]
    for p in periods:
        exists = (
            session.query(RecruitmentPeriod.id)
            .filter(RecruitmentPeriod.period_name == p["period_name"])
            .first()
        )
        if exists:
            continue
        session.add(RecruitmentPeriod(**p))
        inserted += 1
    return inserted


def _seed_months(session) -> int:
    """月份桶（month unique）。"""
    inserted = 0
    for m in ROC_MONTHS:
        exists = (
            session.query(RecruitmentMonth.id)
            .filter(RecruitmentMonth.month == m)
            .first()
        )
        if exists:
            continue
        session.add(RecruitmentMonth(month=m))
        inserted += 1
    return inserted


def _build_visit_specs() -> list[dict]:
    """產生 40 筆**確定性**訪視規格（不碰 DB）。

    冪等關鍵：`_random_name` / `_random_phone` / `rand_date_between` 皆用 **global**
    `random` 模組；本函式 seed 全域 RNG 後產生固定序列、產完即還原全域狀態，
    **完全不依賴 DB 既有資料**——故每次呼叫回傳的 40 筆 (child_name, month) 完全相同。
    批內先以 (name, month) 去重（撞名就重抽，確保 40 筆 key 互異）；
    冪等的「跳過既有」由呼叫端對 DB existing 比對處理，本函式不做 regenerate-on-DB-collision
    （那才是上一版重跑爆增的根因）。
    """
    _rng_state = random.getstate()
    random.seed(11509)  # 固定 seed → 全域 RNG 產生可重現的姓名/電話/日期序列
    try:
        plan = list(FUNNEL_PLAN)
        random.shuffle(plan)

        specs: list[dict] = []
        batch_keys: set[tuple[str, str]] = set()
        seq_by_month: dict[str, int] = {}

        for stage_label, has_deposit, enrolled, transfer_term in plan:
            month = random.choice(ROC_MONTHS)
            gender = random.choice(["男", "女"])
            # 批內 (name, month) 去重（僅針對本批產生的姓名，與 DB 無關）
            name = _random_name(gender)
            attempts = 0
            while (name, month) in batch_keys and attempts < 20:
                name = _random_name(gender)
                attempts += 1
            batch_keys.add((name, month))

            seq_by_month[month] = seq_by_month.get(month, 0) + 1
            seq_no = str(seq_by_month[month])

            m_start, m_end = _roc_month_bounds(month)
            vdate = rand_date_between(m_start, m_end)
            grade = random.choice(GRADES)
            district = random.choice(DISTRICTS)
            source = random.choice(SOURCES)
            referrer = (
                random.choice(REFERRERS)
                if source in ("朋友介紹", "在校生介紹", "在校生弟妹")
                else None
            )

            # 日期字串（含階段備註，呼應 Excel 原始風格）
            roc_y, mm = month.split(".")
            visit_date_str = f"{roc_y}.{mm}.{vdate.day:02d}{stage_label}"

            no_deposit_reason = None
            no_deposit_reason_detail = None
            if not has_deposit and stage_label in ("諮詢", "參觀", "放棄"):
                if stage_label == "放棄":
                    no_deposit_reason = random.choice(NO_DEPOSIT_REASONS)
                    no_deposit_reason_detail = "電訪後家長表示暫不報名"
                elif random.random() < 0.35:
                    no_deposit_reason = random.choice(NO_DEPOSIT_REASONS)

            notes_map = {
                "諮詢": "電話/官網初步詢問，待安排參觀",
                "參觀": "已到園參觀，考慮中",
                "報名": f"已預繳，預計 115.08 讀{grade}",
                "錄取": f"已報到註冊，115.08 入學讀{grade}",
                "放棄": "參觀後未報名",
            }

            specs.append(
                dict(
                    month=month,
                    seq_no=seq_no,
                    visit_date=visit_date_str,
                    child_name=name,
                    birthday=None,
                    grade=grade,
                    phone=_random_phone(),
                    address=None,
                    district=district,
                    source=source,
                    referrer=referrer,
                    deposit_collector=(
                        random.choice(COLLECTORS) if has_deposit else None
                    ),
                    has_deposit=has_deposit,
                    notes=notes_map.get(stage_label),
                    parent_response=None,
                    no_deposit_reason=no_deposit_reason,
                    no_deposit_reason_detail=no_deposit_reason_detail,
                    enrolled=enrolled,
                    transfer_term=transfer_term,
                    expected_start_label=(
                        "115.08" if (has_deposit or enrolled) else None
                    ),
                    target_school_year=115 if (has_deposit or enrolled) else None,
                    target_semester=1 if (has_deposit or enrolled) else None,
                )
            )
        return specs
    finally:
        random.setstate(_rng_state)  # 還原全域 RNG，避免污染同進程其他步驟


def _seed_visits(session) -> int:
    """漏斗主表 visits（~40 筆，key=(child_name, month)）。

    冪等：specs 為確定性產生（與 DB 無關）；只插入 (child_name, month) 尚不存在的筆，
    既有者一律 **跳過**（不 regenerate），故重跑新增 0 筆。
    """
    existing = set(
        session.query(RecruitmentVisit.child_name, RecruitmentVisit.month).all()
    )

    inserted = 0
    for spec in _build_visit_specs():
        key = (spec["child_name"], spec["month"])
        if key in existing:
            continue
        session.add(RecruitmentVisit(**spec))
        existing.add(key)
        inserted += 1
    return inserted


def _seed_competitors(session) -> int:
    """附近競品幼兒園（5 筆合成資料）。

    注意：competitor_school 表已有教育部快取的真實資料（dev DB ~650 筆，含常春藤/明華）；
    本腳本只新增 5 筆 **明確合成** 資料，source_school_id 一律 SEED-COMP-xx 前綴，
    絕不碰真實 govt 資料。本表無「距離」欄位，特色/距離改填 notes 風格欄位
    （school_type / monthly_fee / approved_capacity / district / floor_info）。
    """
    inserted = 0
    competitors = [
        {
            "source_school_id": "SEED-COMP-01",
            "source_key": "seed-comp-rising-star",
            "school_name": "（示範）晨星幼兒園",
            "school_type": "私立",
            "pre_public_type": "無",
            "city": "高雄市",
            "district": "三民區",
            "address": "高雄市三民區建工路200號",
            "approved_capacity": 120,
            "monthly_fee": 16500,
            "google_rating": 4.3,
            "google_rating_count": 86,
            "floor_info": "步行約 5 分鐘；雙語特色、附設游泳池",
            "has_after_school": True,
        },
        {
            "source_school_id": "SEED-COMP-02",
            "source_key": "seed-comp-little-oak",
            "school_name": "（示範）小橡樹幼兒園",
            "school_type": "私立",
            "pre_public_type": "有",
            "city": "高雄市",
            "district": "鳳山區",
            "address": "高雄市鳳山區文化路88號",
            "approved_capacity": 90,
            "monthly_fee": 13800,
            "google_rating": 4.6,
            "google_rating_count": 142,
            "floor_info": "車程約 8 分鐘；準公共、蒙特梭利教學",
            "has_after_school": True,
        },
        {
            "source_school_id": "SEED-COMP-03",
            "source_key": "seed-comp-sunshine",
            "school_name": "（示範）陽光森林幼兒園",
            "school_type": "非營利",
            "pre_public_type": "有",
            "city": "高雄市",
            "district": "鳥松區",
            "address": "高雄市鳥松區大華路15號",
            "approved_capacity": 150,
            "monthly_fee": 9500,
            "google_rating": 4.5,
            "google_rating_count": 210,
            "floor_info": "車程約 12 分鐘；非營利、戶外場地大",
            "has_after_school": False,
        },
        {
            "source_school_id": "SEED-COMP-04",
            "source_key": "seed-comp-rainbow",
            "school_name": "（示範）彩虹橋幼兒園",
            "school_type": "私立",
            "pre_public_type": "無",
            "city": "高雄市",
            "district": "三民區",
            "address": "高雄市三民區九如二路330號",
            "approved_capacity": 80,
            "monthly_fee": 18000,
            "google_rating": 4.1,
            "google_rating_count": 54,
            "floor_info": "步行約 8 分鐘；美語沉浸、課後才藝多元",
            "has_after_school": True,
        },
        {
            "source_school_id": "SEED-COMP-05",
            "source_key": "seed-comp-green-hill",
            "school_name": "（示範）青山附幼",
            "school_type": "公立",
            "pre_public_type": "無",
            "city": "高雄市",
            "district": "苓雅區",
            "address": "高雄市苓雅區四維三路60號",
            "approved_capacity": 60,
            "monthly_fee": 7000,
            "google_rating": 4.4,
            "google_rating_count": 98,
            "floor_info": "車程約 15 分鐘；公立國小附幼、學費低",
            "has_after_school": False,
        },
    ]
    for c in competitors:
        exists = (
            session.query(CompetitorSchool.id)
            .filter(CompetitorSchool.source_school_id == c["source_school_id"])
            .first()
        )
        if exists:
            continue
        session.add(CompetitorSchool(is_active=True, has_penalty=False, **c))
        inserted += 1
    return inserted


def _seed_parent_inquiries(session) -> int:
    """家長詢問（~15 筆）。

    parent_inquiries 無 channel 欄位 → 把 電話/官網/LINE 管道寫進 question 內文。
    無唯一約束 → exists 查以 (name, question) 內容鍵冪等。
    部分標記已讀並附回覆。
    """
    inquiries = [
        (
            "林先生",
            "（電話詢問）請問 115 學年幼幼班還有名額嗎？孩子是 113 年 5 月出生。",
            True,
            "您好，幼幼班目前尚有名額，歡迎來園參觀，可來電 07-7778181 預約。",
        ),
        (
            "陳媽媽",
            "（官網表單）想了解小班的收費標準與課後才藝課程內容，謝謝。",
            True,
            "您好，已將收費明細與才藝課表寄至您留的 email，再請查收。",
        ),
        (
            "王小姐",
            "（LINE 詢問）請問貴園有提供交通車嗎？我們住鳳山區。",
            True,
            "您好，鳳山區部分路線有娃娃車服務，詳細路線可加我們官方 LINE 詢問。",
        ),
        ("黃爸爸", "（電話詢問）想預約參觀，平日下午方便嗎？", False, None),
        ("吳媽媽", "（官網表單）孩子有食物過敏，請問園所餐點如何處理？", False, None),
        (
            "劉先生",
            "（LINE 詢問）請問現在報名 115 上學期需要先預繳訂金嗎？金額多少？",
            True,
            "您好，預繳訂金為 5,000 元，報到時可全額折抵學費。",
        ),
        ("張媽媽", "（電話詢問）中班轉學進來可以嗎？目前讀別園小班。", False, None),
        ("李小姐", "（官網表單）想了解雙語教學的比例與師資。", False, None),
        (
            "周爸爸",
            "（LINE 詢問）園所有沒有提供延長照顧（課後留園）服務？到幾點？",
            True,
            "您好，課後留園服務至 18:30，採月計費，詳情可來電洽詢。",
        ),
        ("鄭媽媽", "（電話詢問）大班會有幼小銜接課程嗎？", False, None),
        (
            "謝小姐",
            "（官網表單）請問參觀需要先預約嗎？週六有開放嗎？",
            True,
            "您好，建議先預約以便安排導覽，週六採預約制，再麻煩來電確認時段。",
        ),
        ("許先生", "（LINE 詢問）住三民區建工路附近，走路會不會太遠？", False, None),
        ("蔡媽媽", "（電話詢問）孩子比較內向，園所對新生適應有什麼安排？", False, None),
        (
            "洪小姐",
            "（官網表單）想索取招生簡章與學費明細電子檔。",
            True,
            "您好，招生簡章與學費明細已寄至您填寫的信箱，歡迎參閱。",
        ),
        (
            "郭爸爸",
            "（LINE 詢問）請問還可以參加暑期班嗎？銜接 115 上學期。",
            False,
            None,
        ),
    ]

    inserted = 0
    for name, question, is_read, reply in inquiries:
        exists = (
            session.query(ParentInquiry.id)
            .filter(ParentInquiry.name == name, ParentInquiry.question == question)
            .first()
        )
        if exists:
            continue
        created = rand_date_between(RANGE_START, RANGE_END)
        created_dt = now_taipei_naive().replace(
            year=created.year, month=created.month, day=created.day
        )
        replied_at = None
        if is_read and reply:
            replied_at = created_dt
        session.add(
            ParentInquiry(
                name=name,
                phone=_random_phone(),
                question=question,
                is_read=is_read,
                reply=reply,
                replied_at=replied_at,
                created_at=created_dt,
            )
        )
        inserted += 1
    return inserted


def step() -> None:
    """灌入招生模組示範資料（冪等）。

    順序：先父層 period/month → 再 visits → competitors → parent_inquiries。
    """
    with session_scope() as session:
        admin = get_admin_user(session)

        n_periods = _seed_periods(session, admin)
        n_months = _seed_months(session)
        n_visits = _seed_visits(session)
        n_competitors = _seed_competitors(session)
        n_inquiries = _seed_parent_inquiries(session)

        print(
            "[seed.recruitment] 新增筆數："
            f"periods={n_periods} months={n_months} visits={n_visits} "
            f"competitors={n_competitors} parent_inquiries={n_inquiries}"
        )


if __name__ == "__main__":
    step()
