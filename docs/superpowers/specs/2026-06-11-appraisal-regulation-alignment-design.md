# 考核模組對齊人事規章第六篇 — 設計文件

日期：2026-06-11
狀態：待業主審閱
前置調查：`.scratch/hr-regs-2026-06-11/reassessment-vs-regulations.md`（workspace）、`.scratch/system-vs-excel-recon-2026-06-11.md`

## 1. 背景

2026-06-11 以六份人事規章（115.01.01 版：第三篇薪資表、第六篇考核辦法、第七篇節慶獎金、附表八、教職員考核表、核薪表(一)）對系統與會計 Excel 實務做三方比對，發現：

1. 規章第六篇第四條、第七條規定考核獎金「合併於年終獎金發給」——**現行系統設計（前一完整學年兩學期併隔年 2/5 年終）符合規章**；會計 3/15 單獨轉帳是偏離規章的實務。
2. 系統 `appraisal_bonus_rates` 的優等各組與廚師甲等與規章不符（2026-06-04 對齊時金標準只有 114上 Excel 的「班導甲等」案例，其餘組合為推估）。
3. 系統計分規則（apxlal01 seed）對齊的是會計 Excel 實務數值（如留校率 tier 含 −1.7），與第六篇明文數值不同。
4. 第六篇明定、系統缺的計分項目共六類。

## 2. 業主決策紀錄（2026-06-11）

| # | 議題 | 決策 |
|---|------|------|
| D1 | 發放時點（規章「併年終」vs 會計 3/15 單獨轉帳） | **照規章字面，系統不改**；3/15 單獨轉帳改制作罷，會計流程需配合系統（前一完整學年於隔年 2/5 隨年終 settlement 發） |
| D2 | 對齊範圍 | **獎金率＋計分規則都改**，接受系統考核分數與會計 Excel 實務不一致（會計需照規章調整作法） |
| D3 | 第六篇 vs 附表八矛盾（留校率 +6/+4、會議扣分制/出席率加分制、才藝門檻/全期授課） | **第六篇為準**；附表八列入規章修訂建議 |
| D4 | 系統缺項（檢測成績、招生人數、曠職、休學細則、主管加分、呈報優異） | **補齊並盡量自動化**（有資料源者自動，無者手填） |
| D5 | 新規則生效時點 | **`effective_from = 2026-02-01`（114下起）**；114上既有規則列不動，保護歷史重現性 |

## 3. 範圍

**改**：`appraisal_bonus_rates` 數值、`appraisal_scoring_rules`（新 effective 版本）、`ScoreItemCode` enum 擴充、`rule_applier` 新 rule type、`status_aggregator` 新自動聚合、考核 UI 手填項清單。
**不改**：發放管道（`services/year_end/appraisal_sync.py`、`api/year_end/appraisal_payout.py`、前端 `AppraisalPayoutView.vue` 全部維持現狀）、引擎 5-step 結構（`services/appraisal/engine.py` 的 base/sum/total/grade/bonus 公式）、等第切點（優≥90/甲80-89/乙/丙/丁<60，已符規章）、補充保費表外處理。
**順手修**：`appraisal_sync.py:399` 一帶的過時 B3 註解（內容描述決策⑥B 之前的「考核進 2 月薪資補充保費基底」，與現行表外設計矛盾）；`mark_salary_stale_from_month` 呼叫是否仍必要在 plan 階段查證後決定去留。

## 4. 獎金率修正

`appraisal_bonus_rates` 現有兩組 effective 列（`2025-08-01`、`2026-08-01`，值相同），對兩組 in-place 更新：

| RoleGroup | Grade | 現值 | 規章值（第六篇第四條＝附表八，兩處一致） |
|-----------|-------|------|------|
| SUPERVISOR | OUTSTANDING | 8000 | **10000** |
| HEAD_TEACHER | OUTSTANDING | 6000 | **8000** |
| STAFF | OUTSTANDING | 6000 | **8000** |
| ASSISTANT | OUTSTANDING | 5500 | **6000** |
| COOK | GOOD | 4000 | **3500** |

不變：SUPERVISOR 甲 5000、HEAD_TEACHER 甲 4000、STAFF 甲 4000、ASSISTANT 甲 3500、COOK 優 6000。

**為何 in-place 而非新 effective 版本**：114上（唯一已 FINALIZED 學期）只出現班導甲等案例（4000，不變），改值不影響任何歷史金額；in-place 避免版本鏈複雜化。已 FINALIZED 的 summary 一律不自動重算（現行行為）。

**角色對應**：規章「班導師、行政會計」→ HEAD_TEACHER＋STAFF；「副班導師、儲備教師」→ ASSISTANT；「主廚、司機」→ COOK。司機現歸 COOK 組（與規章同率，無需新組）。

## 5. 計分規則對齊（新 effective `2026-02-01` 版本列）

既有 `2025-08-01` 規則列**不動**；以下全部以 `effective_from='2026-02-01'` 插入新版本：

| ScoreItemCode | 現行（2025-08-01） | 新規則（第六篇） | rule_type |
|---------------|--------------------|------------------|-----------|
| RETURNING_RATE_0915 / 0315 | tier 0/−1.7/−3（0915）、+6/0/−1.7/−3/−6（0315） | 統一 tier：≥100 **+6**、[95,100) **0**、[90,95) **−2**、[80,90) **−3**、<80 **−4**（第五條(七)；未帶班人員依全校平均留校率，aggregator 既有 facade 擴充） | TIER（改 config） |
| REWARD_PUNISH | 警告−1/小過−3/大過−10，無加分側 | 嘉獎 **+2**/小功 **+3**/大功 **+6**；警告 **−2**/小過 **−3**/大過 **−6**（第五條(十)，功過相抵＝Σ即可） | DISCIPLINARY_TIERED（補加分側 config） |
| SCHOOL_MEETING_ABSENCE | −1/次 | **−0.5×未參加時數，每次活動上限 −2**；豁免假別（婚/喪/產/住院病假/三等親婚禮，證明者）不扣——手填時主任逕不計入 | **新 rule type：PER_HOUR_CAPPED** |
| INSTITUTION_MEETING_0913 / 1115 | −2/次 | 同上（「機構會議及研習如同時間最高扣兩分」） | PER_HOUR_CAPPED |
| SELF_IMPROVEMENT_ACTIVITY | −2/次 | 同上（第五條(十二)各種全體活動同一規則） | PER_HOUR_CAPPED |
| CHILD_ACCIDENT | −3/次 | 主管評議 **−1 ~ −10/件**（第五條(六)），主任手填分值，schema 驗證範圍 | **新 rule type：MANUAL_DELTA**（bounded） |
| AFTER_CLASS_RATE | 全班 80% 門檻 +2 | **各年級門檻**：大100/中90/小80/幼70，達成 +2（第五條(九)） | FLAT_THRESHOLD 擴充 config：`grade_thresholds` map（鍵＝年級，值＝門檻），有 map 時依班級年級選門檻、無則回退 `threshold`（向後相容既有 2025-08-01 列） |
| LATE_EARLY / MISSING_PUNCH | −0.25/次 | 同（第五條(二)3） | 不動（新版本照抄） |
| LEAVE | 超基準每日 −1 | 同（事 3 日、病 6 日基準，第五條(二)1-2；plan 階段確認 aggregator 門檻值） | 不動（新版本照抄） |
| CLASS_HEADCOUNT_BONUS | +2 | 同（師生比幼幼1:8/小1:12/中1:13/大1:14 達成，第五條(八)） | 不動 |
| SPED | +2/位 | 同（補 UI hint「在園需超過 4 個月」，第五條(十一)2） | 不動 |

## 6. 新增計分項目（ScoreItemCode enum 擴充 + 新規則列）

**自動聚合（status_aggregator 擴充；資料源可行性於 plan 階段逐項查證，查證不可行者降級手填並記錄）**：

| 新 code | 規則 | 資料源 |
|---------|------|--------|
| ABSENTEEISM（曠職） | **−4/日**（第五條(二)4） | 考勤：排班日無打卡且無核准假單 |
| STUDENT_WITHDRAWAL（休學） | **−2/人**（入園 5 天含以上、休學當月月費未繳，第五條(五)1） | 學籍 lifecycle 終態轉換 × 該月月費繳費狀態（fees） |
| STUDENT_REINSTATE（復學） | **+1/人**（第五條(五)4） | 學籍 lifecycle 復學轉換 |

**手填（ManualEventEntrySection 新項）**：

| 新 code | 規則 | 說明 |
|---------|------|------|
| TRIAL_LEAVE（試讀離園） | −1/人（第五條(五)2） | 初版手填；招生試讀資料自動化列增強 |
| CLASS_TRANSFER（轉班） | −0.5/人（第五條(五)3；「2週內休學責任歸前者」歸屬規則人工判定） | 初版手填 |
| EXAM_RESULT（檢測成績） | 依當學期檢測公告（第五條(三)），主任填分值 | MANUAL_DELTA（bounded ±10，與幼兒意外同級距；公告若超出再調 config） |
| RECRUIT_SCORE（招生人數） | 依當學期公告（第五條(四)；附表八 +2/人 僅供參考，D3 以第六篇公告制為準），主任填分值 | MANUAL_DELTA |
| SUPERVISOR_SCORE（主管加分） | 依單位主管評核項目（第十三條），主任填分值 | MANUAL_DELTA |
| EXCELLENCE_NOMINATION（呈報優異） | +2，每學期全園 1 位（第五條(十一)1），UI hint 提示唯一性、不做硬 enforce | PER_UNIT +2 |

## 7. 生效時點與回溯保護

- 新規則列與新項目規則一律 `effective_from='2026-02-01'`（114下學期起）。
- 114上 cycle recompute 走 `2025-08-01` 規則列，重現原結果；已 FINALIZED summary 不自動重算。
- 獎金率 in-place 改（§4 理由）。
- 114下（2026-02-01~07-31）為第一個適用規章新規則的學期。

## 8. Migration 計畫

1. `ScoreItemCode` PG enum 新增 9 個值（ABSENTEEISM、STUDENT_WITHDRAWAL、STUDENT_REINSTATE、TRIAL_LEAVE、CLASS_TRANSFER、EXAM_RESULT、RECRUIT_SCORE、SUPERVISOR_SCORE、EXCELLENCE_NOMINATION）——PG enum 值不可移除，downgrade 對 enum no-op（註明）。
2. `appraisal_bonus_rates` 五值 × 兩組 effective in-place UPDATE；downgrade 還原現值。
3. `appraisal_scoring_rules` 插入 `2026-02-01` 全套規則列（既有項目新值＋新項目）；downgrade 刪除該 effective 的列。
4. 冪等寫法與 enum text coercion 慣例沿 `apxlal01`。
5. 含 backfill 的 migration 合併前手動 `alembic upgrade heads` 對 dev DB 驗證（workspace 慣例）。

## 9. API 與前端影響

- 後端：`rule_applier` 新增 PER_HOUR_CAPPED、MANUAL_DELTA 兩個 rule type（純函式，ROUND_HALF_UP 0.01 慣例不變）；`status_aggregator` 新增三個自動項聚合；score_items sync 端點支援新 code（schema 驗證 MANUAL_DELTA 範圍）。
- 前端：考核管理 ManualEventEntrySection 新增手填項與 label/hint；`PERMISSION_NAMES` 無新權限；後端 schema 異動後跑 `dump_openapi.py` + `gen:api`（workspace SOP）。
- 等第、獎金計算公式、簽核流程、portal 揭露行為不變。

## 10. 測試策略

1. **規則純函式**：PER_HOUR_CAPPED（上限觸發/未觸發/0時數）、MANUAL_DELTA（範圍邊界/越界拒絕）、留校率新 tier 五段邊界、REWARD_PUNISH 加分側、AFTER_CLASS_RATE 各年級門檻。
2. **歷史保護回歸**：114上 cycle 以 `2025-08-01` 規則 recompute 重現既有 `test_appraisal_excel_reconcile_114`（15/15 不破）。
3. **新制金標準**：造 114下 樣例（含優等案例驗新獎金率 10000/8000、廚師甲等 3500；含曠職/休學自動項）鎖 TDD。
4. **effective 選版**：2026-01-31 vs 2026-02-01 邊界選到正確規則版本。
5. **migration**：upgrade 後 rates/rules 值斷言；downgrade 還原斷言（enum 除外）。

## 11. 交付物（程式外）

- **規章修訂建議書**（給業主）：①附表八三處與第六篇對齊 ②第六篇第五條(七)舊生率核計日（次學期）與第四條「併年終」的結構性時滯說明——按字面，上學期考核需待 3/15 資料，併入的年終為「隔年 2/5」批次；會計現行 3/15 單獨轉帳作法需停止，改併年終 ③會議活動豁免假別舉證流程。
- 更新 `ivy-backend/CLAUDE.md` 若有涉及考核敘述（plan 階段確認）。

## 12. 範圍外（follow-up backlog）

- 全勤獎金（第三篇：C級副導 500/其餘 1500/月）：系統無、會計實務也未見實發——規章與實務雙漂移，待業主裁定後另案。
- 超額獎金 DB config 對驗（規章第三篇首次揭露權威基準）。
- 節慶獎金未驗細則：新班/新接班第一學期保障 80%、功過 1 分＝節慶 1000 元、停課/繳費<60% 不發、離職前 30 日申請併期滿月薪發。
- 自主成長契約獎金（核薪表 620/600）。
- 核薪表 A/B/C 職等底薪 vs `PositionSalaryConfig` 對驗。

## 13. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 系統考核分數從 114下 起與會計 Excel 實務不一致 | D2 已拍板；規章修訂建議書明列會計需配合處；114下 結算前業主與會計對齊 |
| 自動聚合資料源不如預期（曠職/休學/復學） | plan 階段逐項查證；不可行者降級手填，規則值不變 |
| in-place 改獎金率影響未來 HR 在 UI 手動調過的值 | migration 僅 UPDATE 指定五組 (effective, role_group, grade)，不碰其他列；plan 階段先查 dev DB 是否有 UI 手調痕跡 |

## 14. 計畫階段查證修訂（2026-06-11，撰寫實作計畫時對 code 查證後的權威修訂）

1. **§8.1 修正**：`appraisal_scoring_rules.item_code` 是 `String(40)`、`ScoreItemCode` 是純 Python enum——**沒有 PG enum**。新增計分項目不需要 enum migration，§13「PG enum 不可逆」風險不存在（已自風險表移除）。
2. **§5 修正（會議三項）**：取消新 rule type PER_HOUR_CAPPED。`AppraisalManualEventCount.count` 為單欄 Numeric(8,2)，無法承載逐次活動的「每次上限 −2」；改用既有 **PER_UNIT、`per_unit_delta=-0.5`，count 語意改為「計分時數」**（UI hint：每次活動最多計 4 小時＝封頂 −2，超過者填 4）。規章語意由填報規則＋hint 落實。
3. **§6 修正（自動化查證結果）**：
   - ABSENTEEISM 自動 ✓——`Attendance.status` 有獨立 `absent`（曠職）與 `leave`（全天請假）。**並發現既有 bug**：`status_aggregator` 的 `leave_days` 目前數的是 `status='absent'`（曠職），非 `'leave'`——本次一併修正（LEAVE 數 `'leave'`、新增 `absent_days` 數 `'absent'`），屬行為變更、補回歸測試。
   - STUDENT_REINSTATE 自動 ✓——`StudentChangeLog.event_type='復學'` 可依班級與 cycle 時間窗計數。
   - STUDENT_WITHDRAWAL **降級手填**——規章條件「休學當月月費未繳」需要月粒度繳費狀態，`StudentFeeRecord` 粒度為費用項目，無法可靠判定；主任手填人數，自動化列增強 backlog。
4. **§5 補充（獎懲加分側）**：`DisciplinaryAction.action_type` 現僅 warning/minor/major。於 `models/disciplinary.py` 的 `ACTION_TYPES`/`ACTION_TYPE_LABELS` 新增 `commendation`（嘉獎）/`minor_merit`（小功）/`major_merit`（大功）三值（String 欄位免 migration），aggregator 計數、`apply_disciplinary_tiered` config 增加分側鍵（預設 0 向後相容）。Merit 列 `deduction_amount=0`，不觸發薪資扣款路徑。
5. **§5 補充（留校率未帶班）**：RETURNING_RATE_0915/0315 新版本 `applies_to_role_groups=None`（全角色）；aggregator 對無班級 participant 以全校加權平均留校率代入（第五條(七)2）。
6. **MANUAL_DELTA 範圍定案**：CHILD_ACCIDENT [−10, 0]、EXAM_RESULT [−10, +10]、RECRUIT_SCORE [0, +20]、SUPERVISOR_SCORE [0, +10]；EXCELLENCE_NOMINATION 用 PER_UNIT `+2、unit_cap=1`（規章每學期 1 位）。
