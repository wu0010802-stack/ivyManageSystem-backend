"""utils/activity_constants.py — 才藝模組共用常數（單一來源）。

F2-aux：F2 第三階段曾因 schemas/activity_admin.py 與 api/activity/_shared.py
各自宣告 MAX_PAYMENT_AMOUNT 而引入 typo regression（99_999 vs 999_999），
本檔把所有金額/字數/天數常數集中在一處，schemas + _shared 都 import 同一份。

呼叫端慣例：
- schemas/activity_*.py：import 此檔（已是低層，不會循環）
- api/activity/_shared.py：import 此檔；本檔不 import _shared
- api/fees/* / api/portal/* 等需要相同值的模組：直接 import 此檔
"""

# 單筆金額上限（NT$）— 應用於 ActivityPayment / Course / Supply 價格欄位
MAX_PAYMENT_AMOUNT = 999_999

# Refund notes（退費原因）最少字數；嚴於 Void 是因退費直接影響財務流水
MIN_REFUND_REASON_LENGTH = 15

# Void payment 軟刪原因最少字數
MIN_VOID_REASON_LENGTH = 5

# 繳費日期回補天數上限（活動 POS 場景；學費分期跨季可放寬至 90）
PAYMENT_DATE_BACK_LIMIT_DAYS = 30

# 退費金額閾值：超過此金額的單筆退費必須具備 ACTIVITY_PAYMENT_APPROVE 權限
# Why: 小額退費允許一線櫃檯彈性處理；大額退費強制雙簽以防內部舞弊
REFUND_APPROVAL_THRESHOLD = 1000

# 課程/用品單品價格高額閾值：超過此金額的設定/異動必須具備 ACTIVITY_PAYMENT_APPROVE。
# Why: 課程價格會被寫入 price_snapshot 進入應繳總額，搭配「補齊收入」路徑可建立異常高額
# 應收。一般幼稚園單品價格遠低於 30,000，超過視為設定錯誤或舞弊嘗試。
ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD = 30_000

# 實退 vs calculator 建議值差距閾值（NT$）；超過此差距需 ACTIVITY_PAYMENT_APPROVE 權限。
# Why: 員工算錯/故意多退之事前制衡；與 REFUND_APPROVAL_THRESHOLD（總額）獨立，
# 兩道閘共存，任一觸發都要簽核。
ACTIVITY_REFUND_DIFF_THRESHOLD = 100

# 年級才藝達標獎金（分數）：年級報名達標率 >= 設定 target_pct 時給予的獎金分數。
# 前端 src/constants/activity.ts FULL_ATTENDANCE_BONUS 須與此值一致（dashboard 顯示比對用）。
GRADE_TARGET_BONUS = 1000

# 候補升正式的「佔位」狀態集合：enrolled + promoted_pending 皆佔容量，
# 決定「還有無名額」時務必 IN 兩者；統計/出席/收入等語意只算 enrolled。
# 單一來源：services / api/activity / api/parent_portal 一律 import 此常數，
# 不再各處 inline `["enrolled", "promoted_pending"]`（漏掉 promoted_pending 會超發候補）。
OCCUPYING_STATUSES = ("enrolled", "promoted_pending")

# ActivityCourse.capacity 欄位 nullable（models 為 default=30，僅 ORM insert 套用，
# DB 既有/歷史列可為 NULL）。容量計算一律把 NULL 視為 30。單一來源，取代散落各處的
# `capacity if not None else 30` 與第二份常數定義（api/parent_portal/activity 等）。
DEFAULT_COURSE_CAPACITY = 30


def effective_capacity(course) -> int:
    """課程有效容量：capacity 為 NULL 時回 DEFAULT_COURSE_CAPACITY（30）。

    Why: capacity 欄位 nullable，DB 歷史列可能為 NULL；「還有無名額」的判定
    一律把 NULL 視為 30。把這條口徑收斂成單一函式，避免任一站點漏改而漂移
    （2026-06-23 audit P2-3 即因某站點誤用 999 致容量閘形同虛設）。

    course: 任何具 `.capacity` 屬性的物件（ActivityCourse / 具同名欄位的 row）。
    """
    return course.capacity if course.capacity is not None else DEFAULT_COURSE_CAPACITY
