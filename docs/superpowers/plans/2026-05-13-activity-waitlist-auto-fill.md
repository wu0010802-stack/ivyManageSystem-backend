# 才藝候補名單自動遞補補完 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已開發 60% 的候補名單自動遞補補到可營運：加 in-process scheduler 自動掃過期、加 T-6h 最後提醒、家長公開頁顯示候補位次、admin 候補抽屜加手動升位按鈕。

**Architecture:** 後端仿既有 7 個 scheduler pattern（`services/*_scheduler.py`）新增 in-process scheduler，每 5 分鐘呼叫已存在但需擴充的 `sweep_expired_pending_promotions()`。資料層加 `final_reminder_sent_at` 欄位區隔 T-24h 與 T-6h 戳記。公開查詢 API 擴 response 加 `waitlist_position` / `waitlist_total`。前端 admin 與公開頁分別接 UI。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy / Alembic / asyncio scheduler；Vue 3 + Vite + Vitest

**Spec:** `docs/superpowers/specs/2026-05-13-activity-waitlist-auto-fill-design.md`

**Branches:**
- 後端：`feat/activity-waitlist-autofill-backend`（已開，已 commit spec）
- 前端：`feat/activity-waitlist-autofill-frontend`（Task 9 開）

---

## File Structure

### 後端（ivy-backend）

| 動作 | 路徑 | 責任 |
|------|------|------|
| Create | `alembic/versions/<rev>_add_final_reminder_to_registration_courses.py` | DB 欄位 |
| Modify | `models/activity.py:222-228` | RegistrationCourse 加欄位 |
| Modify | `services/line_service.py:480-530` | 加 final reminder 模板 |
| Modify | `services/activity_service.py:779-883` | sweep 擴 T-6h 與失敗回滾邏輯 |
| Modify | `api/activity/public.py` | query-by-token response 加 position |
| Create | `services/activity_waitlist_scheduler.py` | in-process 排程 |
| Modify | `main.py` startup/shutdown 區段 | 掛載 scheduler |
| Modify | `.env.example` | 環境變數 |
| Create | `tests/test_activity_waitlist_scheduler.py` | scheduler 單元測試 |
| Modify | `tests/test_activity_waitlist_promotion.py` | 補 T-6h 與失敗回滾測試 |
| Modify | `tests/test_activity_public_query_token_phase3.py` 或新增 | 補 position 測試 |

### 前端（ivy-frontend）

| 動作 | 路徑 | 責任 |
|------|------|------|
| Modify | `src/views/public/ActivityPublicQueryView.vue` | 候補位次顯示 |
| Modify | `src/views/activity/ActivityCourseView.vue` | 候補 Drawer 升位按鈕 |
| Modify | `src/views/public/__tests__/ActivityPublicQueryView.test.js` 或新增 | 位次 UI 測試 |
| Modify | `src/views/activity/__tests__/ActivityCourseView.test.js` 或新增 | 升位 UI 測試 |

---

## 後端任務（Branch: `feat/activity-waitlist-autofill-backend`）

### Task 1: Migration — 加 `final_reminder_sent_at` 欄位

**Files:**
- Create: `ivy-backend/alembic/versions/<auto-rev>_add_final_reminder_to_registration_courses.py`

- [ ] **Step 1: 確認當前 head revision**

```bash
cd ~/Desktop/ivy-backend && alembic heads
```

記下回傳的 revision id（例如 `a1b2c3d4e5f6`），作為新 migration 的 `down_revision`。

- [ ] **Step 2: 用 alembic 產生空 migration**

```bash
cd ~/Desktop/ivy-backend && alembic revision -m "add final_reminder_sent_at to registration_courses"
```

產生的檔案會在 `alembic/versions/`，記下 revision id。

- [ ] **Step 3: 填入 upgrade / downgrade**

仿 `alembic/versions/20260419_h6c7d8e9f0a1_add_waitlist_promotion_fields.py` 的 `inspector` 防呆風格：

```python
"""add final_reminder_sent_at to registration_courses

T-6h 最後提醒戳記，與既有 reminder_sent_at（T-24h）區隔。

Revision ID: <auto>
Revises: <previous head>
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "<auto generated>"
down_revision = "<previous head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    if "final_reminder_sent_at" not in existing_cols:
        op.add_column(
            "registration_courses",
            sa.Column("final_reminder_sent_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    if "final_reminder_sent_at" in existing_cols:
        op.drop_column("registration_courses", "final_reminder_sent_at")
```

- [ ] **Step 4: 跑 migration 並驗證**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade heads
```

Expected: `Running upgrade ... -> <new rev>, add final_reminder_sent_at to registration_courses`

驗證：
```bash
psql "postgresql://yilunwu@localhost:5432/ivymanagement" -c "\d registration_courses" | grep final_reminder
```
Expected: `final_reminder_sent_at | timestamp without time zone |`

- [ ] **Step 5: 跑 downgrade 確認可逆，再 upgrade 回來**

```bash
cd ~/Desktop/ivy-backend && alembic downgrade -1 && alembic upgrade heads
```

Expected: 兩次都 OK，無 error。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add alembic/versions/*_add_final_reminder_to_registration_courses.py
git commit -m "feat(activity): migration add final_reminder_sent_at to registration_courses

支援 T-6h 最後提醒戳記，與既有 reminder_sent_at（T-24h）區隔。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Model — `RegistrationCourse` 加欄位

**Files:**
- Modify: `ivy-backend/models/activity.py:222-228`

- [ ] **Step 1: Write the failing test**

擴 `tests/test_activity_waitlist_promotion.py` — 在檔案結尾加：

```python
def test_final_reminder_sent_at_field_exists(session):
    """final_reminder_sent_at 欄位應存在於 RegistrationCourse model。"""
    rc = RegistrationCourse(
        registration_id=1,
        course_id=1,
        status="promoted_pending",
        price_snapshot=1000,
        final_reminder_sent_at=None,
    )
    assert hasattr(rc, "final_reminder_sent_at")
    assert rc.final_reminder_sent_at is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::test_final_reminder_sent_at_field_exists -v
```

Expected: FAIL（`'RegistrationCourse' object has no attribute 'final_reminder_sent_at'` 或 TypeError）

- [ ] **Step 3: 加欄位定義**

`models/activity.py`，找到既有 `reminder_sent_at = Column(...)`（第 227 行附近），在其後加：

```python
    # 候補升正式時間；confirm_deadline 為家長確認期限；reminder_sent_at 為 T-24h 提醒已發時間。
    promoted_at = Column(DateTime, nullable=True, comment="候補轉正起始時間")
    confirm_deadline = Column(DateTime, nullable=True, comment="家長確認截止時間")
    reminder_sent_at = Column(DateTime, nullable=True, comment="T-24h 提醒發送時間")
    final_reminder_sent_at = Column(
        DateTime, nullable=True, comment="T-6h 最後提醒發送時間"
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::test_final_reminder_sent_at_field_exists -v
```

Expected: PASS

- [ ] **Step 5: 跑整套 waitlist 測試確保無 regression**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py -v
```

Expected: 全 PASS（基線測試不應受影響）

- [ ] **Step 6: Commit**

```bash
git add models/activity.py tests/test_activity_waitlist_promotion.py
git commit -m "feat(activity): model add final_reminder_sent_at column

對應 Task 1 migration。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: LINE 通知模板 — `notify_activity_waitlist_final_reminder`

**Files:**
- Modify: `ivy-backend/services/line_service.py:480-530`

- [ ] **Step 1: Write the failing test**

`tests/test_activity_waitlist_promotion.py` 末尾加：

```python
def test_line_service_has_final_reminder_method():
    """LineService 應有 notify_activity_waitlist_final_reminder 方法。"""
    from services.line_service import LineService
    assert hasattr(LineService, "notify_activity_waitlist_final_reminder")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::test_line_service_has_final_reminder_method -v
```

Expected: FAIL（method 不存在）

- [ ] **Step 3: 加 method**

`services/line_service.py` — 在 `notify_activity_waitlist_promotion_reminder`（line 506）後加：

```python
    def notify_activity_waitlist_final_reminder(
        self,
        student_name: str,
        course_name: str,
        confirm_deadline,
    ) -> bool:
        """T-6h 最後提醒；回傳是否推送成功（True=成功，False=失敗或未啟用）。

        失敗時 caller 不應寫 final_reminder_sent_at，下輪再重試。
        """
        if not self._enabled():
            return False
        try:
            remaining = confirm_deadline - _now_taipei_naive()
            hours = max(1, int(remaining.total_seconds() // 3600))
            text = (
                f"⏰ 最後提醒｜{student_name} 的「{course_name}」候補升位\n"
                f"剩餘約 {hours} 小時，請盡快點選下方連結確認，逾期將自動放棄。\n"
                f"{self._confirmation_link_for_activity()}"
            )
            return self._push(text)
        except Exception:
            logger.exception(
                "notify_activity_waitlist_final_reminder failed student=%s course=%s",
                student_name,
                course_name,
            )
            return False
```

注意：`_now_taipei_naive` 與 `_confirmation_link_for_activity` 需確認既有實作；若 helper 命名不同，照既有 `notify_activity_waitlist_promotion_reminder` 的寫法 1:1 仿造（grep 原檔即可看到）。

- [ ] **Step 4: 確認既有兩個 helper 可用**

```bash
cd ~/Desktop/ivy-backend && grep -n "_now_taipei_naive\|_confirmation_link\|def _push" services/line_service.py | head -10
```

若 helper 名稱不同，調整 Step 3 程式碼以對齊既有風格。

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::test_line_service_has_final_reminder_method -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/line_service.py tests/test_activity_waitlist_promotion.py
git commit -m "feat(line): 加候補升位 T-6h 最後提醒模板

回傳 bool 標示推送結果；caller 失敗時應重試而非寫戳記。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 擴 `sweep_expired_pending_promotions` — T-6h 邏輯 + 推送失敗回滾

**Files:**
- Modify: `ivy-backend/services/activity_service.py:779-883`
- Modify: `ivy-backend/tests/test_activity_waitlist_promotion.py`

關鍵設計：
- 新增 T-6h 分支：剩餘 ≤ 6h 且 `final_reminder_sent_at IS NULL` 則發送
- 既有 T-24h：剩餘 ≤ 24h 且 `reminder_sent_at IS NULL` 則發送（不變）
- 兩段都改為「推送成功才寫戳記」（既有 code 是先 try/except 後寫戳記，等於失敗也寫）
- 提醒查詢加 `with_for_update(skip_locked=True)` 防多 worker 重發
- 回傳 dict 新增 `"final_reminded"` 計數

- [ ] **Step 1: Write failing test — T-6h reminder 發送**

`tests/test_activity_waitlist_promotion.py` 的 `TestSweepExpired` class 內加：

```python
    def test_final_reminder_sent_when_within_6h(self, session, svc):
        """剩餘 ≤ 6h 且 final_reminder_sent_at NULL 時應發送並寫戳記。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.promoted_at = datetime.utcnow() - timedelta(hours=42)
        rc.confirm_deadline = datetime.utcnow() + timedelta(hours=5)  # 剩 5h
        rc.reminder_sent_at = datetime.utcnow() - timedelta(hours=18)  # T-24h 已發
        rc.final_reminder_sent_at = None
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.commit()

        assert result["final_reminded"] == 1
        session.refresh(rc)
        assert rc.final_reminder_sent_at is not None

    def test_final_reminder_not_resent(self, session, svc):
        """final_reminder_sent_at 非 NULL 時不重發。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.confirm_deadline = datetime.utcnow() + timedelta(hours=3)
        rc.final_reminder_sent_at = datetime.utcnow() - timedelta(hours=1)
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        assert result["final_reminded"] == 0

    def test_reminder_stamp_not_written_on_line_failure(
        self, session, svc, monkeypatch
    ):
        """LINE 推送失敗時不應寫 final_reminder_sent_at（下輪重試）。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.confirm_deadline = datetime.utcnow() + timedelta(hours=4)
        rc.reminder_sent_at = datetime.utcnow()
        session.flush()

        class StubLineService:
            def notify_activity_waitlist_final_reminder(self, *a, **kw):
                return False  # 模擬推送失敗

            def notify_activity_waitlist_promotion_reminder(self, *a, **kw):
                return False

            def notify_activity_waitlist_promotion_expired(self, *a, **kw):
                return False

        svc._line_svc = StubLineService()

        result = svc.sweep_expired_pending_promotions(session)
        session.refresh(rc)
        assert rc.final_reminder_sent_at is None  # 失敗則不寫戳記
        assert result["final_reminded"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::TestSweepExpired::test_final_reminder_sent_when_within_6h -v
```

Expected: FAIL（`KeyError: 'final_reminded'` 或邏輯未實作）

- [ ] **Step 3: 修改 `_get_final_reminder_offset_hours` helper（新增）**

`services/activity_service.py:41-47` 後加：

```python
def _get_final_reminder_offset_hours() -> int:
    """T-6h 最後提醒的 deadline 前置時數。預設 6h。"""
    try:
        val = int(os.getenv("ACTIVITY_WAITLIST_FINAL_REMINDER_OFFSET_HOURS", "6"))
        return val if val > 0 else 6
    except (TypeError, ValueError):
        return 6
```

- [ ] **Step 4: 改寫 `sweep_expired_pending_promotions`**

取代 `services/activity_service.py:779-883` 整個方法：

```python
    def sweep_expired_pending_promotions(self, session) -> dict:
        """掃描過期未確認的 promoted_pending；逾期者刪除並遞補下一位；
        同時發送 T-24h 與 T-6h 分階段提醒（各只發一次）。

        回傳 {"expired": N, "reminded": M, "final_reminded": K}。

        關鍵設計：
        - 過期查詢與提醒查詢都用 SELECT FOR UPDATE SKIP LOCKED 防多 worker 重複
        - LINE 推送失敗時不寫戳記，下輪重試
        """
        now = _now_taipei_naive()
        reminder_offset = timedelta(hours=_get_reminder_offset_hours())
        final_reminder_offset = timedelta(hours=_get_final_reminder_offset_hours())
        reminder_threshold = now + reminder_offset
        final_reminder_threshold = now + final_reminder_offset

        # 1. 過期者：刪除 + 遞補（既有邏輯不動）
        expired_rows = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "promoted_pending",
                RegistrationCourse.confirm_deadline.isnot(None),
                RegistrationCourse.confirm_deadline < now,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        expired_count = 0
        expired_per_course: dict[int, int] = {}
        for rc, student_name in expired_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .first()
            )
            course_name = course.name if course else f"course_{rc.course_id}"
            reg_id = rc.registration_id
            course_id = rc.course_id
            session.delete(rc)
            self.log_change(
                session,
                reg_id,
                student_name or str(reg_id),
                "候補轉正逾期放棄",
                f"課程「{course_name}」逾期未確認，系統自動放棄",
                "system",
            )
            session.flush()
            expired_per_course[course_id] = expired_per_course.get(course_id, 0) + 1
            if self._line_svc is not None:
                try:
                    self._line_svc.notify_activity_waitlist_promotion_expired(
                        student_name or str(reg_id), course_name
                    )
                except Exception:
                    logger.exception("發送候補轉正逾期通知失敗 reg=%s", reg_id)
            expired_count += 1

        for course_id, count in expired_per_course.items():
            for _ in range(count):
                self._auto_promote_first_waitlist(session, course_id)

        # 2. T-6h 最後提醒（剩餘 ≤ 6h 且 final_reminder_sent_at NULL）
        final_reminder_rows = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "promoted_pending",
                RegistrationCourse.confirm_deadline.isnot(None),
                RegistrationCourse.confirm_deadline >= now,
                RegistrationCourse.confirm_deadline <= final_reminder_threshold,
                RegistrationCourse.final_reminder_sent_at.is_(None),
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        final_reminded_count = 0
        for rc, student_name in final_reminder_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .first()
            )
            course_name = course.name if course else f"course_{rc.course_id}"
            sent_ok = False
            if self._line_svc is not None:
                try:
                    sent_ok = bool(
                        self._line_svc.notify_activity_waitlist_final_reminder(
                            student_name or str(rc.registration_id),
                            course_name,
                            rc.confirm_deadline,
                        )
                    )
                except Exception:
                    logger.exception(
                        "發送候補轉正最後提醒失敗 reg=%s course=%s",
                        rc.registration_id,
                        rc.course_id,
                    )
                    sent_ok = False
            if sent_ok:
                rc.final_reminder_sent_at = now
                final_reminded_count += 1
            # 失敗：不寫戳記，下輪重試

        # 3. T-24h 提醒（剩餘 ≤ 24h 且 reminder_sent_at NULL；不包含已過 T-6h 區段也沒關係）
        reminder_rows = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "promoted_pending",
                RegistrationCourse.confirm_deadline.isnot(None),
                RegistrationCourse.confirm_deadline >= now,
                RegistrationCourse.confirm_deadline <= reminder_threshold,
                RegistrationCourse.reminder_sent_at.is_(None),
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        reminded_count = 0
        for rc, student_name in reminder_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .first()
            )
            course_name = course.name if course else f"course_{rc.course_id}"
            sent_ok = False
            if self._line_svc is not None:
                try:
                    result = self._line_svc.notify_activity_waitlist_promotion_reminder(
                        student_name or str(rc.registration_id),
                        course_name,
                        rc.confirm_deadline,
                    )
                    # 既有方法可能回 None；視 None 為成功保留向後相容
                    sent_ok = result is None or bool(result)
                except Exception:
                    logger.exception(
                        "發送候補轉正提醒失敗 reg=%s course=%s",
                        rc.registration_id,
                        rc.course_id,
                    )
                    sent_ok = False
            else:
                # 沒有 line service 時視為「不需發送」，避免一直查詢；寫戳記
                sent_ok = True
            if sent_ok:
                rc.reminder_sent_at = now
                reminded_count += 1

        return {
            "expired": expired_count,
            "reminded": reminded_count,
            "final_reminded": final_reminded_count,
        }
```

- [ ] **Step 5: 跑 Step 1 的測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py::TestSweepExpired -v
```

Expected: 全 PASS（既有 + 新增 3 條都通過）

- [ ] **Step 6: 跑整套 waitlist 測試確保無 regression**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py -v
```

Expected: 全 PASS

- [ ] **Step 7: 更新 sweep router 回傳 final_reminded**

修 `api/activity/registrations.py:680-700` 的 sweep router log，把 `final_reminded` 也納入：

```python
        result = activity_service.sweep_expired_pending_promotions(session)
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.info(
            "手動觸發候補過期掃描：operator=%s expired=%s reminded=%s final_reminded=%s",
            current_user.get("username", ""),
            result["expired"],
            result["reminded"],
            result.get("final_reminded", 0),
        )
        return {"message": "候補過期掃描完成", **result}
```

- [ ] **Step 8: Commit**

```bash
git add services/activity_service.py api/activity/registrations.py tests/test_activity_waitlist_promotion.py
git commit -m "feat(activity): sweep 擴 T-6h 提醒並修正推送失敗回滾

- 新增 T-6h 最後提醒分支，使用 final_reminder_sent_at 戳記
- 提醒查詢加 SELECT FOR UPDATE SKIP LOCKED 防多 worker 重發
- 推送失敗時不寫戳記，下輪重試（既有 T-24h 也修正）
- sweep 回傳 dict 新增 final_reminded 計數

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 公開查詢加 `waitlist_position` / `waitlist_total`

**Files:**
- Modify: `ivy-backend/api/activity/public.py`
- Modify: `ivy-backend/tests/test_activity_public_query_token_phase3.py` 或新增測試

- [ ] **Step 1: 定位 query-by-token 端點**

```bash
cd ~/Desktop/ivy-backend && grep -n "query-by-token\|public_query_by_token\|def public_query" api/activity/public.py | head -10
```

記錄 endpoint 函式名與 response 組裝位置（通常會有一個 `_serialize_registration` 或類似 helper）。

- [ ] **Step 2: Write the failing test**

於 `tests/test_activity_public_query_token_phase3.py`（或新增 `tests/test_activity_public_waitlist_position.py`）加：

```python
def test_query_by_token_returns_waitlist_position(client, session_factory):
    """公開查詢回傳候補課程的 waitlist_position（自己也算）與 waitlist_total。"""
    # arrange: 課程容量 1，已 enrolled 1 人；候補 3 人，自己排第 2
    course = _make_course(session_factory, capacity=1)
    enrolled_reg = _make_reg_with_course(
        session_factory, course.id, status="enrolled"
    )
    first_wait = _make_reg_with_course(session_factory, course.id, status="waitlist")
    my_reg = _make_reg_with_course(session_factory, course.id, status="waitlist")
    third_wait = _make_reg_with_course(session_factory, course.id, status="waitlist")
    token = _make_query_token(session_factory, my_reg.id)

    # act
    resp = client.post(
        "/api/activity/public/query-by-token",
        json={"token": token, "phone": my_reg.parent_phone},
    )

    # assert
    assert resp.status_code == 200
    data = resp.json()
    rc = next(c for c in data["registration_courses"] if c["course_id"] == course.id)
    assert rc["status"] == "waitlist"
    assert rc["waitlist_position"] == 2  # 自己排第 2
    assert rc["waitlist_total"] == 3


def test_query_by_token_waitlist_excludes_promoted_pending(client, session_factory):
    """waitlist_position 計算只算 status='waitlist'，不算 promoted_pending。"""
    course = _make_course(session_factory, capacity=1)
    _make_reg_with_course(session_factory, course.id, status="enrolled")
    _make_reg_with_course(session_factory, course.id, status="promoted_pending")  # 不算
    my_reg = _make_reg_with_course(session_factory, course.id, status="waitlist")
    token = _make_query_token(session_factory, my_reg.id)

    resp = client.post(
        "/api/activity/public/query-by-token",
        json={"token": token, "phone": my_reg.parent_phone},
    )

    rc = next(c for c in resp.json()["registration_courses"] if c["course_id"] == course.id)
    assert rc["waitlist_position"] == 1
    assert rc["waitlist_total"] == 1


def test_query_by_token_position_null_for_enrolled(client, session_factory):
    """status='enrolled' 時 waitlist_position 與 total 都應為 null。"""
    course = _make_course(session_factory, capacity=5)
    my_reg = _make_reg_with_course(session_factory, course.id, status="enrolled")
    token = _make_query_token(session_factory, my_reg.id)

    resp = client.post(
        "/api/activity/public/query-by-token",
        json={"token": token, "phone": my_reg.parent_phone},
    )

    rc = next(c for c in resp.json()["registration_courses"] if c["course_id"] == course.id)
    assert rc.get("waitlist_position") is None
    assert rc.get("waitlist_total") is None
```

註：`_make_course` / `_make_reg_with_course` / `_make_query_token` 等 helper 若該檔已存在則沿用；若無則新建一個 helper 區塊（仿 `tests/test_activity_waitlist_promotion.py:58-94` 的工具函式風格）。

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_public_query_token_phase3.py -v -k waitlist_position
```

Expected: FAIL（response 缺欄位）

- [ ] **Step 4: 實作 — 在 query-by-token response serializer 加位次**

在 `api/activity/public.py` 序列化每筆 RegistrationCourse 的地方加：

```python
def _compute_waitlist_position_and_total(session, rc) -> tuple[int | None, int | None]:
    """計算候補位次與總人數；非 waitlist 狀態回 (None, None)。"""
    if rc.status != "waitlist":
        return (None, None)
    position = (
        session.query(RegistrationCourse)
        .filter(
            RegistrationCourse.course_id == rc.course_id,
            RegistrationCourse.status == "waitlist",
            RegistrationCourse.created_at <= rc.created_at,
        )
        .count()
    )
    total = (
        session.query(RegistrationCourse)
        .filter(
            RegistrationCourse.course_id == rc.course_id,
            RegistrationCourse.status == "waitlist",
        )
        .count()
    )
    return (position, total)
```

在序列化 RC 為 dict 的地方加：

```python
position, total = _compute_waitlist_position_and_total(session, rc)
rc_dict["waitlist_position"] = position
rc_dict["waitlist_total"] = total
```

註：實際 serializer 位置以 grep `def public_query_by_token` 或 `def serialize_registration_course` 在 `public.py` 找到的點為準。如該函式在 `api/activity/registrations.py` 或共用 helper 模組，加在那邊。

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_public_query_token_phase3.py -v -k waitlist_position
```

Expected: 全 PASS

- [ ] **Step 6: 跑既有 public 測試確認無 regression**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_public_query_token_phase3.py tests/test_activity_public_update_phase2.py -v
```

Expected: 全 PASS

- [ ] **Step 7: Commit**

```bash
git add api/activity/public.py tests/test_activity_public_query_token_phase3.py
git commit -m "feat(activity-public): 查詢 token 回傳候補位次與總人數

waitlist_position 自己算 1；不計入 promoted_pending；
非 waitlist 狀態回 null。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Scheduler service

**Files:**
- Create: `ivy-backend/services/activity_waitlist_scheduler.py`
- Create: `ivy-backend/tests/test_activity_waitlist_scheduler.py`

- [ ] **Step 1: Write the failing test**

`tests/test_activity_waitlist_scheduler.py`（新檔）：

```python
"""tests/test_activity_waitlist_scheduler.py — 候補名單排程器測試"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_scheduler_disabled_by_default(monkeypatch):
    """無 env 變數時 scheduler 應停用。"""
    monkeypatch.delenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", raising=False)
    from services import activity_waitlist_scheduler

    assert activity_waitlist_scheduler.scheduler_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_scheduler_enabled_via_env(monkeypatch, value):
    """環境變數正確值應啟用。"""
    monkeypatch.setenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", value)
    from services import activity_waitlist_scheduler

    assert activity_waitlist_scheduler.scheduler_enabled() is True


def test_check_and_sweep_once_returns_dict(monkeypatch):
    """check_and_sweep_once 應回傳 dict（含 expired / reminded / final_reminded）。"""
    from services import activity_waitlist_scheduler

    captured = {}

    def fake_sweep(session):
        captured["called"] = True
        return {"expired": 0, "reminded": 0, "final_reminded": 0}

    # monkey-patch service singleton
    monkeypatch.setattr(
        "services.activity_waitlist_scheduler._get_activity_service",
        lambda: type("S", (), {"sweep_expired_pending_promotions": staticmethod(fake_sweep)})(),
    )

    # session_scope 在測試中可能會試圖連 DB；我們也 stub
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.session_scope", lambda: FakeSession()
    )

    result = activity_waitlist_scheduler.check_and_sweep_once()
    assert captured["called"] is True
    assert result == {"expired": 0, "reminded": 0, "final_reminded": 0}


def test_check_and_sweep_once_idempotent(monkeypatch):
    """連跑兩次：第二次應無新異動（資料層 idempotent 已由 sweep 既有測試覆蓋）。
    此處只驗證 scheduler tick 不會因為連跑而拋例外。"""
    from services import activity_waitlist_scheduler

    def fake_sweep(session):
        return {"expired": 0, "reminded": 0, "final_reminded": 0}

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler._get_activity_service",
        lambda: type("S", (), {"sweep_expired_pending_promotions": staticmethod(fake_sweep)})(),
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.session_scope", lambda: FakeSession()
    )

    activity_waitlist_scheduler.check_and_sweep_once()
    activity_waitlist_scheduler.check_and_sweep_once()  # 不應拋
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_scheduler.py -v
```

Expected: FAIL（module not found）

- [ ] **Step 3: 建立 scheduler 檔**

`services/activity_waitlist_scheduler.py`：

```python
"""才藝候補名單過期掃描排程。

- 仿 salary_snapshot_scheduler 的 in-process loop pattern
- 每 ACTIVITY_WAITLIST_CHECK_INTERVAL 秒呼叫 sweep_expired_pending_promotions
- sweep 本身 idempotent 且使用 SELECT FOR UPDATE SKIP LOCKED；多 worker 同啟也安全
- 失敗 log warning，下次 tick 再嘗試（不中斷 loop）
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from models.base import session_scope

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = int(
    os.getenv("ACTIVITY_WAITLIST_CHECK_INTERVAL", "300")
)


def scheduler_enabled() -> bool:
    return os.getenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _get_activity_service() -> Any:
    """延遲 import 避免循環依賴。"""
    from services.activity_service import get_activity_service

    return get_activity_service()


def check_and_sweep_once() -> dict:
    """單次 tick：呼叫 sweep_expired_pending_promotions。回傳結果 dict。"""
    svc = _get_activity_service()
    with session_scope() as session:
        result = svc.sweep_expired_pending_promotions(session)
    return result


async def run_activity_waitlist_scheduler(stop_event: asyncio.Event) -> None:
    """每 CHECK_INTERVAL_SECONDS 巡檢一次；失敗 log 不中斷。"""
    logger.info(
        "activity waitlist scheduler started (interval=%ds)",
        CHECK_INTERVAL_SECONDS,
    )
    while not stop_event.is_set():
        try:
            result = check_and_sweep_once()
            if result.get("expired") or result.get("reminded") or result.get(
                "final_reminded"
            ):
                logger.info(
                    "activity waitlist scheduler tick: expired=%s reminded=%s final_reminded=%s",
                    result.get("expired", 0),
                    result.get("reminded", 0),
                    result.get("final_reminded", 0),
                )
        except Exception:
            logger.exception("activity waitlist scheduler tick failed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
```

- [ ] **Step 4: 確認 `get_activity_service` 存在**

```bash
cd ~/Desktop/ivy-backend && grep -n "^def get_activity_service\|^get_activity_service" services/activity_service.py | head -3
```

若沒有 `get_activity_service` 函式，看 service 是怎麼初始化的（grep `ActivityService(` 在 main.py 找實例化點）；在 scheduler 內改為直接實例化：

```python
def _get_activity_service():
    from services.activity_service import ActivityService
    return ActivityService()
```

並重新調整 test 的 monkey-patch 名稱。

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_scheduler.py -v
```

Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add services/activity_waitlist_scheduler.py tests/test_activity_waitlist_scheduler.py
git commit -m "feat(activity): 候補名單過期掃描排程器

仿 salary_snapshot_scheduler 的 in-process asyncio loop；
env ACTIVITY_WAITLIST_SCHEDULER_ENABLED 啟用；
預設 5 分鐘 tick；失敗 log 不中斷。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `main.py` 掛載 scheduler + `.env.example`

**Files:**
- Modify: `ivy-backend/main.py`
- Modify: `ivy-backend/.env.example`

- [ ] **Step 1: 找到 startup 區段掛點**

```bash
cd ~/Desktop/ivy-backend && grep -n "salary_snapshot_scheduler\|run_salary_snapshot_scheduler\|salary_snapshot_stop_event" main.py
```

定位三個位置：(a) import 區（line ~276）；(b) startup task 掛載（line ~278-281）；(c) shutdown 區段（stop_event.set + await）。

- [ ] **Step 2: 在 main.py 加 scheduler 啟動**

（a）import 區：與既有 scheduler 並排，仿 salary_snapshot_scheduler 的延遲 import 風格——通常在 startup function 內部 import：

```python
        from services import activity_waitlist_scheduler as _wl_sched
        if _wl_sched.scheduler_enabled():
            activity_waitlist_stop_event = asyncio.Event()
            activity_waitlist_task = asyncio.create_task(
                _wl_sched.run_activity_waitlist_scheduler(activity_waitlist_stop_event)
            )
            logger.info("activity waitlist scheduler 已啟用")
```

把整段加在 `salary_snapshot_scheduler` 掛載區塊後（main.py:276-281 之後）。注意要把 `activity_waitlist_stop_event` 與 `activity_waitlist_task` 變數提升到 function 外層或保存在某 dict 以便 shutdown 區段存取，比照其他 stop_event 的處理方式。

（b）shutdown 區段：仿其他 scheduler 的 stop_event.set() / await pattern：

```python
        if "activity_waitlist_stop_event" in locals() or hasattr(
            app.state, "activity_waitlist_stop_event"
        ):
            try:
                activity_waitlist_stop_event.set()
                await activity_waitlist_task
            except Exception:
                logger.exception("stopping activity waitlist scheduler failed")
```

實際語法以既有 `salary_snapshot_stop_event` 的處理風格為準。

- [ ] **Step 3: 啟動 server 驗證 startup log**

設 env 並啟動：

```bash
cd ~/Desktop/ivy-backend && ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1 \
    uvicorn main:app --reload --port 8088 2>&1 | head -40
```

Expected log 包含：
```
activity waitlist scheduler started (interval=300s)
activity waitlist scheduler 已啟用
```

Ctrl+C 停止。

- [ ] **Step 4: 更新 .env.example**

```bash
cd ~/Desktop/ivy-backend && grep -n "SALARY_AUTO_SNAPSHOT_ENABLED" .env.example | head -3
```

仿位置加入：

```
# 才藝候補名單過期掃描排程
ACTIVITY_WAITLIST_SCHEDULER_ENABLED=0
# ACTIVITY_WAITLIST_CHECK_INTERVAL=300         # 預設 5 分鐘
# ACTIVITY_WAITLIST_FINAL_REMINDER_OFFSET_HOURS=6  # T-X 最後提醒，預設 6h
```

- [ ] **Step 5: Commit**

```bash
git add main.py .env.example
git commit -m "feat(activity): main.py 掛載候補名單排程器 + .env.example

ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1 即啟用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 後端整合驗證

- [ ] **Step 1: 跑全套後端測試**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_activity_waitlist_promotion.py tests/test_activity_waitlist_scheduler.py tests/test_activity_public_query_token_phase3.py -v
```

Expected: 全 PASS

- [ ] **Step 2: 跑既有 activity 全套測試確認無 regression**

```bash
cd ~/Desktop/ivy-backend && pytest tests/ -k activity -v 2>&1 | tail -30
```

Expected: 全 PASS。觀察是否有 `test_activity_*` 系列因 sweep 行為改變而失敗。

- [ ] **Step 3: 手動端到端驗證（本機 DB）**

1. 啟動 server 開排程：`ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1 ACTIVITY_WAITLIST_CHECK_INTERVAL=30 uvicorn main:app --reload`
2. 透過 psql 建一筆 promoted_pending 並設 `confirm_deadline = NOW() - INTERVAL '1 minute'`
3. 等 30 秒
4. 檢查 log 應出現 `expired=1`
5. 檢查 DB：該 row 應已刪除；候補佇列下一位應升為 promoted_pending

```sql
-- 步驟 2 範例
UPDATE registration_courses
SET status='promoted_pending',
    confirm_deadline=NOW() - INTERVAL '1 minute',
    promoted_at=NOW() - INTERVAL '49 hours'
WHERE id = <挑一筆現有 waitlist 的 id>;
```

- [ ] **Step 4: 確認 main 沒衝突**

```bash
cd ~/Desktop/ivy-backend && git fetch origin && git log --oneline origin/main..HEAD | head -20
```

確認所有 commits 都在 `feat/activity-waitlist-autofill-backend` 分支上。

---

## 前端任務（Branch: `feat/activity-waitlist-autofill-frontend`）

### Task 9: 開前端分支 + 公開頁顯示候補位次

**Files:**
- Modify: `ivy-frontend/src/views/public/ActivityPublicQueryView.vue`
- Create/Modify: `ivy-frontend/src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js`

- [ ] **Step 1: 切前端 main + 開分支**

```bash
cd ~/Desktop/ivy-frontend && git checkout main && git pull --ff-only && \
    git checkout -b feat/activity-waitlist-autofill-frontend
```

- [ ] **Step 2: Write the failing test**

`src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js`（新檔）：

```javascript
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ActivityPublicQueryView from '../ActivityPublicQueryView.vue'

vi.mock('@/api/activityPublic', () => ({
  publicQueryByToken: vi.fn(),
}))

import { publicQueryByToken } from '@/api/activityPublic'

const mountView = () => mount(ActivityPublicQueryView, {
  global: {
    stubs: ['router-link'],
  },
})

describe('ActivityPublicQueryView — 候補位次顯示', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('候補課程顯示「目前第 N 位 / 共 M 位」', async () => {
    publicQueryByToken.mockResolvedValue({
      data: {
        student_name: '王小明',
        registration_courses: [
          {
            course_id: 1,
            course_name: '美術',
            status: 'waitlist',
            waitlist_position: 3,
            waitlist_total: 8,
          },
        ],
      },
    })
    const wrapper = mountView()
    await wrapper.find('[data-test="query-submit"]').trigger('click')
    await new Promise(r => setTimeout(r, 0))

    const txt = wrapper.text()
    expect(txt).toContain('候補')
    expect(txt).toMatch(/第\s*3\s*位/)
    expect(txt).toMatch(/共\s*8\s*位/)
  })

  it('waitlist_position == 1 時顯示「下一位」提示', async () => {
    publicQueryByToken.mockResolvedValue({
      data: {
        student_name: '王小明',
        registration_courses: [
          {
            course_id: 1, course_name: '美術', status: 'waitlist',
            waitlist_position: 1, waitlist_total: 5,
          },
        ],
      },
    })
    const wrapper = mountView()
    await wrapper.find('[data-test="query-submit"]').trigger('click')
    await new Promise(r => setTimeout(r, 0))

    expect(wrapper.text()).toContain('下一位')
  })

  it('waitlist_total == 1 顯示「唯一候補者」', async () => {
    publicQueryByToken.mockResolvedValue({
      data: {
        student_name: '王小明',
        registration_courses: [
          {
            course_id: 1, course_name: '美術', status: 'waitlist',
            waitlist_position: 1, waitlist_total: 1,
          },
        ],
      },
    })
    const wrapper = mountView()
    await wrapper.find('[data-test="query-submit"]').trigger('click')
    await new Promise(r => setTimeout(r, 0))

    expect(wrapper.text()).toContain('唯一候補者')
  })

  it('enrolled 課程不顯示位次區塊', async () => {
    publicQueryByToken.mockResolvedValue({
      data: {
        student_name: '王小明',
        registration_courses: [
          {
            course_id: 1, course_name: '美術', status: 'enrolled',
            waitlist_position: null, waitlist_total: null,
          },
        ],
      },
    })
    const wrapper = mountView()
    await wrapper.find('[data-test="query-submit"]').trigger('click')
    await new Promise(r => setTimeout(r, 0))

    expect(wrapper.text()).not.toContain('目前第')
    expect(wrapper.text()).not.toContain('候補中')
  })
})
```

註：若 `[data-test="query-submit"]` 在現有元件不存在，需配合 Step 3 同時加入。實際選擇器以該檔現有結構為準（grep `<button` 或 `submit`）。

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- ActivityPublicQueryView.waitlist
```

Expected: FAIL

- [ ] **Step 4: 實作 UI**

在 `ActivityPublicQueryView.vue` 候補狀態渲染處（grep `status === 'waitlist'` 或 `候補` 字眼）加：

```vue
<template>
  ...
  <div v-if="course.status === 'waitlist'" class="waitlist-info">
    <span class="badge badge-waitlist">⏳ 候補中</span>
    <template v-if="course.waitlist_position != null">
      <span v-if="course.waitlist_total === 1" class="position position--solo">
        您是目前唯一候補者
      </span>
      <span v-else class="position">
        目前第 <strong>{{ course.waitlist_position }}</strong> 位
        <span class="text-muted">/ 共 {{ course.waitlist_total }} 位</span>
        <small v-if="course.waitlist_position === 1" class="hint">
          您是下一位候補；如有空位將自動通知
        </small>
      </span>
    </template>
  </div>
  ...
</template>

<style scoped>
.waitlist-info {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  margin-top: var(--space-2);
}
.badge-waitlist {
  background: var(--color-warning-bg, #fff7e6);
  color: var(--color-warning-fg, #d97706);
  padding: var(--space-1) var(--space-2);
  border-radius: 999px;
  font-size: 0.85rem;
}
.position {
  font-size: 0.92rem;
  color: var(--color-text-secondary);
}
.position strong {
  color: var(--color-brand-primary);
  font-weight: 700;
}
.position--solo {
  font-weight: 600;
  color: var(--color-brand-primary);
}
.position .hint {
  display: block;
  margin-top: 2px;
  color: var(--color-text-tertiary);
  font-size: 0.8rem;
}
</style>
```

實際 css 變數以該 view 既有 token 為準（grep 既有 `var(--`）。

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- ActivityPublicQueryView.waitlist
```

Expected: 全 PASS

- [ ] **Step 6: 手動瀏覽器驗證**

啟動 frontend：
```bash
cd ~/Desktop/ivy-frontend && npm run dev
```
在前端開啟 `http://localhost:5173/activity-query?token=<某 waitlist 報名的 token>`，確認看到「⏳ 候補中 目前第 N 位 / 共 M 位」。

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/public/ActivityPublicQueryView.vue src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js
git commit -m "feat(activity-public): 公開查詢顯示候補位次

- waitlist_position == 1 時加註「下一位」提示
- waitlist_total == 1 顯示「唯一候補者」
- enrolled 狀態不顯示候補資訊

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Admin 候補 Drawer 升位按鈕

**Files:**
- Modify: `ivy-frontend/src/views/activity/ActivityCourseView.vue`
- Create/Modify: `ivy-frontend/src/views/activity/__tests__/ActivityCourseView.promote.test.js`

- [ ] **Step 1: Write the failing test**

`src/views/activity/__tests__/ActivityCourseView.promote.test.js`（新檔）：

```javascript
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ActivityCourseView from '../ActivityCourseView.vue'

vi.mock('@/api/activity', () => ({
  promoteWaitlist: vi.fn(),
  getCourseWaitlist: vi.fn(),
  getCourseEnrolled: vi.fn(),
  // 視該 view 引用的其他 API 加 mock
}))

import { promoteWaitlist, getCourseWaitlist } from '@/api/activity'

describe('ActivityCourseView — 候補 Drawer 手動升位', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getCourseWaitlist.mockResolvedValue({
      data: [
        {
          registration_id: 11,
          student_name: '陳小華',
          parent_phone: '0922-***-***',
          course_id: 1,
          waitlist_position: 2,
          created_at: '2026-05-01T10:00:00',
        },
      ],
    })
    promoteWaitlist.mockResolvedValue({ data: { message: '成功升為正式報名' } })
  })

  it('點擊升位按鈕顯示確認 dialog', async () => {
    const wrapper = mount(ActivityCourseView, {
      props: { courseId: 1 },  // 視該 view props 結構調整
    })
    await flushPromises()

    const btn = wrapper.find('[data-test="promote-waitlist-btn-11"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')

    expect(wrapper.text()).toContain('跳過順序，立即升此候補為正式報名')
  })

  it('確認後呼叫 promoteWaitlist 並刷新 Drawer', async () => {
    const wrapper = mount(ActivityCourseView, {
      props: { courseId: 1 },
    })
    await flushPromises()
    await wrapper.find('[data-test="promote-waitlist-btn-11"]').trigger('click')
    await wrapper.find('[data-test="promote-confirm"]').trigger('click')
    await flushPromises()

    expect(promoteWaitlist).toHaveBeenCalledWith(11, 1)
    // 升位後重新查 waitlist
    expect(getCourseWaitlist).toHaveBeenCalledTimes(2)
  })

  it('API 失敗時顯示錯誤訊息', async () => {
    promoteWaitlist.mockRejectedValueOnce({
      response: { data: { detail: '該家長已被前一個升位' } },
    })

    const wrapper = mount(ActivityCourseView, {
      props: { courseId: 1 },
    })
    await flushPromises()
    await wrapper.find('[data-test="promote-waitlist-btn-11"]').trigger('click')
    await wrapper.find('[data-test="promote-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('該家長已被前一個升位')
  })
})
```

註：實際 props/structure 以 view 既有為準；mount 時若 view 用 setup composition 需要 router/store，補對應 stub。

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- ActivityCourseView.promote
```

Expected: FAIL

- [ ] **Step 3: 在候補 Drawer 加按鈕與 dialog**

在 `ActivityCourseView.vue` 候補名單 Drawer 渲染處（grep `candidate` 或 `waitlist` 找候補列表）加：

```vue
<template>
  ...
  <div v-for="reg in waitlistEntries" :key="reg.registration_id" class="waitlist-row">
    <span class="name">{{ reg.student_name }}</span>
    <span class="phone">{{ reg.parent_phone }}</span>
    <span class="position">候補第 {{ reg.waitlist_position }} 位</span>
    <button
      :data-test="`promote-waitlist-btn-${reg.registration_id}`"
      class="btn btn-sm btn-primary"
      @click="openPromoteDialog(reg)"
    >
      ⬆️ 升位
    </button>
  </div>

  <Teleport to="body">
    <div v-if="promoteDialog.open" class="modal-backdrop" @click.self="cancelPromote">
      <div class="modal">
        <h3>確認手動升位</h3>
        <p>
          將跳過順序，立即升此候補為<strong>正式報名</strong>（不需家長 48h 確認）。
          系統會自動推送 LINE 告知家長。確定？
        </p>
        <div class="modal-actions">
          <button class="btn btn-default" @click="cancelPromote">取消</button>
          <button
            data-test="promote-confirm"
            class="btn btn-primary"
            :disabled="promoteDialog.submitting"
            @click="confirmPromote"
          >
            確認升位
          </button>
        </div>
        <p v-if="promoteDialog.error" class="error">
          {{ promoteDialog.error }}
        </p>
      </div>
    </div>
  </Teleport>
  ...
</template>

<script setup>
import { reactive } from 'vue'
import { promoteWaitlist, getCourseWaitlist } from '@/api/activity'

// ... 既有 props/state
const promoteDialog = reactive({
  open: false,
  registration: null,
  submitting: false,
  error: '',
})

function openPromoteDialog(reg) {
  promoteDialog.registration = reg
  promoteDialog.error = ''
  promoteDialog.open = true
}

function cancelPromote() {
  promoteDialog.open = false
  promoteDialog.registration = null
  promoteDialog.error = ''
}

async function confirmPromote() {
  if (!promoteDialog.registration) return
  promoteDialog.submitting = true
  promoteDialog.error = ''
  try {
    await promoteWaitlist(
      promoteDialog.registration.registration_id,
      promoteDialog.registration.course_id,
    )
    promoteDialog.open = false
    promoteDialog.registration = null
    await refreshWaitlist()  // 重新拉候補名單
  } catch (e) {
    promoteDialog.error = e?.response?.data?.detail || '升位失敗，請稍後再試'
  } finally {
    promoteDialog.submitting = false
  }
}

async function refreshWaitlist() {
  // 既有 view 應已有 fetch 函式；若無則呼叫 getCourseWaitlist 並更新 state
  // 此處以該 view 既有 method 為準（grep loadWaitlist / fetchWaitlist）
  const { data } = await getCourseWaitlist(currentCourseId.value)
  waitlistEntries.value = data
}
</script>

<style scoped>
.waitlist-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-border);
}
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal {
  background: var(--color-surface);
  padding: var(--space-4);
  border-radius: var(--radius-md);
  max-width: 420px;
  width: 90vw;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.error {
  color: var(--color-danger);
  margin-top: var(--space-2);
}
</style>
```

註：view 已存在 waitlist Drawer，找到既有 row 渲染處與 fetch 函式，把上面片段融入。實際 state/method 名沿用既有命名。

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- ActivityCourseView.promote
```

Expected: 全 PASS

- [ ] **Step 5: 跑既有 ActivityCourseView 測試**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- ActivityCourseView
```

Expected: 既有測試全 PASS（無 regression）

- [ ] **Step 6: 手動瀏覽器驗證**

啟動 frontend；用 admin 帳號（admin/admin123）進「才藝 → 課程管理」，點某有候補的課程，候補 Drawer 應出現「⬆️ 升位」按鈕；點擊 → dialog 出現 → 確認 → 該候補變正式 → Drawer 刷新。

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/activity/ActivityCourseView.vue \
        src/views/activity/__tests__/ActivityCourseView.promote.test.js
git commit -m "feat(activity-admin): 候補 Drawer 加手動升位按鈕

跳過 FIFO 順序直接升 enrolled（不經 48h 確認窗）；
二次確認 dialog；失敗顯示後端 detail 訊息。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: 前端整合驗證

- [ ] **Step 1: 跑全套前端測試**

```bash
cd ~/Desktop/ivy-frontend && npm run test 2>&1 | tail -30
```

Expected: 全 PASS（不應有 regression）

- [ ] **Step 2: 跑 build 確認無 syntax/import 錯誤**

```bash
cd ~/Desktop/ivy-frontend && npm run build 2>&1 | tail -10
```

Expected: build 成功

- [ ] **Step 3: 確認 commits**

```bash
cd ~/Desktop/ivy-frontend && git log --oneline origin/main..HEAD
```

Expected: 看到 Task 9 + Task 10 兩個 commits（或更多細分），都在 `feat/activity-waitlist-autofill-frontend` 分支。

---

### Task 12: 跨端整合驗證

- [ ] **Step 1: 啟動兩端**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

後端確認加 env：先把 `~/Desktop/ivy-backend/.env` 加 `ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1`，再跑 start.sh。

- [ ] **Step 2: 端到端情境驗證 — 自動遞補**

1. Admin 進「才藝 → 課程管理」，找一個有候補名單的課程
2. SQL 把「最近被升位的 promoted_pending」的 `confirm_deadline` 設為過去：
   ```sql
   UPDATE registration_courses
   SET confirm_deadline = NOW() - INTERVAL '1 minute'
   WHERE status = 'promoted_pending'
   ORDER BY promoted_at DESC LIMIT 1;
   ```
3. 等 ≤ 5 分鐘
4. Admin 重新整理候補名單；下一位候補應已自動升為 promoted_pending
5. 該家長 LINE 應收到「升正式待確認」通知

- [ ] **Step 3: 端到端情境驗證 — 公開頁位次**

1. 拿一個 waitlist 報名的 query token（從 SQL 或 admin 頁複製）
2. 開無痕視窗到 `http://localhost:5173/activity-query?token=<token>`
3. 輸入電話查詢
4. 應看到該候補課程顯示「⏳ 候補中 目前第 N 位 / 共 M 位」
5. 若 N=1，應另顯示「下一位」提示

- [ ] **Step 4: 端到端情境驗證 — Admin 手動升位**

1. Admin 進該課程候補 Drawer
2. 點某非第 1 位候補的「⬆️ 升位」
3. 確認 dialog 出現
4. 確認後 → toast 成功 → Drawer 該家長消失（已變 enrolled）
5. 該家長 LINE 應收到「已升正式」通知（不帶 deadline）

- [ ] **Step 5: 確認 audit log**

```sql
SELECT * FROM activity_registration_changes
WHERE change_type IN ('候補升正式', '候補轉正逾期放棄')
ORDER BY created_at DESC LIMIT 5;
```

應看到 Step 2 與 Step 4 對應的稽核紀錄。

- [ ] **Step 6: 跨端整合驗收 checklist**

- [ ] 後端測試全 PASS
- [ ] 前端測試全 PASS
- [ ] Scheduler 啟動 log 正常
- [ ] Sweep 自動遞補可運作
- [ ] 公開頁位次顯示正確
- [ ] Admin 手動升位 dialog 與 API 串接正確
- [ ] LINE 通知（升位、提醒、逾期、手動升位）都觸發
- [ ] 既有手動 sweep router 仍可用
- [ ] Audit log 完整

---

## 後續：PR / Merge

兩個分支各自開 PR：

- 後端：`feat/activity-waitlist-autofill-backend` → `main`
- 前端：`feat/activity-waitlist-autofill-frontend` → `main`

PR description 引用 spec：`docs/superpowers/specs/2026-05-13-activity-waitlist-auto-fill-design.md`。

部署 checklist 按 spec §11。
