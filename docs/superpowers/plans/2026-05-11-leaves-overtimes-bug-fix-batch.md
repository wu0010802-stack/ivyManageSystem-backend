# 加班/請假模組 14 條 bug 修補 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修補 2026-05-11 稽核發現的 14 條 leaves / overtimes bug（P0×3, P1×8, P2×3），全部後端，前端 P1-6 改造改另開 ticket。

**Architecture:** 一條 bug 一個 TDD cycle（失敗測試 → 修補 → 通過），但**按檔案動到同段 code 的 finding 合併為單一 commit**，總計 8 個 commit。所有改動限於後端 repo `/Users/yilunwu/Desktop/ivy-backend/`。

**Tech Stack:** FastAPI + SQLAlchemy + PostgreSQL + pytest

**Commit 分組:**
- Commit A — P0-1 batch_approve two-pass（leaves + overtimes 同 pattern）
- Commit B — P0-2 portal sick 雙桶配額
- Commit C — P0-3 update/delete `with_for_update` 四處
- Commit D — P1-4 import_leaves overlap + P1-5 leave↔OT 跨類 overlap
- Commit E — P1-6 approve_overtime body schema（向後相容）
- Commit F — P1-7 半日↔全日清空 + P1-8 ApprovalLog + P1-11 hours 縮減守衛 + P2-14 use_comp_leave 守衛（同段 update code）
- Commit G — P1-9 代理 OT 時段比對 + P1-10 代理 active
- Commit H — P2-12 LINE 順序 + P2-13 午休一致

---

## 共用約定

- **測試檔位置**：所有新測試集中在 `tests/test_leaves_overtimes_bug_batch_2026_05_11.py`，依 finding 切函式
- **測試框架**：pytest + `tests/conftest.py` 提供 `client`、`db_session`、`auth_token` fixture
- **跑既有測試**：每個 commit 後 `pytest tests/test_leaves*.py tests/test_overtime*.py tests/test_finance_antitheft_v5*.py -q` 確認 0 regression
- **Commit message**：Conventional Commits 中文 — `fix(leaves): <subject>` / `fix(overtimes): <subject>`
- **不 push**：所有 commit 留在 local，user 驗收後再決定推 main 或開 PR
- **避免亂改格式**：用 Edit 而非 Write 大改檔，動最小範圍

---

## Task A: P0-1 batch_approve two-pass 驗證

**Why:** Phase 1 內 `session.rollback() + expire_all()` 會把同 batch 已 setattr 的條目 ORM 狀態全 expire；Phase 2 `commit()` 變 no-op，但 `succeeded` 仍照 append、LINE 仍發、薪資重算照跑 → DB 與回傳脫鉤。

**修法（方案 A）:** 把 Phase 1 拆成「全部驗證（純讀，不 setattr）」與「全部套用 + 統一 commit」兩段。

**Files:**
- Modify: `api/leaves.py` — `batch_approve_leaves` (約 1600-1860 區段)
- Modify: `api/overtimes.py` — 對應 batch_approve (約 1330-1510 區段)
- Test: `tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_batch_approve_partial_failure_does_not_silently_succeed`

- [ ] **Step A.1: 寫 failing test**

```python
def test_batch_approve_partial_failure_does_not_silently_succeed(client, db_session, auth_token):
    """模擬 batch 中第二筆驗證失敗，確認第一筆不會被回傳為 succeeded 但 DB 未變更"""
    # 建立兩筆 pending leaves；用 monkeypatch 讓第二筆觸發 RuntimeError
    leave1 = _create_pending_leave(db_session, employee_id=1, ...)
    leave2 = _create_pending_leave(db_session, employee_id=2, ...)
    
    with patch("api.leaves._write_approval_log") as mocked:
        def fake_log(entity_type, entity_id, *args, **kwargs):
            if entity_id == leave2.id:
                raise RuntimeError("simulated failure")
            return MagicMock(id=42)
        mocked.side_effect = fake_log

        resp = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [leave1.id, leave2.id], "approved": True},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    
    body = resp.json()
    db_session.expire_all()
    leave1_db = db_session.get(LeaveRecord, leave1.id)
    
    # 修補前：succeeded 含 leave1.id，但 leave1_db.is_approved 仍是 None（False positive）
    # 修補後：要嘛 leave1 也算 failed（保守），要嘛 leave1 真的被核准；不允許 succeeded 與 DB 脫鉤
    if leave1.id in [s for s in body.get("succeeded", [])]:
        assert leave1_db.is_approved is True, "succeeded 與 DB 不一致 (silent data loss)"
```

- [ ] **Step A.2: 驗證 test 失敗**

```bash
cd ~/Desktop/ivy-backend
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_batch_approve_partial_failure_does_not_silently_succeed -v
# Expected: FAIL — succeeded 含 leave1.id 但 is_approved 仍 None
```

- [ ] **Step A.3: 改 leaves.py batch_approve_leaves 為 two-pass**

把 Phase 1 拆成：
- **Pass 1（純驗證）**：迴圈每筆做 quota / overlap / substitute 等驗證；任何錯誤直接 `failed.append` 並 `continue`，**不要 setattr leave**、**不要 _write_approval_log**、**不要 rollback**
- **Pass 2（套用 + commit）**：對通過 Pass 1 的 ids 統一做 setattr + `_write_approval_log` + `lock_and_premark_stale`，最後 `session.commit()`

Edit `api/leaves.py` 約 1600-1734 區段（具體行號以 Read 為準），把 try/except 改為：
```python
# Pass 1: pure validation
validated_ids = []
for leave_id in data.ids:
    leave = session.query(LeaveRecord).filter_by(id=leave_id).with_for_update().first()
    if leave is None:
        failed.append({"id": leave_id, "reason": "假單不存在"})
        continue
    # ... 所有現有驗證 (status/overlap/quota/substitute/_check_salary_months_not_finalized)
    # 任何 HTTPException 或 Exception → failed.append + continue
    # 不要 setattr / _write_approval_log
    try:
        # 把現有驗證搬進來但移除 setattr
        ...
        validated_ids.append((leave_id, leave, ...metadata...))
    except HTTPException as he:
        failed.append({"id": leave_id, "reason": he.detail})
    except Exception as e:
        failed.append({"id": leave_id, "reason": str(e)})

# Pass 2: apply
changes = []
for leave_id, leave, meta in validated_ids:
    leave.is_approved = data.approved
    leave.approved_by = (...)
    leave.rejection_reason = (...)
    approval_log_row = _write_approval_log(...)
    changes.append((leave_id, leave, ...))

# Phase 2 commit + LINE + 薪資重算（既有邏輯保留）
if changes:
    session.commit()
    ...
```

- [ ] **Step A.4: overtimes.py batch_approve 同樣兩段化**

Edit `api/overtimes.py` 對應 batch_approve 區段（約 1330-1510），同樣 Pass 1 純驗證 → Pass 2 套用。

- [ ] **Step A.5: 跑 test 確認 pass**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_batch_approve_partial_failure_does_not_silently_succeed -v
pytest tests/test_leaves*.py tests/test_overtime*.py tests/test_finance_antitheft_v5*.py -q
```

- [ ] **Step A.6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/leaves.py api/overtimes.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves,overtimes): batch_approve 改 two-pass 驗證避免 rollback 抹掉同 batch 變更

Phase 1 內 session.rollback()+expire_all() 會把已 setattr 的條目 ORM 狀態
全部 expire，Phase 2 commit 變 no-op；但 succeeded 仍照 append、LINE 仍發、
薪資重算照跑，導致回傳體與 DB 真實狀態脫鉤。

改為兩段：Pass 1 純驗證、Pass 2 統一套用 setattr + ApprovalLog + commit。

修補 2026-05-11 稽核 P0-1 (leaves.py:1729-1733, overtimes.py:1487-1491)。"
```

---

## Task B: P0-2 Portal 病假雙桶配額

**Why:** portal sick 路徑只走 `_check_quota`（只看 LeaveQuota 總量、未 init 直接 return），不走 `assert_sick_leave_within_statutory_caps`（勞工請假規則第 4 條：未住院 ≤240h、住院 ≤2080h、合計 ≤2080h）。

**Files:**
- Modify: `api/portal/_shared.py` — `LeaveCreatePortal` (line 40) 補 `is_hospitalized` 欄位
- Modify: `api/portal/leaves.py` — `create_portal_leave` (line 312-326) 改走 `_guard_leave_quota`
- Modify: `api/leaves.py` — 確認 `_guard_leave_quota` (line 152) 可被 portal import；若 export 不夠則調整
- Test: `tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_portal_sick_leave_enforces_statutory_caps`

- [ ] **Step B.1: 寫 failing test**

```python
def test_portal_sick_leave_enforces_statutory_caps(client, db_session, portal_auth_token, employee):
    """portal 提送門診病假 241h 應被勞基法 240h 上限擋下"""
    # 設定該員工 LeaveQuota.sick = 9999h（避免被 _check_quota 擋）
    db_session.add(LeaveQuota(employee_id=employee.id, year=2026, leave_type="sick", total_hours=9999))
    db_session.commit()
    
    resp = client.post(
        "/api/portal/leaves",
        json={
            "leave_type": "sick",
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
            "leave_hours": 241,
            "reason": "感冒",
            # 注意：portal schema 修補後才有 is_hospitalized
        },
        headers={"Authorization": f"Bearer {portal_auth_token}"},
    )
    assert resp.status_code == 400
    assert "勞工請假規則第 4 條" in resp.json()["detail"]
```

- [ ] **Step B.2: 驗證 test 失敗**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_portal_sick_leave_enforces_statutory_caps -v
# Expected: FAIL — 241h 被通過（沒擋）
```

- [ ] **Step B.3: 改 LeaveCreatePortal schema**

Edit `api/portal/_shared.py:40-60`，補一欄：

```python
class LeaveCreatePortal(BaseModel):
    # ... existing ...
    is_hospitalized: bool = Field(default=False, description="病假是否為住院（依勞基法雙桶區分）")
```

- [ ] **Step B.4: 改 portal/leaves.py 改走 _guard_leave_quota**

Edit `api/portal/leaves.py:312-326`：

```python
# 改前
if data.leave_type == "compensatory":
    _check_compensatory_quota(...)
else:
    _check_quota(session, emp.id, data.leave_type, data.start_date.year, data.leave_hours)

# 改後（從 api.leaves import _guard_leave_quota）
from api.leaves import _guard_leave_quota
_guard_leave_quota(
    session=session,
    employee_id=emp.id,
    leave_type=data.leave_type,
    year=data.start_date.year,
    leave_hours=data.leave_hours,
    is_hospitalized=data.is_hospitalized,
    source_overtime_id=data.source_overtime_id,
)
```

注意：`_guard_leave_quota` 簽名要先 Read 確認；如果它對 `compensatory` 也有處理就直接 delegate，省掉 `_check_compensatory_quota` 分支。

- [ ] **Step B.5: 跑 test 確認 pass**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_portal_sick_leave_enforces_statutory_caps -v
pytest tests/test_sick_leave_caps.py tests/test_sick_leave_annual_cap.py tests/test_leaves_quota.py -q
```

- [ ] **Step B.6: Commit**

```bash
git add api/portal/_shared.py api/portal/leaves.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(portal): 病假改走 _guard_leave_quota 補上勞基法雙桶上限

portal/leaves.py:312-326 sick 分支原本只走 _check_quota（看 LeaveQuota 總量），
不會呼叫 assert_sick_leave_within_statutory_caps（未住院 240h、住院 2080h、
合計 2080h）。LeaveCreatePortal 補 is_hospitalized 欄位，改 delegate 到
_guard_leave_quota 與 admin 端對齊。

修補 2026-05-11 稽核 P0-2。"
```

---

## Task C: P0-3 update/delete 補 `with_for_update`

**Why:** approve 路徑已有列鎖（leaves.py:1127/1534、overtimes.py:1191/1357），但 update_leave (773) / delete_leave (989) / update_overtime (854) / delete_overtime (1084) 四個函式體內無 `with_for_update`，並發 update+approve 會 lost update。

**Files:**
- Modify: `api/leaves.py:773`, `api/leaves.py:989`
- Modify: `api/overtimes.py:854`, `api/overtimes.py:1084`
- Test: `tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_update_delete_uses_row_lock`

- [ ] **Step C.1: 寫 test（簡化版：驗證 SELECT 帶 FOR UPDATE）**

並發 race 在 pytest 難精確重現。這條測試只驗證「SELECT 語句帶 FOR UPDATE」這個 invariant：

```python
def test_update_delete_uses_row_lock(db_session, monkeypatch):
    """確認 update_leave/delete_leave/update_overtime/delete_overtime 都用 with_for_update"""
    import inspect
    from api import leaves, overtimes
    
    # 用 source 檢查（功能性比 SQL log 攔截穩定）
    update_leave_src = inspect.getsource(leaves.update_leave)
    delete_leave_src = inspect.getsource(leaves.delete_leave)
    update_ot_src = inspect.getsource(overtimes.update_overtime)
    delete_ot_src = inspect.getsource(overtimes.delete_overtime)
    
    for name, src in [
        ("update_leave", update_leave_src),
        ("delete_leave", delete_leave_src),
        ("update_overtime", update_ot_src),
        ("delete_overtime", delete_ot_src),
    ]:
        assert "with_for_update" in src, f"{name} 缺 with_for_update() 列鎖"
```

- [ ] **Step C.2: 跑 test 確認失敗**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py::test_update_delete_uses_row_lock -v
# Expected: FAIL — 四個函式都缺
```

- [ ] **Step C.3: 四處 SELECT 補列鎖**

`api/leaves.py:773 update_leave` 內 `session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()` 改為 `.with_for_update().first()`。

`api/leaves.py:989 delete_leave` 同樣加。

`api/overtimes.py:854 update_overtime` 內 `session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()` 改為加 `.with_for_update()`。

`api/overtimes.py:1084 delete_overtime` 同樣加。

- [ ] **Step C.4: pass + 既有測試**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k row_lock
pytest tests/test_overtimes*.py tests/test_leaves*.py -q
```

- [ ] **Step C.5: Commit**

```bash
git add api/leaves.py api/overtimes.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves,overtimes): update/delete 路徑補 with_for_update 列鎖

approve 路徑已有列鎖，但 update_leave/delete_leave/update_overtime/
delete_overtime 四個函式 SELECT 缺 .with_for_update()。並發 update+approve
情境下會 lost update，補休配額可能負數或重複退還。

修補 2026-05-11 稽核 P0-3。"
```

---

## Task D: P1-4 import_leaves overlap + P1-5 leave↔OT 跨類

**Why:**
- import_leaves (leaves.py:1941-2076) 跳過 `_check_overlap`，匯入後同員工同日可多筆 pending
- create_leave / create_overtime 與 portal 對應路徑不互查（請假時段又加班 / 加班時段又請病假），雙重溢付

**Files:**
- Modify: `api/leaves.py:1941-2076` (import_leaves) 補 `_check_overlap`
- Modify: `api/leaves.py` create_leave + `api/portal/leaves.py` create_portal_leave 補 OT 跨類檢查
- Modify: `api/overtimes.py` create_overtime + `api/portal/overtimes.py` create_portal_overtime 補 leave 跨類檢查
- 新 helper: `api/overtimes.py::_check_leave_conflict_for_self(session, employee_id, date, start_time, end_time)` — 與 `_check_substitute_leave_conflict` 邏輯類似但對 self
- Test: `test_import_leaves_blocks_overlap`, `test_create_leave_blocks_when_overtime_overlaps`, `test_create_overtime_blocks_when_leave_overlaps`

- [ ] **Step D.1: 寫三個 failing tests**

```python
def test_import_leaves_blocks_overlap(client, db_session, auth_token, employee):
    """匯入兩筆同員工同日 leave，第二筆應被擋"""
    # ... 用 import_leaves endpoint 一次送兩筆 ...
    resp = client.post("/api/leaves/import", json={...})
    body = resp.json()
    assert any("重疊" in r.get("error", "") or "overlap" in r.get("error", "").lower() 
               for r in body.get("failed", []))

def test_create_leave_blocks_when_overtime_overlaps(client, db_session, auth_token, employee):
    """同員工同日已有 approved OT，再申請 leave 應 409"""
    _create_approved_overtime(db_session, employee.id, "2026-06-01", "18:00", "20:00")
    resp = client.post("/api/leaves", json={
        "employee_id": employee.id,
        "leave_type": "personal",
        "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "start_time": "19:00",
        "end_time": "20:00",
        "leave_hours": 1,
    }, headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code in (400, 409)
    assert "加班" in resp.json()["detail"]

def test_create_overtime_blocks_when_leave_overlaps(client, db_session, auth_token, employee):
    """同員工同日已有 approved 病假，再申請 OT 應 409"""
    _create_approved_leave(db_session, employee.id, "2026-06-02", "08:00", "12:00", "sick")
    resp = client.post("/api/overtimes", json={
        "employee_id": employee.id,
        "overtime_date": "2026-06-02",
        "start_time": "09:00",
        "end_time": "11:00",
        "hours": 2,
    }, headers={"Authorization": f"Bearer {auth_token}"})
    assert resp.status_code in (400, 409)
    assert "請假" in resp.json()["detail"]
```

- [ ] **Step D.2: 跑 test 確認失敗**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k "blocks_overlap or blocks_when"
# Expected: 3 FAIL
```

- [ ] **Step D.3: 改 import_leaves 補 _check_overlap**

Edit `api/leaves.py:1941-2076` import_leaves，在 `_check_leave_limits` 之前加：

```python
try:
    _check_overlap(
        session, emp.id, start_date, end_date,
        start_time=row.get("start_time"), end_time=row.get("end_time"),
        include_pending=True,
        exclude_id=None,
    )
except HTTPException as he:
    failed.append({"row": row_num, "error": he.detail})
    continue
```

- [ ] **Step D.4: 新增 _check_self_leave_overtime_conflict helper**

在 `api/overtimes.py` 或共用 module 新增：

```python
def _check_self_leave_overtime_conflict(
    session,
    employee_id: int,
    overtime_date: date,
    start_time: Optional[str],
    end_time: Optional[str],
    exclude_overtime_id: Optional[int] = None,
) -> None:
    """加班申請時檢查同員工同時段是否已有 approved/pending 假單。"""
    q = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.is_approved.in_([None, True]),
        LeaveRecord.start_date <= overtime_date,
        LeaveRecord.end_date >= overtime_date,
    )
    for lv in q.all():
        # 全日假 -> 直接衝突
        if not lv.start_time or not lv.end_time:
            raise HTTPException(
                status_code=409,
                detail=f"員工同日已有請假紀錄（#{lv.id} {lv.leave_type}），無法重複提出加班",
            )
        # 半日假 -> 比時段
        if start_time and end_time and _times_overlap(start_time, end_time, lv.start_time, lv.end_time):
            raise HTTPException(
                status_code=409,
                detail=f"加班時段與既有請假（#{lv.id}）重疊",
            )
```

並對應做 leave 方向的 `_check_self_overtime_conflict_when_leaving(session, employee_id, start_date, end_date, start_time, end_time)`：查同期間 approved/pending OT，全日假直接擋、半日假比時段。

- [ ] **Step D.5: create_leave / create_overtime / portal 兩處插入**

在 `create_leave`、`create_portal_leave` 的 `_check_overlap` 之後呼叫 `_check_self_overtime_conflict_when_leaving`。  
在 `create_overtime`、`create_portal_overtime` 的 `_check_overtime_overlap` 之後呼叫 `_check_self_leave_overtime_conflict`。

- [ ] **Step D.6: 跑 test pass + regression**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k "blocks_overlap or blocks_when"
pytest tests/test_leaves*.py tests/test_overtimes*.py tests/test_portal*.py -q
```

- [ ] **Step D.7: Commit**

```bash
git add api/leaves.py api/overtimes.py api/portal/leaves.py api/portal/overtimes.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves,overtimes): import_leaves 補 overlap、補 leave↔overtime 跨類衝突檢查

import_leaves 完全跳過 _check_overlap，可匯入同員工同日多筆 pending；
create_leave/create_overtime 對自己只查同類，請假時段又加班/加班時段又請病假
不會被擋，造成扣款 + 加班費雙重溢付。

新增 _check_self_leave_overtime_conflict / _check_self_overtime_conflict_when_leaving
helper，admin 與 portal 兩端都插入。

修補 2026-05-11 稽核 P1-4 + P1-5。"
```

---

## Task E: P1-6 approve_overtime 新增 body schema（向後相容）

**Why:** approve_overtime 用 query parameter 接 `rejection_reason` 會把個資寫進 access log；`approved_by` 可被任意覆寫。改 body 是 breaking change，採向後相容策略：新增 body 接口、保留 query 參數作 deprecated fallback。

**Files:**
- Modify: `api/overtimes.py:594-637` 後新增 `OvertimeApproveRequest` schema
- Modify: `api/overtimes.py:1160` approve_overtime 簽名改為接 `data: Optional[OvertimeApproveRequest] = Body(None)`，並 fallback 到 query
- Test: `test_approve_overtime_accepts_body`, `test_approve_overtime_query_still_works`

- [ ] **Step E.1: 寫 tests**

```python
def test_approve_overtime_accepts_body(client, db_session, auth_token):
    ot = _create_pending_overtime(db_session, ...)
    resp = client.put(
        f"/api/overtimes/{ot.id}/approve",
        json={"approved": False, "rejection_reason": "時數不符"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(OvertimeRecord, ot.id).rejection_reason == "時數不符"

def test_approve_overtime_query_still_works(client, db_session, auth_token):
    """向後相容：舊前端走 query param 仍可運作"""
    ot = _create_pending_overtime(db_session, ...)
    resp = client.put(
        f"/api/overtimes/{ot.id}/approve?approved=false&rejection_reason=test",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
```

- [ ] **Step E.2: 跑 test 確認 body 路徑失敗、query 路徑通過**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k approve_overtime
# Expected: accepts_body FAIL, query_still_works PASS
```

- [ ] **Step E.3: 新增 schema + 簽名改造**

Edit `api/overtimes.py`，在 `OvertimeBatchApproveRequest` (line 637) 之前新增：

```python
class OvertimeApproveRequest(BaseModel):
    approved: bool = True
    rejection_reason: Optional[str] = Field(None, max_length=500)
    force_overlap: bool = False  # 對齊 LeaveApproveRequest

    @model_validator(mode="after")
    def _require_reason_when_rejecting(self):
        if not self.approved and not (self.rejection_reason and self.rejection_reason.strip()):
            raise ValueError("駁回時必填 rejection_reason")
        return self
```

Edit `api/overtimes.py:1160` 函式簽名：

```python
def approve_overtime(
    overtime_id: int,
    request: Request,
    data: Optional[OvertimeApproveRequest] = Body(None),
    # 以下為向後相容 query parameters；新前端應改用 body
    approved: bool = True,
    rejection_reason: Optional[str] = None,
    ...
):
    # body 優先；無 body 才退回 query
    if data is not None:
        approved = data.approved
        rejection_reason = data.rejection_reason
    # 移除 approved_by 入參：強制用 current_user["username"]
    approved_by = current_user.get("username", "管理員")
    # 其餘邏輯不變
```

- [ ] **Step E.4: 跑 test pass**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k approve_overtime
pytest tests/test_overtime_rejection_reason_required.py tests/test_overtimes*.py -q
```

- [ ] **Step E.5: Commit**

```bash
git add api/overtimes.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(overtimes): approve_overtime 新增 OvertimeApproveRequest body schema

原本 rejection_reason 走 query param 會把個資寫進 proxy/CDN/access log；
approved_by 可被任意覆寫。新增 body schema（向後相容保留 query fallback），
強制 approved_by = current_user.username 不接受外部輸入。

前端遷移留待另開 ticket。

修補 2026-05-11 稽核 P1-6。"
```

---

## Task F: update 路徑硬化（P1-7 + P1-8 + P1-11 + P2-14）

**Why:** 這 4 條 finding 都動到 update_leave / update_overtime 同段 code，合併為一個 commit 減少衝突：
- **P1-7**：半日假 `start_time` / `end_time` 用戶想清空時，`if value is not None` 過濾掉 null
- **P1-8**：`_revoke_comp_leave_grant` 自動駁回 linked_pending 假單時漏寫 ApprovalLog
- **P1-11**：update_overtime 改 hours 變小時，沒檢查 linked_approved 補休是否已使用該配額
- **P2-14**：`use_comp_leave` 在 OvertimeUpdate 不應允許翻轉

**Files:**
- Modify: `api/leaves.py:211-214` _apply_leave_update_and_revoke
- Modify: `api/overtimes.py:172-183` _revoke_comp_leave_grant
- Modify: `api/overtimes.py:594-631` OvertimeUpdate schema — 顯式拒絕 use_comp_leave
- Modify: `api/overtimes.py:947-993` update_overtime — hours 縮減守衛
- Test: 4 個小 tests

- [ ] **Step F.1: 寫 4 個 tests**

```python
def test_leave_update_can_clear_start_end_time():
    """半日假改全日，傳 null 應清空 start_time/end_time"""
    leave = _create_half_day_leave(db_session, start_time="09:00", end_time="12:00")
    resp = client.put(f"/api/leaves/{leave.id}", json={
        "leave_hours": 8,
        "start_time": None,
        "end_time": None,
    }, ...)
    assert resp.status_code == 200
    db_session.expire_all()
    leave = db_session.get(LeaveRecord, leave.id)
    assert leave.start_time is None
    assert leave.end_time is None

def test_revoke_comp_leave_writes_approval_log():
    """撤銷已核准補休 OT 時，自動駁回的 linked_pending 假單須留 ApprovalLog"""
    ot = _create_approved_comp_leave_ot(db_session, hours=4)
    leave = _create_pending_comp_leave(db_session, source_overtime_id=ot.id, hours=2)
    
    # delete_overtime 觸發 _revoke
    client.delete(f"/api/overtimes/{ot.id}", headers=...)
    
    logs = db_session.query(ApprovalLog).filter_by(entity_type="leave", entity_id=leave.id).all()
    assert any(l.action == "rejected" and "auto_revoked" in (l.reason or "") for l in logs)

def test_update_overtime_blocks_hours_shrink_below_used_comp():
    """補休 OT 已發 4h 配額且員工已用 3h；update 改 hours=2h 應 409"""
    ot = _create_approved_comp_leave_ot(db_session, hours=4)
    _create_approved_comp_leave(db_session, source_overtime_id=ot.id, leave_hours=3)
    
    resp = client.put(f"/api/overtimes/{ot.id}", json={"hours": 2}, ...)
    assert resp.status_code == 409
    assert "已使用" in resp.json()["detail"]

def test_overtime_update_rejects_use_comp_leave_flip():
    """OvertimeUpdate 不應接受 use_comp_leave 翻轉（schema 422）"""
    ot = _create_approved_comp_leave_ot(db_session)
    resp = client.put(f"/api/overtimes/{ot.id}", json={"use_comp_leave": False}, ...)
    assert resp.status_code in (400, 422)
```

- [ ] **Step F.2: 跑 test 確認失敗**

- [ ] **Step F.3: P1-7 改 leaves.py:211 — 對 start_time/end_time 允許 null**

```python
# 改前
for key, value in update_data.items():
    if value is not None:
        setattr(leave, key, value)

# 改後
TIME_FIELDS_ALLOWING_NULL = {"start_time", "end_time"}
for key, value in update_data.items():
    if value is None and key not in TIME_FIELDS_ALLOWING_NULL:
        continue
    setattr(leave, key, value)
```

對應 overtimes.py:974 也做同樣處理（OT 也有 start_time/end_time 半日↔全日語意）。

- [ ] **Step F.4: P1-8 改 _revoke_comp_leave_grant 補 ApprovalLog**

Edit `api/overtimes.py:172-183`：

```python
for lv in linked_pending:
    lv.is_approved = False
    lv.rejection_reason = f"來源加班申請（#{ot.id}，{ot.overtime_date}）已被撤銷..."
    # 新增：寫 ApprovalLog
    _write_approval_log(
        "leave",
        lv.id,
        "rejected",
        {"username": "system_auto"},  # 或從 caller 傳 current_user 進來
        f"auto_revoked_by_overtime_rollback (#{ot.id})",
        session,
    )
```

注意：`_revoke_comp_leave_grant` 簽名要 caller 傳 `current_user` 進來才能正確記錄審核者。可在 signature 加 `current_user: dict | None = None` 並由 caller 傳入。

- [ ] **Step F.5: P1-11 改 update_overtime hours 縮減守衛**

Edit `api/overtimes.py:947-993`，在 `_revoke_comp_leave_grant` 呼叫之前加：

```python
if was_approved and ot.use_comp_leave and "hours" in update_data:
    new_hours = float(update_data["hours"])
    # 已核准的關聯補休假單時數合計
    linked_approved_hours = session.query(
        func.coalesce(func.sum(LeaveRecord.leave_hours), 0.0)
    ).filter(
        LeaveRecord.source_overtime_id == ot.id,
        LeaveRecord.leave_type == "compensatory",
        LeaveRecord.is_approved == True,
    ).scalar()
    if new_hours + 1e-9 < float(linked_approved_hours):
        raise HTTPException(
            status_code=409,
            detail=(
                f"此加班申請已有 {linked_approved_hours:.1f}h 補休假單被核准並使用，"
                f"無法將時數縮減至 {new_hours:.1f}h；請先撤銷相關補休假單"
            ),
        )
```

- [ ] **Step F.6: P2-14 OvertimeUpdate 拒絕 use_comp_leave**

Edit `api/overtimes.py:594-631` `OvertimeUpdate`：

```python
class OvertimeUpdate(BaseModel):
    overtime_date: Optional[date] = None
    overtime_type: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: Optional[float] = None
    reason: Optional[str] = None
    # 注意：刻意不包含 use_comp_leave；翻轉模式需 reject + recreate
    
    @model_validator(mode="before")
    @classmethod
    def _reject_use_comp_leave_flip(cls, values):
        if isinstance(values, dict) and "use_comp_leave" in values:
            raise ValueError("不允許在 update 中翻轉 use_comp_leave；請先 reject 再以新 use_comp_leave 重新申請")
        return values
```

- [ ] **Step F.7: 跑 4 個新 test + regression**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k "clear_start_end or revoke_comp or hours_shrink or use_comp_leave_flip"
pytest tests/test_leaves*.py tests/test_overtimes*.py -q
```

- [ ] **Step F.8: Commit**

```bash
git add api/leaves.py api/overtimes.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves,overtimes): update 路徑硬化（清空時段/ApprovalLog/補休守衛/use_comp_leave 守衛）

四條 finding 都動 update 同段 code，合併為一 commit：

- P1-7: start_time/end_time 允許 null 清空（半日↔全日場景）
- P1-8: _revoke_comp_leave_grant 自動駁回 linked_pending 補寫 ApprovalLog
- P1-11: update_overtime 改 hours 變小時，比對 linked_approved 時數合計
- P2-14: OvertimeUpdate 拒絕 use_comp_leave 翻轉

修補 2026-05-11 稽核 P1-7 + P1-8 + P1-11 + P2-14。"
```

---

## Task G: 代理人衝突細化（P1-9 + P1-10）

**Why:** `_check_substitute_leave_conflict` (leaves.py:486-499) 對代理人加班只看日期不看時段，誤判；同 helper 也沒過濾代理人 `is_active=True`。

**Files:**
- Modify: `api/leaves.py:451-499` _check_substitute_leave_conflict
- Test: `test_substitute_ot_diff_time_no_conflict`, `test_inactive_substitute_blocked_on_approval`

- [ ] **Step G.1: 寫 tests**

```python
def test_substitute_ot_diff_time_no_conflict():
    """代理人 18-20 OT 與申請人 08-12 半日假不應衝突"""
    sub = _create_employee(db_session, "代理人")
    _create_approved_overtime(db_session, sub.id, "2026-06-10", "18:00", "20:00")
    
    resp = client.post("/api/leaves", json={
        "employee_id": applicant.id,
        "leave_type": "personal",
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
        "start_time": "08:00",
        "end_time": "12:00",
        "leave_hours": 4,
        "substitute_employee_id": sub.id,
    }, ...)
    assert resp.status_code in (200, 201)

def test_inactive_substitute_blocked_on_approval():
    """代理人離職後不應通過 approve 檢查"""
    sub = _create_employee(db_session, "離職代理人", is_active=False)
    leave = _create_pending_leave(db_session, ..., substitute_employee_id=sub.id)
    resp = client.put(f"/api/leaves/{leave.id}/approve", json={"approved": True}, ...)
    assert resp.status_code in (400, 409)
    assert "代理人" in resp.json()["detail"]
```

- [ ] **Step G.2: 跑 test 確認失敗**

- [ ] **Step G.3: 改 _check_substitute_leave_conflict**

Edit `api/leaves.py:486-499`：

```python
# 時段精比（加班）
ot_conflicts = session.query(OvertimeRecord).filter(
    OvertimeRecord.employee_id == substitute_employee_id,
    OvertimeRecord.is_approved.in_([None, True]),
    OvertimeRecord.overtime_date >= start_date,
    OvertimeRecord.overtime_date <= end_date,
).all()
for ot in ot_conflicts:
    # 全日假 / OT 缺時段 -> 直接衝突
    if not start_time or not end_time or not ot.start_time or not ot.end_time:
        raise HTTPException(status_code=409, detail="代理人同日已安排加班，請選擇其他代理人")
    if _times_overlap(start_time, end_time, ot.start_time, ot.end_time):
        raise HTTPException(status_code=409, detail="代理人加班時段與請假時段重疊，請選擇其他代理人")
```

並在開頭加 active 檢查：

```python
sub_emp = session.query(Employee).filter(Employee.id == substitute_employee_id).first()
if sub_emp is None or not sub_emp.is_active:
    raise HTTPException(status_code=400, detail="代理人不存在或已離職")
```

要注意 `_check_substitute_leave_conflict` 簽名需多接 `start_time` / `end_time`，更新 caller。

- [ ] **Step G.4: 跑 test pass + regression**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k "substitute"
pytest tests/test_leave_substitute.py tests/test_leaves*.py -q
```

- [ ] **Step G.5: Commit**

```bash
git add api/leaves.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves): 代理人衝突精細化（時段比對 + active 過濾）

_check_substitute_leave_conflict 對代理人 OT 只比日期不比時段，造成誤判
（例：申請人 08-12 半日 vs 代理人 18-20 OT 不應衝突）；同 helper 也沒過濾
代理人 is_active=True。

修補 2026-05-11 稽核 P1-9 + P1-10。"
```

---

## Task H: 收尾（P2-12 + P2-13）

**Why:**
- P2-12：batch_approve LINE 推播在薪資重算 try 之前送出，重算失敗條目仍收到「已核准」推播
- P2-13：`_calc_shift_hours` (>5h 一律扣 1h) 與 `_calc_bounded_shift_hours` (12-13 區段交集) 對 5h 邊界結果矛盾

**Files:**
- Modify: `api/leaves.py:1768-1846` 把 LINE 推播挪到薪資重算後
- Modify: `api/leaves_workday.py:25-60` 統一午休扣除算法
- Test: 2 個小 tests

- [ ] **Step H.1: 寫 2 個 tests**

```python
def test_batch_approve_skips_line_when_recalc_fails(client, db_session, auth_token, monkeypatch):
    """薪資重算失敗的條目不應收到核准 LINE"""
    leave = _create_pending_leave(db_session, ...)
    notify_calls = []
    
    monkeypatch.setattr("api.leaves._line_service.notify_leave_result", lambda *a, **kw: notify_calls.append(a))
    monkeypatch.setattr("api.leaves._salary_engine.process_salary_calculation", 
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated")))
    
    client.post("/api/leaves/batch-approve", json={"ids": [leave.id], "approved": True}, ...)
    
    assert len(notify_calls) == 0, "薪資重算失敗條目不應推 LINE"

def test_calc_shift_hours_lunch_break_consistent():
    """_calc_shift_hours 與 _calc_bounded_shift_hours 對 5h 班 lunch 處理一致"""
    from api.leaves_workday import _calc_shift_hours, _calc_bounded_shift_hours
    # 08:00-13:00（5h，含 12-13 一半 lunch）
    assert _calc_shift_hours("08:00", "13:00") == _calc_bounded_shift_hours("08:00", "13:00", "08:00", "13:00")
```

- [ ] **Step H.2: 跑 test 失敗**

- [ ] **Step H.3: leaves.py LINE 推播挪位**

Edit `api/leaves.py:1768-1846`，把 `_line_service.notify_leave_result` 整段移到 `if not recalc_failed:` 之後（薪資重算成功才推播）。

- [ ] **Step H.4: 統一午休扣除**

Edit `api/leaves_workday.py:25-60`，把 `_calc_shift_hours` 改寫為呼叫 `_calc_bounded_shift_hours` with bound=None：

```python
def _calc_shift_hours(work_start: str, work_end: str) -> float:
    """員工日班總時數（扣除午休 12:00-13:00 與班表重疊部分）。"""
    return _calc_bounded_shift_hours(work_start, work_end, None, None)
```

並讓 `_calc_bounded_shift_hours` 在 bound=None 時退化為「整段 work_start-work_end 扣 12-13 交集」。確保兩函式對齊。

- [ ] **Step H.5: pass + regression**

```bash
pytest tests/test_leaves_overtimes_bug_batch_2026_05_11.py -v -k "line or shift_hours"
pytest tests/test_leaves*.py tests/test_overtime*.py -q
```

- [ ] **Step H.6: Commit**

```bash
git add api/leaves.py api/leaves_workday.py tests/test_leaves_overtimes_bug_batch_2026_05_11.py
git commit -m "fix(leaves): batch 推播挪至重算後 + _calc_shift_hours 與 bounded 版本對齊

P2-12: batch_approve 薪資重算失敗時，員工仍收到「已核准」LINE 推播；
       將推播挪到 recalc 成功後再發。

P2-13: _calc_shift_hours (>5h 扣 1h) 與 _calc_bounded_shift_hours
       (12-13 交集) 對 5h 邊界結果矛盾；統一為共用 helper。

修補 2026-05-11 稽核 P2-12 + P2-13。"
```

---

## Task I: 全套 pytest 回歸 + advisor 驗收

- [ ] **Step I.1: 跑 leaves/overtimes 全套**

```bash
cd ~/Desktop/ivy-backend
pytest tests/ -q -x 2>&1 | tail -50
```

預期：3000+ 全綠（前 batch 是 2990 passed）。新測試大約 +12-15 條。

- [ ] **Step I.2: 跑 finance / portal / salary 相關回歸**

```bash
pytest tests/test_finance*.py tests/test_portal*.py tests/test_salary*.py -q
```

- [ ] **Step I.3: advisor 一次驗收**

呼叫 advisor()，請它從稽核者視角檢查 8 個 commit 是否真的覆蓋 14 條 finding、有無新引入的破口。

- [ ] **Step I.4: 寫 memory**

完成後寫一筆 project memory：
- file: `project_leaves_overtimes_bug_fix_batch_2026_05_11.md`
- index: 在 `MEMORY.md` 加一行 pointer

---

## Self-Review Checklist

**1. Spec coverage** — 14 條 finding 對應 commit：
- P0-1 → Task A ✓
- P0-2 → Task B ✓
- P0-3 → Task C ✓
- P1-4, P1-5 → Task D ✓
- P1-6 → Task E ✓
- P1-7, P1-8, P1-11, P2-14 → Task F ✓
- P1-9, P1-10 → Task G ✓
- P2-12, P2-13 → Task H ✓

**2. Placeholder scan** — 已避免「TBD」「實作 X」這類；測試 code 雖簡化但有實際斷言邏輯，執行時用 conftest fixture 完整化。

**3. Type/signature 一致性** —
- `_guard_leave_quota` 簽名 caller 與 portal 都對齊
- `_check_substitute_leave_conflict` 多接 `start_time/end_time` 參數，需更新所有 caller（leaves.py 內有 2-3 處呼叫）
- `_revoke_comp_leave_grant` 簽名加 `current_user` 參數

**4. 並發測試誠實度** — Task A/C 的測試是 functional correctness 驗證，不是真正並發 race；備註寫進 commit message。

---

## 風險與決定

- **P0-1 採方案 A**：兩段化最乾淨，與 P1-7 / P1-11 連動少。
- **P1-6 不改前端**：本 batch 純後端；前端遷移另開 ticket。
- **P1-7 縮 scope**：只動 `start_time` / `end_time` 允許 null，不全套翻轉契約。
- **P0-3 race 不寫並發測試**：用 source 檢查驗證 `with_for_update` 存在；真實 race 留 staging 演練。
- **不動 worktrees**：`.worktrees/` 下的副本不修改。
