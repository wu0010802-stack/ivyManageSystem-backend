# 批次加班建立 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓管理端在加班管理頁一次為多位員工建立加班記錄（學校活動多人出席），共用日期/類型/原因、時數可逐人微調、全部或全無驗證、狀態 pending。

**Architecture:** 後端新增 `POST /overtimes/batch-create`，採兩階段提交（Phase 1 對每位員工跑與單筆建立完全相同的驗證鏈並蒐集**所有**失敗；Phase 2 全通過才一次 commit）。抽出共用驗證 helper `_validate_overtime_for_employee` 供單筆與批次共用，杜絕驗證漂移。前端新增 `BatchOvertimeDialog.vue`（複用 `MeetingManagementPanel.vue` 多選模式），接到 `OvertimeView.vue`。

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic v2（後端）；Vue 3 `<script setup lang="ts">` + Element Plus + Vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-06-01-batch-overtime-create-design.md`

---

## 檔案結構

**後端（ivy-backend）：**
- Modify `api/overtimes.py`：抽出 `_parse_hhmm_on_date` / `_validate_overtime_for_employee` 兩個 helper；重構單筆 `create_overtime` 改用 helper（行為不變）；新增 `BatchOvertimeEmployeeItem` / `BatchOvertimeCreate` 請求模型 + `POST /overtimes/batch-create` 端點。
- Modify `schemas/overtimes.py`：新增 `BatchOvertimeCreateResultOut`（200 回傳）。
- Modify `tests/test_overtimes.py`：helper 單元測試 + batch-create 整合測試（複用既有 `_admin_app_client` fixture）。

**前端（ivy-frontend）：**
- Modify `src/api/overtimes.ts`：新增 `batchCreateOvertimes`。
- Modify `src/api/_generated/schema.d.ts`：OpenAPI regen 產生（不手改）。
- Create `src/components/overtime/BatchOvertimeDialog.vue`：批次建立 dialog 元件。
- Modify `src/views/OvertimeView.vue`：工具列加「批次加班」按鈕 + 掛入 dialog。
- Create `tests/unit/BatchOvertimeDialog.test.js`：dialog 邏輯測試。

---

## 後端

### Task 1：抽出共用驗證 helper，重構單筆建立

**Files:**
- Modify: `api/overtimes.py`（新增兩 helper；重構 `create_overtime` L607-648 區塊）
- Test: `tests/test_overtimes.py`

- [ ] **Step 1：先讀現況，確認重構標的**

Run: `git -C /Users/yilunwu/Desktop/ivy-backend show HEAD:api/overtimes.py | sed -n '588,690p'`
Expected: 看到 `create_overtime` 內 L607-618 的 HH:MM→datetime parse、L620-648 的驗證鏈（overlap→409 / conflicting_leave / monthly cap / quarterly cap / type calendar）。確認**沒有** `assert_months_not_finalized` 呼叫（封存守衛只在 approve 路徑）。

- [ ] **Step 2：寫 helper 的失敗測試**

在 `tests/test_overtimes.py` 末尾新增（沿用檔案頂部既有 import：`from api.overtimes import _check_overtime_overlap` 等已存在；新增 `_validate_overtime_for_employee`、`_parse_hhmm_on_date` 到該 import）：

```python
# ── _parse_hhmm_on_date / _validate_overtime_for_employee 共用 helper ──
from api.overtimes import _parse_hhmm_on_date, _validate_overtime_for_employee


class TestParseHhmmOnDate:
    def test_none_returns_none(self):
        assert _parse_hhmm_on_date(date(2026, 6, 5), None) is None

    def test_parses_to_datetime_on_given_date(self):
        dt = _parse_hhmm_on_date(date(2026, 6, 5), "14:30")
        assert dt == datetime(2026, 6, 5, 14, 30)


class TestValidateOvertimeForEmployee:
    """helper 必須沿用單筆建立的驗證鏈；overlap 命中時 raise 409。"""

    def test_raises_409_on_overlap(self):
        existing = _make_record(None, None, status="pending")
        session = _mock_session([existing])
        with pytest.raises(HTTPException) as exc:
            _validate_overtime_for_employee(
                session,
                employee_id=1,
                overtime_date=date(2026, 6, 5),
                overtime_type="weekday",
                start_dt=None,
                end_dt=None,
                hours=2.0,
            )
        assert exc.value.status_code == 409
        assert "時間重疊" in exc.value.detail
```

- [ ] **Step 3：跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_overtimes.py::TestValidateOvertimeForEmployee -x -q`
Expected: FAIL — `ImportError: cannot import name '_validate_overtime_for_employee'`。

- [ ] **Step 4：實作兩個 helper**

在 `api/overtimes.py` 的 `create_overtime` 函式**之前**（約 L586，`@router.post("/overtimes"...)` 上方）新增：

```python
def _parse_hhmm_on_date(overtime_date: date, hhmm: Optional[str]) -> Optional[datetime]:
    """將 'HH:MM' 字串組成指定日期的 datetime；None 原樣回傳。"""
    if not hhmm:
        return None
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(
        overtime_date, datetime.min.time().replace(hour=h, minute=m)
    )


def _validate_overtime_for_employee(
    session,
    employee_id: int,
    overtime_date: date,
    overtime_type: str,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    hours: float,
) -> None:
    """單筆建立與批次建立共用的完整驗證鏈（避免驗證漂移）。

    任一不通過即 raise HTTPException（overlap→409，其餘各檢查自行 raise 400/409）。
    刻意不含 assert_months_not_finalized：與單筆建立對齊（封存守衛在 approve 路徑）。
    """
    overlap = _check_overtime_overlap(
        session, employee_id, overtime_date, start_dt, end_dt
    )
    if overlap:
        st = overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
        et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
        raise HTTPException(
            status_code=409,
            detail=(
                f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                f"（ID: {overlap.id}，{st}～{et}），請勿重複申請"
            ),
        )
    _check_employee_has_conflicting_leave(
        session, employee_id, overtime_date, start_dt, end_dt
    )
    _check_monthly_overtime_cap(session, employee_id, overtime_date, hours)
    _check_quarterly_overtime_cap(session, employee_id, overtime_date, hours)
    _check_overtime_type_calendar(session, overtime_date, overtime_type)
```

- [ ] **Step 5：重構 `create_overtime` 改用 helper**

把 `create_overtime` 內 L607-648 區塊（time parse + 重疊 + 跨類 + 月 + 季 + 類型日曆）替換為：

```python
        start_dt = _parse_hhmm_on_date(data.overtime_date, data.start_time)
        end_dt = _parse_hhmm_on_date(data.overtime_date, data.end_time)

        _validate_overtime_for_employee(
            session,
            data.employee_id,
            data.overtime_date,
            data.overtime_type,
            start_dt,
            end_dt,
            data.hours,
        )
```

> 其餘（emp 存在檢查、`pay` 計算、`OvertimeRecord` 建立、commit、audit）保持不動。

- [ ] **Step 6：跑 helper 測試 + 既有加班整段測試確認零回歸**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_overtimes.py -q`
Expected: PASS（新測試通過；既有單筆建立/重疊/季 cap 整合測試全綠，因行為未變）。

- [ ] **Step 7：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/overtimes.py tests/test_overtimes.py
git commit -m "refactor: 抽出加班驗證共用 helper，單筆建立改用

為批次建立鋪路，避免驗證鏈在兩處複製漂移。行為不變。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2：新增批次建立的 Pydantic 模型與 Out schema

**Files:**
- Modify: `api/overtimes.py`（`OvertimeCreate` class 之後，約 L419 後）
- Modify: `schemas/overtimes.py`

- [ ] **Step 1：在 `schemas/overtimes.py` 新增 200 回傳模型**

於檔案末尾新增：

```python
class BatchOvertimeCreateResultOut(IvyBaseModel):
    """POST /overtimes/batch-create 成功回傳（全部建立）。

    驗證失敗時回 422，body 為 {"detail": {"message": str, "errors": list}}，
    不走本 response_model（FastAPI HTTPException 路徑）。
    """

    message: str
    created_ids: list[int]
```

- [ ] **Step 2：在 `api/overtimes.py` 新增請求模型**

於 `OvertimeUpdate` class 之前（約 L420）新增：

```python
class BatchOvertimeEmployeeItem(BaseModel):
    employee_id: int
    hours: float

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v):
        if v <= 0:
            raise ValueError("加班時數必須大於 0")
        if v > MAX_OVERTIME_HOURS:
            raise ValueError(f"單筆加班時數不得超過 {MAX_OVERTIME_HOURS} 小時")
        return v


class BatchOvertimeCreate(BaseModel):
    overtime_date: date
    overtime_type: str  # weekday / weekend / holiday
    start_time: Optional[str] = None  # HH:MM，共用，選填
    end_time: Optional[str] = None  # HH:MM，共用，選填
    reason: Optional[str] = None
    use_comp_leave: bool = False
    employees: List[BatchOvertimeEmployeeItem] = Field(..., min_length=1)

    @field_validator("overtime_type")
    @classmethod
    def validate_overtime_type(cls, v):
        if v not in OVERTIME_TYPE_LABELS:
            allowed = ", ".join(OVERTIME_TYPE_LABELS.keys())
            raise ValueError(f"無效的加班類型，允許值：{allowed}")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v):
        return validate_hhmm_format(v)

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                raise ValueError("start_time 必須早於 end_time（不支援跨日加班）")
        return self
```

- [ ] **Step 3：把 Out schema 加進 import**

把 `schemas.overtimes` import（L43-49）內補上 `BatchOvertimeCreateResultOut`：

```python
from schemas.overtimes import (
    BatchOvertimeCreateResultOut,
    OvertimeApproveResultOut,
    OvertimeCreateResultOut,
    OvertimeDeleteResultOut,
    OvertimeImportResultOut,
    OvertimeUpdateResultOut,
)
```

- [ ] **Step 4：確認可 import（無語法錯）**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -c "from api.overtimes import BatchOvertimeCreate, BatchOvertimeEmployeeItem; from schemas.overtimes import BatchOvertimeCreateResultOut; print('ok')"`
Expected: 輸出 `ok`。

- [ ] **Step 5：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/overtimes.py schemas/overtimes.py
git commit -m "feat: 批次加班建立的請求/回傳 schema

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3：實作 `POST /overtimes/batch-create` 端點

**Files:**
- Modify: `api/overtimes.py`（單筆 `create_overtime` 端點之後新增）
- Test: `tests/test_overtimes.py`

- [ ] **Step 1：寫整合測試（複用既有 `_admin_app_client` fixture）**

在 `tests/test_overtimes.py` 末尾新增。沿用既有 `_make_emp` / `_make_admin_user` / `_seed_ot` / `_do_login`：

```python
class TestBatchCreateOvertime:
    """POST /api/overtimes/batch-create：全部或全無 + 蒐集所有失敗。"""

    def _payload(self, emp_ids, hours=2.0, **kw):
        base = {
            "overtime_date": "2026-06-05",
            "overtime_type": "weekday",
            "reason": "校慶活動",
            "use_comp_leave": False,
            "employees": [{"employee_id": i, "hours": hours} for i in emp_ids],
        }
        base.update(kw)
        return base

    def test_all_pass_creates_all_pending(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            ids = [e1.id, e2.id]
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["created_ids"]) == 2
        with session_factory() as session:
            rows = session.query(_OvertimeRecord).all()
            assert len(rows) == 2
            assert all(r.status == "pending" for r in rows)

    def test_one_over_monthly_cap_aborts_whole_batch(self, _admin_app_client):
        """乙已逼近月上限，整批不建立（DB 維持 0 筆），422 帶該員工 error。"""
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            ids = [e1.id, e2.id]
            # 乙 6 月已 approved 45h（月上限 46h），再 +2h → 超月上限
            _seed_ot(session, e2.id, date(2026, 6, 1), 45.0)
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]["errors"]
        assert any(e["employee_id"] == e2.id for e in errors)
        with session_factory() as session:
            # 乙那筆 seed 之外，不應有任何新建立（甲 0 筆、乙仍只有 1 筆 seed）
            assert session.query(_OvertimeRecord).filter(
                _OvertimeRecord.employee_id == e1.id
            ).count() == 0

    def test_collects_all_failures(self, _admin_app_client):
        """甲、乙各自不同原因失敗 → errors 同時含兩人（驗證不在第一個失敗就中止）。"""
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            ids = [e1.id, e2.id]
            _seed_ot(session, e1.id, date(2026, 6, 1), 45.0)
            _seed_ot(session, e2.id, date(2026, 6, 1), 45.0)
            session.commit()
        _do_login(client)

        resp = client.post("/api/overtimes/batch-create", json=self._payload(ids))
        assert resp.status_code == 422, resp.text
        errors = resp.json()["detail"]["errors"]
        err_ids = {e["employee_id"] for e in errors}
        assert e1.id in err_ids and e2.id in err_ids

    def test_duplicate_employee_id_reported(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            _make_admin_user(session)
            eid = e1.id
            session.commit()
        _do_login(client)

        resp = client.post(
            "/api/overtimes/batch-create", json=self._payload([eid, eid])
        )
        assert resp.status_code == 422, resp.text
        with session_factory() as session:
            assert session.query(_OvertimeRecord).count() == 0

    def test_comp_leave_zero_pay_no_grant(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            _make_admin_user(session)
            eid = e1.id
            session.commit()
        _do_login(client)

        resp = client.post(
            "/api/overtimes/batch-create",
            json=self._payload([eid], use_comp_leave=True),
        )
        assert resp.status_code == 200, resp.text
        with session_factory() as session:
            row = session.query(_OvertimeRecord).filter(
                _OvertimeRecord.employee_id == eid
            ).first()
            assert row.overtime_pay == 0.0
            assert row.use_comp_leave is True
            assert row.comp_leave_granted is False

    def test_per_employee_hours(self, _admin_app_client):
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            e2 = _make_emp(session, "B002", "乙")
            _make_admin_user(session)
            ids = [e1.id, e2.id]
            session.commit()
        _do_login(client)

        payload = self._payload(ids)
        payload["employees"][0]["hours"] = 2.0
        payload["employees"][1]["hours"] = 3.0
        resp = client.post("/api/overtimes/batch-create", json=payload)
        assert resp.status_code == 200, resp.text
        with session_factory() as session:
            by_emp = {
                r.employee_id: r.hours
                for r in session.query(_OvertimeRecord).all()
            }
            assert by_emp[e1.id] == 2.0
            assert by_emp[e2.id] == 3.0

    def test_requires_permission(self, _admin_app_client):
        """無 OVERTIME_WRITE → 403。建一個無權限 user 登入。"""
        client, session_factory = _admin_app_client
        with session_factory() as session:
            e1 = _make_emp(session, "B001", "甲")
            u = _User(
                employee_id=None,
                username="noperm",
                password_hash=_hash_password("AdminPass123"),
                role="staff",
                permission_names=[],
                is_active=True,
                must_change_password=False,
            )
            session.add(u)
            eid = e1.id
            session.commit()
        resp = client.post(
            "/api/auth/login", json={"username": "noperm", "password": "AdminPass123"}
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/overtimes/batch-create", json=self._payload([eid])
        )
        assert resp.status_code == 403
```

- [ ] **Step 2：跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_overtimes.py::TestBatchCreateOvertime -x -q`
Expected: FAIL — 404（端點尚未存在）。

- [ ] **Step 3：實作端點**

在 `api/overtimes.py` 單筆 `create_overtime` 端點（結尾 L690 `finally: session.close()`）**之後**新增：

```python
@router.post(
    "/overtimes/batch-create",
    status_code=200,
    response_model=BatchOvertimeCreateResultOut,
    dependencies=[Depends(_batch_approve_limiter)],
)
def batch_create_overtimes(
    data: BatchOvertimeCreate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """一次為多位員工建立加班記錄（學校活動多人出席）。

    全部或全無：Phase 1 對每位員工跑完整驗證並蒐集所有失敗；
    任一失敗 → 422 整批不寫入。Phase 2 全通過才一次 commit。
    每筆狀態 pending，不觸發薪資重算（與單筆建立一致）。
    """
    session = get_session()
    try:
        start_dt = _parse_hhmm_on_date(data.overtime_date, data.start_time)
        end_dt = _parse_hhmm_on_date(data.overtime_date, data.end_time)

        # ── Phase 1：全員驗證（不寫 DB），蒐集所有失敗 ──
        errors: list[dict] = []
        validated: list[tuple[Employee, float]] = []
        seen: set[int] = set()

        for item in data.employees:
            if item.employee_id in seen:
                errors.append({
                    "employee_id": item.employee_id,
                    "name": None,
                    "reason": "員工在批次清單中重複出現",
                })
                continue
            seen.add(item.employee_id)

            emp = (
                session.query(Employee)
                .filter(Employee.id == item.employee_id)
                .first()
            )
            if not emp:
                errors.append({
                    "employee_id": item.employee_id,
                    "name": None,
                    "reason": EMPLOYEE_DOES_NOT_EXIST,
                })
                continue

            try:
                _validate_overtime_for_employee(
                    session,
                    item.employee_id,
                    data.overtime_date,
                    data.overtime_type,
                    start_dt,
                    end_dt,
                    item.hours,
                )
            except HTTPException as exc:
                errors.append({
                    "employee_id": item.employee_id,
                    "name": emp.name,
                    "reason": exc.detail,
                })
                continue

            validated.append((emp, item.hours))

        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "批次建立失敗，請修正下列項目後重送",
                    "errors": errors,
                },
            )

        # ── Phase 2：全通過 → 一次建立 + 單次 commit ──
        records: list[OvertimeRecord] = []
        for emp, hours in validated:
            pay = (
                0.0
                if data.use_comp_leave
                else calculate_overtime_pay(
                    emp.base_salary, hours, data.overtime_type
                )
            )
            records.append(
                OvertimeRecord(
                    employee_id=emp.id,
                    overtime_date=data.overtime_date,
                    overtime_type=data.overtime_type,
                    start_time=start_dt,
                    end_time=end_dt,
                    hours=hours,
                    overtime_pay=pay,
                    use_comp_leave=data.use_comp_leave,
                    reason=data.reason,
                    status=ApprovalStatus.PENDING.value,
                )
            )
        session.add_all(records)
        session.commit()

        created_ids = [r.id for r in records]
        request.state.audit_summary = (
            f"管理端批次建立加班：{len(created_ids)} 筆 "
            f"{data.overtime_type} {data.overtime_date}"
        )
        request.state.audit_changes = {
            "action": "overtime_batch_create",
            "overtime_date": data.overtime_date.isoformat(),
            "overtime_type": data.overtime_type,
            "use_comp_leave": data.use_comp_leave,
            "employee_ids": [emp.id for emp, _ in validated],
            "created_ids": created_ids,
        }
        return {
            "message": f"已建立 {len(created_ids)} 筆加班記錄",
            "created_ids": created_ids,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

- [ ] **Step 4：跑 batch-create 測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_overtimes.py::TestBatchCreateOvertime -q`
Expected: PASS（7 條全綠）。

- [ ] **Step 5：跑加班整段測試確認零回歸**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m pytest tests/test_overtimes.py tests/test_overtimes_quarterly_cap.py tests/test_leave_overtime_conflict.py -q`
Expected: PASS（既有測試全綠）。

- [ ] **Step 6：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/overtimes.py tests/test_overtimes.py
git commit -m "feat: 新增 POST /overtimes/batch-create 批次加班建立

學校活動多人出席時一次為多位員工建立加班記錄；共用日期/類型/原因、
時數逐人微調；全部或全無驗證（蒐集所有失敗）；狀態 pending。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4：OpenAPI regen → 下放前端型別

**Files:**
- Modify: `ivy-frontend/src/api/_generated/schema.d.ts`（產生，不手改）

- [ ] **Step 1：後端 dump OpenAPI**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only，.gitignore 擋）。

- [ ] **Step 2：前端 regen schema**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api`
Expected: `src/api/_generated/schema.d.ts` 更新，新增 `/overtimes/batch-create` path。

- [ ] **Step 3：確認新 path 入 schema**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && grep -c "batch-create" src/api/_generated/schema.d.ts`
Expected: ≥ 1。

- [ ] **Step 4：Commit（前端）**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore: regen OpenAPI schema — 批次加班建立端點

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 前端

### Task 5：新增 `batchCreateOvertimes` API wrapper

**Files:**
- Modify: `ivy-frontend/src/api/overtimes.ts`

- [ ] **Step 1：新增 wrapper**

在 `src/api/overtimes.ts` 的 `batchApproveOvertimes` 之後新增：

```ts
import type { ApiBody } from './_generated/typed'

// 批次建立（學校活動多人出席）；後端全部或全無，失敗回 422 detail.errors
export const batchCreateOvertimes = (payload: ApiBody<'/overtimes/batch-create', 'post'>) =>
  api.post('/overtimes/batch-create', payload)
```

> 注意：`import api from './index'` 已在檔案頂部；`import type { ApiBody }` 若與既有 import 行重複，合併成一行。

- [ ] **Step 2：typecheck**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run typecheck`
Expected: 0 error（`ApiBody<'/overtimes/batch-create','post'>` 已由 Task 4 schema 提供）。

- [ ] **Step 3：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/api/overtimes.ts
git commit -m "feat: 加班 api 新增 batchCreateOvertimes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6：建立 `BatchOvertimeDialog.vue`

**Files:**
- Create: `ivy-frontend/src/components/overtime/BatchOvertimeDialog.vue`
- Test: `ivy-frontend/tests/unit/BatchOvertimeDialog.test.js`

- [ ] **Step 1：寫元件邏輯測試**

建立 `tests/unit/BatchOvertimeDialog.test.js`：

```js
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import BatchOvertimeDialog from '@/components/overtime/BatchOvertimeDialog.vue'

vi.mock('@/api/overtimes', () => ({
  batchCreateOvertimes: vi.fn(() => Promise.resolve({ data: { message: 'ok', created_ids: [1, 2] } })),
}))

const employees = [
  { id: 1, name: '甲', is_active: true },
  { id: 2, name: '乙', is_active: true },
]

function factory() {
  return mount(BatchOvertimeDialog, {
    props: { modelValue: true, employees },
    global: { plugins: [ElementPlus] },
  })
}

describe('BatchOvertimeDialog', () => {
  it('預設帶入所有員工，預設時數套用到每列', () => {
    const wrapper = factory()
    expect(wrapper.vm.rows).toHaveLength(2)
    expect(wrapper.vm.rows.every(r => r.selected)).toBe(true)
    expect(wrapper.vm.rows.every(r => r.hours === wrapper.vm.form.defaultHours)).toBe(true)
  })

  it('buildPayload 只含已勾選員工，帶逐人時數', () => {
    const wrapper = factory()
    wrapper.vm.form.overtime_date = '2026-06-05'
    wrapper.vm.rows[0].selected = true
    wrapper.vm.rows[0].hours = 2
    wrapper.vm.rows[1].selected = false
    const payload = wrapper.vm.buildPayload()
    expect(payload.employees).toEqual([{ employee_id: 1, hours: 2 }])
    expect(payload.overtime_date).toBe('2026-06-05')
  })

  it('解析 422 errors 成顯示清單', () => {
    const wrapper = factory()
    const err = { response: { data: { detail: { errors: [{ employee_id: 2, name: '乙', reason: '超出當月加班上限' }] } } } }
    wrapper.vm.applyBatchErrors(err)
    expect(wrapper.vm.batchErrors).toHaveLength(1)
    expect(wrapper.vm.batchErrors[0].reason).toContain('超出當月加班上限')
  })
})
```

- [ ] **Step 2：跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run tests/unit/BatchOvertimeDialog.test.js`
Expected: FAIL — 找不到元件檔。

- [ ] **Step 3：實作元件**

建立 `src/components/overtime/BatchOvertimeDialog.vue`：

```vue
<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { batchCreateOvertimes } from '@/api/overtimes'
import { apiError } from '@/utils/error'
import { OVERTIME_TYPES as overtimeTypes } from '@/constants/approvalEnums'

const props = defineProps<{
  modelValue: boolean
  employees: { id: number; name: string; is_active?: boolean }[]
}>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  created: []
}>()

interface Row {
  id: number
  name: string
  selected: boolean
  hours: number
}

interface BatchError {
  employee_id: number
  name: string | null
  reason: string
}

const form = reactive({
  overtime_date: '',
  overtime_type: 'weekday',
  start_time: '',
  end_time: '',
  reason: '',
  use_comp_leave: false,
  defaultHours: 1,
})

const rows = ref<Row[]>([])
const batchErrors = ref<BatchError[]>([])
const submitting = ref(false)

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const selectedCount = computed(() => rows.value.filter(r => r.selected).length)
const allSelected = computed({
  get: () => rows.value.length > 0 && rows.value.every(r => r.selected),
  set: (v: boolean) => rows.value.forEach(r => { r.selected = v }),
})

const resetState = () => {
  form.overtime_date = ''
  form.overtime_type = 'weekday'
  form.start_time = ''
  form.end_time = ''
  form.reason = ''
  form.use_comp_leave = false
  form.defaultHours = 1
  batchErrors.value = []
  rows.value = props.employees
    .filter(e => e.is_active !== false)
    .map(e => ({ id: e.id, name: e.name, selected: true, hours: form.defaultHours }))
}

// 對話框開啟時初始化；預設時數變動時同步未手改的列
watch(() => props.modelValue, (open) => { if (open) resetState() })
watch(() => form.defaultHours, (h) => { rows.value.forEach(r => { r.hours = h }) })

const buildPayload = () => ({
  overtime_date: form.overtime_date,
  overtime_type: form.overtime_type,
  start_time: form.start_time || null,
  end_time: form.end_time || null,
  reason: form.reason || null,
  use_comp_leave: form.use_comp_leave,
  employees: rows.value
    .filter(r => r.selected)
    .map(r => ({ employee_id: r.id, hours: r.hours })),
})

const applyBatchErrors = (error: unknown) => {
  const detail = (error as { response?: { data?: { detail?: { errors?: BatchError[] } } } })
    ?.response?.data?.detail
  batchErrors.value = Array.isArray(detail?.errors) ? detail!.errors : []
}

const submit = async () => {
  batchErrors.value = []
  if (!form.overtime_date) {
    ElMessage.warning('請選擇加班日期')
    return
  }
  const payload = buildPayload()
  if (payload.employees.length === 0) {
    ElMessage.warning('請至少選擇一位員工')
    return
  }
  submitting.value = true
  try {
    const resp = await batchCreateOvertimes(payload)
    ElMessage.success(resp.data.message || '批次建立完成')
    visible.value = false
    emit('created')
  } catch (error) {
    applyBatchErrors(error)
    if (batchErrors.value.length === 0) {
      ElMessage.error('建立失敗: ' + apiError(error, (error as Error).message))
    } else {
      ElMessage.error('整批未建立，請修正下列項目')
    }
  } finally {
    submitting.value = false
  }
}

defineExpose({ form, rows, batchErrors, buildPayload, applyBatchErrors })
</script>

<template>
  <el-dialog v-model="visible" title="批次加班（活動多人出席）" width="720px" top="5vh">
    <el-form label-width="100px">
      <el-form-item label="加班日期" required>
        <el-date-picker v-model="form.overtime_date" type="date" value-format="YYYY-MM-DD" style="width: 100%;" />
      </el-form-item>
      <el-form-item label="加班類型" required>
        <el-select v-model="form.overtime_type" style="width: 100%;">
          <el-option v-for="ot in overtimeTypes" :key="ot.value" :label="`${ot.label}（${ot.desc}）`" :value="ot.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="開始時間">
        <el-time-picker v-model="form.start_time" format="HH:mm" value-format="HH:mm" placeholder="活動開始（選填）" style="width: 100%;" />
      </el-form-item>
      <el-form-item label="結束時間">
        <el-time-picker v-model="form.end_time" format="HH:mm" value-format="HH:mm" placeholder="活動結束（選填）" style="width: 100%;" />
      </el-form-item>
      <el-form-item label="預設時數">
        <el-input-number v-model="form.defaultHours" :min="0.5" :step="0.5" :max="12" />
        <span class="dialog-hint">套用到下方每位員工，可逐人微調</span>
      </el-form-item>
      <el-form-item label="補休方式">
        <el-switch v-model="form.use_comp_leave" active-text="補休（加班費為 0）" inactive-text="計薪" active-color="#67c23a" />
      </el-form-item>
      <el-form-item label="原因">
        <el-input v-model="form.reason" type="textarea" :rows="2" />
      </el-form-item>

      <el-divider>選擇員工</el-divider>
      <div class="batch-actions">
        <el-checkbox v-model="allSelected">全選</el-checkbox>
        <span class="text-muted">已選 {{ selectedCount }} 人</span>
      </div>
      <div class="employee-list">
        <div v-for="row in rows" :key="row.id" class="employee-item">
          <el-checkbox v-model="row.selected">{{ row.name }}</el-checkbox>
          <el-input-number v-model="row.hours" :min="0.5" :step="0.5" :max="12" :disabled="!row.selected" size="small" />
        </div>
      </div>

      <el-alert v-if="batchErrors.length > 0" type="error" :closable="false" show-icon class="batch-error-alert">
        <template #title>整批未建立，請修正下列項目：</template>
        <ul class="batch-error-list">
          <li v-for="(e, idx) in batchErrors" :key="idx">
            {{ e.name || ('員工 #' + e.employee_id) }}：{{ e.reason }}
          </li>
        </ul>
      </el-alert>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">確認建立</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-hint {
  margin-left: 12px;
  color: var(--text-tertiary);
}
.batch-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.employee-list {
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.employee-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 10px;
}
.text-muted {
  color: var(--text-tertiary);
}
.batch-error-alert {
  margin-top: 12px;
}
.batch-error-list {
  margin: 6px 0 0;
  padding-left: 18px;
}
</style>
```

- [ ] **Step 4：跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run tests/unit/BatchOvertimeDialog.test.js`
Expected: PASS（3 條）。

- [ ] **Step 5：typecheck**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run typecheck`
Expected: 0 error。

- [ ] **Step 6：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/components/overtime/BatchOvertimeDialog.vue tests/unit/BatchOvertimeDialog.test.js
git commit -m "feat: 批次加班 dialog 元件（多選員工 + 逐人時數 + 422 清單）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7：接入 `OvertimeView.vue`

**Files:**
- Modify: `ivy-frontend/src/views/OvertimeView.vue`

- [ ] **Step 1：import 元件 + 狀態**

在 `<script setup>` import 區（約 L17，`MeetingManagementPanel` import 之後）新增：

```ts
import BatchOvertimeDialog from '@/components/overtime/BatchOvertimeDialog.vue'
```

在 `saveOvertimeLoading` 宣告（約 L131）附近新增：

```ts
const batchCreateVisible = ref(false)
const openBatchCreate = () => { batchCreateVisible.value = true }
```

- [ ] **Step 2：工具列加按鈕**

在 `<el-button type="success" @click="openCreate">`（L369-371「新增加班」）**之前**新增：

```vue
            <el-button type="primary" plain @click="openBatchCreate">
              <el-icon><Plus /></el-icon> 批次加班
            </el-button>
```

- [ ] **Step 3：掛入 dialog**

在 `ApprovalLogDrawer`（約 L586）**之前**新增：

```vue
    <BatchOvertimeDialog
      v-model="batchCreateVisible"
      :employees="employeeStore.employees as { id: number; name: string; is_active?: boolean }[]"
      @created="refreshAllData"
    />
```

- [ ] **Step 4：typecheck + build**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run typecheck && npm run build`
Expected: typecheck 0 error；build success。

- [ ] **Step 5：跑前端整段相關測試確認零回歸**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run tests/unit/BatchOvertimeDialog.test.js src/views/__tests__/OvertimeView.test.* 2>/dev/null; npx vitest run tests/unit/BatchOvertimeDialog.test.js`
Expected: 相關測試 PASS（OvertimeView 既有測試若存在亦全綠）。

- [ ] **Step 6：Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/views/OvertimeView.vue
git commit -m "feat: 加班管理頁掛入批次加班按鈕與 dialog

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 整合驗證（Task 8）

- [ ] **Step 1：起兩端**

Run: `cd /Users/yilunwu/Desktop/ivyManageSystem && ./start.sh`
Expected: 後端 `:8088`、前端 `:5173` 起來。

- [ ] **Step 2：手測批次建立**

到加班管理 → 一般加班 → 點「批次加班」→ 選日期/類型 → 勾多位員工、改其中一人時數 → 確認建立 → 應顯示「已建立 N 筆」，列表/待審核出現 N 筆 pending。

- [ ] **Step 3：手測全部或全無**

故意讓一位員工同日已有加班（時間重疊）→ 批次含該員工 → 送出應顯示紅色錯誤清單、整批未建立（列表筆數不變）。

---

## 自我檢查（已完成）

- **Spec coverage**：端點（T3）、schema（T2）、共用 helper 防漂移（T1）、pending 不重算（T3 實作 + test）、全部或全無＋蒐集所有失敗（T3 test）、逐人時數（T2/T3/T6）、422 自訂 body（T3 raise + T6 解析）、前端 dialog（T6）、接入（T7）、api-contract SOP 分開 commit（各 task）、portal 不動（無 portal task）。皆有對應 task。
- **Placeholder scan**：無 TBD/TODO；每個改碼步驟均附完整程式碼與確切指令。
- **Type consistency**：`_validate_overtime_for_employee` / `_parse_hhmm_on_date` / `BatchOvertimeCreate` / `BatchOvertimeEmployeeItem` / `BatchOvertimeCreateResultOut` / `batchCreateOvertimes` / `buildPayload` / `applyBatchErrors` / `batchCreateVisible` 跨 task 命名一致。422 body 形狀 `{detail:{message, errors:[{employee_id,name,reason}]}}` 後端 raise（T3）與前端解析（T6）一致。
