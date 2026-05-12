# Patch 建議：InsuranceService.calculate(salary=0) 短路回零

## 背景

`services/insurance_service.py` 是你正在改的 WIP（勞健保級距 DB 化收尾）。
本檔同時是 bug sweep round 4 找到的真實 bug 修補位置，但為避免綑綁你的 WIP，
這個修補沒有自動 commit，請你決定何時併入。

## Bug

DB 實測：4 名員工（id=65/66/67/68 黃雍娟/黃毓慧/簡佩儀/歐瑞煌）`base_salary=0`
但 `InsuranceService.calculate(0)` 仍走 `get_bracket(0)` clamp 到最低級距
1500，扣 labor=277 + health=458 = 735。寫入 salary_records 後得到 net=-735。

這 4 名員工 `hire_date=NULL` `is_active=true` `base=0`，屬「半創建狀態」資料，
但 engine 仍為他們 bulk 計算薪資紀錄。

## Patch 位置

`services/insurance_service.py` `InsuranceService.calculate()` 方法，
在現有 `if salary < 0: raise` 與 `if not 0 <= pension_self_rate <= 0.06: raise`
之後、`bracket = self.get_bracket(salary)` 之前，插入：

```python
        # salary == 0 短路：投保薪資為 0 在保險語意上無意義（級距最低下限 1500）。
        # 若仍走 get_bracket(0) 會 clamp 到 1500 級距，產生 277+458=735 元扣款；
        # base=0 員工跑薪資會得到 net=-735（員工倒貼公司），DB 已實測 4 筆殘留。
        #
        # 合法場景：
        # - 育嬰留停／半月在職：base 不會真的 0，prorate 後 ≥ 1500 進入級距
        # - 自願免薪但要投保：應顯式設 labor/health/pension_insured_salary 三個分項投保欄位
        # - 員工 base=0 + 旗標 no_employment_insurance / health_exempt：本來就走 0
        #
        # Refs: bug sweep round 4 (2026-05-12) DB 完整性檢查發現。
        if salary == 0 and not (labor_insured or health_insured or pension_insured):
            return InsuranceCalculation(
                insured_amount=0,
                salary_range="N/A",
                labor_employee=0,
                labor_employer=0,
                labor_government=0,
                health_employee=0,
                health_employer=0,
                pension_employer=0,
                pension_employee=0,
                total_employee=0,
                total_employer=0,
                labor_insured_amount=0,
                health_insured_amount=0,
                pension_insured_amount=0,
            )
```

## 測試建議

新增 `tests/test_insurance_zero_salary.py`：

```python
def test_zero_salary_short_circuit(service):
    """base=0 員工不應產生保費扣款（避免 net_salary < 0）"""
    r = service.calculate(0)
    assert r.labor_employee == 0
    assert r.health_employee == 0
    assert r.total_employee == 0
    assert r.insured_amount == 0

def test_zero_salary_with_split_insured_still_calculates(service):
    """salary=0 但顯式設分項投保時不短路（業務語意：員工自願免薪但仍要投保）"""
    r = service.calculate(0, labor_insured=30000)
    assert r.labor_employee > 0  # 用 labor_insured 算
    assert r.health_employee == 0  # health_insured 未設 → fallback salary=0 → 短路
    # 注意：此情境下 health_employee=0 是 0-fallback 結果，與短路語意微妙混雜
```

## DB 殘留清理（option）

```sql
-- 先看清單
SELECT id, employee_id, salary_year, salary_month, net_salary
FROM salary_records WHERE net_salary < 0;

-- 清掉 4 筆 -735 殘留（確認都是「base=0 員工」後執行）
DELETE FROM salary_records WHERE net_salary < 0;

-- 4 個半創建員工（is_active=true 但 hire_date=NULL 且 base=0）
SELECT id, name FROM employees
WHERE base_salary = 0 AND hire_date IS NULL AND is_active = true;
-- 確認後決定：軟刪除 (is_active=false) 或硬刪除
```
