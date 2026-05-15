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
