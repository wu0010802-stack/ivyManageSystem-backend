"""資遣費與平均工資計算

法源：
- 平均工資：勞基法第 2 條第 4 款
- 舊制資遣費：勞基法第 17 條
- 新制資遣費：勞工退休金條例第 12 條

⚠️ 狀態：預留 API，目前無生產 caller（2026-05-26 確認）
    本 module 是純函式法律邏輯庫，主 repo 唯一呼叫者是 tests/test_severance.py。
    無 endpoint / service / api 直接呼叫——18 個 test 等於法律邏輯的可執行文件。

    若日後要接到實際離職結算 flow（建議與 services/offboarding/ 整合）：
    1. 補 spec：產品決策資遣費觸發條件、舊新制適用、平均工資來源
    2. 呼叫方需自己用 utils.rounding.round_half_up 守 .5 邊界
       （本 module 純 float 算法不 round，配合政府/勞健保標準）
    3. 將 services/salary/severance.py 加入 money-rounding-gate paths
    4. 移除 tests/test_severance_dead_code_guard.py guard test

    Guard：tests/test_severance_dead_code_guard.py 會在 production caller
    出現時 fail，提醒落實上述整合步驟（避免悄悄接生產但跳過 spec/rounding/CI gate）。
"""

from datetime import date

# 新制資遣費係數（勞工退休金條例第 12 條）：
# 每滿 1 年發給 0.5 個月平均工資，最高以 6 個月為限。
SEVERANCE_NEW_MONTHS_PER_YEAR = 0.5
SEVERANCE_NEW_MAX_MONTHS = 6.0


def calculate_service_years(hire_date: date, end_date: date) -> float:
    """年資（小數年）。離職日早於到職日回傳 0。"""
    if end_date <= hire_date:
        return 0.0
    return (end_date - hire_date).days / 365.25


def calculate_average_monthly_wage(records: list[tuple[float, int]]) -> float:
    """平均月工資（勞基法第 2 條第 4 款）。

    records: 事由發生當日前 6 個月，每筆 (該月所得工資, 該月日數)
    公式：工資總額 ÷ 總日數 × 30
    """
    if not records:
        return 0.0
    total_wage = sum(r[0] for r in records)
    total_days = sum(r[1] for r in records)
    if total_days == 0:
        return 0.0
    return total_wage / total_days * 30


def calculate_severance_pay_new(service_years: float, avg_monthly_wage: float) -> float:
    """新制資遣費（勞退條例第 12 條）：每滿 1 年 0.5 個月，上限 6 個月。"""
    if service_years <= 0 or avg_monthly_wage <= 0:
        return 0.0
    months = min(
        service_years * SEVERANCE_NEW_MONTHS_PER_YEAR, SEVERANCE_NEW_MAX_MONTHS
    )
    return avg_monthly_wage * months


def calculate_severance_pay_old(service_years: float, avg_monthly_wage: float) -> float:
    """舊制資遣費（勞基法第 17 條）：每滿 1 年發給 1 個月平均工資，剩餘月數按比例，無上限。"""
    if service_years <= 0 or avg_monthly_wage <= 0:
        return 0.0
    return avg_monthly_wage * service_years
