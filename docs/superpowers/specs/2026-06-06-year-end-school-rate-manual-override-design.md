# 年終全校達成率：HR 手動覆寫（Phase 1）+ 自算預繳率（Phase 2 outline）

- 日期：2026-06-06
- 範圍：後端（year_end 引擎/API + migration）+ 前端（年終人工步驟一欄）
- 狀態：Phase 1 設計定案待實作；Phase 2 列為 follow-on（待業主定義口徑）

---

## 1. 背景與問題

2026-06-05/06 用義華實際報表（`114年年終經營績效1150213.xls`）對帳系統年終引擎時發現：

- 年終 6-step **公式正確**（小計 = (基本+節慶)×平均績效%×達成% … 已逐筆驗證）。
- 但年終的「**全校達成率**」是系統**自算**的量（`services/year_end/enrollment_rates.py::school_achievement_rate`）：
  - 系統 = `_q2(count_enrolled_on(cycle.bonus_calc_date) / enrollment_target × 100)`，數的是**嚴格在籍**（enrollment/graduation/withdrawal 純日期），且**兩學期共用單一基準日**（`refresh_enrollment_rates` 內有 `TODO(phase2) per-semester basis date 未存`）。
  - 園所 Excel 的「達成率」是 **預繳率/註冊率**（上學期 115.01.15「新生+舊生預繳」161/176、下學期 114.09.15「新生+舊生註冊」121/160）——**不同 metric、不同日期、兩學期不同基準**。
- 數字差：161/176 = **91.48**（系統 `_q2`），園所用 **91.5x**；傳到 `resolve_org_achievement_rate`（`_q1((75.6+91.48)/2)=83.5` vs 園所 83.6）→ **呂麗珍年終 38006.53 vs Excel 38052.04，差 $45.51/人**（全園 ~21 人量級近千元）。

根因：系統自算的「嚴格在籍率」≠ 園所手算的「預繳/註冊率」，是 **metric 定義差**，非公式 bug。

### 現況補充（程式碼事實）
- `OrgYearSettings`（每 cycle 兩列，semester_first True/False）：`enrollment_target`、`enrollment_actual`、`school_achievement_rate`（自算）、`org_achievement_rate`。
- `refresh_enrollment_rates` 由在籍資料回填 `school_achievement_rate` 與 `class_performance_rate`；**`returning_student_rate`（班舊生率）明載「Phase1 人工維護」不回填**——已有「自算欄 + 人工欄並存」的先例。
- 年終人工端點 `upsert_class_target`（`api/year_end/__init__.py`）只存 `head_count_target / returning_student_rate / 老師指派`，**OrgYearSettings 目前無任何人工輸入路徑**。
- 既有「匯入舊 Excel」路徑 `POST /year_end/cycles/import_excel` 把 `org_achievement_rate_first/second` 當參數（預設 83.6/91.5）→ 該路徑已能對上 Excel；本設計處理的是**原生 build 路徑**。

---

## 2. 目標 / 非目標

**Phase 1 目標（本 spec 實作範圍）**
- 讓 HR 能在年終人工步驟**手動覆寫每學期的全校達成率**，填入園所實算的預繳/註冊率（如 75.6 / 91.5）。
- 系統仍自算一個**建議值**（不被洗掉），HR 填了就以 HR 為準；HR 清空則回退自算。
- 結果：原生 build 路徑可 **100% 對上園所 Excel 年終數字**。

**非目標（Phase 1 不做）**
- 不改自算邏輯本身（仍是嚴格在籍率，僅當「建議值」）。
- 不改班級 `class_performance_rate` / `returning_student_rate` 既有行為。
- 不做 Phase 2 的「系統自算預繳率」（見 §6）。

---

## 3. Phase 1 設計

### 3.1 資料模型（Alembic migration）
- `OrgYearSettings` 新增欄位：
  - `school_achievement_rate_override: Numeric(6,3), nullable=True`，comment「HR 手動覆寫全校達成率；NULL=用自算 school_achievement_rate」。
- 保留 `school_achievement_rate`（自算）原樣，作為「系統建議值」，**不被 override 覆蓋**（前端可同時顯示兩值，留 provenance）。
- migration：單一 head、可逆（upgrade `add_column` nullable / downgrade `drop_column`）。鏈接於當前 head（實作時 `alembic heads` 確認）。

### 3.2 `refresh_enrollment_rates`：不動
- 仍只寫自算 `school_achievement_rate`，**絕不碰 `school_achievement_rate_override`**（與它現在不碰 `returning_student_rate` 完全同模式）。

### 3.3 解析（resolver）
- `OrgYearSettings` 新增 property：
  ```python
  @property
  def effective_school_achievement_rate(self) -> Decimal:
      return (self.school_achievement_rate_override
              if self.school_achievement_rate_override is not None
              else self.school_achievement_rate)
  ```
- 改讀 effective 的 consumer（全部 3 處，實作時以 grep `school_achievement_rate` 核對）：
  - `settlement_builder.gather_performance_rates`（讀 `org_first/second.school_achievement_rate` 組 `school_rate_first/second`）。
  - `settlement_builder` 內 `_school_rates` 預查（`select(OrgYearSettings.school_achievement_rate)`）→ 改成取整列或加 override 欄一起算 effective。
  - GET 端點輸出（讓前端拿到 effective）。
- override 自然往下傳到 `resolve_org_achievement_rate` → step3 → 每人小計。

### 3.4 API
- 新增 `POST /year_end/cycles/{cycle_id}/org-settings`（upsert by `cycle_id + semester_first`），權限 `Permission.YEAR_END_WRITE`，結構仿 `upsert_class_target`。
  - payload（Pydantic `OrgYearSettingsOverrideUpsert`）：`semester_first: bool`、`school_achievement_rate_override: Decimal | None`（傳 None 清除回自算）、選配 `enrollment_target: int | None`。
  - 回傳 `OrgYearSettingsOut`：含 `school_achievement_rate`（自算）、`school_achievement_rate_override`、`effective_school_achievement_rate` 三值。
- GET 既有 cycle/org-settings 查詢端點同步加上述三欄輸出（無則新增 GET）。
- **reset/clone**（`api/year_end/__init__.py:142` 把 `school_achievement_rate` 設 0 重置）：override 一併重置回 `None`。

### 3.5 前端（cross-front-back）
- 年終人工步驟（grid 設定區）新增「全校達成率（上學期 / 下學期）」兩個輸入欄，旁顯示系統建議值（自算）作 placeholder/hint，仿舊生率欄擺位。
- `src/api/yearEnd.ts`（或對應 module）加 upsert/GET 呼叫；後端改完跑 `dump_openapi.py` + `npm run gen:api` 重生型別。
- 空欄 = 不覆寫（送 null）；填值 = 覆寫。

### 3.6 測試（TDD，先寫紅）
- pytest：
  1. resolver：override 非空 → effective=override；override=None → effective=自算。
  2. 端點 upsert：新增、更新、清除（送 null 回退自算）、權限守衛。
  3. `gather_performance_rates` 取 effective（override 設值後 school_rate_first/second 反映 override）。
  4. **端到端**：HR 設下學期 75.6 / 上學期 91.5 → `build_settlements` → 呂麗珍 應領 = **38052.04**（＝園所 Excel）。
  5. `refresh_enrollment_rates` 跑過後 override 不被洗掉。
- migration：upgrade/downgrade round-trip（沿用既有 migration 測試慣例）。
- 前端：若輸入欄有邏輯則補 vitest。

### 3.7 稽核 / provenance
- override 欄本身即留痕（可查 HR 填了什麼 vs 系統建議）。年終已有兩關簽核 gate settlement。
- 可選（不阻擋本 phase）：把 override 變更納入既有 audit log。

---

## 4. 資料流（Phase 1）

```
在籍資料 ──refresh_enrollment_rates──▶ OrgYearSettings.school_achievement_rate（自算/建議值）
HR 輸入 ──POST org-settings──▶ OrgYearSettings.school_achievement_rate_override（NULL=不覆寫）
                                              │
                  effective = override ?? 自算 ┘
                                              ▼
gather_performance_rates → school_rate_first/second → resolve_org_achievement_rate(_q1 平均)
                                              ▼
                         compute_subtotal_amount(gross, org_rate) → 每人年終
```

---

## 5. 風險與緩解
- **碰金額**：以 TDD 端到端測試釘住「HR 填 → 呂麗珍 38052.04」；兩關簽核仍 gate。
- **自算被誤當權威**：前端同時顯示「系統建議 vs HR 實填」，避免混淆。
- **reset 漏清 override**：明確在 reset 路徑清回 None + 測試覆蓋。
- **migration**：nullable add column，零回填、可逆，低風險。

---

## 6. Phase 2（B）outline — 系統自算「預繳/註冊率」（本 spec 不實作）

改自算邏輯，把「建議值」從嚴格在籍率換成園所口徑的預繳/註冊率。**待業主定義口徑後另開 spec。**

**現況可用資料**
- 新生預繳：`RecruitmentVisit.has_deposit` / `target_school_year`；`RecruitmentPeriod.effective_deposit_count`（有效預繳=預繳−轉期）/ `not_enrolled_deposit`（未就讀退預繳）。
- 舊生續讀/下學期預繳：**目前無現成資料模型**（recruitment 只管新生）。

**待業主釐清（開 Phase 2 spec 前必答）**
1. 「達成率」分子精確口徑：用 `deposit_count` 還是 `effective_deposit_count`（扣轉期）？要不要扣 `not_enrolled_deposit`？
2. **舊生**續讀/預繳怎麼來：新建「舊生續讀登記」資料，還是沿用 fees `prepayment` 折抵推斷？由誰、何時登記？
3. 兩學期各自基準日（上 115.01.15 / 下 114.09.15）要存進 `OrgYearSettings`（解掉現有 `TODO(phase2)` 單一基準日）。
4. 分母 target 與 §3 的 `enrollment_target` 是否同一個。

**與 Phase 1 的關係**：Phase 1 的 override 永遠是最終權威；Phase 2 只改「建議值」的算法，HR 仍可覆寫。故 Phase 1 先行、獨立可上線。

---

## 7. 交付（Phase 1）
- 後端：1 migration + `OrgYearSettings` property/欄位 + resolver 改 3 處 + 1~2 端點 + pytest。
- 前端：年終人工步驟一欄 + api wrapper + gen:api。
- 後端一筆 commit、前端一筆 commit（分 repo，描述同一功能）。
