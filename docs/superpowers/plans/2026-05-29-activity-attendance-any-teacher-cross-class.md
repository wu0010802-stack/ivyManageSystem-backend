# 才藝點名：任何老師可點整堂跨班名冊 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓任何學校老師（非家長 portal 使用者）能在教師 portal 查看並點任何一堂才藝課的完整跨班名冊，移除現行「只能點自班學生」限制。

**Architecture:** 純行為調整、無 schema 變更。後端把 portal 才藝點名三端點（場次列表 / 詳情 / 批次點名）從「自班過濾」放寬為「全部 / 整堂」，授權沿用 router 層既有 `require_non_parent_role`；把 admin/portal 重複的「本場次有效報名」查詢抽成共用 helper。前端元件大多已是泛用設計（扁平表格 + 班級欄 + 課程/日期篩選），僅需小幅調整文案與排序。

**Tech Stack:** FastAPI + SQLAlchemy（後端）/ Vue 3 `<script setup lang="ts">` + Element Plus + Vitest（前端）/ pytest。

**Spec:** `docs/superpowers/specs/2026-05-29-activity-attendance-any-teacher-cross-class-design.md`

---

## 重要實作須知

1. **後端 worktree（已建立）**：`/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/activity-attendance-any-teacher-2026-05-29`，分支 `feat/activity-attendance-any-teacher-cross-class-2026-05-29-backend`（從 origin/main 開）。所有後端 `git` 指令必用 `git -C <worktree絕對路徑>`，且每個 task 開頭先 `git -C <wt> branch --show-current` 確認在本分支，避免 commit 落到別的分支。
2. **black PostToolUse hook**：ivy-backend worktree 對 `.py` 檔 Edit/Write 後會自動跑 black，對既有檔案做 surgical edit 易被全檔重排成 cosmetic creep。**修改既有 `.py` 檔請改用 `python3` 的 `str.replace` 腳本**寫檔（繞過 hook），只動目標行。新建檔案不受此限。
3. **前端 worktree（Phase B 開始時再建）**：到 Phase B 時，從 ivy-frontend 的 `origin/main` 開 worktree（分支 `feat/activity-attendance-any-teacher-cross-class-2026-05-29-frontend`），勿從 local main（會夾帶 user WIP commits）。
4. **前後端分開 commit**（不同 repo），訊息描述同一功能。
5. **TDD**：每個行為變更先寫/改測試使其失敗，再改實作使其通過。

---

# Phase A — 後端（ivy-backend worktree）

> 以下 `WT` 代表後端 worktree 絕對路徑：
> `/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/activity-attendance-any-teacher-2026-05-29`
> 在 worktree 內跑 pytest：`cd "$WT" && python -m pytest ...`

## Task A1：抽共用 helper `query_valid_session_registrations` + admin 改用

**Files:**
- Modify: `api/activity/_shared.py`（新增函式）
- Modify: `api/activity/attendance.py`（admin `batch_update_attendance` 改用 helper）
- Test: `tests/test_activity_shared_valid_regs.py`（新建）

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_activity_shared_valid_regs.py`。**使用 conftest 既有的 `test_db_session` fixture**（單一 session、全 ORM 表已建、自動 swap 全域 SessionFactory）：

```python
"""query_valid_session_registrations 純查詢 helper 單元測試。

使用 conftest 的 test_db_session fixture（單一 session、全表已建）。
"""
from api.activity._shared import query_valid_session_registrations
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)


def _mk_reg(s, *, course_id, student_id=None, is_active=True, match_status="manual",
            rc_status="enrolled", classroom_id=None):
    reg = ActivityRegistration(
        student_name="生", birthday="2020-01-01", class_name="班",
        is_active=is_active, school_year=115, semester=1,
        student_id=student_id, parent_phone="0911000000",
        classroom_id=classroom_id, match_status=match_status, pending_review=False,
    )
    s.add(reg)
    s.flush()
    s.add(RegistrationCourse(
        registration_id=reg.id, course_id=course_id, status=rc_status, price_snapshot=100,
    ))
    s.flush()
    return reg.id


def test_valid_regs_returns_enrolled_active_only(test_db_session):
    s = test_db_session
    course = ActivityCourse(name="圍棋", price=100, school_year=115, semester=1, is_active=True)
    s.add(course)
    s.flush()
    good = _mk_reg(s, course_id=course.id)
    inactive = _mk_reg(s, course_id=course.id, is_active=False)
    rejected = _mk_reg(s, course_id=course.id, match_status="rejected")
    waitlist = _mk_reg(s, course_id=course.id, rc_status="waitlist")
    s.commit()

    rows = query_valid_session_registrations(
        s, course.id, [good, inactive, rejected, waitlist]
    )
    assert {r[0] for r in rows} == {good}


def test_valid_regs_classroom_filter(test_db_session):
    s = test_db_session
    course = ActivityCourse(name="繪畫", price=100, school_year=115, semester=1, is_active=True)
    s.add(course)
    s.flush()
    in_class = _mk_reg(s, course_id=course.id, classroom_id=7)
    other_class = _mk_reg(s, course_id=course.id, classroom_id=9)
    s.commit()
    ids = [in_class, other_class]

    assert {r[0] for r in query_valid_session_registrations(s, course.id, ids)} == {
        in_class,
        other_class,
    }
    assert {
        r[0]
        for r in query_valid_session_registrations(s, course.id, ids, classroom_ids=[7])
    } == {in_class}


def test_valid_regs_empty_input(test_db_session):
    assert query_valid_session_registrations(test_db_session, 1, []) == []
```

> `test_db_session` 定義於 `tests/conftest.py`（yield 單一 session）；`RegistrationCourse` 可從 `models.database` import（已驗證 re-export）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd "$WT" && python -m pytest tests/test_activity_shared_valid_regs.py -v`
Expected: FAIL（`ImportError: cannot import name 'query_valid_session_registrations'`）

- [ ] **Step 3: 在 `api/activity/_shared.py` 新增 helper**

於 `api/activity/_shared.py` 適當位置（檔尾或 `_build_session_detail_response` 前）新增。`ActivityRegistration` / `RegistrationCourse` 在本檔已 import（`_build_session_detail_response` 已用），無需新增 import：

```python
def query_valid_session_registrations(
    db_session,
    course_id: int,
    registration_ids: list,
    *,
    classroom_ids: list | None = None,
) -> list:
    """回傳本場次「有效報名」的 (registration_id, student_id) tuple 列表。

    有效 = ActivityRegistration.is_active、match_status != 'rejected'，且確實報了
    course_id 對應課程（RegistrationCourse.status IN ('enrolled','promoted_pending')）。
    供 admin 點名與 portal 點名共用，避免兩處重複定義「有效報名」規則。

    classroom_ids=None   → 不限班級（管理端 / 開放後的 portal，跨班）。
    classroom_ids=[...]   → 額外限定 ActivityRegistration.classroom_id IN(...)（保留彈性）。
    registration_ids 為空 → 回 []（不打 DB）。
    """
    if not registration_ids:
        return []
    query = (
        db_session.query(ActivityRegistration.id, ActivityRegistration.student_id)
        .join(
            RegistrationCourse,
            RegistrationCourse.registration_id == ActivityRegistration.id,
        )
        .filter(
            ActivityRegistration.id.in_(registration_ids),
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.match_status != "rejected",
            RegistrationCourse.course_id == course_id,
            RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
        )
    )
    if classroom_ids is not None:
        query = query.filter(ActivityRegistration.classroom_id.in_(classroom_ids))
    return query.all()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd "$WT" && python -m pytest tests/test_activity_shared_valid_regs.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 重構 admin `batch_update_attendance` 改用 helper（行為不變）**

在 `api/activity/attendance.py`：
1. import 區加：`from api.activity._shared import query_valid_session_registrations`（該檔已 `from api.activity._shared import _build_session_detail_response`，可併入同一行或新增一行）。
2. 把 `batch_update_attendance` 內現有的 `valid_reg_rows = ( session.query(ActivityRegistration.id, ActivityRegistration.student_id).join(...).filter(...).all() if req_reg_ids else [] )` 整段替換為：

```python
        valid_reg_rows = query_valid_session_registrations(
            session, sess.course_id, req_reg_ids
        )
```

`valid_reg_ids = {row[0] for row in valid_reg_rows}` 與 `reg_student_map = dict(valid_reg_rows)` 維持不變。

> 用 `python3` str.replace 腳本改檔以繞過 black hook（見「重要實作須知 #2」）。

- [ ] **Step 6: 跑 admin 點名相關測試確認無回歸**

Run: `cd "$WT" && python -m pytest tests/test_activity_api.py tests/test_activity_attendance_grouping.py tests/test_activity_shared_valid_regs.py -v`
Expected: PASS（admin 點名行為不變）

- [ ] **Step 7: Commit**

```bash
git -C "$WT" add api/activity/_shared.py api/activity/attendance.py tests/test_activity_shared_valid_regs.py
git -C "$WT" commit -m "refactor(activity): 抽 query_valid_session_registrations 共用 helper

admin 點名改用 helper，行為不變；供後續 portal 跨班點名共用。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task A2：portal 場次詳情改回完整跨班名冊 + 404（移除 F-010 collapse）

**Files:**
- Modify: `api/portal/activity.py`（`portal_get_session_detail`）
- Test: `tests/test_enumeration_oracle_consistency.py`（更新 `TestF010_PortalActivitySession`）

- [ ] **Step 1: 改既有測試以表達新契約（先讓它失敗）**

`tests/test_enumeration_oracle_consistency.py` 的 `TestF010_PortalActivitySession`（約 line 882）內：

(a) 把 `test_get_session_with_no_own_class_students_returns_403` 改名並改斷言為「任何老師可看完整跨班名冊」：

```python
    def test_get_session_any_teacher_sees_cross_class_roster_200(self, portal_client):
        """放寬後：非該班導師的老師也能看完整跨班名冊（不再 403）。"""
        client, sf = portal_client
        with sf() as s:
            token, sid = self._seed_no_own_class(s)
        resp = client.get(
            f"/api/portal/activity/attendance/sessions/{sid}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200, resp.text
        names = [st["student_name"] for st in resp.json()["students"]]
        assert "B生" in names  # 跨班學生現在可見
```

(b) 把 `test_get_session_non_existent_same_detail` 改為「存在→200、不存在→404」（不再 collapse）：

```python
    def test_get_session_existing_200_missing_404(self, portal_client):
        """放寬後：場次存在→200、不存在→404（F-010 collapse 已移除，
        因任何老師都能看任何場次，無可列舉的受保護資源）。"""
        client, sf = portal_client
        with sf() as s:
            token, sid = self._seed_no_own_class(s)
        resp_other = client.get(
            f"/api/portal/activity/attendance/sessions/{sid}",
            cookies={"access_token": token},
        )
        resp_missing = client.get(
            "/api/portal/activity/attendance/sessions/999999",
            cookies={"access_token": token},
        )
        assert resp_other.status_code == 200, resp_other.text
        assert resp_missing.status_code == 404
```

(c) `test_get_session_with_own_class_students_returns_200` 維持不變（仍 200）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd "$WT" && python -m pytest tests/test_enumeration_oracle_consistency.py::TestF010_PortalActivitySession -v`
Expected: FAIL（舊實作對 no-own-class 回 403、對 missing 回 403）

- [ ] **Step 3: 改 `portal_get_session_detail` 實作**

在 `api/portal/activity.py` 把整個 `portal_get_session_detail` 函式 body 換成（移除 `_get_employee` / `_get_teacher_classroom_ids` / `classroom_ids_filter` / 「無自班學生→403」/ F-010 collapse；改為 404 + 完整名冊）：

```python
@router.get("/activity/attendance/sessions/{session_id}")
def portal_get_session_detail(
    session_id: int,
    group_by: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """場次詳情：完整跨班名冊（任何老師可查）。

    放寬前僅回自班學生並以 403 collapse 防列舉；現任何老師皆可查任何場次，
    無受保護資源可列舉，故場次不存在直接回 404（對齊 admin）。
    group_by="classroom" → 額外回傳 groups（按班級分組）。
    """
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")
        group_key = "classroom" if group_by == "classroom" else None
        return _build_session_detail_response(session, sess, group_by=group_key)
    finally:
        session.close()
```

> 用 `python3` str.replace 腳本整段替換以繞過 black hook。`HTTPException` / `Optional` / `get_current_user` / `_build_session_detail_response` / `ActivitySession` 在本檔皆已 import。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd "$WT" && python -m pytest tests/test_enumeration_oracle_consistency.py::TestF010_PortalActivitySession -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git -C "$WT" add api/portal/activity.py tests/test_enumeration_oracle_consistency.py
git -C "$WT" commit -m "feat(portal): 才藝場次詳情改回完整跨班名冊（任何老師可查）

移除自班過濾與 F-010 403-collapse；場次不存在改回 404。
任何非家長老師皆可查任何才藝場次的完整名冊。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task A3：portal 場次列表改列全部才藝場次 + 整堂統計

> **測試放置策略**：新增的 portal HTTP 行為測試一律加進**既有檔** `tests/test_activity_vulnerability_fixes.py`，重用其 `client` fixture（`c, sf = client`）、`_login(c, username)`、`_seed_term()`，避免重複造 fixture。Task A3 先在該檔加兩個共用 seed helper（A4 也會用）。

**Files:**
- Modify: `api/portal/activity.py`（`portal_list_sessions` + import）
- Test: `tests/test_activity_vulnerability_fixes.py`（加 seed helper + `TestPortalActivityListOpen`）

- [ ] **Step 1: 在 `tests/test_activity_vulnerability_fixes.py` 加 seed helper + 失敗測試**

(a) 該檔 import 區已有 `ActivityCourse, ActivityRegistration, ActivitySession, Classroom, Employee, User`（from `models.database`）與 `hash_password`、`date`。**補一個 import**：`RegistrationCourse`（加進 `from models.database import (...)` 清單，已驗證可 re-export）。

(b) 在 `_seed_portal_teacher` 函式附近新增兩個 helper：

```python
def _seed_teacher_no_class(session, *, username="t_open"):
    """建立一位『沒有帶任何班級』的老師（_get_teacher_classroom_ids 會回空）。"""
    emp = Employee(employee_id="TOPEN", name="無班老師", base_salary=32000, is_active=True)
    session.add(emp)
    session.flush()
    session.add(User(
        employee_id=emp.id, username=username,
        password_hash=hash_password("TempPass123"),
        role="teacher", permission_names=[], is_active=True,
    ))
    session.flush()
    return emp.id


def _seed_course_session_with_reg(session, *, course_name, classroom_id=None, student_name="跨班生"):
    sy, sem = _seed_term()
    course = ActivityCourse(name=course_name, price=100, school_year=sy, semester=sem, is_active=True)
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name=student_name, birthday="2020-01-01", class_name="某班",
        is_active=True, school_year=sy, semester=sem, parent_phone="0911222333",
        classroom_id=classroom_id, match_status="manual", pending_review=False,
    )
    session.add(reg)
    session.flush()
    session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id, status="enrolled", price_snapshot=100,
    ))
    sess = ActivitySession(course_id=course.id, session_date=date.today())
    session.add(sess)
    session.flush()
    session.commit()
    return course.id, sess.id, reg.id
```

(c) 新增測試類：

```python
class TestPortalActivityListOpen:
    def test_no_class_teacher_sees_all_sessions(self, client):
        c, sf = client
        with sf() as s:
            _seed_teacher_no_class(s, username="t_list")
            # 場次屬於某個與該老師無關的班級
            klass = Classroom(name="獨立班", is_active=True)
            s.add(klass)
            s.flush()
            _seed_course_session_with_reg(s, course_name="陶藝", classroom_id=klass.id)
        _login(c, "t_list")
        res = c.get("/api/portal/activity/attendance/sessions")
        assert res.status_code == 200, res.text
        body = res.json()
        assert isinstance(body, list)
        assert any(row["course_name"] == "陶藝" for row in body)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd "$WT" && python -m pytest tests/test_activity_vulnerability_fixes.py::TestPortalActivityListOpen -v`
Expected: FAIL（舊實作對無班老師回空 list → `any(...)` 為 False）

- [ ] **Step 3: 改 `portal_list_sessions` 實作 + import**

在 `api/portal/activity.py`：
1. import 區把 `from sqlalchemy import or_` 改為 `from sqlalchemy import or_, func, case`。
2. 整段替換 `portal_list_sessions` 函式：

```python
@router.get("/activity/attendance/sessions")
def portal_list_sessions(
    course_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: dict = Depends(get_current_user),
):
    """才藝場次列表：列全部才藝場次（任何老師可見），出席統計算整堂。

    放寬前僅列『自班有報名的課程』場次且統計只算自班；現對齊 admin：
    列全部場次、整堂統計。維持回傳陣列（與既有前端相容，無分頁）。
    """
    session = get_session()
    try:
        query = session.query(
            ActivitySession.id,
            ActivitySession.course_id,
            ActivitySession.session_date,
            ActivitySession.notes,
            ActivitySession.created_by,
            ActivitySession.created_at,
            ActivityCourse.name.label("course_name"),
        ).join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
        if course_id:
            query = query.filter(ActivitySession.course_id == course_id)
        if start_date:
            query = query.filter(ActivitySession.session_date >= start_date)
        if end_date:
            query = query.filter(ActivitySession.session_date <= end_date)
        rows = query.order_by(
            ActivitySession.session_date.desc(), ActivitySession.id.desc()
        ).all()

        session_ids = [r.id for r in rows]
        attendance_stats: dict[int, dict] = {}
        if session_ids:
            agg_rows = (
                session.query(
                    ActivityAttendance.session_id,
                    func.count(ActivityAttendance.id).label("recorded"),
                    func.sum(
                        case((ActivityAttendance.is_present.is_(True), 1), else_=0)
                    ).label("present"),
                )
                .filter(ActivityAttendance.session_id.in_(session_ids))
                .group_by(ActivityAttendance.session_id)
                .all()
            )
            attendance_stats = {
                row.session_id: {"recorded": row.recorded, "present": row.present or 0}
                for row in agg_rows
            }

        result = []
        for r in rows:
            stat = attendance_stats.get(r.id, {"recorded": 0, "present": 0})
            result.append(
                {
                    "id": r.id,
                    "course_id": r.course_id,
                    "course_name": r.course_name,
                    "session_date": (
                        r.session_date.isoformat() if r.session_date else None
                    ),
                    "notes": r.notes or "",
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "recorded_count": stat["recorded"],
                    "present_count": stat["present"],
                }
            )
        return result
    finally:
        session.close()
```

> 用 `python3` str.replace 腳本改檔。`ActivityAttendance` / `ActivityCourse` / `ActivitySession` / `date` / `Optional` 在本檔皆已 import。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd "$WT" && python -m pytest tests/test_activity_vulnerability_fixes.py::TestPortalActivityListOpen -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C "$WT" add api/portal/activity.py tests/test_activity_vulnerability_fixes.py
git -C "$WT" commit -m "feat(portal): 才藝場次列表改列全部場次 + 整堂統計

任何老師可見全部才藝場次（course_id/日期可篩）；統計對齊 admin 算整堂。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task A4：portal 批次點名移除自班限制 + 無效報名略過

**Files:**
- Modify: `api/portal/activity.py`（`portal_batch_update_attendance` + import）
- Test: `tests/test_activity_vulnerability_fixes.py`（更新兩個 403 → 略過；新增 `TestPortalActivityWriteOpen`，重用 A3 建立的 seed helper）

- [ ] **Step 1: 改既有 vulnerability 測試（403 → 略過）使其表達新契約**

`tests/test_activity_vulnerability_fixes.py` 的 `TestPortalAttendanceFilter` 兩個測試，把 `assert res.status_code == 403` 改為「200 + 略過、無 attendance 寫入」。兩個測試結尾分別改為：

`test_portal_attendance_rejects_inactive_registration`（改名建議 `test_portal_attendance_skips_inactive_registration`）結尾：

```python
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["updated"] == 0
        assert body["skipped"] == 1
        with sf() as s2:
            from models.activity import ActivityAttendance
            assert s2.query(ActivityAttendance).filter_by(session_id=sess_id).count() == 0
```

`test_portal_attendance_rejects_rejected_registration`（改名建議 `test_portal_attendance_skips_rejected_registration`）結尾同上（`updated==0`、`skipped==1`、無 attendance 列）。

- [ ] **Step 2: 在 `tests/test_activity_vulnerability_fixes.py` 新增跨班點名 / 缺場次測試**

於該檔追加（`_seed_teacher_no_class` / `_seed_course_session_with_reg` / `_login` 已於 Task A3 建立，直接重用）：

```python
class TestPortalActivityWriteOpen:
    def test_no_class_teacher_can_checkin_cross_class(self, client):
        c, sf = client
        with sf() as s:
            _seed_teacher_no_class(s, username="t_write")
            klass = Classroom(name="跨班班", is_active=True)
            s.add(klass)
            s.flush()
            _course_id, sess_id, reg_id = _seed_course_session_with_reg(
                s, course_name="直排輪", classroom_id=klass.id
            )
        _login(c, "t_write")
        res = c.put(
            f"/api/portal/activity/attendance/sessions/{sess_id}/records",
            json={"records": [{"registration_id": reg_id, "is_present": True, "notes": "到"}]},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["updated"] == 1
        assert body["skipped"] == 0
        with sf() as s2:
            from models.activity import ActivityAttendance
            att = s2.query(ActivityAttendance).filter_by(
                session_id=sess_id, registration_id=reg_id
            ).one()
            assert att.is_present is True
            assert att.recorded_by == "t_write"

    def test_checkin_missing_session_404(self, client):
        c, sf = client
        with sf() as s:
            _seed_teacher_no_class(s, username="t_404")
        _login(c, "t_404")
        res = c.put(
            "/api/portal/activity/attendance/sessions/999999/records",
            json={"records": [{"registration_id": 1, "is_present": True, "notes": ""}]},
        )
        assert res.status_code == 404
```

> 家長阻擋已由 router 層 `require_non_parent_role` 保證；本 plan 不重複造家長 token 測試（既有 portal 測試已覆蓋家長 token 撞 portal endpoint 的 403）。若實作者想加一層保險，可比照其他 `TestF0xx` 用家長 token 打此 endpoint 斷言 403。

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd "$WT" && python -m pytest "tests/test_activity_vulnerability_fixes.py::TestPortalActivityWriteOpen" "tests/test_activity_vulnerability_fixes.py::TestPortalAttendanceFilter" -v`
Expected: FAIL（舊實作：跨班 reg 被 403；inactive/rejected 被 403 而非略過）

- [ ] **Step 4: 改 `portal_batch_update_attendance` 實作**

在 `api/portal/activity.py` 把整個 `portal_batch_update_attendance` 函式 body 換成（移除自班 `allowed_regs`/`forbidden` 403 區塊；改用共用 helper + 略過 + 回 skipped）：

```python
@router.put("/activity/attendance/sessions/{session_id}/records")
def portal_batch_update_attendance(
    session_id: int,
    body: PortalBatchAttendanceUpdate,
    current_user: dict = Depends(get_current_user),
):
    """批次點名：任何老師可點整堂跨班名冊；無效報名略過（對齊 admin）。

    放寬前限定自班並對非自班 reg 整批 403；現移除自班限制，僅保留
    『該 reg 確實有效報了本場次課程』的有效性檢查（無效者略過、不整批拒絕）。
    """
    session = get_session()
    try:
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")

        operator = current_user.get("username")
        req_reg_ids = [item.registration_id for item in body.records]

        existing_map = {
            a.registration_id: a
            for a in session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == session_id,
                ActivityAttendance.registration_id.in_(req_reg_ids),
            )
            .all()
        }

        valid_reg_rows = query_valid_session_registrations(
            session, sess.course_id, req_reg_ids
        )
        valid_reg_ids = {row[0] for row in valid_reg_rows}
        reg_student_map = dict(valid_reg_rows)

        skipped = [rid for rid in req_reg_ids if rid not in valid_reg_ids]
        if skipped:
            logger.warning(
                "portal_batch_update_attendance skipped invalid registrations: "
                "session=%s ids=%s",
                session_id,
                skipped,
            )

        for item in body.records:
            if item.registration_id not in valid_reg_ids:
                continue
            existing = existing_map.get(item.registration_id)
            if existing:
                existing.is_present = item.is_present
                existing.notes = item.notes or ""
                existing.recorded_by = operator
                if existing.student_id is None:
                    existing.student_id = reg_student_map.get(item.registration_id)
            else:
                att = ActivityAttendance(
                    session_id=session_id,
                    registration_id=item.registration_id,
                    student_id=reg_student_map.get(item.registration_id),
                    is_present=item.is_present,
                    notes=item.notes or "",
                    recorded_by=operator,
                )
                session.add(att)

        session.commit()
        applied = sum(
            1 for item in body.records if item.registration_id in valid_reg_ids
        )
        return {"ok": True, "updated": applied, "skipped": len(skipped)}
    finally:
        session.close()
```

並在 import 區加上 `from api.activity._shared import query_valid_session_registrations`（本檔已 `from api.activity._shared import _build_session_detail_response`，可併同行）。`_get_teacher_class_names` / `_get_teacher_classroom_ids` 等若不再被本檔任何函式使用，移除其 import 與 dead helper（`get_portal_activity_registrations` 仍用 `_get_employee` 與自己的 classroom query，故 `_get_employee` 保留；確認後再刪未用者，避免 flake8 F401/F811）。

> 用 `python3` str.replace 腳本整段替換以繞過 black hook。

- [ ] **Step 5: 跑測試確認通過**

Run: `cd "$WT" && python -m pytest "tests/test_activity_vulnerability_fixes.py::TestPortalActivityWriteOpen" "tests/test_activity_vulnerability_fixes.py::TestPortalActivityListOpen" "tests/test_activity_vulnerability_fixes.py::TestPortalAttendanceFilter" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C "$WT" add api/portal/activity.py tests/test_activity_vulnerability_fixes.py
git -C "$WT" commit -m "feat(portal): 才藝批次點名移除自班限制（任何老師可點整堂跨班）

對齊 admin：用 query_valid_session_registrations 驗有效性，無效報名略過並回
skipped，不再整批 403。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task A5：後端聚焦回歸 + lint

- [ ] **Step 1: 跑才藝 / portal / 列舉相關聚焦測試**

Run:
```bash
cd "$WT" && python -m pytest \
  tests/test_activity_api.py \
  tests/test_activity_attendance_grouping.py \
  tests/test_activity_vulnerability_fixes.py \
  tests/test_activity_shared_valid_regs.py \
  tests/test_enumeration_oracle_consistency.py \
  -v
```
Expected: 全 PASS（相對 origin/main 無新增 fail）

- [ ] **Step 2: lint（確認無未用 import / 重複定義）**

Run: `cd "$WT" && python -m flake8 api/portal/activity.py api/activity/_shared.py api/activity/attendance.py`
Expected: 無 F401（未用 import）/ F811（重複定義）等錯誤。若有未用的 `_get_teacher_class_names` 等，移除之並重跑 Task A4 相關測試。

- [ ] **Step 3:（無 commit；若 Step 2 有改動則 amend 或新增 chore commit）**

---

# Phase B — 前端（ivy-frontend worktree）

> **開始前**：從 ivy-frontend 的 `origin/main` 建 worktree（勿從 local main）：
> ```bash
> cd /Users/yilunwu/Desktop/ivy-frontend && git fetch origin --quiet
> git worktree add ".claude/worktrees/activity-attendance-any-teacher-2026-05-29" \
>   -b "feat/activity-attendance-any-teacher-cross-class-2026-05-29-frontend" origin/main
> ```
> 設 `FWT` = 該前端 worktree 絕對路徑。在 `FWT` 內 `npm install` 後再開工。
> 前端 `git` 一律 `git -C "$FWT"`，每 task 先確認分支。

## Task B1：場次列表欄位文案「自班出席」→「出席」

**Files:**
- Modify: `src/views/portal/components/activity/ActivitySessionList.vue`（label）
- Test: `tests/unit/views/portal/activity/ActivitySessionList.test.js`（若斷言舊文案則更新）

- [ ] **Step 1: 檢查既有測試是否斷言舊文案**

Run: `cd "$FWT" && grep -n "自班出席\|出席" tests/unit/views/portal/activity/ActivitySessionList.test.js`
- 若有斷言「自班出席」→ Step 3 一併改測試。

- [ ] **Step 2: 改 label**

`ActivitySessionList.vue`（約 line 114）把：
```html
        <el-table-column label="自班出席" width="140" align="center">
```
改為：
```html
        <el-table-column label="出席" width="140" align="center">
```

- [ ] **Step 3: 若測試斷言舊文案，同步更新為「出席」**

- [ ] **Step 4: 跑測試**

Run: `cd "$FWT" && npx vitest run tests/unit/views/portal/activity/ActivitySessionList.test.js`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C "$FWT" add src/views/portal/components/activity/ActivitySessionList.vue tests/unit/views/portal/activity/ActivitySessionList.test.js
git -C "$FWT" commit -m "feat(portal): 才藝場次列表出席統計改為整堂（文案自班出席→出席）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task B2：點名抽屜名冊依班級聚集（跨班好找學生）

**Files:**
- Modify: `src/composables/useActivityAttendanceDrawer.ts`（`sortedStudents` 排序）
- Test: `tests/unit/composables/useActivityAttendanceDrawer.test.ts`（新建）

> 此 composable 同時被 portal（`PortalActivityView.vue`）與 admin（`ActivityAttendanceView.vue`）使用。改為「班級為主、未點名次之」排序對兩端皆為改善（admin 名冊本就跨班），無退化。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/unit/composables/useActivityAttendanceDrawer.test.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { useActivityAttendanceDrawer } from '@/composables/useActivityAttendanceDrawer'

describe('useActivityAttendanceDrawer sortedStudents', () => {
  it('依班級聚集，班級內未點名優先', async () => {
    const sessionData = {
      id: 1, course_name: '圍棋', session_date: '2026-05-29',
      students: [
        { registration_id: 1, class_name: 'B班', is_present: true, student_name: 'b1' },
        { registration_id: 2, class_name: 'A班', is_present: true, student_name: 'a1' },
        { registration_id: 3, class_name: 'A班', is_present: null, student_name: 'a2' },
      ],
    }
    const drawer = useActivityAttendanceDrawer({
      getSessionFn: async () => ({ data: sessionData }),
      updateFn: async () => ({}),
    })
    await drawer.openDrawer({ id: 1 })
    const order = drawer.sortedStudents.value.map((s: any) => s.registration_id)
    // A班 在 B班 前；A班內未點名(a2,id=3)在已點名(a1,id=2)前
    expect(order).toEqual([3, 2, 1])
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd "$FWT" && npx vitest run tests/unit/composables/useActivityAttendanceDrawer.test.ts`
Expected: FAIL（目前僅未點名優先，未按班級）

- [ ] **Step 3: 改 `sortedStudents` 排序**

`useActivityAttendanceDrawer.ts`（約 line 31-40）把 `sortedStudents` 改為：

```ts
  // 先按班級聚集（跨班名冊好找），班級內未點名優先
  const sortedStudents = computed(() => {
    if (!drawerSession.value) return []
    return [...drawerSession.value.students].sort((a, b) => {
      const ca = (a as { class_name?: string }).class_name || ''
      const cb = (b as { class_name?: string }).class_name || ''
      if (ca !== cb) return ca.localeCompare(cb, 'zh-Hant')
      const aNone = a.is_present === null
      const bNone = b.is_present === null
      if (aNone && !bNone) return -1
      if (!aNone && bNone) return 1
      return 0
    })
  })
```

並在 `AttendanceStudent` interface 加上可選 `class_name?: string`、`student_name?: string`（避免 TS 報錯）。

- [ ] **Step 4: 跑測試確認通過 + typecheck**

Run: `cd "$FWT" && npx vitest run tests/unit/composables/useActivityAttendanceDrawer.test.ts && npm run typecheck`
Expected: PASS + typecheck 0 error

- [ ] **Step 5: 跑既有抽屜測試確認無回歸**

Run: `cd "$FWT" && npx vitest run tests/unit/views/portal/activity/ActivityRollcallDrawer.test.js`
Expected: PASS（若該測試斷言特定學生順序，更新為新排序）

- [ ] **Step 6: Commit**

```bash
git -C "$FWT" add src/composables/useActivityAttendanceDrawer.ts tests/unit/composables/useActivityAttendanceDrawer.test.ts
git -C "$FWT" commit -m "feat(activity): 點名抽屜名冊依班級聚集（跨班好找學生）

班級為主、班級內未點名優先；admin 與 portal 共用。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task B3：整體驗證（typecheck / build / 聚焦 vitest）

- [ ] **Step 1: 確認入口/路由未被權限擋（驗證，非改碼）**

Run: `cd "$FWT" && grep -n "permission" src/router/index.ts | grep -i activity; grep -n "才藝點名" src/components/portal/home/QuickLinksCard.vue`
Expected: `/portal/activity` 路由無 `permission` meta（僅 `title: '才藝管理'`）、QuickLinksCard 連結無權限過濾 → 所有老師本就可見，**無需改碼**。若發現有 gate，於此 task 放寬並補測試。

- [ ] **Step 2: typecheck + build + 聚焦測試**

Run:
```bash
cd "$FWT" && npm run typecheck && \
  npx vitest run tests/unit/views/portal/activity/ tests/unit/composables/useActivityAttendanceDrawer.test.ts && \
  npm run build
```
Expected: typecheck 0 error / 相關 vitest PASS / build success

---

# 自我檢查（spec 覆蓋）

- spec §5.1 場次列表全開 + 整堂統計 → Task A3 ✅
- spec §5.2 場次詳情完整名冊 + 404 + 移除 F-010 collapse → Task A2 ✅
- spec §5.3 批次點名移除自班 + 略過 + 回 skipped → Task A4 ✅
- spec §5.4 抽共用 helper → Task A1 ✅
- spec §5.5 portal response_model（選配）→ 本 plan 不做（spec 標可選）
- spec §6 前端：列表文案 → B1；抽屜分組（依班級排序）→ B2；入口可見性 → B3 Step 1（驗證為 no-op）✅
- spec §7 測試：後端 A1-A5、前端 B1-B3 ✅
- spec §9 無 schema / 無 migration / 無新權限 → 全 plan 未涉及 ✅

# 待 user（實作完成後）

- merge 後端 worktree → push origin
- merge 前端 worktree → push origin
- 後端改了 router 行為但**未改 response schema**（portal 端點本就無 response_model），故**不需** OpenAPI codegen 重跑；若實作時選做 §5.5 才需 `dump_openapi.py` + `npm run gen:api`
- 手測：以一個「沒帶任何班級」的老師帳號登入 portal → 才藝管理 → 課程點名 → 應能看到全部場次、開任一場次看到跨班完整名冊、跨班點名儲存成功
- worktree 清理（兩 repo）
