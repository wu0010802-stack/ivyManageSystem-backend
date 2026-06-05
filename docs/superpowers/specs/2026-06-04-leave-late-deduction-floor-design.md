# 請假/遲到扣款無條件捨去（對齊義華薪資 Excel）設計

- 日期：2026-06-04
- 範圍：後端 `services/salary/`（純計算層）
- 分支：`fix/leave-deduction-floor-2026-06-04-be`（從 local main `ebe4f5a`）

## 起因
對照園所實際發薪 Excel（`義華薪資115.03/115.05sx.xlsx`）逐筆對帳發現：請假扣款**公式正確**（`日薪(base/30) × 時數/8 × 比率`，事假 1.0 / 生理.病假 0.5），但**進位慣例不同**：
- 園所 Excel：小數一律**無條件捨去（floor）**
- 系統原本：`round_half_up`（四捨五入）

→ 小數時數請假（半天、幾小時），系統每筆比園所**多扣 1 元**（對員工不利）。實證 6/6：張庭滋 491.67→系統 492 / Excel 491；王品嬑 245.83→246/245；孔祥盈 163.96→164/163、327.93→328/327；林家亘 696.75→697/696。

業主決策（2026-06-05）：**對齊 Excel，改無條件捨去**。

## 設計
1. `utils/rounding.py` 新增 `round_down(value, ndigits=0)`（`ROUND_DOWN`，朝零截斷），與既有 `round_half_up` 並存。**勞健保/政府單據維持 `round_half_up`**；僅員工端扣款（請假/遲到/早退）改 `round_down`。
2. `services/salary/utils.py` `_sum_leave_deduction`（含 legacy 版，兩者對齊）：每筆請假記錄的扣款 `round_down` 後再累加（病假半薪段+全薪段合併後捨去）。
3. `services/salary/deduction.py` `calculate_attendance_deduction`：`late_deduction` / `early_leave_deduction` 回傳前 `round_down`。

## 不變式 / 邊界
- 整數金額（整天請假、整數遲到）不受影響（捨去無作用）。
- per-record 捨去：同月同假別多筆小數記錄時，與 Excel「整欄一次捨去」可能差 1 元（罕見邊界）；常見情境（每假別一筆/整天）完全相符。
- 健保/勞保金額不在本變更範圍（依投保級距表，Excel 未揭露輸入，未對帳）。

## 風險
- **影響全體員工薪資**（小數時數請假/遲到者少扣 1 元）。員工有利、法律安全，但 **push 到 prod 前需業主最終簽核**。
- 既有測試若斷言 round_half_up 後的小數扣款值，需更新為 floor 值（行為變更的預期測試調整，非遮蔽 bug）。

## 測試
- `tests/test_leave_deduction_floor.py`：round_down helper + 請假半天/整天 + 遲到 floor（對齊 Excel 案例）。
- 全薪資/扣款回歸套件須通過（受影響的既有測試同步更新預期值）。
