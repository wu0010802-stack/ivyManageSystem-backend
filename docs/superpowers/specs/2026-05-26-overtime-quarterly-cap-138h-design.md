# 加班季上限 138h（勞基法 §32 II）enforce 設計

- 日期：2026-05-26
- 領域：HR / 加班 / 勞動法令遵循
- 狀態：spec drafted，待 user 二審後進 writing-plans

## 1. 背景與動機

勞基法第 32 條第 2 項規定：「延長之工作時間，一個月不得超過 46 小時。但雇主經工會同意，如事業單位無工會者，經勞資會議同意後，得將前項第二款工作時間延長之限制，於每三個月不得超過 138 小時，每月不得超過 54 小時。」

現況：
- ✅ **月度 46h cap 已 enforce**：`services/overtime_conflict_service.py:187 check_monthly_overtime_cap` + 純函式 `_assert_within_monthly_cap`；admin `api/overtimes.py` 5 處 + portal `api/portal/overtimes.py` 1 處共 6 個 call site
- ❌ **季 138h cap 完全不存在**：全 codebase `grep` `138 / quarterly_overtime / 三個月` 0 命中（quarterly 命中皆為「extra_dependents_quarterly」季扣眷屬，與 OT 無關）

風險：勞檢可裁罰 NT$2 萬-100 萬/案（勞基法 §79）。在現行 46h/月 hard block 下 46×3=138 剛好不超過、季 cap 理論上不會觸發；但仍要做的價值：

1. **Defense in depth**：monthly check 因 bug/race 漏算的兜底
2. **Future-proof**：若改採彈性方案 54h/月 立即有用
3. **勞檢合規 audit trail**：勞檢時可拿出來證明系統實作 §32 II 雙重上限

## 2. 範圍

**包含**：
- 後端 `services/overtime_conflict_service.py` 新增 1 純函式 + 1 DB-aware 函式
- `utils/constants.py` 新增 cap 值常數
- admin `api/overtimes.py` 5 處 + portal `api/portal/overtimes.py` 1 處 並排呼叫新 helper
- 新增 ~14 條測試（純函式 4 + DB-aware 6 + 整合 4）
- 後端 `CLAUDE.md` 文件更新一句

**不包含（YAGNI 排除）**：
- ❌ Read-only API `GET /overtimes/quarterly-cap-status`（user 已選最小化）
- ❌ 前端 UI dashboard / simulate 顯示 hint
- ❌ Setting toggle 支援彈性方案 54h/月（46h hard block 已足覆蓋預設情境）
- ❌ Day-by-day rolling window（曆月對齊已合規，day-level 複雜且訊息難讀）
- ❌ §84-1 責任制員工排除（系統無此分類欄位）
- ❌ DB schema migration（無欄位變更）
- ❌ 歷史資料回溯掃描 / 修補（只擋新申請與修改）

## 3. 設計決策

### 3.1 窗口定義：曆月對齊 rolling 3 個月

對於 OT 日期 D 所在月份 M，檢查 3 個包含 M 的窗口：
- W1: 月份 [M-2, M]（如 M=2026-05 → 2026-03 ~ 2026-05）
- W2: 月份 [M-1, M+1]（如 2026-04 ~ 2026-06）
- W3: 月份 [M, M+2]（如 2026-05 ~ 2026-07）

任一窗口累計 + 新筆 > 138 即 block。**非 day-by-day rolling** — 勞動部解釋令本身用「連續月份」表達；day-level 實作複雜度高 3 倍但合規效益相同。

### 3.2 嚴格度：Hard block（HTTP 400）

與現行 monthly 46h cap 行為一致。HR 若要 override 須先刪其他日期 OT。

### 3.3 適用對象：全員

不分月薪/時薪，不排除責任制（系統無此欄位）。與 monthly cap 一致。

### 3.4 計算口徑

- **Pending + Approved 都算**（`is_approved IN (NULL, True)`）；rejected 不算
- 支援 `exclude_id` 參數，update 路徑可排除自身舊記錄
- 與 monthly cap 完全同口徑

### 3.5 Cap 值

`MAX_QUARTERLY_OVERTIME_HOURS = 138.0`（hardcode 常數，與 `MAX_MONTHLY_OVERTIME_HOURS = 46.0` 同檔同層）。

## 4. 架構與元件

### 4.1 新增

**`utils/constants.py`**（既有檔，加 2 行）：
```python
MAX_QUARTERLY_OVERTIME_HOURS = 138.0  # 勞基法第 32 條第 2 項：每連續三個月延長工時上限
OVERTIME_QUARTERLY_WINDOW_MONTHS = 3   # rolling 窗口長度（月）
```

**`services/overtime_conflict_service.py`**（既有檔，加 2 函式）：

```python
def _assert_within_quarterly_cap(
    worst_existing_hours: float,
    new_hours: float,
    window_label: str,  # e.g. "2026/03~2026/05"
    employee_id: int,
) -> None:
    """純函式：驗證最壞窗口既存 + 新加班時數不超過勞基法第 32 條第 2 項
    每連續三個月 138h 上限。worst_existing_hours 由 caller 取 3 窗口的 max。"""
    existing = float(worst_existing_hours or 0)
    new = float(new_hours or 0)
    total = existing + new
    if total > MAX_QUARTERLY_OVERTIME_HOURS + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"員工 #{employee_id} 連續三個月（{window_label}）"
                f"已申請加班 {existing:.1f} 小時，"
                f"加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，"
                f"超過勞基法第 32 條第 2 項每連續三個月延長工時上限 "
                f"{MAX_QUARTERLY_OVERTIME_HOURS:.0f} 小時。"
            ),
        )


def check_quarterly_overtime_cap(
    session,
    employee_id: int,
    target_date: date,
    new_hours: float,
    exclude_id: Optional[int] = None,
) -> None:
    """查詢員工 3 個包含 target_date 月份的 rolling 3-month 窗口已申請 OT，
    加上新時數後驗證任一窗口不超過 138h。"""
    # 算 3 個窗口的 (start_month, end_month) 範圍
    windows = []  # list of (start_date, end_date, label)
    for offset in (-2, -1, 0):
        start_year, start_month = _shift_month(target_date.year, target_date.month, offset)
        end_year, end_month = _shift_month(target_date.year, target_date.month, offset + 2)
        start_date = date(start_year, start_month, 1)
        _, last_day = cal_module.monthrange(end_year, end_month)
        end_date = date(end_year, end_month, last_day)
        label = f"{start_year}/{start_month:02d}~{end_year}/{end_month:02d}"
        windows.append((start_date, end_date, label))

    # 對每窗 SUM existing
    results = []  # (existing_hours, label)
    for start, end, label in windows:
        q = session.query(func.coalesce(func.sum(OvertimeRecord.hours), 0)).filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
            or_(OvertimeRecord.is_approved.is_(None), OvertimeRecord.is_approved == True),
        )
        if exclude_id is not None:
            q = q.filter(OvertimeRecord.id != exclude_id)
        existing = float(q.scalar() or 0)
        results.append((existing, label))

    # 取「最先超過」的窗口（按 W1→W2→W3 順序，方便 HR 從早到晚排查）
    new = float(new_hours or 0)
    for existing, label in results:
        if existing + new > MAX_QUARTERLY_OVERTIME_HOURS + 1e-9:
            _assert_within_quarterly_cap(existing, new, label, employee_id)
            return  # _assert 必會 raise，這行只是 defensive


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """月份位移 helper：(2026, 5) + 2 = (2026, 7)；(2026, 1) - 2 = (2025, 11)"""
    total = (year * 12 + month - 1) + offset
    return total // 12, total % 12 + 1
```

### 4.2 修改的 call site

**admin `api/overtimes.py`** — 5 處（皆位於現行 `_check_monthly_overtime_cap(...)` 之後立刻呼叫）。Implementation phase 用 `grep -n "_check_monthly_overtime_cap" api/overtimes.py api/portal/overtimes.py` 對齊全部 call site 後逐一補上 quarterly check：
- `api/overtimes.py:614`
- `api/overtimes.py:762`
- `api/overtimes.py:1095`
- `api/overtimes.py:1306`
- `api/overtimes.py:1668`

**portal `api/portal/overtimes.py`** — 1 處：
- `api/portal/overtimes.py:153`（portal create）

呼叫 pattern：

```python
from services.overtime_conflict_service import (
    check_monthly_overtime_cap as _check_monthly_overtime_cap,
    check_quarterly_overtime_cap as _check_quarterly_overtime_cap,  # 新增
)

# 既有
_check_monthly_overtime_cap(session, emp_id, overtime_date, hours, exclude_id=...)
# 緊接新增
_check_quarterly_overtime_cap(session, emp_id, overtime_date, hours, exclude_id=...)
```

兩 cap 都過才放行。順序：monthly 先（更小窗口更易擋下、訊息更直接），quarterly 後。

### 4.3 不變動

- DB schema 完全不動（無 migration）
- API 契約完全不動（無新 endpoint、無 response model 變更）
- 前端零改動（錯誤訊息走既有 axios `displayMessage` 流程）
- monthly cap 邏輯完全不動

## 5. Data Flow

範例：`check_quarterly_overtime_cap(session, emp_id=42, target_date=2026-05-15, new_hours=5.0)`

1. **算 3 個窗口**：
   - W1 = (2026-03-01, 2026-05-31, "2026/03~2026/05")
   - W2 = (2026-04-01, 2026-06-30, "2026/04~2026/06")
   - W3 = (2026-05-01, 2026-07-31, "2026/05~2026/07")

2. **對每窗 SUM existing OT**（3 個輕量 SUM query）：
   ```sql
   SELECT COALESCE(SUM(hours), 0)
   FROM overtime_records
   WHERE employee_id = 42
     AND overtime_date BETWEEN '2026-03-01' AND '2026-05-31'
     AND (is_approved IS NULL OR is_approved = TRUE)
   ```
   結果範例：`[(120.0, "2026/03~2026/05"), (90.0, "2026/04~2026/06"), (50.0, "2026/05~2026/07")]`

3. **按順序檢查**：
   - W1: 120 + 5 = 125 ≤ 138 ✓
   - W2: 90 + 5 = 95 ≤ 138 ✓
   - W3: 50 + 5 = 55 ≤ 138 ✓
   - 全過 → 不 raise

範例 2（W1 超標）：
   - W1: 135 + 5 = 140 > 138 → HTTPException 400「員工 #42 連續三個月（2026/03~2026/05）已申請加班 135.0 小時，加上此筆 5.0 小時合計 140.0 小時，超過勞基法第 32 條第 2 項每連續三個月延長工時上限 138 小時。」

效能：每呼叫 3 個 SUM。OvertimeRecord 表 `(employee_id, overtime_date)` index 需確認；若無在 implementation phase 評估是否補（一般 OT 表體量不大，可能不需）。

## 6. Error Handling

### 文案規範

訊息必含 6 要素，順序固定：

```
員工 #{employee_id} 連續三個月（{window_label}）已申請加班 {existing:.1f} 小時，
加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，
超過勞基法第 32 條第 2 項每連續三個月延長工時上限 138 小時。
```

| 要素 | 取值 |
|------|------|
| 員工 ID | caller 提供 |
| 窗口 | `YYYY/MM~YYYY/MM`（曆月格式）|
| 累計 | 該窗口 existing hours (`.1f`) |
| 新筆 | new_hours (`.1f`) |
| 合計 | existing + new (`.1f`) |
| 法源 | 勞基法第 32 條第 2 項 + 138h 上限 |

### HTTP 行為

- Status: **400**（與 monthly cap 一致；商業規則違反，非請求格式錯）
- 多窗口同時超標時回報「最先超過」（W1 > W2 > W3 順序），讓 HR 從早到晚排查
- 不寫 audit log（與 monthly cap 行為一致；400 本身會被 sentry/access log 記到）

## 7. Testing

### 7.1 純函式測試（`tests/test_overtimes.py` 加 ~4 條）

沿用既有 `_assert_within_monthly_cap` 測試模式：

```python
def test_assert_within_quarterly_cap_boundary_pass():
    """138.0 剛好不算超過"""
    _assert_within_quarterly_cap(132.0, 6.0, "2026/03~2026/05", 1)  # 不 raise

def test_assert_within_quarterly_cap_over_blocks():
    """138.1 即 raise"""
    with pytest.raises(HTTPException, match="超過勞基法第 32 條"):
        _assert_within_quarterly_cap(132.0, 6.2, "2026/03~2026/05", 1)

def test_assert_within_quarterly_cap_none_safety():
    """None 不會 crash"""
    _assert_within_quarterly_cap(None, 10.0, "x", 1)
    _assert_within_quarterly_cap(10.0, None, "x", 1)

def test_assert_within_quarterly_cap_message_format():
    """訊息含 6 要素"""
    with pytest.raises(HTTPException) as exc:
        _assert_within_quarterly_cap(135.0, 5.0, "2026/03~2026/05", 42)
    msg = exc.value.detail
    assert "#42" in msg
    assert "2026/03~2026/05" in msg
    assert "135.0" in msg
    assert "5.0" in msg
    assert "140.0" in msg
    assert "138" in msg
```

### 7.2 DB-aware 測試（新檔 `tests/test_overtimes_quarterly_cap.py`，~6 條）

| Test | 場景 |
|------|------|
| `test_quarterly_cap_all_windows_pass` | 3 窗口都不超過 → pass |
| `test_quarterly_cap_middle_window_blocks` | W2 (2026/04~06) 超過 → block 且訊息提 "2026/04~2026/06" |
| `test_quarterly_cap_exclude_id_excludes_self` | update 路徑 exclude_id 排除自己舊紀錄 |
| `test_quarterly_cap_rejected_not_counted` | `is_approved=False` 不算進累計 |
| `test_quarterly_cap_year_boundary` | target_date=2026-01-15 → W1 = 2025/11~2026/01 跨年正確 |
| `test_quarterly_cap_pending_counted` | `is_approved=None` 算進累計（與 monthly 一致）|

### 7.3 整合測試（~4 條）

| 路徑 | 場景 |
|------|------|
| `POST /api/overtimes` (admin create) | 138 boundary：累計 132h、申請 6h → 200；申請 7h → 400 |
| `POST /api/portal/overtimes` (portal create) | 同上 |
| admin batch import (`api/overtimes.py:1668`) | 單筆超 138 → 整批 400 + rollback |
| admin approve / status change (`api/overtimes.py:1095`) | 已 pending 但累計 quarterly 會超過 → approve 時 400 擋下 |

合計約 14 新 test，預估 +400 行測試 + ~80 行 production code。

## 8. CLAUDE.md / 文件更新

`ivy-backend/CLAUDE.md`「加班」相關段落加一句：

> 加班規則同步檢查勞基法 §32 II 雙重上限：月度 46h（`check_monthly_overtime_cap`）+ 季 138h（`check_quarterly_overtime_cap`，曆月對齊 rolling 3 月）。兩者並排呼叫，admin/portal create/update/approve/batch 5+1 個 call site 同步 enforce。

## 9. 風險與已知限制

| 風險 | 緩解 |
|------|------|
| Rolling 窗口是曆月對齊非 day-level，極端情境下（同一月不同段集中）可能與真 rolling 有 ±1-2h 誤差 | 勞動部解釋令本身用「連續月份」描述，曆月對齊合規無虞 |
| 在現行 46h/月 嚴格 enforce 下 quarterly cap 永遠不觸發 | 仍有 defense in depth / future-proof / 合規 audit trail 價值（見 §1）|
| 6 個 call site 需手動補，遺漏即破口 | implementation phase 用 grep 對齊既有 monthly call site list；integration test 4 條涵蓋 4 個 entry path |
| 3 個 SUM query 每次呼叫 = 比 monthly 多 3 倍 DB hit | OT 表體量小、SUM 為輕量 query；若 prod 出現熱點 future optimize 為 1 個 UNION ALL query |

## 10. Out of Scope（明列 follow-up，這份 spec 不做）

- 彈性方案 setting toggle（54h/月）支援
- 加班月度 / 季度 dashboard
- 自動產出勞檢報表（含 §32 II 合規證明）
- §84-1 責任制員工分類欄位 + 排除邏輯
- 跨年度 quarterly 計算規則（曆月對齊已隱含正確處理跨年）

## 11. 驗收條件

- [ ] `check_quarterly_overtime_cap` 與 `_assert_within_quarterly_cap` 落地，pytest 14 條全綠
- [ ] admin/portal 6 個 call site 全部並排呼叫（grep 確認）
- [ ] 整合測試 4 條 happy + boundary 全綠
- [ ] 既有 pytest（5000+ 條）零 regression
- [ ] CLAUDE.md 加一句已交代
- [ ] OpenAPI 無變更（無新 endpoint）
- [ ] 前端零改動（無 schema.d.ts regen）

## 12. 後續

依 brainstorming 流程，spec 通過 user 二審後 invoke `superpowers:writing-plans` skill 產出 step-by-step 實作計畫，含 sub-task 拆分與 verification gate。
