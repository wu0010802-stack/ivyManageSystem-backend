"""
薪資計算常數 - 模組級與類別級常數統一定義

「設定預設數值」（獎金基數、主管紅利、節慶/超額目標人數與每人金額、職位等級對應）
自 2026-06-25 起改由 `config_defaults.py` 單一定義，本檔 re-export 之以保持
`from services.salary.constants import X` 既有呼叫端不破壞（物件同一性）。
本檔僅自有定義純法規常數（工時/加班倍率/請假扣薪規則等，無 DB 對應、無 fallback 問題）。
設計：`docs/superpowers/specs/2026-06-25-salary-config-single-source-design.md` §2.1。
"""

# 單一事實來源 re-export（保持 `constants.X is config_defaults.X` 物件同一性）
from .config_defaults import (  # noqa: F401
    POSITION_GRADE_MAP,
    FESTIVAL_BONUS_BASE,
    TARGET_ENROLLMENT,
    OVERTIME_TARGET,
    OVERTIME_BONUS_PER_PERSON,
    SUPERVISOR_DIVIDEND,
    SUPERVISOR_FESTIVAL_BONUS,
    OFFICE_FESTIVAL_BONUS_BASE,
)

MONTHLY_BASE_DAYS = 30  # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）
MAX_DAILY_WORK_HOURS = (
    12.0  # 時薪制每日工時上限（正常 8H + 最高加班 4H，防止打卡異常灌水）
)

# 時薪制加班費倍率（勞基法第 24 條）
HOURLY_OT1_RATE = 1.34  # 日工時第 9–10 小時倍率
HOURLY_OT2_RATE = 1.67  # 日工時第 11 小時起倍率
HOURLY_REGULAR_HOURS = 8  # 正常日工時上限
HOURLY_OT1_CAP_HOURS = 10  # 第一分段上限（到第 10 小時止）

# 勞基法第 43 條 + 勞工請假規則第 4 條：
# 普通傷病假一年內未逾 30 日（240h）部分工資折半，超過部分雇主得不給薪。
SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS = 240.0

# 請假扣薪預設規則（與 api/leaves.py 同步，作為 deduction_ratio=None 時的 fallback）
LEAVE_DEDUCTION_RULES = {
    "personal": 1.0,  # 事假: 全扣
    "sick": 0.5,  # 病假: 扣半薪
    "menstrual": 0.5,  # 生理假: 扣半薪
    "annual": 0.0,  # 特休: 不扣
    "maternity": 0.0,  # 產假: 不扣
    "paternity": 0.0,  # 陪產假: 不扣
    "official": 0.0,  # 公假: 不扣
    "marriage": 0.0,  # 婚假: 不扣
    "bereavement": 0.0,  # 喪假: 不扣
    "prenatal": 0.0,  # 產檢假: 不扣
    "paternity_new": 0.0,  # 陪產檢及陪產假: 不扣
    "miscarriage": 0.0,  # 流產假: 不扣
    "family_care": 1.0,  # 家庭照顧假: 不給薪
    "parental_unpaid": 0.0,  # 育嬰留職停薪: 不扣
    "compensatory": 0.0,  # 補休: 不扣薪（加班換休）
}

# 預設扣款規則
DEFAULT_LATE_PER_MINUTE = 1  # 遲到每分鐘扣款（會被按比例覆蓋）
DEFAULT_EARLY_PER_MINUTE = 1  # 早退每分鐘扣款（會被按比例覆蓋）
DEFAULT_MISSING_PUNCH = 0  # 未打卡不扣款（僅記錄）
DEFAULT_MEETING_ABSENCE_PENALTY = 100  # 園務會議缺席扣節慶獎金（fallback；實際由 BonusConfig.meeting_absence_penalty 覆寫）
DEFAULT_MEETING_HOURS = 2  # 園務會議每次時數（fallback；實際由 BonusConfig.meeting_default_hours 覆寫；業主實務 2 hr）

# 注意：POSITION_GRADE_MAP / FESTIVAL_BONUS_BASE / TARGET_ENROLLMENT / OVERTIME_TARGET /
# OVERTIME_BONUS_PER_PERSON / SUPERVISOR_DIVIDEND / SUPERVISOR_FESTIVAL_BONUS /
# OFFICE_FESTIVAL_BONUS_BASE 已移至 config_defaults.py（單一事實來源），於檔首 re-export。
