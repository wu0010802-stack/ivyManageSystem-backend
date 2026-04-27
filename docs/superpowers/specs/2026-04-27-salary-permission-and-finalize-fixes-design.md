# 薪資模組 P1 漏洞修復設計（2026-04-27）

## 範圍

本次修復涵蓋三個 P1 缺陷:

1. **Bug #6** — 薪資快照 / simulate / audit-log 五個端點漏 self-check,SALARY_READ 持有者可讀任意員工資料
2. **Bug #1 + #4** — 批次重算單筆失敗 / 假單&加班審核後薪資重算失敗時,SalaryRecord 仍可被封存
3. **Bug #5** — 批次加班審核 Phase 1 失敗 rollback 後,Phase 2 仍把已撤回的 ot_id 回報為 succeeded

---

## Bug #6 — 漏權限守衛

### 現況

`api/salary.py:241` `_enforce_self_or_full_salary` 規定非 admin/hr 即使持 `SALARY_READ` 也只能查本人。`get_salary_breakdown` (L1058)、`get_salary_record` (L1156)、`update_salary_record` (L1193)、`get_employee_salary_history` (L1266) 都已套用,但下列 5 個端點未套:

| 端點 | 行號 | 漏洞 |
|---|---|---|
| `list_salary_snapshots` | L1649 | 任意 employee_id 即可列出他人快照清單 |
| `get_salary_snapshot` | L1663 | snapshot_id 可拿到他人完整快照欄位 |
| `get_salary_snapshot_diff` | L1706 | snapshot_id 可拿他人 diff |
| `simulate_salary` | L1825 | req.employee_id 可試算他人薪資 |
| `get_salary_audit_log` | L999 | record_id 可拿他人薪資操作歷史 |

### 修法(全採方案 A)

**`list_salary_snapshots`**:取出 `viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)`;若非 None(即非 admin/hr),強制覆寫 `employee_id = viewer_employee_id`,再呼叫 service。

**`get_salary_snapshot` / `get_salary_snapshot_diff`**:先 query `SalarySnapshot.employee_id`(輕量查詢),`_enforce_self_or_full_salary(current_user, snap.employee_id)` 通過後才呼 service build detail / diff。

**`simulate_salary`**:在拿到 `emp` 後立刻 `_enforce_self_or_full_salary(current_user, req.employee_id)`,放在現有 `if not emp` 檢查之後。

**`get_salary_audit_log`**:現有程式 L1010 只做 `EXISTS`(`first()` 的回傳值未保留),改為 `record = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()`,然後 `_enforce_self_or_full_salary(current_user, record.employee_id)`。

### 測試

`tests/test_salary_snapshot_permission.py`(新檔):
- 一般 staff(SALARY_READ + non-admin)用本人 employee_id 呼 5 個端點 → 200
- 同 staff 用他人 employee_id / snapshot_id → 403
- admin / hr 角色不受限 → 200

---

## Bug #1 + #4 — 失敗薪資仍可封存

### 現況

| 路徑 | 問題行 |
|---|---|
| 批次重算 inner except | `engine.py:2879-2906` errors.append 後 commit 其他人的成功結果,失敗員工的 SalaryRecord 留下舊值 |
| 假單審核失敗降級 | `leaves.py:1196-1201` 只回 salary_warning,薪資未更新 |
| finalize 完整性檢查 | `api/salary.py:1486 _find_missing_salary_employees` 只檢查 row 存在 |

加班審核(`overtimes.py:1289-1291`)的薪資重算失敗也是同模式,本次一併處理。

### 修法(方案 A — needs_recalc 旗標)

#### Schema

`models/salary.py SalaryRecord` 新增:

```python
needs_recalc = Column(
    Boolean,
    nullable=False,
    server_default="false",
    default=False,
    comment="True 表示最後一次重算失敗或上游審核變動後未成功重算;封存時必須為 False",
)
```

Index:`Index("ix_salary_ym_needs_recalc", "salary_year", "salary_month", "needs_recalc")` 加速 finalize 完整性查詢。

#### Migration

`alembic/versions/20260427_h3d4e5f6g7h8_add_salary_needs_recalc.py`
- `down_revision = "g2c3d4e5f6g7"`
- upgrade:`ADD COLUMN needs_recalc BOOLEAN NOT NULL DEFAULT FALSE` + 建 index
- downgrade:DROP INDEX + DROP COLUMN

#### 共用 helper

新增 `services/salary/utils.py mark_salary_stale(session, emp_id, year, month) -> bool`:
- 查 `SalaryRecord` (emp, year, month)
- 若存在,set `needs_recalc = True`,回 True;否則 False
- 不 commit(由 caller 控制 transaction)

#### engine.py 批次重算 except 路徑(用 SAVEPOINT 取代 expire)

**為何不用 `session.expire()`:** `models/base.py:82` 的 `sessionmaker(bind=get_engine())` 未指定 `autoflush=False`,SQLAlchemy 預設 `autoflush=True`。後續員工迭代中任何 query(holiday/classroom/period_records 等)都可能觸發 autoflush,把失敗員工尚未 expire 的 dirty UPDATE 送進 transaction;之後 expire() 雖然 reload in-memory,但 SQL-level 已寫入,batch commit 時失敗員工的 record 會出現「部分新值 + needs_recalc=True」,不符合「保留舊值」的語意。

**改用 SAVEPOINT** 在每個 emp iteration 開 nested transaction:

```python
for emp in employees:
    sp = session.begin_nested()  # SAVEPOINT
    try:
        # ... 既有計算與 _fill_salary_record(salary_record, breakdown, self) ...
        sp.commit()  # RELEASE SAVEPOINT
        results.append((emp, breakdown))
    except Exception as e:
        sp.rollback()  # ROLLBACK TO SAVEPOINT — 撤回此員工所有 in-memory 與 SQL-level 修改
        logger.error("薪資計算失敗 員工=%s(id=%d): %s", emp.name, emp.id, e, exc_info=True)
        errors.append({"employee_id": emp.id, "employee_name": emp.name, "error": str(e)})
        # rollback 後重新 query 失敗員工該月舊 record(可能 None),標 needs_recalc=True
        stale = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .first()
        )
        if stale is not None:
            stale.needs_recalc = True
        # else: 從未算過,沒有舊 record;由 finalize 的 missing 檢查擋下
```

最後 `session.commit()` 同時寫入「成功員工的新值」與「失敗員工的 needs_recalc=True」。

成功員工的 needs_recalc 由 `_fill_salary_record` 一律 set False(包含預載舊 record 與本輪新建 record)。

#### 假單審核失敗降級(leaves.py)

L1196-1201 的 except 區塊改為:

```python
except Exception as e:
    result["salary_recalculated"] = False
    result["salary_warning"] = "操作成功,但薪資重算失敗,請手動前往薪資頁面重新計算"
    logger.error(f"請假審核後薪資重算失敗:{e}")
    # 把所有應重算月份的 SalaryRecord 標 stale,避免被誤封存
    from services.salary.utils import mark_salary_stale
    for year, month in sorted(months_to_recalc):
        try:
            mark_salary_stale(session, emp_id, year, month)
        except Exception:
            logger.warning("標記 stale 失敗", exc_info=True)
    session.commit()
```

#### 加班審核失敗降級(overtimes.py)

L1289-1291 / 單筆審核同模式,加 mark_salary_stale + commit。

#### finalize 完整性檢查(api/salary.py)

`_find_missing_salary_employees` 重命名為 `_find_unfinalizable_employees`,額外回傳 stale 員工:

```python
def _find_unfinalizable_employees(session, year, month) -> dict:
    return {
        "missing": [...],  # 既有邏輯
        "stale": [
            {"id": r.employee_id, "name": emp.name}
            for (r, emp) in session.query(SalaryRecord, Employee)
                .join(Employee, SalaryRecord.employee_id == Employee.id)
                .filter(
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                    SalaryRecord.needs_recalc == True,
                ).all()
        ],
    }
```

`finalize_salary_month` (L1551) 修改:

```python
if not data.force:
    issues = _find_unfinalizable_employees(session, data.year, data.month)
    blocking = issues["missing"] + issues["stale"]
    if blocking:
        # 訊息分兩段顯示 missing 與 stale,讓管理員知道要先解決什麼
        raise HTTPException(409, detail=...)
```

`force=True` 仍可繞過(維持原語意),但日誌升 warning 並把 stale 員工列出。

### 測試

`tests/test_salary_finalize_stale_guard.py`(新檔):
- 模擬批次重算單筆失敗(用注入的 emp 觸發 ValueError),其他人成功 → 失敗員工 SalaryRecord.needs_recalc == True、舊欄位保留;成功員工 needs_recalc == False
- finalize 對含 needs_recalc=True 的月份回 409,訊息含失敗員工姓名
- finalize force=True 可繞過
- 假單審核時若 mock 薪資重算丟例外 → 該員工該月 needs_recalc == True、salary_warning 回傳
- 重新成功重算後 needs_recalc 自動回 False、finalize 可通過

---

## Bug #5 — 批次加班審核 Phase 1 rollback 仍回報 succeeded

### 現況

`overtimes.py:1169-1299` 兩階段流程:

- Phase 1 在迴圈中修改 `ot.is_approved` / 寫 approval log,並 append 到 `changes`
- 任一筆 except → `session.rollback() + session.expire_all()`,但 `changes` list 中前面已成功的條目沒清掉
- Phase 2 `session.commit()` 不會把已 expire 的修改寫入(屬性 reload 成舊值),但 `succeeded.append(ot_id)` 仍把全部 changes 加進去
- 結果:前端看到 succeeded,DB 實際是 pending

### 修法(方案 A — Fail-fast)

Phase 1 任一筆失敗就整批 abort:

```python
changes = []
for idx, ot_id in enumerate(data.ids):
    try:
        # ... 既有驗證 + 修改 ...
        changes.append((ot_id, ot, was_approved))
    except Exception as e:
        session.rollback()
        # 已 append 到 changes 的前序變更已被 rollback 撤回,標 failed
        for chg_id, _, _ in changes:
            failed.append({
                "id": chg_id,
                "reason": "批次中後續記錄驗證失敗,已整批 abort,請重新提交",
            })
        # 失敗那筆
        failed.append({"id": ot_id, "reason": str(e)})
        # 後續未處理的也標 failed
        for remaining_id in data.ids[idx + 1:]:
            failed.append({
                "id": remaining_id,
                "reason": "批次中前序記錄驗證失敗,已整批 abort",
            })
        changes = []
        break
```

Phase 2 維持原邏輯(只在 changes 非空時 commit + append succeeded + 重算薪資)。

加班審核後的薪資重算失敗(L1288-1291)沿用 Bug #1+#4 的 `mark_salary_stale` helper。

### 測試

`tests/test_overtime_batch_approve_failfast.py`(新檔):
- 批次 [ok1, bad, ok2]:bad 在驗證階段(例如 overlap)丟錯 → succeeded == [],failed 含全部三筆,DB 中 ok1 / ok2 仍為 pending
- 批次 [ok1, ok2]:全成功 → succeeded == [ok1, ok2],failed == [],DB 已核准

---

## 整體 footprint

**新增檔案:**
- `alembic/versions/20260427_h3d4e5f6g7h8_add_salary_needs_recalc.py`
- `tests/test_salary_snapshot_permission.py`
- `tests/test_salary_finalize_stale_guard.py`
- `tests/test_overtime_batch_approve_failfast.py`

**修改檔案:**
- `models/salary.py`(SalaryRecord 加欄位 + index)
- `services/salary/utils.py`(新 helper `mark_salary_stale`)
- `services/salary/engine.py`(批次重算 except + `_fill_salary_record` set False)
- `api/salary.py`(5 端點加 self-check + `_find_unfinalizable_employees` + finalize 檢查)
- `api/leaves.py`(假單審核失敗降級補 mark_salary_stale)
- `api/overtimes.py`(批次審核 fail-fast + 失敗降級補 mark_salary_stale)

**Commits 規劃**(依 CLAUDE.md「修 bug 與補測試分兩個 commit」):

1. `test: 補薪資快照/simulate/audit-log self-check 失敗測試`
2. `fix: 為快照/simulate/audit-log 端點加上本人查詢守衛`
3. `test: 補批次重算/審核失敗未標 stale 之回歸測試`
4. `feat: SalaryRecord 加 needs_recalc 旗標(schema + migration)`
5. `fix: 批次重算/假單&加班審核失敗時標記 SalaryRecord 為 needs_recalc;finalize 拒絕 stale`
6. `test: 補加班批次審核 Phase 1 partial-rollback 仍回報成功之回歸測試`
7. `fix: 加班批次審核改 fail-fast,避免 rollback 後仍 append succeeded`

---

## 不變式遵守確認(CLAUDE.md)

- ✅ 所有路由仍有 `require_permission`(僅加 self-check,不動現有守衛)
- ✅ Schema 異動走 Alembic
- ✅ 不使用 print(),logger 既有
- ✅ 業務不變式(gross_salary 公式、festival_bonus 月份限制等)未動
- ✅ TDD:每 bug 先補回歸測試再修

## 不在範圍

- Bug #2(曠職忽略週排班)、Bug #3(全日未打卡擋曠職)— 需先驗證 ShiftAssignment / Attendance 建立路徑,本次不處理
- 其他薪資端點權限審計(本次只補 advisor 點名的 5 個)
