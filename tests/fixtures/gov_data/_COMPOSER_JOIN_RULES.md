# Composer Join Rules（反推自 INSURANCE_TABLE_2026）

T6 composer 必須遵循這份規則合成 IvyKids 級距。
反推依據：以 fixture 數值與 `services/insurance_service.py:56` `INSURANCE_TABLE_2026` 對照。

## Amount 列表（IvyKids 82 列的 amount column）

amount 列表 = labor_brackets ∪ pension ∪ nhi_brackets 的聯集，去重排序：

- mol_labor_brackets：勞保投保薪資（含職業工會等最低 11100、最高 45800）
- mol_pension：勞退月提繳工資分級（最低 1500、最高 150000）
- nhi_brackets：健保投保金額（最低 29500、最高 219500）

實測：兩者聯集會產生 INSURANCE_TABLE_2026 的 82 列 amount。

## 各欄位合成公式

### `labor_employee` / `labor_employer`
直接查 `mol_labor_premium.by_amount[X]`，其中 X 為：
- 若 amount < 勞保最低投保薪資（11100） → X = 11100（floor）
- 若 amount > 勞保最高投保薪資（45800） → X = 45800（cap）
- 否則 X = amount

註：勞保表 amount 區間限定 11100..45800，IvyKids 級距 amount 可能 < 11100（如 1500）或 > 45800（如 150000、313000），都使用 floor/cap 規則。

### `pension`
直接 `round(amount × 0.06)`。
驗證：amount=1500 → 90 ✅、amount=29500 → 1770 ✅、amount=150000 → 9000 ✅。
若 amount > 勞退最高 150000，仍按 amount × 6% 計（INSURANCE_TABLE_2026 中 amount=313000 對應 pension=9000，意味勞退仍封頂於 150000 × 6% = 9000）。實作上：
```
pension = round(min(amount, 150000) × 0.06)
```

### `health_employee`
直接查 `nhi_premium.by_amount[X]["本人負擔金額（負擔比率30%）"]`，其中 X 為：
- 若 amount < 健保最低投保金額（2026 = 29500） → X = 29500（floor）
- 若 amount > 健保最高投保金額（219500） → X = 219500（cap）
- 否則：取 nhi_brackets 中 ≥ amount 的最小級距值

註：健保的 amount 級距與勞保不一定完全對齊，必須先到 nhi_brackets 找「對應的健保級距」再去 nhi_premium 查金額。

驗證：
- amount=1500 → X=29500（floor）→ health_employee=458 ✅
- amount=29500 → X=29500 → 458 ✅
- amount=150000 → X 須查 nhi_brackets 中 150000 對應級距（很可能 = 150000）→ 對應 nhi_premium row 的本人負擔。INSURANCE_TABLE_2026 row amount=150000 → health_employee=2327。

### `health_employer`
**重要修正**（與最初 spec/plan 假設不同）：直接取政府表「投保單位負擔金額（負擔比率60%）」，**不需要** 乘以 `(1 + average_dependents)`。

公式：`nhi_premium.by_amount[X]["投保單位負擔金額（負擔比率60%）"]`，X 同 health_employee。

驗證：
- amount=1500 → X=29500 → health_employer=1428 ✅（INSURANCE_TABLE_2026 對齊）
- amount=29500 → X=29500 → 1428 ✅
- amount=150000 → INSURANCE_TABLE_2026 顯示 7259。fixture 中 nhi_premium amount=150000 對應的「投保單位負擔金額」需驗證 = 7259。

→ spec §11 第 5 條「健保眷屬數加權」需修：average_dependents 不參與 health_employer 計算。眷屬欄位（本人+1/+2/+3）僅供員工自身選擇用，IvyKids 級距表不採。

## 邊界情況

| amount 範圍 | 勞保 X | 健保 X | 勞退 amount |
|---|---|---|---|
| < 11100 | 11100 | 29500 (2026) | 自身（≥1500） |
| 11100..29499 | 自身 | 29500 (floor) | 自身 |
| 29500..45800 | 自身 | 自身或對應健保級 | 自身 |
| 45801..150000 | 45800 (cap) | 自身或對應 | 自身 |
| 150001..219500 | 45800 (cap) | 自身 | 150000 (cap) |
| > 219500 | 45800 (cap) | 219500 (cap) | 150000 (cap) |

## 給 T6 composer 的提示

T6 composer 寫完 oracle test 應為：

```python
def test_compose_2026_matches_insurance_table_oracle():
    # 載 5 fixture, parser 解析, composer 合成
    composed = composer.compose_brackets(...)
    assert sorted(composed_dicts, key=...) == sorted(INSURANCE_TABLE_2026, key=...)
```

若失敗，極大機率是這份 markdown 的某條規則不正確。**修 composer**，並更新此 markdown 紀錄修正。
oracle 是既有 hardcode 表（業主驗證過），不可修。
