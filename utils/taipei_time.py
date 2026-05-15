"""utils/taipei_time.py — 台灣時區一致性 helper（F2 第一階段抽出）。

Why: 大量端點寫入 created_at / payment_date / approved_at 時混用
`datetime.now()`（裸 naive）與 `datetime.now(TAIPEI_TZ).date()`，server
部署在 UTC 時會在午夜前後產生 ±8 小時稽核錯位（落帳到昨天、簽核日跨日、
unlock 窗口偏移）。本檔提供統一入口。

公開：
- TAIPEI_TZ — ZoneInfo("Asia/Taipei") 單例
- today_taipei() -> date — 取今日（台灣時區）
- now_taipei_naive() -> datetime — 取現在時刻並 strip tzinfo（與既有 naive 欄位相容）
- validate_payment_date(value, *, back_limit_days) -> date — 統一禁止未來日、限制回補天數
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def today_taipei() -> date:
    """統一取「今日」（Asia/Taipei）。

    Why: server 若部署在 UTC，近午夜台灣時間 datetime.now().date() 會落到昨天，
    與日結 snapshot 錯位。本函式確保所有 activity / 金流寫入以台灣時間為準。
    """
    return datetime.now(TAIPEI_TZ).date()


def now_taipei_naive() -> datetime:
    """統一取「現在時刻」(Asia/Taipei naive)。

    Why: `ActivityPaymentRecord.created_at` 已用 naive 寫入台灣時間；簽核
    `approved_at` / unlock `unlocked_at` / pending `reviewed_at` / query token
    `issued_at` 若用裸 datetime.now()，server 部署在 UTC 時會比同檔
    `today = datetime.now(TAIPEI_TZ).date()` 慢 8 小時，造成稽核時序錯位
    （例：簽核紀錄落在 close_date 前一日、unlock 事件 cutoff 過濾窗口偏移）。
    任何寫入或對比 naive datetime 欄位的端點都應改用本函式以保持一致。
    """
    return datetime.now(TAIPEI_TZ).replace(tzinfo=None)


def validate_payment_date(value: date, *, back_limit_days: int = None) -> date:
    # F2-aux：default 從 magic number 30 改用 utils.activity_constants 單一來源。
    # lazy import 避免 module-level 循環：activity_constants 不 import taipei_time。
    if back_limit_days is None:
        from utils.activity_constants import PAYMENT_DATE_BACK_LIMIT_DAYS

        back_limit_days = PAYMENT_DATE_BACK_LIMIT_DAYS
    """金流端點共用守衛：禁未來日、限制回補天數，比對基準為台灣時區今日。

    Why: 缺此守衛會計可填未來日造帳或回填遠古日期搬動財報歸月；裸
    date.today() 比對在 UTC 部署近午夜會偏一天，使學費跨月分期合法情境
    被誤擋。統一以台灣時區比對。

    back_limit_days 預設 30 天（活動端）；學費分期跨季合法情境可放寬 90 天
    （`api/fees/_helpers.py` 設定）。
    """
    today = datetime.now(TAIPEI_TZ).date()
    if value > today:
        raise ValueError("繳費日期不可為未來日期")
    earliest = today - timedelta(days=back_limit_days)
    if value < earliest:
        raise ValueError(f"繳費日期超出範圍，最多回補 {back_limit_days} 天")
    return value
