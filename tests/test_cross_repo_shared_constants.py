"""跨 repo 共用常數的 canonical 值鎖定（後端側）。

此測試「釘住」與前端雙寫的常數值——任何人在後端改值卻沒同步更新本檔的
期望值即 fail，強迫做出有意識的決策（並提醒同步前端）。

⚠️ 這是「弱次級守衛」：只能抓「同一 repo 內改了 const 卻沒改本測試」。
真正抓「前後端單側漂移」的是前端 CI 的跨 repo 比對
（ivy-frontend/scripts/check-shared-constants.mjs，在 openapi-drift job 內兩 repo
並存時讀後端值直接比對前端值）。本檔與該 script 的期望值必須一致。

對應前端常數（值必須一致）：
- MINIMUM_MONTHLY_WAGE      ↔ ivy-frontend src/constants/laborCompliance.ts
- MINIMUM_HOURLY_WAGE       ↔ ivy-frontend src/constants/laborCompliance.ts
- REFUND_APPROVAL_THRESHOLD ↔ ivy-frontend src/constants/pos.ts
- GRADE_TARGET_BONUS        ↔ ivy-frontend src/constants/activity.ts (FULL_ATTENDANCE_BONUS)

排除（看起來像雙寫但其實不是同一概念）：
- 勞退率 0.06：前端 PENSION_SELF_RATE_MAX 是「員工自提上限」（勞退條例§14），
  後端 PENSION_EMPLOYER_RATE 是「雇主提撥率」（§6）——法源與語意不同，值碰巧都 6%，
  不可互相 drift-check。
"""

from services.salary.minimum_wage import (
    MINIMUM_HOURLY_WAGE,
    MINIMUM_MONTHLY_WAGE,
)
from utils.activity_constants import GRADE_TARGET_BONUS, REFUND_APPROVAL_THRESHOLD


def test_minimum_wage_canonical():
    """基本工資（勞基法§21）；前端 laborCompliance.ts 須一致。"""
    assert MINIMUM_MONTHLY_WAGE == 29500
    assert MINIMUM_HOURLY_WAGE == 196


def test_refund_approval_threshold_canonical():
    """退費簽核門檻；前端 pos.ts REFUND_APPROVAL_THRESHOLD 須一致。"""
    assert REFUND_APPROVAL_THRESHOLD == 1000


def test_grade_target_bonus_canonical():
    """年級才藝達標獎金；前端 activity.ts FULL_ATTENDANCE_BONUS 須一致。"""
    assert GRADE_TARGET_BONUS == 1000
