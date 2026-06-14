"""法定參考資料（純 Python 常數 + 函式）。

收錄 m00 設定型模組落庫所需的「政府/法定 canonical 值」，全部以純 Python
常數內嵌，**不 import alembic migration**（避免把 migration 當 runtime 依賴）。

來源對齊：
- 勞健保級距：``alembic/versions/20260507_d9e0f1g2h3i4_insurance_brackets_to_db.py``
  的 ``_BRACKETS_2026``（82 筆，民國 115 / 西元 2026 公告級距）。
- 勞健保費率：``models/config.py`` InsuranceRate 欄位預設值。
- 職位標準底薪：``services/salary/engine.py`` 的 ``_POSITION_SALARY_DEFAULTS``
  與 ``models/config.py`` PositionSalaryConfig 欄位預設值（確保與薪資引擎一致）。
- 考核計分目錄：``alembic/versions/20260511_a7p8p9r0i1s2_appraisal_seed_catalog.py``
  的 ``CATALOG_ITEMS``（15 項，對齊 Excel 半年考核表編號 2-16）。

dict 鍵名一律對齊對應 model 欄位，供 m00 直接 ``Model(**row)`` 落庫。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 勞健保投保金額分級表（2026 / 民國 115 公告，canonical 82 筆）
#
# tuple 順序對齊 migration ``_BRACKETS_2026``：
#   (amount, labor_employee, labor_employer, health_employee, health_employer, pension)
# ---------------------------------------------------------------------------
_BRACKETS_2026: list[tuple[int, int, int, int, int, int]] = [
    (1500, 277, 972, 458, 1428, 90),
    (3000, 277, 972, 458, 1428, 180),
    (4500, 277, 972, 458, 1428, 270),
    (6000, 277, 972, 458, 1428, 360),
    (7500, 277, 972, 458, 1428, 450),
    (8700, 277, 972, 458, 1428, 522),
    (9900, 277, 972, 458, 1428, 594),
    (11100, 277, 972, 458, 1428, 666),
    (12540, 313, 1097, 458, 1428, 752),
    (13500, 338, 1182, 458, 1428, 810),
    (15840, 396, 1386, 458, 1428, 950),
    (16500, 413, 1444, 458, 1428, 990),
    (17280, 432, 1512, 458, 1428, 1037),
    (17880, 447, 1564, 458, 1428, 1073),
    (19047, 476, 1666, 458, 1428, 1143),
    (20008, 500, 1751, 458, 1428, 1200),
    (21009, 525, 1838, 458, 1428, 1261),
    (22000, 550, 1925, 458, 1428, 1320),
    (23100, 577, 2022, 458, 1428, 1386),
    (24000, 600, 2100, 458, 1428, 1440),
    (25250, 632, 2210, 458, 1428, 1515),
    (26400, 660, 2310, 458, 1428, 1584),
    (27600, 690, 2415, 458, 1428, 1656),
    (28590, 715, 2501, 458, 1428, 1715),
    (29500, 738, 2582, 458, 1428, 1770),
    (30300, 758, 2651, 470, 1466, 1818),
    (31800, 795, 2783, 493, 1539, 1908),
    (33300, 833, 2914, 516, 1611, 1998),
    (34800, 870, 3045, 540, 1684, 2088),
    (36300, 908, 3176, 563, 1757, 2178),
    (38200, 955, 3342, 592, 1849, 2292),
    (40100, 1002, 3509, 622, 1940, 2406),
    (42000, 1050, 3675, 651, 2032, 2520),
    (43900, 1098, 3841, 681, 2124, 2634),
    (45800, 1145, 4008, 710, 2216, 2748),
    (48200, 1145, 4008, 748, 2332, 2892),
    (50600, 1145, 4008, 785, 2449, 3036),
    (53000, 1145, 4008, 822, 2565, 3180),
    (55400, 1145, 4008, 859, 2681, 3324),
    (57800, 1145, 4008, 896, 2797, 3468),
    (60800, 1145, 4008, 943, 2942, 3648),
    (63800, 1145, 4008, 990, 3087, 3828),
    (66800, 1145, 4008, 1036, 3233, 4008),
    (69800, 1145, 4008, 1083, 3378, 4188),
    (72800, 1145, 4008, 1129, 3523, 4368),
    (76500, 1145, 4008, 1187, 3702, 4590),
    (80200, 1145, 4008, 1244, 3881, 4812),
    (83900, 1145, 4008, 1301, 4060, 5034),
    (87600, 1145, 4008, 1359, 4239, 5256),
    (92100, 1145, 4008, 1428, 4457, 5526),
    (96600, 1145, 4008, 1498, 4675, 5796),
    (101100, 1145, 4008, 1568, 4892, 6066),
    (105600, 1145, 4008, 1638, 5110, 6336),
    (110100, 1145, 4008, 1708, 5328, 6606),
    (115500, 1145, 4008, 1791, 5589, 6930),
    (120900, 1145, 4008, 1875, 5850, 7254),
    (126300, 1145, 4008, 1959, 6112, 7578),
    (131700, 1145, 4008, 2043, 6373, 7902),
    (137100, 1145, 4008, 2126, 6634, 8226),
    (142500, 1145, 4008, 2210, 6896, 8550),
    (147900, 1145, 4008, 2294, 7157, 8874),
    (150000, 1145, 4008, 2327, 7259, 9000),
    (156400, 1145, 4008, 2426, 7568, 9000),
    (162800, 1145, 4008, 2525, 7878, 9000),
    (169200, 1145, 4008, 2624, 8188, 9000),
    (175600, 1145, 4008, 2724, 8497, 9000),
    (182000, 1145, 4008, 2823, 8807, 9000),
    (189500, 1145, 4008, 2939, 9170, 9000),
    (197000, 1145, 4008, 3055, 9533, 9000),
    (204500, 1145, 4008, 3172, 9896, 9000),
    (212000, 1145, 4008, 3288, 10259, 9000),
    (219500, 1145, 4008, 3404, 10622, 9000),
    (228200, 1145, 4008, 3539, 11043, 9000),
    (236900, 1145, 4008, 3674, 11464, 9000),
    (245600, 1145, 4008, 3809, 11885, 9000),
    (254300, 1145, 4008, 3944, 12306, 9000),
    (263000, 1145, 4008, 4079, 12727, 9000),
    (273000, 1145, 4008, 4234, 13211, 9000),
    (283000, 1145, 4008, 4389, 13695, 9000),
    (293000, 1145, 4008, 4544, 14179, 9000),
    (303000, 1145, 4008, 4700, 14663, 9000),
    (313000, 1145, 4008, 4855, 15146, 9000),
]

# 三制度最高投保上限（2026，來源同 migration 回填值）。
_INSURANCE_MAX_INSURED = {
    "labor_max_insured": 45800,
    "health_max_insured": 219500,
    "pension_max_insured": 150000,
}

# 適用年度（西元）；m00 跨 config_year 需要 2025 與 2026 兩套，故此處集中常數。
INSURANCE_EFFECTIVE_YEARS: tuple[int, ...] = (2025, 2026)

# ---------------------------------------------------------------------------
# 職位標準底薪（對齊 services/salary/engine.py._POSITION_SALARY_DEFAULTS，
# 與 models/config.py PositionSalaryConfig 欄位預設值）。
#
# 鍵名對齊 PositionSalaryConfig 欄位，供 m00 直接落 position_salary_configs。
# ---------------------------------------------------------------------------
_POSITION_SALARY_STANDARDS: dict[str, int | None] = {
    "head_teacher_a": 39240,
    "head_teacher_b": 37160,
    "head_teacher_c": 33000,
    "assistant_teacher_a": 35240,
    "assistant_teacher_b": 33000,
    "assistant_teacher_c": 29500,
    "admin_staff": 37160,
    "english_teacher": 32500,
    "art_teacher": 30000,
    "designer": 30000,
    "nurse": 29800,
    "driver": 30000,
    "kitchen_staff": 29700,
    "director": None,
    "principal": None,
}

# m01 會用到的 7 職稱（role key）→ 對應 PositionSalaryConfig 底薪欄位。
# role key 對齊「共享契約」employees_by_role：
#   supervisor/admin/accountant/homeroom/assistant/art/support
# 才藝時薪（art）為兼職時薪制；支援（support）走廚房/司機等支援底薪。
_ROLE_TO_SALARY_FIELD: dict[str, str] = {
    "supervisor": "director",  # 主管：以主任標準底薪為基（None→由個人 base_salary）
    "admin": "admin_staff",  # 行政
    "accountant": "admin_staff",  # 會計（比照行政底薪）
    "homeroom": "head_teacher_b",  # 班導（B 級為常見預設）
    "assistant": "assistant_teacher_b",  # 助教（副班導 B 級）
    "art": "art_teacher",  # 才藝時薪（美術老師標準）
    "support": "kitchen_staff",  # 支援（廚房/支援底薪）
}

# 才藝時薪（兼職）每小時費率預設（與 art_teacher_payroll 慣用值對齊；m01 可覆寫）。
ART_TEACHER_HOURLY_RATE: int = 360

# ---------------------------------------------------------------------------
# 考核計分目錄（15 項，對齊 Excel 半年考核表編號 2-16）。
# tuple 順序：(code, label, sign, default_weight, data_source, description, display_order)
# ---------------------------------------------------------------------------
_APPRAISAL_CATALOG: list[tuple[str, str, str, float, str, str, int]] = [
    (
        "LEAVE",
        "請休假",
        "NEGATIVE",
        0,
        "leave",
        "請假與休假合併扣分；公式於 engine 計算",
        1,
    ),
    (
        "LATE_EARLY",
        "遲到/早退",
        "NEGATIVE",
        -0.25,
        "attendance",
        "每次 -0.25；可從 attendance 模組自動匯入",
        2,
    ),
    ("NO_CLOCK", "未打卡", "NEGATIVE", -0.25, "attendance", "每次 -0.25", 3),
    (
        "MISS_PRESCHOOL_MEETING",
        "園務會議未參加",
        "NEGATIVE",
        -1,
        "manual",
        "每次 -1；由主管手動登錄",
        4,
    ),
    (
        "ORG_MEETING_0913",
        "9/13 機構會議研習",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        5,
    ),
    (
        "ORG_MEETING_1115",
        "11/15 機構會議研習",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        6,
    ),
    (
        "TEAM_ACTIVITY_1115",
        "11/15 自強活動",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        7,
    ),
    (
        "DROPOUT_0915",
        "9/15 休學人數",
        "NEGATIVE",
        0,
        "manual",
        "休學人數扣分 = 休學人數×係數，公式於 engine 計算",
        8,
    ),
    (
        "DROPOUT_0315",
        "3/15 休學人數",
        "NEGATIVE",
        0,
        "manual",
        "公式：(全園休學×2 + 試讀休學×1 - 回園×1)/班級數",
        9,
    ),
    (
        "CHILD_INCIDENT",
        "幼兒意外",
        "NEGATIVE",
        0,
        "manual",
        "依嚴重度扣分；note 存事件明細",
        10,
    ),
    (
        "RETURNING_RATE_0315",
        "3/15 舊生註冊率",
        "NEUTRAL",
        0,
        "monthly_enrollment_snapshots",
        "舊生註冊率達標加分、未達扣分",
        11,
    ),
    (
        "CLASS_SIZE",
        "帶班人數",
        "NEUTRAL",
        0,
        "monthly_enrollment_snapshots",
        "編制以上加、以下扣；公式於 engine 計算",
        12,
    ),
    (
        "AFTER_CLASS_RATE",
        "才藝班參加率",
        "POSITIVE",
        0,
        "activity_service",
        "達 100% 加 2 分；可從 activity 模組自動帶入",
        13,
    ),
    ("SPED", "特別辦法（特教生）", "POSITIVE", 2, "manual", "每位特教生 +2", 14),
    (
        "REWARD_PUNISH",
        "獎懲",
        "NEUTRAL",
        0,
        "disciplinary",
        "可多筆並列；note 存大過/嘉獎明細",
        15,
    ),
]


def insurance_brackets(
    years: tuple[int, ...] = INSURANCE_EFFECTIVE_YEARS,
) -> list[dict]:
    """回傳勞健保投保金額分級表 list[dict]，鍵名對齊 InsuranceBracket 欄位。

    預設同時產出 2025 與 2026 兩個 ``effective_year`` 各 82 筆（period-aware
    resolver 需跨年度）。每筆含 ``effective_year/amount/labor_employee/
    labor_employer/health_employee/health_employer/pension``。
    """
    rows: list[dict] = []
    for year in years:
        for amount, le, lr, he, hr, pension in _BRACKETS_2026:
            rows.append(
                {
                    "effective_year": year,
                    "amount": amount,
                    "labor_employee": le,
                    "labor_employer": lr,
                    "health_employee": he,
                    "health_employer": hr,
                    "pension": pension,
                }
            )
    return rows


def insurance_rates(years: tuple[int, ...] = INSURANCE_EFFECTIVE_YEARS) -> list[dict]:
    """回傳勞健保費率 list[dict]，鍵名對齊 InsuranceRate 欄位。

    費率值取自 InsuranceRate 欄位預設（labor 12.5% / health 5.17% / 勞退 6% 等），
    三制度上限取自 migration 回填值。預設產出每個 ``rate_year`` 各一列。
    """
    rows: list[dict] = []
    for year in years:
        rows.append(
            {
                "rate_year": year,
                "version": 1,
                "labor_rate": 0.125,
                "labor_employee_ratio": 0.20,
                "labor_employer_ratio": 0.70,
                "labor_government_ratio": 0.10,
                "health_rate": 0.0517,
                "health_employee_ratio": 0.30,
                "health_employer_ratio": 0.60,
                "pension_employer_rate": 0.06,
                "average_dependents": 0.56,
                "supplementary_health_rate": 0.0211,
                "supplementary_health_threshold": 29500,
                "is_active": True,
                **_INSURANCE_MAX_INSURED,
            }
        )
    return rows


def position_salary_standards() -> dict:
    """回傳職位標準底薪 dict，鍵名對齊 PositionSalaryConfig 欄位。

    值與薪資引擎 ``_POSITION_SALARY_DEFAULTS`` 一致（確保 seed 後跑真引擎
    底薪解析無漂移）。``director`` / ``principal`` 允許 None（留空=不套標準）。

    回傳的 dict 涵蓋 m01 會用到的 7 職稱對應欄位：
    主管(director) / 行政(admin_staff) / 會計(admin_staff) /
    班導(head_teacher_*) / 助教(assistant_teacher_*) /
    才藝時薪(art_teacher) / 支援(kitchen_staff)。
    """
    return dict(_POSITION_SALARY_STANDARDS)


def role_salary_field(role: str) -> str:
    """role key（supervisor/admin/.../support）→ PositionSalaryConfig 底薪欄位名。"""
    return _ROLE_TO_SALARY_FIELD[role]


def base_salary_for_role(role: str) -> int | None:
    """role key → 標準底薪值（None=留空，由個人 base_salary 決定）。"""
    return position_salary_standards()[role_salary_field(role)]


def appraisal_catalog() -> list[dict]:
    """回傳考核計分目錄 list[dict]（15 項），鍵名對齊 AppraisalScoreItemCatalog 欄位。

    每筆含 ``code/label/sign/default_weight/data_source/description/
    display_order/is_active``，``display_order`` 對齊 Excel 半年考核表欄位順序。
    """
    return [
        {
            "code": code,
            "label": label,
            "sign": sign,
            "default_weight": weight,
            "data_source": data_source,
            "description": desc,
            "display_order": order,
            "is_active": True,
        }
        for code, label, sign, weight, data_source, desc, order in _APPRAISAL_CATALOG
    ]
