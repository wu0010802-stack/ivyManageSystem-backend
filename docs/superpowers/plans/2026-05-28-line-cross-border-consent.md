# LINE 推播跨境合規 Implementation Plan (BE Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** BE Phase 1：User.line_push_consent opt-in flag + line_service push gate + 5 個 build_*_message backward-compat 去識別化（內部 ignore student_name 改用「您的孩子」）+ deep link endpoint。

**Architecture:** 3 commit 同 PR：(C1) alembic migration + User column / (C2) line_service consent gate + 訊息去識別化 + deep link + pytest / (C3) 最終驗收 + push。**FE Phase 3 為 separate spec scope**（後續 user 開 ivy-frontend worktree 做）。

**Tech Stack:** SQLAlchemy / Alembic / FastAPI / pytest

**Spec:** `docs/superpowers/specs/2026-05-28-line-cross-border-consent-design.md` (commit `b73ccfe`)

---

## File Structure

**New files:**
- `alembic/versions/YYYYMMDD_lncon01_user_line_push_consent.py` — migration 加 column
- `tests/test_line_consent_gate.py` — 6 pytest (4 gate + 2 redaction)
- `api/parent_portal/notifications.py` (optional) — deep link endpoint（如時間允許）

**Modified files:**
- `models/auth.py` — User class 加 `line_push_consent: Boolean default False`
- `services/line_service.py` — 5 個 build_*_message 內部 ignore student_name + 加 detail_url kwarg；push_*_to_user 加 consent_checked kwarg + _check_line_push_consent helper

**Unchanged but referenced:**
- `models/auth.py:62 User.line_user_id` — 既有 LINE binding column (家長與 staff 共用)
- `services/line_service.py:339-413` push_text_to_user / push_to_user / push_flex_to_user 等 push 端點

---

## Task 1: alembic migration + User column

**Files:**
- Create: `alembic/versions/YYYYMMDD_lncon01_user_line_push_consent.py`
- Modify: `models/auth.py` (User class 加 column)

### Steps

- [ ] **Step 1.1: 確認 alembic head**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-line-cross-border-consent-2026-05-28-backend
alembic heads
```
記錄當前 head SHA。

- [ ] **Step 1.2: 寫 migration**

Create `alembic/versions/20260528_lncon01_user_line_push_consent.py`：

```python
"""user_line_push_consent: 家長 LINE 推播跨境同意 flag

Revision ID: lncon01
Revises: <Step 1.1 head>
Create Date: 2026-05-28
"""

import sqlalchemy as sa
from alembic import op

revision = "lncon01"
down_revision = "<Step 1.1 head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "line_push_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="LINE 推播跨境傳輸同意（P0 #6 / Spec E）；opt-in 預設 False",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "line_push_consent")
```

- [ ] **Step 1.3: User class 加 column**

`models/auth.py` 在 `line_user_id` 與 `line_follow_confirmed_at` 之間加：

```python
line_push_consent = Column(
    Boolean,
    default=False,
    server_default=sa.text("false"),
    nullable=False,
    comment="LINE 推播跨境傳輸同意（P0 #6）；opt-in 預設 False",
)
```

確認 `from sqlalchemy import Boolean` 已 import (User class 內已用 Boolean for is_active 等)。

- [ ] **Step 1.4: alembic upgrade dry-run**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-line-cross-border-consent-2026-05-28-backend
alembic upgrade head 2>&1 | tail -5
```
Expected: 無錯 (dev PG 跑 ADD COLUMN)。

- [ ] **Step 1.5: 跑 auth + user sample test 確認 model 改動 OK**

```bash
pytest tests/test_auth.py tests/test_audit_login.py -v --tb=line 2>&1 | tail -10
```
Expected: 全綠 (User column 加多 1 個 nullable=False with default 不影響既有 query / fixture)。

- [ ] **Step 1.6: Commit (C1)**

```bash
git add alembic/versions/20260528_lncon01_user_line_push_consent.py models/auth.py
git commit -m "$(cat <<'EOF'
feat(auth): User.line_push_consent column for LINE cross-border opt-in

Spec E PR-E-BE-C1 (audit P0 #6)：

User 表加 line_push_consent Boolean default False (opt-in)。alembic migration
lncon01 加 column。既有家長 backfill 預設 False (符合 §8 §21 個資法告知-同意
原則)，待 FE Phase 3 上線後家長 explicit 勾選同意才開始收 LINE push。

PROD ops 部署提醒：先寄信通知家長將推出 LINE 推播同意機制 + grace period。

Refs: Spec docs/superpowers/specs/2026-05-28-line-cross-border-consent-design.md §3.2
EOF
)"
```

---

## Task 2: line_service consent gate + 訊息去識別化 + tests

**Files:**
- Modify: `services/line_service.py` (5 個 build_*_message + 3+ 個 push_*_to_user + 新加 _check_line_push_consent)
- Create: `tests/test_line_consent_gate.py`

### Steps

- [ ] **Step 2.1: 加 _check_line_push_consent helper**

在 `services/line_service.py` 頂層加：

```python
def _check_line_push_consent(line_user_id: str) -> bool:
    """Query User WHERE line_user_id, return line_push_consent value。
    
    未綁定 LINE / consent False / DB error → return False (fail-closed)。
    跨境合規不可放行，DB 異常時保守 skip 推播。
    """
    from models.auth import User
    from models.database import session_scope
    try:
        with session_scope() as session:
            user = session.query(User).filter(User.line_user_id == line_user_id).first()
            if not user:
                return False
            return bool(user.line_push_consent)
    except Exception as e:
        logger.warning("check_line_push_consent failed for %s: %s", line_user_id, e)
        return False
```

- [ ] **Step 2.2: push_*_to_user 加 consent gate + consent_checked kwarg**

找 `services/line_service.py` 內 push_*_to_user methods (line 339 push_text_to_group / 377 push_to_user / 408 push_text_to_user / 412 push_flex_to_user)：

對 **`push_to_user`** / **`push_text_to_user`** / **`push_flex_to_user`** 3 個（個人推播）加 consent gate。`push_text_to_group` 是群播 skip 不加（spec §2 Non-goals）：

```python
def push_to_user(self, line_user_id: str, text: str, consent_checked: bool = False) -> bool:
    if not consent_checked and not _check_line_push_consent(line_user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", line_user_id)
        return False
    # ... 既有 code ...

def push_text_to_user(self, user_id: str, text: str, consent_checked: bool = False) -> None:
    if not consent_checked and not _check_line_push_consent(user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", user_id)
        return
    # ... 既有 ...

def push_flex_to_user(self, user_id: str, ..., consent_checked: bool = False):
    if not consent_checked and not _check_line_push_consent(user_id):
        logger.info("LINE push skip (no consent): line_user_id=%s", user_id)
        return
    # ... 既有 ...
```

- [ ] **Step 2.3: 5 個 build_*_message 改去識別化（backward-compat）**

對 `build_activity_waitlist_promoted_message` / `build_activity_waitlist_promotion_reminder_message` / `build_activity_waitlist_promotion_expired_message` / `build_activity_waitlist_final_reminder_message` / `build_dismissal_message`：

- 保留原簽章 (student_name / classroom_name 仍接受)
- 新增 `detail_url: Optional[str] = None` 參數
- 函式內**不再 inline student_name / classroom_name**
- 改用「您的孩子」+ 保留 course_name (非 PII)
- 若 detail_url 有值，追加 `\n詳情：{detail_url}`

例（build_dismissal_message）：

```python
# Before
def build_dismissal_message(student_name, classroom_name, note=None):
    msg = f"【接送通知】\n學生：{student_name}\n班級：{classroom_name}"
    if note:
        msg += f"\n備註：{note}"
    return msg

# After (backward-compat)
def build_dismissal_message(
    student_name: str,      # 保留簽章但內部 ignore
    classroom_name: str,    # 保留簽章但內部 ignore
    note: Optional[str] = None,
    detail_url: Optional[str] = None,
) -> str:
    msg = "【接送通知】\n您的孩子已可接送"
    if note:
        msg += f"\n備註：{note}"
    if detail_url:
        msg += f"\n詳情：{detail_url}"
    return msg
```

5 個 build_*_message 同樣模式。

- [ ] **Step 2.4: 寫 6 個 pytest**

Create `tests/test_line_consent_gate.py`：

```python
"""Spec E PR-E-BE: LINE consent gate + 訊息去識別化 pytest。"""

from unittest.mock import patch, MagicMock

import pytest

from services.line_service import (
    _check_line_push_consent,
    build_activity_waitlist_promoted_message,
    build_dismissal_message,
)


def test_check_line_push_consent_user_not_bound_returns_false(test_db_session):
    """line_user_id 不存在於 User 表 → False (fail-closed)。"""
    assert _check_line_push_consent("U_nonexistent_123") is False


def test_check_line_push_consent_consent_false_returns_false(test_db_session):
    """User 存在但 line_push_consent=False → False。"""
    from models.auth import User
    user = User(
        username="parent1",
        password_hash="hash",
        role="parent",
        line_user_id="U_consent_false",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _check_line_push_consent("U_consent_false") is False


def test_check_line_push_consent_consent_true_returns_true(test_db_session):
    """User 存在且 line_push_consent=True → True。"""
    from models.auth import User
    user = User(
        username="parent2",
        password_hash="hash",
        role="parent",
        line_user_id="U_consent_true",
        line_push_consent=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    assert _check_line_push_consent("U_consent_true") is True


def test_check_line_push_consent_db_error_returns_false():
    """DB 異常時 fail-closed return False。"""
    with patch("services.line_service.session_scope", side_effect=Exception("db dead")):
        assert _check_line_push_consent("U_anything") is False


def test_build_activity_waitlist_promoted_message_no_student_name():
    """生成訊息不含 student_name (跨境合規)。"""
    msg = build_activity_waitlist_promoted_message(
        student_name="小明",  # 傳但 ignored
        course_name="鋼琴課",
    )
    assert "小明" not in msg
    assert "您的孩子" in msg
    assert "鋼琴課" in msg  # course_name 非 PII 保留


def test_build_dismissal_message_no_classroom_name():
    """生成訊息不含 student_name 與 classroom_name。"""
    msg = build_dismissal_message(
        student_name="小華",
        classroom_name="向日葵班",
    )
    assert "小華" not in msg
    assert "向日葵班" not in msg
    assert "您的孩子" in msg
```

- [ ] **Step 2.5: 跑 pytest**

```bash
pytest tests/test_line_consent_gate.py -v 2>&1 | tail -15
```
Expected: 6 pass。

- [ ] **Step 2.6: 跑全套 sample 確認 baseline**

```bash
pytest tests/test_line_consent_gate.py tests/test_auth.py tests/test_audit_login.py -v --tb=line 2>&1 | tail -10
```
Expected: 既有 audit + auth test 不破。

- [ ] **Step 2.7: Commit (C2)**

```bash
git add services/line_service.py tests/test_line_consent_gate.py
git commit -m "$(cat <<'EOF'
feat(line): consent gate + message redaction for cross-border compliance

Spec E PR-E-BE-C2 (audit P0 #6)：

- _check_line_push_consent helper: query User.line_push_consent;
  fail-closed (未綁定 / DB error 都 return False)
- push_to_user / push_text_to_user / push_flex_to_user 加 consent gate +
  consent_checked kwarg (batch caller 可 pre-check 避免 N+1 query)
- broadcast / push_text_to_group 不動 (群播 by-design 非個人 PII context)
- 5 個 build_*_message backward-compat 去識別化：保留原簽章但內部 ignore
  student_name/classroom_name 改用「您的孩子」+ 新增 detail_url optional
  (deep link 漸進加，本 PR 不動 64 caller)
- 6 個新 pytest cover consent gate + message redaction

Refs: Spec docs/superpowers/specs/2026-05-28-line-cross-border-consent-design.md §3.3
EOF
)"
```

---

## Task 3: 最終驗收 + push branch

### Steps

- [ ] **Step 3.1: 全套 pytest（background ~22-40 min）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-line-cross-border-consent-2026-05-28-backend
pytest --tb=short 2>&1 | tail -15
```

- [ ] **Step 3.2: git log + diff stat**

```bash
git log --oneline origin/main..HEAD
git diff origin/main..HEAD --stat
```
Expected: 4 commit (spec + plan + migration + consent gate)

- [ ] **Step 3.3: Push branch**

```bash
git push -u origin feat/line-cross-border-consent-2026-05-28-backend
```

- [ ] **Step 3.4: 報告**

- Branch pushed
- Roll-out checklist (spec §8 含「先寄信通知家長」、prod migration、smoke verify)
- FE Phase 3 separate spec scope，留 user 自行決定何時做

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 User.line_push_consent column + migration | Task 1 | ✓ |
| §2 G2 line_service consent gate | Task 2 Step 2.1-2.2 | ✓ |
| §2 G3 5 build_*_message 去識別化 | Task 2 Step 2.3 | ✓ |
| §2 G4 deep link endpoint | **Out-of-scope本 PR** (留 follow-up，spec §3.4 設計已備但本 PR 不實作；caller 漸進補 detail_url 時加) | DEFERRED |
| §2 G5 FE Phase 3 LIFF consent | **Separate ivy-frontend spec / PR** | DEFERRED |
| §2 G6 FE Settings toggle | **Separate** | DEFERRED |
| §2 G7 零回歸 | Task 3 Step 3.1 | ✓ |
| §2 G8 既有 user backfill False | Migration server_default false | ✓ |
| §4 pytest 5-7 | Task 2 Step 2.4 (6 個) | ✓ |
| §5 Roll-out | Task 3 Step 3.4 | ✓ |

**本 PR 達成 BE Phase 1 P0 #6 核心合規**：PII 不再上 LINE + consent gate 落地。FE Phase 3 + Phase 2 legal text 為 user 後續 work。
