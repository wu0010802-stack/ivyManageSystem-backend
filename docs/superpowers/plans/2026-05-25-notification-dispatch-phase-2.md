# 通知中央 Dispatcher Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把現有 21+ 個 `line_service.notify_*` / `approval_notifier.notify_approval` caller 全部遷移到 `dispatch.enqueue`，分 4 個獨立 PR 按域提交，每 PR 完成後 grep 驗該域歸零、`line_service.notify_*` 21 method 從 public 改為 `_notify_*` internal helper、刪除 `services/notification/approval_notifier.py`。

**Architecture:** Strangler fig 模式：(1) `services/notification/dispatch.py` 內部 LINE adapter 在 `_fan_out` pre-resolve `User.id → User.line_user_id` 後傳給 adapter（caller 仍用 int recipient_user_id）。(2) 每 PR 一個域，每 caller 一個 commit，commit 前後行為 byte-identical。(3) PR-D 是純 cleanup（沒有業務改動）— rename method + 加 deprecation comment + delete approval_notifier.

**Tech Stack:** FastAPI, SQLAlchemy, pytest（既有測試），grep CI gate（防回退）。

**Spec：** `docs/superpowers/specs/2026-05-25-notification-dispatch-design.md`

**Plan：** Phase 1 已 merged（dispatch 骨架）。本 Phase 2 在其基礎上把 caller 接上。

**Phase 2 不含：** 員工通知中心 UI（Phase 3）、outbox 升級（Phase 4）。

**完成判準：**
1. `grep -rn "line_service\.notify_\|notify_approval\b" api/ services/` 在所有 router 歸零（只剩 dispatch / line_service / line_service 內部互呼）
2. `services/notification/approval_notifier.py` 已刪除
3. `services/line_service.py` 21 個 `notify_*` 全部 rename 為 `_notify_*` + 加 deprecation docstring
4. CI 通過（5012+ pytest 全綠）
5. 手動驗證一個 happy path：員工送假 → 看到 LINE 推播 + notification_logs 多一 row

---

## 檔案結構（4 PR 累積）

**修改（PR-A）：**
- `services/notification/dispatch.py` — LineAdapter call 前 pre-resolve User.id → line_user_id（內部 helper `_resolve_line_user_id`）
- `services/notification/_channels/line.py` — fallback path 改為接受 int recipient_user_id（adapter 內 query User，或維持只接受 str 由 _fan_out 預先解析；推薦後者）
- `api/leaves.py` — 兩處 `notify_approval(...)` → `dispatch.enqueue(...)`
- `api/overtimes.py` — 一處 `notify_approval(...)` → `dispatch.enqueue(...)`
- `api/punch_corrections.py` — 兩處 `notify_approval(...)` → `dispatch.enqueue(...)`
- 既有測試 `tests/test_leaves*.py` / `tests/test_overtimes*.py` / `tests/test_punch_corrections*.py` — verify 仍綠
- 新 `tests/notification/test_resolve_line_user_id.py` — 4 case（user 存在/不存在/無 line_user_id/無 line_follow_confirmed_at）

**修改（PR-B）：**
- `api/announcements.py` — `notify_parent_announcement` → `dispatch.enqueue("parent.announcement", ...)`
- `api/portal/parent_messages.py` — `notify_parent_message_received` → `dispatch.enqueue("parent.message_received", ...)`
- `services/contact_book_service.py` — 兩處 `notify_parent_contact_book_published` → `dispatch.enqueue("parent.contact_book_published", ...)`
- 既有測試 verify

**修改（PR-C）：**
- `services/notification/event_types.py` — 加 4 個新 event：`activity.waitlist_reminder` / `activity.waitlist_final_reminder` / `activity.waitlist_expired` / `growth_report.published`
- `services/notification/channel_matrix.py` — 新 event 對映
- `services/notification/renderers.py` — 4 新 renderer
- `services/notification/_channels/line.py` — `LINE_HANDLERS` 註冊 4 個對映現有 line_service method 的 handler（保留 Flex/quick reply）
- `api/portal/leaves.py` — `notify_leave_submitted` → `dispatch.enqueue("leave.submitted", ...)`
- `api/portal/overtimes.py` — `notify_overtime_submitted` → `dispatch.enqueue("overtime.submitted", ...)`
- `api/salary/calculate.py` — 兩處 `notify_salary_batch_complete` → `dispatch.enqueue("salary.batch_completed", ...)`
- `api/dismissal_calls.py` — `notify_dismissal_created` → `dispatch.enqueue("dismissal.created", ...)`
- `api/activity/registrations.py` — `notify_activity_waitlist_promoted` → `dispatch.enqueue("activity.waitlist_promoted", ...)`
- `services/activity_service.py` — 4 處 waitlist 相關 → `dispatch.enqueue(...)` 對映新 event_types
- `api/activity/pos_approval.py` — `notify_pos_unlock_to_approver` → `dispatch.enqueue("pos.unlock_requested", ...)`
- `api/portfolio/reports.py` — `push_to_user` (growth report) → `dispatch.enqueue("growth_report.published", ...)`
- 新 tests + 既有 tests verify

**修改/刪除（PR-D）：**
- `services/line_service.py` — 21 個 `notify_*` rename 為 `_notify_*` + 加 deprecation docstring；`LINE_HANDLERS` 對映同步更新
- `services/notification/approval_notifier.py` — **DELETE**
- `services/notification/__init__.py` — 移除 approval_notifier 相關 export
- CI `.github/workflows/*.yml` — 加 grep gate（任何 commit 引入 `line_service.notify_` 在 api/ 即 fail）

**不動：**
- Phase 1 全部新模組（dispatch.py 除新增 `_resolve_line_user_id`）
- 既有 LINE adapter `_inbox_ws_push` 流程
- WS manager / inbox WS skeleton

---

## 約定

- 工作目錄：每 PR 開新 worktree（命名 `.claude/worktrees/notification-dispatch-phase-2-<pr-id>-2026-05-25-backend`）
- 每 PR 完成後 push + open PR + 等 user merge，再開下一個
- TDD：caller 遷移時不寫新測試（既有 router 測試已 cover LINE 推播），新增 event_type 時補 renderer + channel_matrix test
- Grep 防回退：每 PR 完成後 `grep -rn "<舊 method 名>" api/ services/` 該 PR 範圍內歸零（line_service 內部與 LINE_HANDLERS 對映不算）
- Commit 格式：`feat(notification): migrate <router> to dispatch.enqueue` / `refactor(notification): retire line_service public notify_*`

---

## PR-A：簽核三件遷移 + dispatch line_user_id 解析（~2.5 工作日）

### Task A0：建立 worktree

- [ ] **Step 1: 建立 worktree**

```bash
cd ~/Desktop/ivy-backend
git worktree add .claude/worktrees/notification-dispatch-phase-2a-2026-05-25-backend \
  -b feat/notification-dispatch-phase-2a-approvals-2026-05-25-backend main
cd .claude/worktrees/notification-dispatch-phase-2a-2026-05-25-backend
pwd
```

Expected: pwd 結尾為 `notification-dispatch-phase-2a-2026-05-25-backend`。

### Task A1：dispatch 加 `_resolve_line_user_id` helper

**Files:**
- Modify: `services/notification/dispatch.py`（加 helper + 在 `_fan_out` 內呼叫）
- Modify: `services/notification/_channels/line.py`（fallback 改為要求 str；移除 int warning fallback）
- Test: `tests/notification/test_resolve_line_user_id.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""User.id → LINE user_id 解析測試。"""

import pytest
from unittest.mock import patch
from services.notification.dispatch import _resolve_line_user_id


def test_resolve_returns_line_user_id_when_user_active_and_followed(test_db_session):
    from models.database import User
    user = User(
        id=1, username="u1", password_hash="x",
        line_user_id="Uxxxxx", line_follow_confirmed_at="2026-01-01 00:00:00",
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _resolve_line_user_id(test_db_session, user_id=1) == "Uxxxxx"


def test_resolve_returns_none_when_user_inactive(test_db_session):
    from models.database import User
    user = User(
        id=2, username="u2", password_hash="x",
        line_user_id="Uxxxxx", line_follow_confirmed_at="2026-01-01 00:00:00",
        is_active=False,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _resolve_line_user_id(test_db_session, user_id=2) is None


def test_resolve_returns_none_when_no_line_user_id(test_db_session):
    from models.database import User
    user = User(
        id=3, username="u3", password_hash="x",
        line_user_id=None, is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _resolve_line_user_id(test_db_session, user_id=3) is None


def test_resolve_returns_none_when_not_followed(test_db_session):
    from models.database import User
    user = User(
        id=4, username="u4", password_hash="x",
        line_user_id="Uxxxxx", line_follow_confirmed_at=None,
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _resolve_line_user_id(test_db_session, user_id=4) is None


def test_resolve_returns_none_when_user_not_found(test_db_session):
    assert _resolve_line_user_id(test_db_session, user_id=99999) is None
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_resolve_line_user_id.py -v
```

Expected: ImportError

- [ ] **Step 3: 加 `_resolve_line_user_id` 到 `dispatch.py`**

在 `_pref_enabled` 後加：

```python
def _resolve_line_user_id(session, user_id: int) -> str | None:
    """User.id → User.line_user_id（active + line_follow_confirmed 才回）。

    沿用 line_service.should_push_to_parent 的可達性檢查；fail-closed。
    """
    if user_id is None:
        return None
    try:
        from models.database import User
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            return None
        if not user.line_user_id or not user.line_follow_confirmed_at:
            return None
        return user.line_user_id
    except Exception as exc:
        logger.warning("_resolve_line_user_id failed (fail-closed): %s", exc)
        return None
```

- [ ] **Step 4: 修 `_fan_out` 在 LINE adapter call 前解析**

找到 `_fan_out` 內 `for ch in active_channels:` 段，LINE 分支改為：

```python
        for ch in active_channels:
            if ch == "in_app":
                continue
            if ch == "line":
                line_user_id = _resolve_line_user_id(log_session, evt.recipient_user_id)
                if line_user_id is None:
                    failed.append({"channel": "line", "error": "unreachable_user"})
                    continue
                # 包裝 evt 把 recipient_user_id 改為 LINE user_id
                from dataclasses import replace
                line_evt = replace(evt, recipient_user_id=line_user_id)
                try:
                    _get_line_adapter().send(line_evt, rendered, log_id=log_id or 0)
                    succeeded.append("line")
                except Exception as exc:
                    logger.exception("LINE channel failed event=%s user=%s", evt.event_type, evt.recipient_user_id)
                    failed.append({"channel": "line", "error": type(exc).__name__})
                continue
            # ws or others
            adapter = _get_ws_adapter()
            try:
                adapter.send(evt, rendered, log_id=log_id or 0)
                succeeded.append(ch)
            except Exception as exc:
                logger.exception("channel %s failed event=%s", ch, evt.event_type)
                failed.append({"channel": ch, "error": type(exc).__name__})
```

- [ ] **Step 5: 簡化 `_channels/line.py`**

移除 int recipient 的 warning 路徑（_fan_out 已 pre-resolve）：

```python
class LineAdapter:
    def __init__(self, line_service):
        self._ls = line_service

    def send(self, evt, rendered, *, log_id: int) -> None:
        handler = LINE_HANDLERS.get(evt.event_type)
        if handler is None:
            text = (rendered.title or "") + ("\n" + rendered.body if rendered.body else "")
            if not isinstance(evt.recipient_user_id, str):
                raise ValueError(
                    f"LINE adapter 收到非 str recipient_user_id={evt.recipient_user_id!r}; "
                    "_fan_out 應先呼叫 _resolve_line_user_id"
                )
            self._ls.push_text_to_user(evt.recipient_user_id, text)
            return
        handler(self._ls, evt, rendered)
```

對應更新 `tests/notification/test_channels_line.py`：把 int recipient 的 warning case 改為 ValueError raise case。

- [ ] **Step 6: 跑測試確認通過**

```bash
pytest tests/notification/ -v 2>&1 | tail -15
```

Expected: 全綠（含 5 個新 resolve test + 既有 fan_out 測試適應）

- [ ] **Step 7: Commit**

```bash
git add services/notification/dispatch.py services/notification/_channels/line.py \
  tests/notification/test_resolve_line_user_id.py tests/notification/test_channels_line.py
git commit -m "feat(notification): add _resolve_line_user_id; LineAdapter requires str recipient"
```

### Task A2：遷移 api/leaves.py

**Files:**
- Modify: `api/leaves.py:1563, 2019`

- [ ] **Step 1: 看現況 1563 area**

```bash
sed -n '1540,1580p' api/leaves.py
```

理解 `notify_approval(...)` 的 context 結構（leave_type / start / end / approver name 等）。

- [ ] **Step 2: 替換 1563 area**

把：

```python
notify_approval(
    line_service=_line_service,
    doc_type="leave",
    action="approve",  # or "reject"
    line_user_id=...,
    name=...,
    context={"leave_type": ..., "start": ..., "end": ...},
    rejection_reason=...,
)
```

改為（call 改在 commit 之前；dispatch.enqueue 註冊到 session.info，after_commit 自動 fan-out）：

```python
from services.notification import dispatch

dispatch.enqueue(
    session=session,
    event_type="leave.approved" if action == "approve" else "leave.rejected",
    recipient_user_id=user.id,  # 員工 User.id (int)
    context={
        "reviewer_name": current_user.get("display_name") or current_user.get("username"),
        "leave_type": leave_record.leave_type.name,
        "start": leave_record.start_date.isoformat(),
        "end": leave_record.end_date.isoformat(),
        "leave_id": leave_record.id,
        "rejection_reason": rejection_reason if action == "reject" else None,
    },
    sender_id=current_user["user_id"],
    source_entity_type="leave_request",
    source_entity_id=leave_record.id,
)
# 注意：dispatch.enqueue 在 session.commit() 前呼叫；commit 後 hook 自動 fan-out
```

需要根據實際變數名調整。`user` 是 leave 的 owner（員工），`current_user` 是審核者。

注意把原 `notify_approval` 的 caller 從 commit 後位置移到 commit 前（dispatch.enqueue 必須在 tx 內）。

- [ ] **Step 3: 替換 2019 area**

同樣處理 line 2019 的 batch case。

- [ ] **Step 4: 移除 from import**

```python
# from services.notification.approval_notifier import notify_approval
```

改為：

```python
from services.notification import dispatch
```

- [ ] **Step 5: 跑既有 leave router 測試**

```bash
pytest tests/test_leaves*.py -v 2>&1 | tail -15
```

Expected: 零回歸（如果有 mock `notify_approval` 的 test，要改 mock 路徑為 `dispatch.enqueue`）

- [ ] **Step 6: Commit**

```bash
git add api/leaves.py tests/test_leaves*.py 2>/dev/null
git commit -m "feat(notification): migrate api/leaves.py to dispatch.enqueue"
```

### Task A3：遷移 api/overtimes.py（同 A2 pattern）

**Files:**
- Modify: `api/overtimes.py:300`

按 A2 同樣 pattern：
- 替換 `notify_approval(doc_type="overtime", ...)` 為 `dispatch.enqueue("overtime.approved"|"overtime.rejected", ...)`
- 移除 import
- 跑測試
- Commit `feat(notification): migrate api/overtimes.py to dispatch.enqueue`

### Task A4：遷移 api/punch_corrections.py（2 處）

**Files:**
- Modify: `api/punch_corrections.py:209, 317`

按 A2 同樣 pattern；event_type 為 `"punch_correction.approved"` / `"punch_correction.rejected"`。

Commit: `feat(notification): migrate api/punch_corrections.py to dispatch.enqueue`

### Task A5：刪除 approval_notifier + grep 驗證

- [ ] **Step 1: 確認沒有 caller 還在 import**

```bash
grep -rn "from services\.notification\.approval_notifier\|notify_approval\b" \
  api/ services/ 2>/dev/null | grep -v __pycache__ | grep -v test
```

Expected: empty

- [ ] **Step 2: 刪除 approval_notifier.py**

```bash
git rm services/notification/approval_notifier.py
```

- [ ] **Step 3: 移除 __init__.py 中的 approval_notifier import（如有）**

```bash
grep -n "approval_notifier" services/notification/__init__.py
```

如有 import，移除該行。

- [ ] **Step 4: 跑全套測試確認零回歸**

```bash
pytest tests/ -x --tb=short -q --ignore=tests/spike_rls 2>&1 | tail -8
```

Expected: 5012+ passed（如有 test 直接 import `approval_notifier`，要改）

- [ ] **Step 5: Commit**

```bash
git add services/notification/approval_notifier.py services/notification/__init__.py
git commit -m "refactor(notification): delete approval_notifier (replaced by dispatch.enqueue)"
```

### Task A6：push + 開 PR

- [ ] **Step 1: push**

```bash
git push -u origin feat/notification-dispatch-phase-2a-approvals-2026-05-25-backend
```

- [ ] **Step 2: PR**

```bash
gh pr create --title "feat(notification): Phase 2 PR-A — migrate approval routers to dispatch.enqueue" \
  --body "$(cat <<'EOF'
## Summary
Phase 2 PR-A — 把簽核三件（leave / overtime / punch_correction）從 approval_notifier 遷移到 dispatch.enqueue。

- 新 dispatch helper `_resolve_line_user_id` — _fan_out 在 call LINE adapter 前 pre-resolve User.id → line_user_id
- LineAdapter 簽名收緊：fallback path 只接受 str recipient，int 改為 raise ValueError
- 3 router 5 call site 全改：api/{leaves,overtimes,punch_corrections}.py
- **刪除** services/notification/approval_notifier.py

## Test plan
- [ ] CI 全綠
- [ ] 手動驗：員工送假 → 主管核准 → 員工收 LINE + notification_logs 多 row

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR-B：家長域遷移（~1.5 工作日）

### Task B0：建立 worktree（在 PR-A merged 後）

```bash
cd ~/Desktop/ivy-backend
git fetch origin main
git worktree add .claude/worktrees/notification-dispatch-phase-2b-2026-05-25-backend \
  -b feat/notification-dispatch-phase-2b-parent-2026-05-25-backend origin/main
cd .claude/worktrees/notification-dispatch-phase-2b-2026-05-25-backend
```

### Task B1：遷移 api/announcements.py

**Files:**
- Modify: `api/announcements.py:608`

- [ ] **Step 1: 看現況**

```bash
sed -n '590,620p' api/announcements.py
```

- [ ] **Step 2: 替換**

把 `_line_service.notify_parent_announcement(line_id, ...)` 改為：

```python
dispatch.enqueue(
    session=session,
    event_type="parent.announcement",
    recipient_user_id=parent_user.id,
    context={
        "title": announcement.title,
        "preview": announcement.body[:80],
        "announcement_id": announcement.id,
    },
    sender_id=current_user["user_id"],
    source_entity_type="announcement",
    source_entity_id=announcement.id,
)
```

- [ ] **Step 3: 跑 announcement test**

```bash
pytest tests/test_announcements*.py -v 2>&1 | tail -10
```

- [ ] **Step 4: Commit**

```bash
git add api/announcements.py
git commit -m "feat(notification): migrate api/announcements.py to dispatch.enqueue (parent.announcement)"
```

### Task B2：遷移 api/portal/parent_messages.py

按 B1 pattern。event_type `parent.message_received`。context 包含 `teacher_name`、`student_name`、`body_preview`、`thread_id`。

Commit: `feat(notification): migrate api/portal/parent_messages.py to dispatch.enqueue`

### Task B3：遷移 services/contact_book_service.py（2 處）

按 B1 pattern。event_type `parent.contact_book_published`。context 包含 `student_name`、`date`。

注意：service 層 enqueue 也需要 session — 該 service 已接收 session 參數。

Commit: `feat(notification): migrate services/contact_book_service.py to dispatch.enqueue`

### Task B4：grep 驗 + push + PR

- [ ] **Step 1: grep**

```bash
grep -rn "notify_parent_\|notify_parent\b" api/ services/ 2>/dev/null \
  | grep -v __pycache__ | grep -v test | grep -v services/line_service.py | grep -v _channels/
```

Expected: empty

- [ ] **Step 2: 全套測試**

```bash
pytest tests/ -x --tb=short -q --ignore=tests/spike_rls 2>&1 | tail -8
```

- [ ] **Step 3: push + PR**

```bash
git push -u origin feat/notification-dispatch-phase-2b-parent-2026-05-25-backend
gh pr create --title "feat(notification): Phase 2 PR-B — migrate parent routers to dispatch.enqueue" --body "..."
```

---

## PR-C：員工送單 / 薪資 / 接送 / 才藝 / POS / Growth Report 遷移（~3 工作日）

### Task C0：建立 worktree（在 PR-B merged 後）

```bash
cd ~/Desktop/ivy-backend && git fetch origin main
git worktree add .claude/worktrees/notification-dispatch-phase-2c-2026-05-25-backend \
  -b feat/notification-dispatch-phase-2c-salary-activity-2026-05-25-backend origin/main
cd .claude/worktrees/notification-dispatch-phase-2c-2026-05-25-backend
```

### Task C1：加 4 個新 event_type

**Files:**
- Modify: `services/notification/event_types.py`
- Modify: `services/notification/channel_matrix.py`
- Modify: `services/notification/renderers.py`
- Modify: `services/notification/_channels/line.py`（LINE_HANDLERS 註冊）
- Test: `tests/notification/test_channel_matrix.py`（更新 count）
- Test: `tests/notification/test_renderers.py`（新增 4 個 renderer happy path）

- [ ] **Step 1: 加 event_types**

在 `event_types.py` 員工域段加：

```python
ACTIVITY_WAITLIST_REMINDER = "activity.waitlist_reminder"
ACTIVITY_WAITLIST_FINAL_REMINDER = "activity.waitlist_final_reminder"
ACTIVITY_WAITLIST_EXPIRED = "activity.waitlist_expired"
GROWTH_REPORT_PUBLISHED = "growth_report.published"
```

加進 `NOTIFICATION_EVENT_TYPES` frozenset。

- [ ] **Step 2: 加 channel_matrix entries**

```python
"activity.waitlist_reminder":      ("in_app", "line"),  # 推給家長
"activity.waitlist_final_reminder":("in_app", "line"),  # 推給家長
"activity.waitlist_expired":       ("in_app", "line"),  # 推給家長
"growth_report.published":         ("line",),            # 推給家長（不寫 in_app — 走家長 LIFF 不是員工）
```

注意：activity waitlist 系列原本是推給「家長」而非員工。需要決定 in_app 是否該寫（家長 v1 沒 in_app UI）。建議 channel 改為 `("line",)` 對齊家長域慣例。**最終決定**：上述全部改為 `("line",)`，移除 in_app：

```python
"activity.waitlist_reminder":      ("line",),
"activity.waitlist_final_reminder":("line",),
"activity.waitlist_expired":       ("line",),
"growth_report.published":         ("line",),
```

- [ ] **Step 3: 加 4 renderer**

在 `renderers.py` 員工域段補：

```python
@renderer("activity.waitlist_reminder")
def _r_activity_waitlist_reminder(ctx: dict) -> Rendered:
    return Rendered(
        title=f"⏰ 候補提醒：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}，請於 {ctx.get('deadline', '近日')} 前確認",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("activity.waitlist_final_reminder")
def _r_activity_waitlist_final(ctx: dict) -> Rendered:
    return Rendered(
        title=f"🚨 候補最後提醒：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}，候補名額即將釋出",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("activity.waitlist_expired")
def _r_activity_waitlist_expired(ctx: dict) -> Rendered:
    return Rendered(
        title=f"❌ 候補名額已過期：{ctx['course_name']}",
        body=f"學生 {ctx['student_name']}",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("growth_report.published")
def _r_growth_report(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📊 {ctx['student_name']} {ctx['period']} 成長報告已發布",
        body=ctx.get("summary", ""),
        deep_link=f"/parent/growth-reports/{ctx['report_id']}",
    )
```

- [ ] **Step 4: 註冊 LINE_HANDLERS 對映現有 line_service Flex method**

在 `_channels/line.py`：

```python
def _h_activity_waitlist_reminder(line_service, evt, rendered):
    line_service._notify_activity_waitlist_promotion_reminder(  # 已 rename 為 _ prefix
        student_id=evt.context.get("student_id"),
        course_name=evt.context["course_name"],
        student_name=evt.context["student_name"],
        # 沿用既有 method 簽名
    )

LINE_HANDLERS = {
    "activity.waitlist_reminder": _h_activity_waitlist_reminder,
    "activity.waitlist_final_reminder": _h_activity_waitlist_final_reminder,
    "activity.waitlist_expired": _h_activity_waitlist_expired,
    # 其他 19 event 暫不註冊，走 fallback push_text
}
```

注意：PR-D 之前 line_service 還沒 rename，這裡先用 public name `notify_activity_waitlist_promotion_reminder`。PR-D rename 後再改 `_notify_*`。

- [ ] **Step 5: 跑測試**

```bash
pytest tests/notification/test_event_types.py tests/notification/test_channel_matrix.py tests/notification/test_renderers.py -v
```

需要更新 count assertion（從 19 改為 23）。

- [ ] **Step 6: Commit**

```bash
git add services/notification/event_types.py services/notification/channel_matrix.py \
  services/notification/renderers.py services/notification/_channels/line.py \
  tests/notification/test_*.py
git commit -m "feat(notification): add 4 event_types (activity waitlist subtypes + growth report)"
```

### Task C2-C8：各 caller 遷移（同 A2 pattern）

每個 caller 一個 commit：

- [ ] C2 `api/portal/leaves.py:392` → `leave.submitted` recipient 為 reviewer User.id
- [ ] C3 `api/portal/overtimes.py:196` → `overtime.submitted` recipient 為 reviewer User.id
- [ ] C4 `api/salary/calculate.py:134, 266` → `salary.batch_completed` recipient 為 HR 群組（loop enqueue 多筆）
- [ ] C5 `api/dismissal_calls.py:259` → `dismissal.created` (recipient None — 群組推播 by classroom_id in context)
- [ ] C6 `api/activity/registrations.py:663` → `activity.waitlist_promoted` recipient 為家長 User.id
- [ ] C7 `services/activity_service.py:829, 872, 926, 1126` → 4 個對應的 activity event_type
- [ ] C8 `api/activity/pos_approval.py:548` → `pos.unlock_requested` recipient 為 approver User.id
- [ ] C9 `api/portfolio/reports.py:667` → `growth_report.published` recipient 為家長 User.id

每個都跑既有測試確認零回歸 + 個別 commit。

### Task C10：grep + push + PR

- [ ] grep `line_service.notify_\|_line_service.notify_\|_line_svc.notify_` 在 api/ 與 services/ 應 empty（除 line_service.py 自己 + LINE_HANDLERS 對映）
- [ ] 全套測試
- [ ] push + PR

---

## PR-D：line_service retirement（~1 工作日）

### Task D0：建立 worktree（在 PR-C merged 後）

```bash
cd ~/Desktop/ivy-backend && git fetch origin main
git worktree add .claude/worktrees/notification-dispatch-phase-2d-2026-05-25-backend \
  -b feat/notification-dispatch-phase-2d-cleanup-2026-05-25-backend origin/main
```

### Task D1：rename `notify_*` → `_notify_*`

**Files:**
- Modify: `services/line_service.py` — 21 method rename
- Modify: `services/notification/_channels/line.py` — `LINE_HANDLERS` 對映同步改

- [ ] **Step 1: 列出全部 21 method**

```bash
grep -n "    def notify_" services/line_service.py
```

- [ ] **Step 2: 用 sed rename**

```bash
# 在 services/line_service.py 內把 def notify_ → def _notify_
sed -i.bak 's/    def notify_/    def _notify_/g' services/line_service.py
rm services/line_service.py.bak
```

- [ ] **Step 3: 內部互呼也要 rename**

```bash
grep -n "self\.notify_\|self\._notify_" services/line_service.py
```

確認 line_service 內部 method 互呼也對齊（若有 `self.notify_xxx()` 改為 `self._notify_xxx()`）。

- [ ] **Step 4: 更新 LINE_HANDLERS 對映**

`_channels/line.py` 內所有 `line_service.notify_xxx` call 改為 `line_service._notify_xxx`。

- [ ] **Step 5: 加 deprecation docstring**

在 `services/line_service.py` 檔首加：

```python
# Phase 2 完成（2026-XX-XX）：所有 notify_* method 已 rename 為 _notify_*，
# 視為 dispatch._channels.line 內部 helper。新 caller 一律走 dispatch.enqueue；
# 直接呼叫 _notify_* 不再支援，下個 minor version 可進一步 inline 至 LINE_HANDLERS。
```

- [ ] **Step 6: 跑全套測試**

```bash
pytest tests/ -x --tb=short -q --ignore=tests/spike_rls 2>&1 | tail -8
```

如有 test 直接呼叫 `line_service.notify_xxx`，要 update 用 `_notify_xxx`。

- [ ] **Step 7: Commit**

```bash
git add services/line_service.py services/notification/_channels/line.py
git commit -m "refactor(notification): retire line_service public notify_* (rename to _notify_*)"
```

### Task D2：加 CI grep gate

**Files:**
- Modify: `.github/workflows/<existing-ci>.yml`（加 job）

- [ ] **Step 1: 看現有 ci.yml**

```bash
cat .github/workflows/ci.yml | head -30
```

- [ ] **Step 2: 加 job**

在 `jobs:` 段加：

```yaml
  notification-no-public-line-notify:
    name: 通知 caller 防回退（禁 line_service.notify_*）
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Grep public notify_* in api/ services/
        run: |
          # 允許 services/line_service.py 內部、services/notification/_channels/ 內部呼叫
          MATCH=$(grep -rn "line_service\.notify_\|_line_service\.notify_\|_line_svc\.notify_" \
            api/ services/ 2>/dev/null | \
            grep -v __pycache__ | \
            grep -v "services/notification/_channels/" | \
            grep -v "services/line_service.py" || true)
          if [ -n "$MATCH" ]; then
            echo "❌ 發現 public line_service.notify_* caller（請改用 dispatch.enqueue）:"
            echo "$MATCH"
            exit 1
          fi
          echo "✅ 無 public line_service.notify_* caller"
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(notification): add grep gate forbidding line_service.notify_* in api/services/"
```

### Task D3：push + PR

- [ ] push + PR `feat(notification): Phase 2 PR-D — retire line_service public notify_*`

---

## Self-Review checklist（每 PR 結束時）

- [ ] **Caller 歸零**：該 PR 範圍內 grep 對應 `notify_*` 在 api/ services/ empty
- [ ] **既有測試零回歸**：全套 pytest 5012+ passed（3 個 audit_router 已知 fail 除外）
- [ ] **commit 訊息按 Conventional Commits**：feat / refactor / ci 對應行為
- [ ] **每 caller 一個 commit**：方便 revert / bisect
- [ ] **PR 內含手動驗證 checklist**：至少一個 happy path（送假 / 接送 / 才藝） 手動跑過看 LINE 推 + notification_logs row

## 預估時程

| PR | 工作日 | 預計 commit 數 |
|----|-------|--------------|
| PR-A 簽核三件 | 2.5 | ~7（A1 dispatch helper 1 + 5 caller + A5 delete + A6 push） |
| PR-B 家長域 | 1.5 | ~4（3 caller + grep verify） |
| PR-C 員工/薪資/才藝/POS/Growth | 3 | ~12（C1 add 4 events + 9 caller migration + grep） |
| PR-D 退役 | 1 | ~3（rename + CI gate + push） |
| **合計** | **8 工作日** | **~26 commit** |

## 後續

Phase 2 全 4 PR merged 後：
- Phase 3 plan（員工通知中心 UI）已寫，可開工
- Phase 4（outbox / GC / Sentry 告警）列為 backlog
