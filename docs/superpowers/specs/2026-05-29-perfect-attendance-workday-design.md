# 全勤章判定修正：對齊官方工作日

**日期**：2026-05-29
**範圍**：純後端，`services/milestone_detector.py`（純函式）+ caller `api/portfolio/auto_milestone.py` + 測試
**對應 audit finding**：P1 #11「Milestone detector 3 天可拿全勤章」

## 問題

`detect_perfect_attendance_months` 原規則：一個月有 **≥3 筆記錄且全「出席」** → 發全勤章。
分母是「有記錄的天數」而非「該月應到天數」。兩個失效模式：

1. **稀疏月誤發**：該月只記了 3 天（全出席）就拿章，哪怕實際有 20 個上學日。
2. **隱形缺席**：缺席若沒被標成「缺席」而是整個沒建 attendance row，缺席就消失 →
   剩下的全是「出席」→ 誤發。

考勤資料語意（已查證）：`StudentAttendance` 每生每日最多一 row（unique
student_id+date），status ∈ {出席, 缺席, 病假, 事假, 遲到}；老師逐日點名 upsert，
**未點名學生不建 row**（失效模式 2 成立）。

## 解法（業主決策：嚴格 A）

**全勤月 = 已結束的月份中，該月每個官方工作日學生都有 status=="出席" 記錄。**
缺任一天記錄、或有任何非「出席」狀態（**遲到也破功**，業主決策）→ 不發章。
一條規則同時殺掉兩個失效模式。

設計要點：
- **官方工作日**用既有 `services/workday_rules`（`load_day_rule_maps` + `classify_day`，
  排除週末/假日、含補班日）。業主確認學校少在政府工作日自行停課，故分母用政府日曆。
- **不做** enrollment 窗裁剪：嚴格規則自然處理邊界（月中入學的早段工作日無記錄 →
  該月不發，正符合「**滿月**全勤」語意）。
- **只發已結束月份**（月底 < reference_date），未結束當月不發。
- `milestone_detector` 維持純函式：新增參數 `official_workdays: Iterable[date]`，
  由 caller（`auto_milestone.py`）以 workday_rules 算 `[min記錄日, ref_date]` 區間後傳入。
  DB 依賴留在 caller 的 `_official_workdays_in_range` helper。
- 移除已無用的 `PERFECT_ATTENDANCE_MIN_DAYS` 常數。

## 明確不做（另案 follow-up）

- **不掛 scheduler**：auto-detect 維持手動觸發。
- **不加** `override_reason` 欄位 / audit。
- **不自動撤銷**舊規則已誤發的章（破壞性）；列為 User 可選清理（idempotent insert
  不會回收既存 milestone）。

## 測試

- 純函式（`tests/test_milestone_detector.py`，8 條）：全工作日出席→發章 / 缺一天→不發 /
  遲到破功 / 缺席破功 / **稀疏月不再誤發**（原 bug）/ 未結束當月不發 / 週末假日忽略 /
  空工作日集合不發。
- 端到端（`tests/test_auto_milestone_api.py`，2 條）：seed 整月平日出席→建全勤 milestone /
  缺一個工作日→不建。驗證 caller 的官方工作日計算 wiring。

## 流程

worktree off `origin/main`（`fix/perfect-attendance-workday-2026-05-29-backend`）→
TDD → 後端一個 PR。
