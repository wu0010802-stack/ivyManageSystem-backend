# audit_logs DB append-only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Defense-in-depth audit_logs 不可竄改：(1) E2E pytest 驗 trigger 真擋（dialect-aware fixture 自裝 trigger）(2) 加 `ivy_audit_writer` PG role + REVOKE UPDATE/DELETE + GRANT INSERT + role membership (3) audit middleware SET LOCAL ROLE 切換 per-transaction。

**Architecture:** 4 commit 同 PR：(C1) E2E trigger pytest / (C2) alembic migration audwrt01 / (C3) audit middleware SET LOCAL ROLE + tests / (C4) spec.md（已先 commit `6d099b9`）。

**Tech Stack:** PostgreSQL trigger + role / SQLAlchemy raw SQL / alembic migration / pytest dialect-aware fixture

**Spec:** `docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md` (commit `6d099b9`)

---

## File Structure

**New files:**
- `tests/test_audit_logs_immutable.py` — 3 E2E test + autouse install_audit_trigger fixture
- `tests/test_audit_writer_role.py` — 2 test for SET LOCAL ROLE + fail-open
- `alembic/versions/YYYYMMDD_audwrt01_audit_writer_role.py` — role + GRANT/REVOKE migration

**Modified files:**
- `utils/audit.py` — `write_audit_log` 內加 `SET LOCAL ROLE ivy_audit_writer`（PG only）+ try/except fail-open

**Unchanged but referenced:**
- `alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py` — 既有 trigger
- `alembic/versions/20260518_parlsr001_parent_rls_phase0.py` — 既有 4 role 定義
- `models/audit.py` — AuditLog schema
- `tests/conftest.py:test_db_session` — 既有 SQLite create_all fixture

---

## Task 1: trigger E2E pytest (PR-D1)

**Goal:** 加 dialect-aware fixture 自裝 trigger DDL，驗 UPDATE/DELETE raise + INSERT 正常。

**Files:**
- Create: `tests/test_audit_logs_immutable.py`

### Steps

- [ ] **Step 1.1: 確認 AuditLog schema 細節**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
grep -n "Column\|class AuditLog" models/audit.py | head -20
```
Expected: `entity_id = Column(String(50)...)` / `changes = Column(Text, nullable=True...)` / `action` `entity_type` required。

- [ ] **Step 1.2: 寫 test file**

Create `tests/test_audit_logs_immutable.py` per Spec §3.2 完整代碼（已含 trigger SQL for PG + SQLite, autouse `_install_audit_trigger` fixture, schema-aligned `_create_test_audit_log` helper, 3 個 test）。

- [ ] **Step 1.3: 跑 test 確認 pass**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
pytest tests/test_audit_logs_immutable.py -v 2>&1 | tail -15
```
Expected: 3 個 test 全 pass。

如果 fail：
- `test_audit_log_update_raises` 沒 raise → trigger install fixture 沒生效（看 `_install_audit_trigger` fixture 的 dialect 判斷 + DDL 是否 commit）
- schema mismatch → grep `models/audit.py` 對齊 columns

- [ ] **Step 1.4: 跑全套 pytest 確認零回歸**

```bash
pytest --tb=line 2>&1 | tail -10
```
Expected baseline + 3 new test，0 new fail（與 main baseline 對比）。

- [ ] **Step 1.5: Commit (C1)**

```bash
git add tests/test_audit_logs_immutable.py
git commit -m "$(cat <<'EOF'
test(audit): E2E trigger verification for audit_logs immutability

Spec D PR-D1：3 個 pytest + dialect-aware autouse fixture（自裝 trigger DDL
匹配既有 alembic l7m8n9o0p1q2 migration）。

防 future alembic downgrade / migration drift 意外移除 trigger 但無人察覺。
直接走 raw SQL UPDATE/DELETE assert IntegrityError/OperationalError。

注意 tests/conftest.py:167 用 Base.metadata.create_all 不跑 alembic migration
→ test DB 沒 trigger，本 test 自己 CREATE TRIGGER 在 PG/SQLite 雙 dialect。

Refs: Spec docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md §3.2
EOF
)"
```

---

## Task 2: alembic migration audwrt01 (PR-D2)

**Goal:** 建 `ivy_audit_writer` LOGIN role + REVOKE UPDATE/DELETE FROM admin/parent/public + GRANT INSERT + GRANT role membership.

**Files:**
- Create: `alembic/versions/YYYYMMDD_audwrt01_audit_writer_role.py`

### Steps

- [ ] **Step 2.1: 確認當前 alembic head**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
alembic heads
```
Expected: 單一 head SHA。記錄為 `down_revision`。

- [ ] **Step 2.2: 確認 audit_logs.id sequence 名稱**

```bash
psql -U postgres -d <dev_db> -c "\d audit_logs" 2>/dev/null | grep -i sequence
# 或 dev 沒 PG 跑 alembic 預設名稱：audit_logs_id_seq
```

如果 dev 沒 PG，可信任 PG 預設名稱 `audit_logs_id_seq`（SERIAL 預設 sequence 命名規則）。

- [ ] **Step 2.3: 寫 migration**

Create `alembic/versions/20260528_audwrt01_audit_writer_role.py` per Spec §3.3 完整代碼。重點：
- `down_revision` 填 Step 2.1 抓到的 head
- 含 PG dialect check（SQLite skip 整個 migration）
- DO $$ ... BEGIN ... END $$ 包 CREATE ROLE 避免 already exists 失敗
- REVOKE UPDATE, DELETE FROM PUBLIC + ivy_admin_role + ivy_parent_role
- GRANT INSERT ON audit_logs TO ivy_audit_writer + ivy_admin_role
- GRANT SELECT ON audit_logs TO ivy_admin_role
- GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO 兩個 role
- **CRITICAL**: `GRANT ivy_audit_writer TO ivy_admin_login`（advisor 抓的 SET LOCAL ROLE prerequisite）
- `downgrade()` 做 reverse：REVOKE + GRANT back to admin + DROP ROLE

- [ ] **Step 2.4: 本地 alembic upgrade dry run（如有 PG）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
alembic upgrade head 2>&1 | tail -10
```

如本地無 PG（test 走 SQLite），dialect skip 應直接 pass。

- [ ] **Step 2.5: 跑全套 pytest 確認 migration 不破 baseline**

```bash
pytest --tb=line 2>&1 | tail -10
```
Expected: baseline + 3 new test (Task 1) = +3 new，0 new fail。

- [ ] **Step 2.6: Commit (C2)**

```bash
git add alembic/versions/20260528_audwrt01_audit_writer_role.py
git commit -m "$(cat <<'EOF'
feat(audit): audwrt01 migration — ivy_audit_writer role + REVOKE/GRANT

Spec D PR-D2：建 ivy_audit_writer LOGIN role + REVOKE UPDATE/DELETE FROM
ivy_admin_role/ivy_parent_role/public + GRANT INSERT ON audit_logs +
GRANT SELECT ON audit_logs TO admin + sequence USAGE。

CRITICAL: GRANT ivy_audit_writer TO ivy_admin_login（advisor 抓）— 沒這個
runtime SET LOCAL ROLE ivy_audit_writer 會 permission denied。

PG only；SQLite test 走 dialect.name skip。downgrade 完整。

PROD ops 需手動跑 ALTER ROLE ivy_audit_writer PASSWORD '<from-secret>'
（secret 不入 alembic）。spec §5.1 roll-out checklist 詳列。

Refs: Spec docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md §3.3
EOF
)"
```

---

## Task 3: audit middleware SET LOCAL ROLE + tests (PR-D3)

**Goal:** `utils/audit.py` 內 audit_log INSERT 前 SET LOCAL ROLE ivy_audit_writer（PG only），fail-open if 失敗。加 2 個 pytest。

**Files:**
- Modify: `utils/audit.py`
- Create: `tests/test_audit_writer_role.py`

### Steps

- [ ] **Step 3.1: 找 audit_log INSERT 確切位置**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
grep -n "session.add\|AuditLog(\|session.commit" utils/audit.py | head -20
```

找 `session.add(audit_log_obj)` + `session.commit()` 序列位置（spec §3.4 提到 `utils/audit.py:340-360`）。

- [ ] **Step 3.2: 加 SET LOCAL ROLE 邏輯**

在 audit INSERT 之前（同 transaction 內）加：

```python
# 切換到 audit_writer role（per-transaction，commit 後自動 reset）
# fail-open: SET LOCAL ROLE 失敗（role 未建/權限缺）→ log warning + 走預設 admin connection
from sqlalchemy import text

if session.bind.dialect.name == "postgresql":
    try:
        session.execute(text("SET LOCAL ROLE ivy_audit_writer"))
    except Exception as e:
        logger.warning(
            "audit: SET LOCAL ROLE ivy_audit_writer failed, falling back to admin role: %s", e
        )
        # 不 raise，繼續用 admin connection 寫入（既有 design 仍可工作；trigger 仍主防線）

session.add(audit_log_obj)
session.commit()
```

具體插入位置由 Step 3.1 結果決定（可能在 `_write_audit_async` 或 `write_audit_log` 內）。

- [ ] **Step 3.3: 寫 2 個 pytest**

Create `tests/test_audit_writer_role.py`：

```python
"""Spec D PR-D3: audit middleware SET LOCAL ROLE switching + fail-open。"""

from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError


def test_audit_middleware_uses_audit_writer_role(test_db_session):
    """PG dialect 時 audit_log INSERT 前 SET LOCAL ROLE ivy_audit_writer 被 call。"""
    # 這個 test 假設 test_db_session 是 SQLite（dialect skip → SET LOCAL ROLE 不被 call）
    # 或 mock dialect 為 PG 後 assert text("SET LOCAL ROLE ivy_audit_writer") 被 execute
    
    from utils.audit import write_audit_log
    
    # mock session.bind.dialect.name 為 postgresql
    with patch.object(test_db_session.bind.dialect, "name", "postgresql"):
        with patch.object(test_db_session, "execute") as execute_spy:
            # write_audit_log call 需配合實際簽章；視 utils/audit.py 接口
            try:
                write_audit_log(
                    session=test_db_session,
                    action="TEST",
                    entity_type="TEST",
                    entity_id="1",
                )
            except Exception:
                pass  # 不在乎 INSERT 結果，只看 execute 是否被 call SET LOCAL ROLE
            
            calls = [call for call in execute_spy.call_args_list]
            set_role_calls = [
                c for c in calls
                if c.args and "SET LOCAL ROLE" in str(c.args[0])
                and "ivy_audit_writer" in str(c.args[0])
            ]
            assert set_role_calls, f"Expected SET LOCAL ROLE ivy_audit_writer execute, got: {calls}"


def test_audit_writer_role_missing_falls_open(test_db_session):
    """SET LOCAL ROLE raise → fall back，audit INSERT 仍成功 + log warning。"""
    from utils.audit import write_audit_log
    
    # mock dialect 為 PG，但讓 SET LOCAL ROLE raise ProgrammingError
    original_execute = test_db_session.execute
    
    def mock_execute(stmt, *args, **kwargs):
        if "SET LOCAL ROLE" in str(stmt):
            raise ProgrammingError("SET LOCAL ROLE", None, Exception("permission denied"))
        return original_execute(stmt, *args, **kwargs)
    
    with patch.object(test_db_session.bind.dialect, "name", "postgresql"):
        with patch.object(test_db_session, "execute", side_effect=mock_execute):
            with patch("utils.audit.logger") as logger_spy:
                # write_audit_log 應 fall-open（不 raise，繼續 INSERT）
                write_audit_log(
                    session=test_db_session,
                    action="TEST_FALLBACK",
                    entity_type="TEST",
                    entity_id="999",
                )
                # assert warning 被 log
                warning_calls = [
                    c for c in logger_spy.warning.call_args_list
                    if "SET LOCAL ROLE" in str(c.args[0] if c.args else "")
                ]
                assert warning_calls, "Expected SET LOCAL ROLE fail log warning"
```

**注意**：`write_audit_log` 實際簽章 + caller pattern 看 Step 3.1 結果調整。test 內 `write_audit_log(...)` call 對齊實際 utils/audit.py interface。

- [ ] **Step 3.4: 跑 audit tests + 全套 pytest**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
pytest tests/test_audit_writer_role.py tests/test_audit_logs_immutable.py -v 2>&1 | tail -20
pytest --tb=line 2>&1 | tail -10
```
Expected: 5 new test 全 pass（3 + 2），baseline 不破。

- [ ] **Step 3.5: Commit (C3)**

```bash
git add utils/audit.py tests/test_audit_writer_role.py
git commit -m "$(cat <<'EOF'
feat(audit): switch audit middleware to ivy_audit_writer role

Spec D PR-D3：audit_log INSERT 前 SET LOCAL ROLE ivy_audit_writer (PG only)，
per-transaction (SET LOCAL 在 commit/rollback 後自動 reset)。SET LOCAL ROLE
失敗 → log warning + fall-open 走 admin connection 寫入（trigger 仍主防線）。

2 個新 pytest：
- test_audit_middleware_uses_audit_writer_role — PG dialect 時 SET LOCAL ROLE
  被 call
- test_audit_writer_role_missing_falls_open — SET LOCAL ROLE raise 時 audit
  仍 INSERT 成功 + log warning

Refs: Spec docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md §3.4
EOF
)"
```

---

## Task 4: 最終驗收 + push branch

**Goal:** 全套 pytest sanity + push worktree branch 讓 user review。

### Steps

- [ ] **Step 4.1: 全套 pytest 最終跑（背景 ~22-40 min）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
pytest --tb=short 2>&1 | tail -15
```

- [ ] **Step 4.2: git log + diff stat 確認 4 commit**

```bash
git log --oneline origin/main..HEAD
git diff origin/main..HEAD --stat
```
Expected:
- 4 commit (spec + 3 implementation)
- Files: spec.md, plan.md, tests/test_audit_logs_immutable.py, alembic migration, utils/audit.py, tests/test_audit_writer_role.py

- [ ] **Step 4.3: push worktree branch 給 user review**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-audit-append-only-2026-05-28-backend
git push -u origin feat/audit-append-only-2026-05-28-backend
```

- [ ] **Step 4.4: 報告完成 + Roll-out checklist**

向 user 回報：
- ✅ 4 commit 完成（commit SHA）
- ✅ Branch `feat/audit-append-only-2026-05-28-backend` pushed to origin
- ✅ 全套 pytest pass + 5 new test
- **CRITICAL Roll-out steps（spec §5.1 9 條）**
- 提醒：`ALTER ROLE ivy_audit_writer PASSWORD '<from-secret>'` ops 必跑（secret 不在 git）

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 E2E trigger verification | Task 1 | ✓ |
| §2 G2 audit_writer role + REVOKE/GRANT migration | Task 2 | ✓ |
| §2 G3 audit middleware SET LOCAL ROLE | Task 3 | ✓ |
| §2 G4 spec.md | C4 (已 commit 6d099b9) | ✓ |
| §2 G5 零回歸 | Task 1/2/3 各自 step | ✓ |
| §2 G6 Prod roll-out checklist | Task 4 Step 4.4 | ✓ |
| §3.2 trigger E2E + dialect-aware fixture | Task 1 Step 1.2 | ✓ |
| §3.3 audwrt01 + GRANT ivy_audit_writer TO ivy_admin_login | Task 2 Step 2.3 | ✓ |
| §3.4 SET LOCAL ROLE + fail-open | Task 3 Step 3.2 | ✓ |
| §4 5 個 pytest | Task 1 (3) + Task 3 (2) | ✓ |
| §5 Roll-out 9 條 checklist | Task 4 Step 4.4 | ✓ |
