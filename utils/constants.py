"""
跨模組共用的驗證常數。
修改這裡的值會同步影響管理端與 portal 端的驗證邏輯。
"""

# ── 系統設定年份範圍 ───────────────────────────────────────
MIN_CONFIG_YEAR = 2000
MAX_CONFIG_YEAR = 2100

# ── 假別類型 ──────────────────────────────────────────────
LEAVE_TYPE_LABELS: dict[str, str] = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
    "compensatory": "補休",
    "occupational_injury": "公傷病假",
    "pregnancy_rest": "安胎休養假",
    "typhoon": "颱風假",
}

# ── 加班計算（勞基法） ──────────────────────────────────────
MAX_OVERTIME_HOURS = 12.0  # 單筆加班上限（正常 8H + 延長最多 4H）
MAX_MONTHLY_OVERTIME_HOURS = 46.0  # 勞基法第 32 條第 2 項：每月延長工時上限
DAILY_WORK_HOURS = 8  # 每日法定工時
WEEKDAY_FIRST_2H_RATE = 1.34  # 平日前 2 小時
WEEKDAY_AFTER_2H_RATE = 1.67  # 平日第 3-4 小時
WEEKDAY_THRESHOLD_HOURS = 2  # 平日倍率分界時數
HOLIDAY_RATE = 2.0  # 例假日 / 國定假日

# 休息日（週休二日第一天）倍率常數（勞基法第 24 條第 2 項）
RESTDAY_FIRST_2H_RATE = 1.34  # 前 2 小時（勞基法第 24 條第 2 項，法定下限）
RESTDAY_MID_RATE = 1.67  # 第 3-8 小時
RESTDAY_AFTER_8H_RATE = 2.67  # 超過 8 小時
RESTDAY_FIRST_SEGMENT = 2  # 第一分段上限
RESTDAY_SECOND_SEGMENT = 8  # 第二分段上限
RESTDAY_MIN_HOURS = 2  # 最低計費時數（工作不足 2h 仍算 2h）

# ── 加班類型 ──────────────────────────────────────────────
OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}

# ── 學生事故 ──────────────────────────────────────────────
VALID_INCIDENT_TYPES = {"身體健康", "意外受傷", "行為觀察", "其他"}
VALID_SEVERITIES = {"輕微", "中度", "嚴重"}

# ── 學生評量 ──────────────────────────────────────────────
VALID_ASSESSMENT_TYPES = {"期中", "期末", "學期"}
VALID_DOMAINS = {"身體動作與健康", "語文", "認知", "社會", "情緒", "美感", "綜合"}
VALID_RATINGS = {"優", "良", "需加強"}
