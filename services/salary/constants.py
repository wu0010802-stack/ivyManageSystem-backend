"""
薪資計算常數 - 模組級與類別級常數統一定義
"""

MONTHLY_BASE_DAYS = 30  # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）
MAX_DAILY_WORK_HOURS = 12.0  # 時薪制每日工時上限（正常 8H + 最高加班 4H，防止打卡異常灌水）

# 時薪制加班費倍率（勞基法第 24 條）
HOURLY_OT1_RATE = 1.34        # 日工時第 9–10 小時倍率
HOURLY_OT2_RATE = 1.67        # 日工時第 11 小時起倍率
HOURLY_REGULAR_HOURS = 8      # 正常日工時上限
HOURLY_OT1_CAP_HOURS = 10     # 第一分段上限（到第 10 小時止）

# 請假扣薪預設規則（與 api/leaves.py 同步，作為 deduction_ratio=None 時的 fallback）
LEAVE_DEDUCTION_RULES = {
    "personal": 1.0,        # 事假: 全扣
    "sick": 0.5,             # 病假: 扣半薪
    "menstrual": 0.5,        # 生理假: 扣半薪
    "annual": 0.0,           # 特休: 不扣
    "maternity": 0.0,        # 產假: 不扣
    "paternity": 0.0,        # 陪產假: 不扣
    "official": 0.0,         # 公假: 不扣
    "marriage": 0.0,         # 婚假: 不扣
    "bereavement": 0.0,      # 喪假: 不扣
    "prenatal": 0.0,         # 產檢假: 不扣
    "paternity_new": 0.0,    # 陪產檢及陪產假: 不扣
    "miscarriage": 0.0,      # 流產假: 不扣
    "family_care": 1.0,      # 家庭照顧假: 不給薪
    "parental_unpaid": 0.0,  # 育嬰留職停薪: 不扣
}

# 預設扣款規則
DEFAULT_LATE_PER_MINUTE = 1       # 遲到每分鐘扣款（會被按比例覆蓋）
DEFAULT_EARLY_PER_MINUTE = 1      # 早退每分鐘扣款（會被按比例覆蓋）
DEFAULT_AUTO_LEAVE_THRESHOLD = 120  # 遲到超過幾分鐘轉事假半天
DEFAULT_MISSING_PUNCH = 0         # 未打卡不扣款（僅記錄）
DEFAULT_MEETING_PAY = 200         # 園務會議加班費
DEFAULT_MEETING_PAY_6PM = 100     # 6點下班者園務會議加班費
DEFAULT_MEETING_ABSENCE_PENALTY = 100  # 園務會議缺席扣節慶獎金

# 節慶獎金職位等級對應
# A級 = 幼兒園教師, B級 = 教保員, C級 = 助理教保員
POSITION_GRADE_MAP = {
    '幼兒園教師': 'A',
    '教保員': 'B',
    '助理教保員': 'C',
}

# 節慶獎金基數 (依職位等級和角色)
# 角色: head_teacher=班導, assistant_teacher=副班導
FESTIVAL_BONUS_BASE = {
    'head_teacher': {
        'A': 2000,
        'B': 2000,
        'C': 1500,
    },
    'assistant_teacher': {
        'A': 1200,
        'B': 1200,
        'C': 1200,
    }
}

# 節慶獎金目標人數 (依年級和教師配置)
# 格式: grade_name -> { teacher_count -> target }
# 2_teachers = 班導+副班導 (1班1副班導)
# 1_teacher = 只有班導 (無副班導)
# shared_assistant = 2班共用同一個副班導
TARGET_ENROLLMENT = {
    '大班': {'2_teachers': 27, '1_teacher': 14, 'shared_assistant': 20},
    '中班': {'2_teachers': 25, '1_teacher': 13, 'shared_assistant': 18},
    '小班': {'2_teachers': 23, '1_teacher': 12, 'shared_assistant': 16},
    '幼幼班': {'2_teachers': 15, '1_teacher': 7, 'shared_assistant': 12},
}

# 超額獎金目標人數（與節慶獎金不同）
OVERTIME_TARGET = {
    '大班': {'2_teachers': 25, '1_teacher': 13, 'shared_assistant': 20},
    '中班': {'2_teachers': 23, '1_teacher': 12, 'shared_assistant': 18},
    '小班': {'2_teachers': 21, '1_teacher': 11, 'shared_assistant': 16},
    '幼幼班': {'2_teachers': 14, '1_teacher': 7, 'shared_assistant': 12},
}

# 超額獎金每人金額（依角色和年級）
OVERTIME_BONUS_PER_PERSON = {
    'head_teacher': {
        '大班': 400, '中班': 400, '小班': 400, '幼幼班': 450
    },
    'assistant_teacher': {
        '大班': 100, '中班': 100, '小班': 100, '幼幼班': 150
    }
}

# 主管紅利（依職稱）
SUPERVISOR_DIVIDEND = {
    '園長': 5000,
    '主任': 4000,
    '組長': 3000,
    '副組長': 1500
}

# 主管節慶獎金基數（依職稱）
SUPERVISOR_FESTIVAL_BONUS = {
    '園長': 6500,
    '主任': 3500,
    '組長': 2000
}

# 司機/美編/行政節慶獎金基數（全校比例計算，無超額獎金）
OFFICE_FESTIVAL_BONUS_BASE = {
    '司機': 1000,
    '美編': 1000,
    '行政': 2000
}
