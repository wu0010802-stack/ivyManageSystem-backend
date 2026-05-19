# MOE Phase 2：每月幼生在園/出席統計（月報匯出器）

| 欄位 | 內容 |
|------|------|
| 日期 | 2026-05-19 |
| 範圍 | 跨前後端（ivy-backend + ivy-frontend） |
| 狀態 | design — 待 user review |
| 父 Spec | `2026-05-11-moe-reporting-module-design.md` §5 |
| 對應原始需求 | #9 全國幼兒園幼生管理系統「每月實際在園人數」 |
| 工期估計 | 1 週 |

---

## 1. 背景

業主每月需登入 ece.moe.edu.tw 填報「本園實際在園人數」與「出席率」，作為**教育券撥款依據**。目前手工逐班逐月填，跨月加退園、轉班、缺席統計易錯。

Phase 1（schema 共用基礎）與 Phase 4（IEP / 特教加給 / 在學證明）已落地。本 phase 補做 Phase 2，產出**3-sheet Excel**讓業主對照貼到政府網站。

不在範圍：政府 API 對接（不開放）、自動排程（業主明確選擇手動觸發）。

---

## 2. 資料模型現況

### 2.1 已建（Phase 1 migration `v8a9b0c1d2e3`）

`monthly_enrollment_snapshots`（`models/gov_moe.py:145`）欄位完全可用：

```
id, year, month, classroom_id (FK SET NULL, nullable),
age_group String(10),
total_count, male_count, female_count,
disadvantaged_count, disability_count, indigenous_count, foreign_count,
expected_attendance_days, actual_attendance_days,
attendance_rate Integer (百分比×100 整數，如 95.43% 存 9543),
snapshot_date Date, generated_at DateTime, generated_by String(100)

UNIQUE (year, month, classroom_id, age_group)
```

**本 Phase 不需新增 migration**。

### 2.2 算法用到的既有表

| 表 | 用途 | 關鍵欄位 |
|----|------|--------|
| `students` | 在園學生 + 統計屬性 | `birthday`、`gender`、`enrollment_date`、`withdrawal_date`、`graduation_date`、`classroom_id`、`lifecycle_status`、`is_disadvantaged`、`disability_type`、`indigenous_status`、`nationality` |
| `student_attendances` | 每日出席狀態 | `student_id`、`date`、`status`（'出席'/'缺席'/'病假'/'事假'/'遲到'） |
| `holidays` | 國定假日 | `date`、`is_active` |
| `workday_overrides` | 補班日 | `date`、`is_active` |
| `student_classroom_transfers` | 轉班歷史（決定月底班級歸屬） | `student_id`、`to_classroom_id`、`transferred_at` |
| `classrooms` | 班級顯示名 | `name` |

---

## 3. 演算法規格

### 3.1 名詞定義（以下「月份」= `(year, month)` pair）

- **月底基準日** `snapshot_date` = 該月最後一個日曆日（如 5 月 → 2026-05-31）
- **應到日數**（per student）= 該學生個別在園日數 ∩ 月份工作日
- **工作日**（per 月）= weekday(Mon–Fri) − 該月 active Holiday + 該月 active WorkdayOverride
- **實到日數**（per student）= 該學生本月 `student_attendances` 中 `status IN ('出席', '遲到')` 的天數
- **班級歸屬**（per student, per 月）= 該學生月底前最後一筆 `student_classroom_transfers.to_classroom_id`；若無 transfer 紀錄則 fallback `student.classroom_id`

### 3.2 學生在園日期區間

對每位學生計算其本月「在園日區間」`[student_start, student_end]`：

```python
student_start = max(月初日, student.enrollment_date or 月初日)
student_end   = min(月底日,
                    student.withdrawal_date or 月底日,
                    student.graduation_date or 月底日)
```

若 `student_start > student_end`：本月零在園日，**不納入分母**也不計入 total_count。

### 3.3 「本月在園學生」篩選 SQL

```python
session.query(Student).filter(
    Student.lifecycle_status.in_([
        "enrolled", "active", "on_leave", "transferred",
        "withdrawn", "graduated"
    ]),
    # 排除 prospect（未報名）
    or_(Student.enrollment_date.is_(None),
        Student.enrollment_date <= 月底日),
    or_(Student.withdrawal_date.is_(None),
        Student.withdrawal_date >= 月初日),
    or_(Student.graduation_date.is_(None),
        Student.graduation_date >= 月初日),
).all()
```

額外於 Python 層算 `student_start, student_end` 再過濾出真正本月有在園日的。

### 3.4 age_group 切分

```python
def calc_age_group(birthday: date, ref_date: date) -> str:
    if birthday is None:
        return "未知"
    age = relativedelta(ref_date, birthday).years
    if age <= 2: return "2-3"  # < 2 歲（罕見）也歸 2-3 防呆
    if age == 3: return "3-4"
    if age == 4: return "4-5"
    return "5-6"  # 5 歲以上含 5-6 與超齡（fallback 防呆）
```

`ref_date = snapshot_date`（月底）。顯示時轉「2-3 歲」「3-4 歲」「4-5 歲」「5-6 歲」「未知」。

### 3.5 工作日計算

```python
def working_days_in_month(session, year: int, month: int) -> set[date]:
    first = date(year, month, 1)
    last  = first + relativedelta(months=1, days=-1)
    days  = {first + timedelta(d) for d in range((last - first).days + 1)
             if (first + timedelta(d)).weekday() < 5}
    holidays = set(session.query(Holiday.date).filter(
        Holiday.is_active == True,
        Holiday.date.between(first, last)
    ).all())
    overrides = set(session.query(WorkdayOverride.date).filter(
        WorkdayOverride.is_active == True,
        WorkdayOverride.date.between(first, last)
    ).all())
    return (days - holidays) | overrides
```

### 3.6 班級歸屬（月底快照）

```python
def classroom_at_month_end(session, student_id: int, snapshot_date: date) -> int | None:
    last_transfer = (session.query(StudentClassroomTransfer)
        .filter(StudentClassroomTransfer.student_id == student_id,
                StudentClassroomTransfer.transferred_at <= datetime.combine(snapshot_date, time.max))
        .order_by(StudentClassroomTransfer.transferred_at.desc())
        .first())
    if last_transfer:
        return last_transfer.to_classroom_id
    return session.query(Student.classroom_id).filter(Student.id == student_id).scalar()
```

### 3.7 「foreign」判定

```python
TAIWAN_ALIASES = {"本國", "台灣", "中華民國", "中華民國（台灣）", "ROC"}

def is_foreign(nationality: str | None) -> bool:
    if not nationality:
        return False  # NULL/空 視為本國，保守不誤報
    return nationality.strip() not in TAIWAN_ALIASES
```

### 3.8 attendance_rate 計算

```python
group_expected = sum(student_expected_days for s in group)
group_actual   = sum(student_actual_days for s in group)
rate_pct       = round(group_actual / group_expected * 10000) if group_expected else 0
# 存 9543 = 95.43%
```

整體出席率採**人日加權**（不是先算每人比率再平均），與政府網站定義一致。

### 3.9 重新產生（覆寫）

同一 `(year, month)` 允許重複產生。流程：
1. 取 PG advisory lock：`SELECT pg_try_advisory_xact_lock(hashtext('moe_monthly_gen_{year}_{month}'))`，未取到 → `409 Conflict: 另一個產生請求進行中`
2. 讀既有 `monthly_enrollment_snapshots` rows where `(year, month)` 為快照 → 留 audit log
3. `DELETE` 既有 rows
4. 重新計算並 `INSERT`
5. Audit log 寫 `event = "gov_moe.monthly.regenerate"`，`details` 含 `{previous_rows: [...], new_rows: [...]}`

---

## 4. API 設計

全部掛在既有 `api/gov_moe/__init__.py` 的 `prefix="/gov-moe"`，新增子 router `monthly.py`。

### 4.1 POST `/gov-moe/monthly/generate`

**Body**：
```json
{ "year": 2026, "month": 5 }
```

**Response 200**：
```json
{
  "year": 2026, "month": 5,
  "rows_generated": 12,
  "snapshot_date": "2026-05-31",
  "generated_at": "2026-06-01T10:23:45+08:00",
  "generated_by": "wu0010802@gmail.com"
}
```

**Error**：
- `400` `year/month 超出合理範圍`（year < 2020 or > 當年+1 or month 不在 1–12）
- `409` `另一個產生請求進行中`（advisory lock 取不到）
- `403` 缺 `GOV_REPORTS_EXPORT`（generate 屬寫入動作）

**權限**：`require_staff_permission(Permission.GOV_REPORTS_EXPORT)`

### 4.2 GET `/gov-moe/monthly`

**Query**：`?year=2026&month=5`

**Response 200**（三維度合一）：
```json
{
  "year": 2026, "month": 5,
  "snapshot_date": "2026-05-31",
  "generated_at": "2026-06-01T10:23:45+08:00",
  "generated_by": "wu0010802@gmail.com",
  "classroom_summary": [
    {
      "classroom_id": 1, "classroom_name": "蘋果班",
      "teacher_names": "張老師、李老師",
      "expected_days": 480, "actual_days": 458, "attendance_rate_pct": 95.42,
      "total_count": 24, "male_count": 12, "female_count": 12,
      "disadvantaged_count": 2, "disability_count": 1,
      "indigenous_count": 0, "foreign_count": 0
    },
    ...
  ],
  "student_detail": [
    {
      "student_id": 1, "student_no": "S0001", "name": "王小明",
      "id_number": "A123456789",
      "classroom_name": "蘋果班", "age_group": "4-5",
      "expected_days": 22, "actual_days": 20,
      "attendance_rate_pct": 90.91,
      "is_disadvantaged": false
    },
    ...
  ],
  "overview": {
    "total_students": 28,
    "by_age_group": { "2-3": 0, "3-4": 8, "4-5": 12, "5-6": 8 },
    "disadvantaged_pct": 7.14,
    "disability_pct": 3.57,
    "indigenous_pct": 0,
    "foreign_pct": 0
  }
}
```

**Error**：`404 尚未產生` 若 `(year, month)` 在 snapshot 表零筆。

**權限**：`require_staff_permission(Permission.GOV_REPORTS_VIEW)`

### 4.3 GET `/gov-moe/monthly/export`

**Query**：`?year=2026&month=5&format=xlsx`

**Response**：
- `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- `Content-Disposition: attachment; filename="義華幼兒園_月報_2026-05_產生於2026-06-01.xlsx"`

若 `(year, month)` 尚未產生 → `404`（前端對應顯示「請先產生」按鈕引導）。

**權限**：`require_staff_permission(Permission.GOV_REPORTS_EXPORT)`

---

## 5. Excel writer 設計

新建 `services/gov_moe/monthly_excel_writer.py`，使用既有 `utils/excel_io.py` 慣用的 `openpyxl`。

### 5.1 共用樣式 helper

```python
def apply_header_style(cell):
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor="4472C4")
    cell.alignment = Alignment(horizontal="center", vertical="center")
```

凍結 row 1、欄寬自動調整（min 10, max 30）、空值寫 `"-"`。

### 5.2 Sheet 1 — 班級總表

| 班級 | 教師 | 應到人日 | 實到人日 | 出席率 | 男 | 女 | 弱勢 | 身障 | 原民 | 外籍 |
|------|------|---------|---------|--------|----|----|------|------|------|------|
| 蘋果班 | 張老師、李老師 | 480 | 458 | 95.42% | 12 | 12 | 2 | 1 | 0 | 0 |
| ... | | | | | | | | | | |
| **合計** | — | 1300 | 1238 | 95.23% | 14 | 14 | 2 | 1 | 0 | 0 |

最末加「**合計**」列粗體底色淡藍。

### 5.3 Sheet 2 — 幼生明細

| 學號 | 姓名 | 身分證 | 班級 | 年齡層 | 應到日數 | 實到日數 | 出席率 | 弱勢標記 |
|------|------|--------|------|--------|---------|---------|--------|---------|
| S0001 | 王小明 | A123456789 | 蘋果班 | 4-5 歲 | 22 | 20 | 90.91% | 否 |
| ... | | | | | | | | |

`弱勢標記`：取 `is_disadvantaged`（boolean → 是/否）；身障/原民/外籍以 hover 註釋呈現太複雜，本 Phase 不做，留 Phase 3 增強。

身分證未填顯示 `-`，前端表頭加註「（缺號者請補政府申報欄位）」。

### 5.4 Sheet 3 — 統計摘要

```
總人數                    28
─────────────────────────
年齡層分布
  2-3 歲                   0
  3-4 歲                   8
  4-5 歲                  12
  5-6 歲                   8
─────────────────────────
特殊屬性占比
  弱勢                  2 (7.14%)
  身障                  1 (3.57%)
  原住民               0 (0%)
  外籍                  0 (0%)
─────────────────────────
出席統計
  全園應到人日       1,300
  全園實到人日       1,238
  全園出席率         95.23%
─────────────────────────
產生資訊
  快照日期           2026-05-31
  產生時間           2026-06-01 10:23
  產生人             wu0010802@gmail.com
```

---

## 6. UI 設計

### 6.1 路由

新增 `views/admin/gov-reports/MonthlyReportView.vue`：

- Path：`/admin/gov-reports/monthly`
- meta：需 `Permission.GOV_REPORTS_VIEW`
- 在 `views/admin/gov-reports/` sidebar group 加入連結（與 `IepView` / `SubsidiesView` / `CertificatesView` 並列）

### 6.2 頁面結構

```
┌────────────────────────────────────────────────────┐
│ 月度幼生在園統計（教育部申報用）                    │
├────────────────────────────────────────────────────┤
│ 月份：[2026 ▼] [5 月 ▼]  [產生 / 重算本月]  [匯出 Excel] │
│ 上次產生：2026-06-01 10:23 by wu0010802             │
├────────────────────────────────────────────────────┤
│ [班級總表] [幼生明細] [統計摘要]                    │
│                                                    │
│ <table 對應 tab 內容>                              │
│                                                    │
└────────────────────────────────────────────────────┘
                          ↓
              「對照 ece.moe.edu.tw → 幼生通報 → 月報」
```

### 6.3 互動細節

- 月份選擇器預設 = 「上個完整月份」（如今天 2026-06-15 → 預設 2026-05；今天 2026-06-01 → 預設 2026-05）
- `[產生 / 重算本月]` 永遠可點，但若 `(year, month)` 已有快照按下會跳 ElMessageBox `「已產生過，覆寫並重算？」` 確認
- 重算過程顯示 `el-loading`；完成後 toast `「已產生 N 筆」`
- `[匯出 Excel]` 在尚未產生時 disabled + tooltip `「請先產生本月」`
- 三個 tab 用 `el-tabs` 切換，table 用 `el-table` + 自動高度 + 排序
- 頁尾灰色小字 `對照 ece.moe.edu.tw → 幼生通報 → 月報` 一行

### 6.4 元件拆分

- `MonthlyReportView.vue` — 主頁，含月份選擇、產生按鈕、tab 切換
- `ClassroomSummaryTable.vue` — Sheet 1 對應 table（複用元件，支援未來 Phase 3 復用）
- `StudentDetailTable.vue` — Sheet 2 對應 table
- `OverviewSummaryCard.vue` — Sheet 3 對應卡片（不是 table，用 grid layout）

### 6.5 API wrapper

`src/api/govMoe.ts` 新增：

```typescript
export const generateMonthlyReport = (payload: { year: number; month: number }) =>
  api.post('/gov-moe/monthly/generate', payload)

export const getMonthlyReport = (params: { year: number; month: number }) =>
  api.get('/gov-moe/monthly', { params })

export const exportMonthlyReport = (params: { year: number; month: number }) =>
  api.get('/gov-moe/monthly/export', {
    params: { ...params, format: 'xlsx' },
    responseType: 'blob',
  })
```

---

## 7. 邊界與錯誤處理

| 情境 | 行為 |
|------|------|
| 學生月中加入（enrollment_date 在月中）| 應到日數從加入日起算；未到園的工作日不計入分母 |
| 學生月中退園（withdrawal_date 在月中）| 算到退園日；之後工作日不計入分母 |
| 學生畢業（graduation_date 在月中）| 算到畢業日 |
| 月中轉班 | 採月底快照班級；月初未轉前的出席記錄仍計入該學生，但歸到月底所在班級 |
| `(year, month)` 完全無學生（如測試開園前）| 回 200 + 空 sheet（snapshot 表寫 0 筆 rows） |
| Holiday 表該年無資料 | 仍跑（只少了國定假日扣除），warning log；前端不阻擋 |
| 並發產生 | PG advisory lock；第二個 caller 收 409 |
| 學生 `birthday` 為 NULL | `age_group = "未知"`，仍計入總人數 |
| 學生 `nationality` 為 NULL | 視為本國，不計入 foreign_count |
| 學生本月零在園日（如 enroll 月底前一天，當天碰巧週末+假日）| 不納入分母，仍計入 total_count 但 expected_days = 0 |

---

## 8. Audit log

寫入既有 `audit_logs` 表（用既有 `utils.audit.write_audit_log`）：

```python
event = "gov_moe.monthly.generate" or "gov_moe.monthly.regenerate"
target_type = "monthly_enrollment_snapshot"
target_id = f"{year}-{month:02d}"
details = {
    "year": year,
    "month": month,
    "rows_before": <int>,
    "rows_after": <int>,
    "snapshot_date": <iso>,
}
```

不存所有 row 的前後值（量大），只存 row 計數差異。完整 row 已在 snapshot 表本身可查。

---

## 9. 權限

- `Permission.GOV_REPORTS_VIEW` — GET endpoint（讀快照、看報表）
- `Permission.GOV_REPORTS_EXPORT` — POST generate、GET export

兩位元 Phase 1 已建好且被使用，本 Phase 重用，不新增。

---

## 10. 測試

### 10.1 pytest（`tests/test_gov_moe_monthly.py`）

純函式測試（不需 DB）：
- `calc_age_group(2020-01-15, 2026-05-31)` → "5-6"
- `is_foreign("本國")` → False；`is_foreign("美國")` → True；`is_foreign(None)` → False
- 出席率算法（人日加權正確）

DB 整合測試：
- 跨月加入：5/15 enroll，應到日 = 5/15–5/31 工作日交集
- 跨月退園：5/20 withdraw，應到日 = 5/1–5/20 工作日交集
- 月中轉班：5/10 轉班，月底班級 = transfer 後班級；統計歸到該班
- 無學生月份：snapshot 表寫 0 row，GET 回 404
- 重新產生：第二次跑覆寫前後 row 數差異 + audit log
- 並發產生：兩 thread 同時跑，一個 409
- Holiday 扣除：5 月加 1 個自訂 Holiday → 應到 -1
- WorkdayOverride 加：週六補班 → 應到 +1
- 學生 NULL birthday → `age_group = "未知"`
- 弱勢/身障/原民/外籍計數正確（含 fixture 各 1 名）

權限測試：
- 沒 GOV_REPORTS_VIEW → GET 403
- 沒 GOV_REPORTS_EXPORT → POST/Export 403

Excel 整合測試：
- 跑 `monthly_excel_writer.build(snapshot_rows, ...)`，用 `openpyxl.load_workbook` 驗證 3 sheets 名稱、首列 header、合計列正確

### 10.2 vitest（`tests/MonthlyReportView.test.ts`）

> 註：前端 TS-only 強制（2026-05-19 起，見 workspace CLAUDE.md §「規範共通項」），測試檔一律 `.test.ts`。

- 月份預設為「上個完整月份」
- 尚未產生時 `匯出 Excel` disabled，tooltip 顯示
- 已產生時點 `產生 / 重算本月` 顯示確認 Modal
- 三 tab 切換顯示對應 component
- 產生中 loading 顯示

---

## 11. 工作流（Implementation order）

> 此處列順序，不列每步細節（細節在 implementation plan 中由 writing-plans 產出）。

**後端優先（feat/moe-phase2-monthly-report-2026-05-19-backend）**

1. 建 `services/gov_moe/monthly_calculator.py` — 純函式：`calc_age_group`、`is_foreign`、`working_days_in_month`、`classroom_at_month_end`、`compute_student_attendance`，pytest 全綠
2. 建 `services/gov_moe/monthly_excel_writer.py` — 用 openpyxl，takes pre-built rows，pytest 全綠
3. 建 `api/gov_moe/monthly.py`：3 endpoint，掛 `__init__.py`，整合測試
4. Audit log + advisory lock + 並發測試
5. 更新 OpenAPI（`scripts/dump_openapi.py` 自動產出）

**前端後行（feat/moe-phase2-monthly-report-2026-05-19-frontend）**

6. `npm run gen:api` 拉新 schema
7. `src/api/govMoe.ts` 加 3 個 wrapper
8. 建 `views/admin/gov-reports/MonthlyReportView.vue` + 3 子元件
9. 加 router + sidebar 連結
10. vitest 全綠
11. dev server 手動驗證

**整合驗證**（最後一步，user 參與）

12. `./start.sh` 起雙端，產 2026-04 月報，驗 Excel 三 sheet 數字
13. 跨月加退/轉班/Holiday 邊界 manual 測試

---

## 12. Worktree 與分支

```
feat/moe-phase2-monthly-report-2026-05-19-backend   (BE worktree)
feat/moe-phase2-monthly-report-2026-05-19-frontend  (FE worktree)
```

分開 commit、分開 PR。後端 PR 不要 merge 前先讓前端拉本地後端 branch 做 codegen + 整合驗證，OK 後兩端各自 PR。

---

## 13. 不在本 Phase 範圍

- 自動排程（每月 1 號自動 generate）— 業主明確不要
- 政府網站爬蟲對接 — 政府不開放
- Phase 3（幼生 / 教保員對照清單）— 另開 phase
- 學期初/異動清單 — Phase 3
- 多年度趨勢圖 — Phase 5+（如有需求再評估）
- 月報 PDF 版 — Excel 即足
