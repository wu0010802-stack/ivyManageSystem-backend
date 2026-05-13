# 考勤異動申請共用框架整合（Attendance Request Consolidation）

**日期**：2026-05-13
**作者**：brainstorming with Claude
**狀態**：design approved，等待 implementation plan
**範圍**：ivy-backend（單 repo），少量 ivy-frontend 配合（BatchApprovalDialog 錯誤顯示）

---

## 1. 動機

`api/leaves.py`（2300 行）、`api/overtimes.py`（1918 行）、`api/punch_corrections.py`（278 行）三者在領域模型上高度對稱（`is_approved/approved_by/rejection_reason`、跨月切月、薪資封存守衛、列鎖、LINE 通知），但目前各自重寫核心邏輯。

近期 14 條 bug 批次修補（2026-05-12，見 `memory/project_leaves_overtimes_bug_batch_2026_05_12.md`）暴露同類問題在三邊修補不齊的長期成本。本整合目標為**降低未來事故率**，而非單純減行數。

## 2. 範圍與策略

- **方案 A（保守整合）**：抽 service 層 helper，router 與 model 維持獨立。**API 與 schema 零變更**（除 Stage 2 cross_type_offset 影響加班費總額）。
- **兩階段 stacked**：Stage 1 純抽取（無行為變更）/ Stage 2 行為層整合（fail-fast batch、cross-type offset、Excel 骨架）。
- **測試**：B 策略 — 既有測試 + 為每個新 helper 寫獨立單元測試。
- **七項 helper**：(a) approval log writer / (b) finalize guard / (c) batch executor / (d) stale marker / (e) delegate（含跨類抵扣）/ (f) LINE notifier / (g) Excel 匯入骨架。

## 3. 目錄佈局

```
ivy-backend/services/
├── approval/                    ← 新建子套件
│   ├── __init__.py
│   ├── log_writer.py            (a)
│   ├── batch_executor.py        (c)
│   └── delegate.py              (e)
├── salary/                      ← 既有，擴充
│   ├── finalize_guard.py        (b)
│   └── stale_marker.py          (d)
└── notification/                ← 新建子套件
    ├── __init__.py
    └── approval_notifier.py     (f)

ivy-backend/utils/
└── excel_io.py                  (g)
```

**設計原則**：service 為無狀態純函式 + 接 `session`；router 仍 owning 三個 endpoint，僅 delegate 共用邏輯；既有 helpers 標 `# DEPRECATED` 不刪，Stage 2 結尾統一 sweep。

## 4. Stage 1 — 純抽取（commits 1-4）

無行為變更，零 API 影響。每 commit 是「helper 搬遷 + 三邊 router import 改寫 + 新 helper 單元測試」。

### (a) `services/approval/log_writer.py`

```python
def write_approval_log(
    session: Session,
    *,
    doc_type: Literal["leave", "overtime", "punch_correction"],
    doc_id: int,
    action: Literal["create", "approve", "reject", "update", "cancel"],
    actor_id: int,
    rejection_reason: str | None = None,
    metadata: dict | None = None,
) -> ApprovalLog: ...
```

- 三邊欄位不一致（leave 有 delegate_id、ot 沒有）→ 統一收進 `metadata` JSON
- **不改 ApprovalLog 表結構**

### (b) `services/salary/finalize_guard.py`

```python
def collect_affected_months(records: Sequence[Leave | Overtime | PunchCorrection]) -> set[YearMonth]: ...
def assert_months_not_finalized(session, months: set[YearMonth]) -> None: ...  # raise 409
```

- 推廣 leaves 的 `_collect_leave_months` 為 polymorphic
- 解 MEMORY 標的「leaves 已修但 ot/pc 還沒」

### (d) `services/salary/stale_marker.py`

```python
@contextmanager
def lock_and_mark_stale(session, *, employee_id: int, months: set[YearMonth]):
    """with_for_update + 寫 stale 旗標 + 排除 finalized"""
    ...
```

- 包成 context manager 避免漏寫
- punch_corrections 目前沒做，此次補上

### (f) `services/notification/approval_notifier.py`

```python
def notify_approval(*, doc_type, doc_id, action, actor, target_user_id, rejection_reason=None) -> None:
    """非阻塞 LINE 推送，caller 負責在 commit 後才呼叫"""
    ...
```

- 統一訊息模板（doc_type 參數化）
- 時序由 caller 控制（commit 後才呼叫）

## 5. Stage 2 — 行為層整合（commits 5-9）

### (c) `services/approval/batch_executor.py` — 兩段提交

```python
def execute_batch_approval(
    session: Session,
    *,
    doc_type: str,
    record_ids: list[int],
    action: Literal["approve", "reject"],
    actor_id: int,
    rejection_reason: str | None = None,
    validator: Callable[[Session, Record], None],
    side_effects: Callable[[Session, list[Record]], None],
) -> BatchResult: ...
```

- Pass 1：載入 + with_for_update + validator 全跑（任一失敗即 abort）
- Pass 2：批次寫 ApprovalLog + 變更狀態 + 呼叫 side_effects + commit
- Pass 3：commit 後 LINE 推送
- **行為差異**：原本 ot/pc 部分成功會留 partial state，新版改 fail-fast。需 release note + 前端 `BatchApprovalDialog` 同步。

### (e) `services/approval/delegate.py`

```python
def resolve_delegate_for_leave(session, leave: Leave) -> EmployeeRef | None: ...
def resolve_cross_type_offset(session, leave: Leave) -> Overtime | None:
    """leave↔OT 跨類抵扣"""
```

- `resolve_delegate_for_leave`：抽 leave 既有邏輯（多人輪值、班導優先序、`_scoped_query`），無行為變更
- `resolve_cross_type_offset`：**新增**。**單向觸發**：approve leave 時偵測同員工同日已核准但未發放的 OT，回傳要 offset 的 OT 紀錄（caller 負責寫入扣抵）。approve OT 時不反向觸發（避免雙向遞迴與時序歧義）。
- **行為差異**：跨類抵扣後加班費總額可能下降。**金流影響高**。
- **Feature flag**：`ENABLE_LEAVE_OT_OFFSET` 環境變數，預設 `false`。dev DB 驗證通過後再決定是否 prod 啟用。

### (g) `utils/excel_io.py` — Excel 骨架

```python
class ExcelImportSchema(BaseModel):
    """子類別宣告 columns + per-row validator"""

def parse_excel(file: UploadFile, schema: type[ExcelImportSchema]) -> ImportResult: ...
```

- 統一錯誤格式：`{row, col, value, error_code, message}`
- 本次只把 leaves 改用新骨架做樣板；ot/pc 留 TODO（候選 #2 收尾）
- **行為差異**：錯誤回報格式統一化，前端 `LeaveImportDialog` 同步調整

## 6. Commit 切分

```
Stage 1（純抽取，無行為變更）
  commit 1: services/approval/log_writer.py + 三邊改用 + 測試
  commit 2: services/salary/finalize_guard.py + 三邊改用 + 測試
  commit 3: services/salary/stale_marker.py + 三邊改用（含 pc 新增）+ 測試
  commit 4: services/notification/approval_notifier.py + 三邊改用 + 測試

Stage 2（行為變更）
  commit 5: batch_executor + 三邊改用 + fail-fast 測試
  commit 6: delegate.resolve_delegate_for_leave 抽出（無行為變更）+ 測試
  commit 7: delegate.resolve_cross_type_offset 新增 + leave approve flow 接上 + feature flag + 測試 + RELEASE_NOTES
  commit 8: excel_io 骨架 + leaves 匯入改用 + 測試
  commit 9: 清除 # DEPRECATED 舊 helpers
```

兩條分支：
- `refactor/attendance-consolidation-stage1`（commits 1-4，先 merge）
- `refactor/attendance-consolidation-stage2`（commits 5-9，stacked on stage1）

## 7. 測試策略

### Stage 1（每 commit）
- 新增 helper 獨立單元測試：
  - `tests/services/approval/test_log_writer.py`：5-8 case（doc_type × action × metadata）
  - `tests/services/salary/test_finalize_guard.py`：跨月、finalized 偵測、空集合
  - `tests/services/salary/test_stale_marker.py`：正常 exit / 例外釋鎖 / finalized 跳過
  - `tests/services/notification/test_approval_notifier.py`：mock LineService 驗時序與 reason 帶入
- 既有 leaves/overtimes/punch_corrections 測試全綠（contract test）

### Stage 2（行為變更補強）
- `test_batch_executor.py`：fail-fast（10 筆第 5 筆失敗→前 4 筆不可寫入）、權限檢查順序、跨類混合
- `test_delegate.py`：代理人優先序（班導 > 副班導 > 同 classroom 任一）、cross-type offset 三場景（OT 足/不足/已部分領取）
- `test_excel_io.py`：錯誤格式統一性（缺欄、型別錯、business validator 失敗）

### 驗收
- 每 commit `pytest -x` 全綠
- Stage 1/2 結束跑 full suite
- Stage 2 commit 7 額外跑 115.04 對齊驗證（feature flag on/off 兩遍）

## 8. 風險與回滾

| 風險 | 影響 | 緩解 |
|---|---|---|
| Stage 1 helper 抽錯參數預設 → 三邊微妙不一致 | 中 | 單元測試鎖契約 + 既有測試做 contract test |
| Stage 2 (c) fail-fast 改 partial-success 客戶端體驗 | 中 | RELEASE_NOTES + 前端 BatchApprovalDialog 同步 |
| Stage 2 (e) cross_type_offset 影響加班費金流 | **高** | feature flag `ENABLE_LEAVE_OT_OFFSET` 預設 off + dev DB 115.04 對齊驗證 + 人資 release note |
| 中間有 hotfix 進來 | 低 | Stage 1 merge 後 Stage 2 rebase；Stage 1 純抽取衝突面積小 |
| punch_corrections 新增 stale_marker 標到舊資料 | 中 | mark_stale 排除 finalized 月份，僅作用於 non-finalized |

### 回滾粒度
- Stage 1：純抽取 commit 可單獨 `git revert`
- Stage 2：commit 5 (batch) 與 commit 7 (offset) **可分別獨立 revert**

## 9. 不在範圍內

- 合表為 `attendance_requests` discriminator 表（C 方案，已否決）
- 抽 `AttendanceRequestBase` 泛型基類（B 方案，已否決）
- Permission IntFlag → RBAC 改造
- 統一 ApprovalLog / AuditLog / StudentChangeLog / RegistrationChange 四套日誌
- ot/pc 的 Excel 匯入接新骨架（留候選 #2）
- 任何前端架構改造（僅 BatchApprovalDialog 錯誤顯示同步）

## 10. 預期工時

- Stage 1：2 天（4 commits + 測試）
- Stage 2：2 天（5 commits + feature flag + 對齊驗證）
- 前端 BatchApprovalDialog 同步：0.5 天

合計 4.5 工作天。
