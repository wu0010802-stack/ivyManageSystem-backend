# Design: leaves + overtimes 共用 approval helper 抽取

**Date:** 2026-05-14
**Scope:** ivy-backend
**Target files:** `api/leaves.py` (2341 行) / `api/overtimes.py` (1915 行) / `utils/approval_helpers.py` (148 行)

## 動機

memory `feedback_plan_template_codebase_drift` 與 `project_quality_refactor_batch_2026_05_11` 標註的「#3 leaves+overtimes 共用 helper」留待後續。

**主軸不是省行數，而是預防「單側加 guard 漏同步另一側」的 P1 bug。** memory `project_leaves_overtimes_bug_batch_2026_05_12` P1-5 即此 pattern 漏修才補。

## 設計準則

每個欲抽出的 helper 須通過下列測試：

> 「此 helper 能否在未來 `api/punch_corrections.py` 一併使用？」

不能 → 業務邏輯卡在裡面，不該抽。

## 要抽的 helper（原規劃 4 個，實作落地 3 個，全部在 `utils/approval_helpers.py`）

### 1. `assert_not_self_approval`

```python
def assert_not_self_approval(
    approver: dict, owner_employee_id: int, doc_label: str
) -> None:
    """Approver 與申請人為同一員工時 raise 403。

    Why: 純管理員（無 employee_id）本身無法提出申請，不構成自我核准風險；
    僅在 approver 確實擁有 employee_id 且與 owner 相同時拒絕。
    """
```

替換點：
- `api/leaves.py` L1199-1204（單筆 approve）
- `api/leaves.py` 批次 approve（同邏輯重複）
- `api/overtimes.py` L1309-1314（單筆 approve）
- `api/overtimes.py` 批次 approve

### 2. `assert_approver_eligible`

```python
def assert_approver_eligible(
    session, doc_type: str, submitter_employee_id: int,
    approver_role: str, doc_label: str
) -> None:
    """整合 _get_submitter_role + _check_approval_eligibility + 403 raise。

    Why merge: 三步永遠連發；分散兩 helper 加一個 raise 模板 → 4 處重複 8 行。
    保留現有 _get_submitter_role / _check_approval_eligibility 不刪，
    供其他端點（meetings/shifts）單獨使用。
    """
```

替換點：4 處同上。

### 3. `collect_months_from_date_range`

```python
def collect_months_from_date_range(
    start: date, end: date
) -> set[tuple[int, int]]:
    """蒐集 [start, end] 區間內所有 (year, month) tuple。

    Why: leaves.py L1261-1270 有 inline `while _cur <= _end` 12 行 loop；
    overtimes.py 用 `collect_months_from_dates([single_date])`。兩邊行為一致
    但實作分歧——抽 helper 順帶解掉。
    punch_corrections 未來改跨日時也會用。
    """
```

替換點：
- `api/leaves.py` L1261-1270 inline loop
- overtimes 維持 `collect_months_from_dates`（單日已是正確語意，不動）

### 4. `apply_approval_salary_guards` — **實作時放棄**

原規劃將 `assert_months_not_finalized + lock_and_premark_stale` 收成單一
helper。實作驗證後放棄：

- `api/leaves.py` L1259-1271 在此處**只做 lock**（assert 在其他 path 上做）
- `api/overtimes.py` L1331-1341 同時做 assert + lock

兩者不對稱。若強行合成「assert + lock 二合一」會改變 leaves 此處的守衛時序
（多出一個 assert），屬於行為變更而非 refactor。保留 `lock_and_premark_stale`
單獨 helper 已足夠；不抽。

## 不抽的（防止反向過度設計）

| 候選 | 不抽原因 |
|------|---------|
| `_execute_approval_state_machine()` | 中間夾 substitute_guard / force_overlap 三重 / 補休配額 / 月加班上限——業務分歧大，硬抽會變 `if doc_type == "leave"` 分支 |
| `_batch_approve_generic()` | Phase 2 呼叫的服務不同（`_grant_comp_leave_quota` 只在 overtime），抽框架仍要 callback 參數，比原碼更複雜 |
| `_check_cross_type_substitute_conflict()` | 業務需求未明（overtime 是否該檢查請假衝突待業主確認），不在本次範圍 |
| `_serialize_approval_record()` | GET list 欄位差異多（leaves 多換班關聯），硬抽 helper 比原碼冗 |

## 預期成效

- **行數**：兩檔合計減 ~80-130 行（5-7%）；不誇大
- **主要價值**：4 個守衛集中一處；未來新增 guard 改一處，3 router 同步
- **bonus**：leaves inline loop vs overtimes 用 helper 的業務不一致解掉

## 實作步驟

1. EnterWorktree（**base from origin/main**——避開 main 上 gov_data 刪除 WIP）
2. **Commit A**：新增 4 helper + 單元測試（`tests/test_approval_helpers.py`）
3. **Commit B**：`api/leaves.py` 4 處呼叫切換
4. **Commit C**：`api/overtimes.py` 4 處呼叫切換
5. 驗證：
   ```bash
   pytest tests/test_approval_helpers.py tests/test_leaves*.py tests/test_overtimes*.py -q
   ```

## 風險

- **WIP 干擾**：main 有 gov_data 刪除中改動（`tests/conftest.py`、`api/insurance.py` 等）——必須 base from `origin/main` 而非 HEAD
- **批次端點分歧**：兩檔批次 approve（leaves L1617-2070、overtimes L1455-1680）內部呼叫 substitute guard / comp_leave 等業務動作，僅在 Phase 1/2 殼層套用 helper 1+2+4，不動 Phase 2 內部
- **不變式**：所有既有 pytest 必須零 regression；本次不改任何業務行為，僅 refactor

## 不變動

- `_get_submitter_role` / `_check_approval_eligibility` / `_write_approval_log` 保留
- 任何 router 對外 API（request/response schema）不變
- 任何權限位元 / DB schema 不變
- 前端不需任何同步改動
