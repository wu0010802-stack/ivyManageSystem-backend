"""薪資設定預設數值 — 單一事實來源（low-level 純資料模組）。

設計：`docs/superpowers/specs/2026-06-25-salary-config-single-source-design.md` §2.1。

本模組集中定義「同時存在於 DB（權威）與原始碼（fallback）」的薪資設定預設數值
（獎金基數、主管紅利、節慶/超額目標人數與每人金額、職位等級對應）。其餘處
（`constants.py` re-export、`startup/seed.py`、`models/config.py` default）一律引用本模組，
避免同一數值多處手抄漂移。

**依賴方向（避免循環 import）**：本模組為最低層純資料，**不得 import** `models/`、
`services/salary/engine.py` 或 `constants.py`。
"""

# 節慶獎金職位等級對應
# A級 = 幼兒園教師, B級 = 教保員, C級 = 助理教保員
POSITION_GRADE_MAP = {
    "幼兒園教師": "A",
    "教保員": "B",
    "助理教保員": "C",
}

# 節慶獎金基數 (依職位等級和角色)
# 角色: head_teacher=班導, assistant_teacher=副班導
FESTIVAL_BONUS_BASE = {
    "head_teacher": {
        "A": 2000,
        "B": 2000,
        "C": 1500,
    },
    "assistant_teacher": {
        "A": 1200,
        "B": 1200,
        "C": 1200,
    },
    "art_teacher": {  # 美語教師（classroom.art_teacher_id），依第十二條一律 2000
        "A": 2000,
        "B": 2000,
        "C": 2000,
    },
}

# 節慶獎金目標人數 (依年級和教師配置)
# 格式: grade_name -> { teacher_count -> target }
# 2_teachers = 班導+副班導 (1班1副班導)
# 1_teacher = 只有班導 (無副班導)
# shared_assistant = 2班共用同一個副班導
#
# 2026-06-25 業主裁定：DB GradeTarget seed（startup/seed.py:156-）為正確值，
# 本 fallback 已對齊（大班 27/14、中班 25/13、小班 23）；prod 走 DB 數字不變，
# 本表僅 dev/test/fresh 無 DB GradeTarget 時生效。詳見設計 §2.0。
TARGET_ENROLLMENT = {
    "大班": {"2_teachers": 27, "1_teacher": 14, "shared_assistant": 20},
    "中班": {"2_teachers": 25, "1_teacher": 13, "shared_assistant": 18},
    "小班": {"2_teachers": 23, "1_teacher": 12, "shared_assistant": 16},
    "幼幼班": {"2_teachers": 15, "1_teacher": 7, "shared_assistant": 12},
}

# 超額獎金目標人數（與節慶獎金不同）
OVERTIME_TARGET = {
    "大班": {"2_teachers": 25, "1_teacher": 13, "shared_assistant": 20},
    "中班": {"2_teachers": 23, "1_teacher": 12, "shared_assistant": 18},
    "小班": {"2_teachers": 21, "1_teacher": 11, "shared_assistant": 16},
    "幼幼班": {"2_teachers": 14, "1_teacher": 7, "shared_assistant": 12},
}

# 超額獎金每人金額（依角色和年級）
OVERTIME_BONUS_PER_PERSON = {
    "head_teacher": {"大班": 400, "中班": 400, "小班": 400, "幼幼班": 450},
    "assistant_teacher": {"大班": 100, "中班": 100, "小班": 100, "幼幼班": 150},
}

# 主管紅利（依主管職）
SUPERVISOR_DIVIDEND = {"園長": 5000, "主任": 4000, "組長": 3000, "副組長": 1500}

# 主管節慶獎金基數（依主管職）
SUPERVISOR_FESTIVAL_BONUS = {"園長": 6500, "主任": 3500, "組長": 2000}

# 司機/美編/行政節慶獎金基數（全校比例計算，無超額獎金）
OFFICE_FESTIVAL_BONUS_BASE = {"司機": 1000, "美編": 1000, "行政": 2000}
