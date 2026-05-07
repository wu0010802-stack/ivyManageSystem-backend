# Gov Data API Fixtures

凍結自 2026-05-07 真實政府開放資料 API。後續 parser/composer 測試以此為 oracle。
若 parser 預期 schema 與 fixture 不符，**先更新 parser 不要改 fixture**（fixture 是政府事實）。

## 6 個資料源

| source | 實際 URL | HTTP | 抓取時間 (Asia/Taipei) | 樣本欄位 |
|---|---|---|---|---|
| `mol_labor_brackets_2026.json` | data.gov.tw dataset 6258 JSON resource | 200 | 2026-05-07 14:52 | 月投保薪資 / 投保薪資等級 / 月薪資總額 / 適用起日 |
| `mol_labor_premium_2026.json` | data.gov.tw dataset 6259 JSON resource | 200 | 2026-05-07 14:52 | 投保薪資 / 勞保普通費率 / 就保費率 / 勞工應負擔保費金額 / 單位應負擔保費金額 |
| `mol_pension_2026.json` | 勞動部 OdService 勞工退休金月提繳工資分級表 | 200 | 2026-05-07 14:54 | 等級 / 實際工資範圍 / 月提繳工資金額 / 生效日（民國） |
| `nhi_brackets_2026.json` | data.gov.tw dataset 20251 (健保署) | 200 | 2026-05-07 14:54 | 組別級距 / 投保等級 / 月投保金額（元）/ 實際薪資月額（元） |
| `nhi_premium_2026.json` | 健保署資料開放平台保險費負擔金額表 | 200 | 2026-05-07 14:56 | 投保金額等級 / 月投保金額 / 本人負擔（30%）/ 本人+N 眷口負擔 / 投保單位負擔（60%）/ 政府補助（10%） |
| `mol_minimum_wage.json` | data.gov.tw dataset 6281（基本工資制定與調整經過） | 200 | 2026-05-07 14:52 | 年度 / 指示發佈日（民國）/ 內容調整金額（合併月薪+時薪文字）/ 實施日期（民國） |

## 注意事項與已知差異

### 1. mol_minimum_wage 落後現實
fixture 最新一筆是 **年度 2023 / 實施日 20240101 / 月薪 27,470 / 時薪 183**。
2024 年公告（28590/190 → 2025/1/1 實施）與 2025 年公告（29500/196 → 2026/1/1 實施）**未在政府開放資料平台中**。

→ T5 parser 測試以 fixture 內最新一筆 (`date(2024,1,1), 27470, 183`) 作斷言。
→ T8 minimum_wage_history bootstrap 仍寫 2025/1/1=28590 與 2026/1/1=29500（已知正確值，留 reason 為「初始 bootstrap」）；future sync 抓不到新值時 `_compose_and_stage_minimum_wage` 會自動 skip（已存在的 effective_date 不重複建 staging）。

### 2. mol_minimum_wage 欄位是民國年 + 文字混合
- `實施日期（民國）`：`"20240101"` = 民國 113 年 1 月 1 日 → 西元 2024-01-01。前 3 位數 / 後 4 位數需拆解 + 民國轉西元。
- `內容/調整金額（新台幣）`：例 `"月薪25,250、時薪168"`。parser 需 regex 抽出 monthly / hourly。

### 3. mol_labor_premium 起點為「投保薪資 11100」非基本工資 29500
勞保最低投保薪資並非基本工資 29500，而是適用「無一定雇主或自願加保」者的最低級 11100。fixture 第 1 筆即 `投保薪資=11100, 勞工應負擔=277, 單位應負擔=972`。
這對應既有 `INSURANCE_TABLE_2026` 中 `amount=1500..28800` 範圍 row 的 `labor_employee=277, labor_employer=972`（亦即低於勞保最低時，使用勞保最低級的金額）。

### 4. nhi_premium 2026 第一級 amount=29500
2026 年健保最低投保金額已對齊基本工資 29500（不是過去的 27470）。
fixture 第 1 筆 `月投保金額=29500, 本人負擔=458, 投保單位負擔=1428`。
→ INSURANCE_TABLE_2026 中所有 amount ≤ 29500 的 row 的 `health_employee=458, health_employer=1428`，
  對應「未達健保最低級時，使用健保最低級的金額」。

### 5. mol_pension 含 62 級到 amount=150000
等級 1 = 1500、等級 62 = 150000（勞退最高月提繳工資）。

## 重新抓取程序
若政府 schema 異動或要更新到新年度：

1. 重新打 6 個 URL，用 `curl` 寫到對應 fixture 檔
2. `jq` 抽 top-level keys 確認欄位無刪減
3. 跑 `pytest tests/test_gov_data_parser.py tests/test_gov_data_composer.py` 確認測試仍綠（若失敗，更新 parser 對應的欄位 get）
4. 更新此 README 的「抓取時間」欄
5. commit
