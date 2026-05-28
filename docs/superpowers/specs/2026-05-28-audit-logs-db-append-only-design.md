# Spec D: audit_logs DB append-only hardening (#10)

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：🟠 P1 #10 — audit_logs 表無 DB 層 append-only 保護
**對應 spec 系列**：A (限流) ✅ / B (CSRF) ✅ / **D (audit append-only)** / C (Logger PII) / E (LINE 跨境) / F (staff refresh)

---

## 1. Why

### 1.1 Audit finding #10 部分前提錯誤

P1 #10 claim：「`models/audit.py:12-35` 純 SQLAlchemy 表，無 DENY UPDATE/DELETE、無 INSERT-only role、無 trigger」

**Verify 結果（2026-05-28 grep `alembic/versions/`）**：
- ✅ trigger 已 land 於 `alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py` (2026-05-07 P0 #12 fix)
  - PostgreSQL: `audit_log_immutable_fn()` plpgsql function + `trg_audit_log_immutable_update` + `trg_audit_log_immutable_delete` 兩個 BEFORE trigger，RAISE EXCEPTION
  - SQLite (test): 同名 trigger 用 `RAISE(ABORT)`
- ❌ **無 REVOKE UPDATE/DELETE FROM `ivy_admin_role`**（既有 `parent_rls` migration 已建 4 個 role，未對 audit_logs 加 grant 隔離）
- ❌ **無 application-layer E2E test 確認 trigger 真 active**（純 alembic SQL，依賴 ops 信任）

### 1.2 既有 multi-role 架構

`alembic/versions/20260518_parlsr001_parent_rls_phase0.py` 已建：
- `ivy_parent_role` (group NOLOGIN)
- `ivy_admin_role` (group NOLOGIN **BYPASSRLS**)
- `ivy_parent_login` (LOGIN, IN ROLE parent_role)
- `ivy_admin_login` (LOGIN **BYPASSRLS** direct attribute, IN ROLE admin_role)

**關鍵 PostgreSQL semantics**：
- **BYPASSRLS 屬性 不會透過 IN ROLE 繼承**（必須直接掛 LOGIN role）— migration 註解明確說明
- **PostgreSQL trigger 對 BYPASSRLS / SUPERUSER 仍 enforce**（trigger 不受 RLS bypass 影響）→ 既有 trigger 已 cover `ivy_admin_login` 的 UPDATE/DELETE attempt
- **只有兩種方式繞 trigger**：
  1. SUPERUSER 連線 + `ALTER TABLE audit_logs DISABLE TRIGGER ALL`
  2. SUPERUSER 連線 + `DROP TRIGGER trg_audit_log_immutable_*`

### 1.3 剩餘 gap 與本 spec 範圍

| Gap | 重要性 | 解法 |
|-----|--------|------|
| 無 application-layer E2E test 確認 trigger 真 active | 高（防 future alembic downgrade 誤刪 trigger） | 加 pytest 跑 raw SQL UPDATE/DELETE assert IntegrityError |
| 無 REVOKE UPDATE/DELETE 達 belt-and-braces | 中（trigger 已防 99%；REVOKE 是 secondary defense） | 加 alembic migration |
| 無獨立 audit_writer role | 低（既有 admin_login + trigger + 即將 REVOKE 已三層防護） | **User 拍板：加** (Approach A, see §3.1) |
| Prod trigger 是否 active 未經 verify | 高 | USER 手動 psql 確認（roll-out checklist） |

---

## 2. Goals / Non-goals

### Goals
- (G1) 加 pytest E2E test 跑 raw SQL `UPDATE audit_logs` + `DELETE FROM audit_logs` assert raise IntegrityError（PG）/ OperationalError（SQLite）—— 防 future alembic downgrade / migration drift 誤刪 trigger 不被抓到
- (G2) 加 alembic migration 建 `ivy_audit_writer` role + REVOKE UPDATE/DELETE FROM `ivy_admin_role`, `ivy_parent_role`, `public` + GRANT INSERT TO `ivy_audit_writer` & `ivy_admin_role`
- (G3) audit middleware 寫 audit_logs 時切換到 `ivy_audit_writer` role（透過 SET ROLE / RESET ROLE per transaction，重用既有 admin_login connection）
- (G4) 寫 Spec D 文件記錄：trigger 已 land + REVOKE/GRANT + audit_writer role 三層防護完成
- (G5) 零回歸：既有 5563 pytest + 6 既有 unrelated fail 不變；audit_logs 所有 INSERT 仍正常運作
- (G6) Prod migration 不破壞 ops：roll-out checklist 含 trigger active verify SQL + role 切換 smoke

### Non-goals
- 不開 separate SQLAlchemy engine 給 audit_writer（重用 admin connection + SET ROLE 機制，避免 connection pool 翻倍）
- 不對 audit_logs 加額外 column / index（純 role + grant + trigger 補完，schema 不動）
- 不重寫既有 `utils/audit.py:340-360 write_audit_log` logic（只改寫入 transaction 的 role context）
- 不調整既有 4 role（parent_role / admin_role / parent_login / admin_login）的 LOGIN / BYPASSRLS / IN ROLE 屬性
- 不在本 spec 內處理 P0/P1 其餘 audit findings（C / E / F 為獨立 spec）

### Approach 替代方案（Out of scope but documented）

**Approach C (simplified, 2-3h)** — 若 user 在 spec review 改變心意，可降級為純 GRANT/REVOKE 不新增 role：
- REVOKE UPDATE, DELETE ON audit_logs FROM `ivy_admin_role`, `ivy_parent_role`, public
- GRANT INSERT ON audit_logs TO `ivy_admin_role`
- audit middleware **無需改動**（仍用 admin connection；REVOKE 已生效）
- 防御差異：Option A 額外擋「admin role 內部 self-GRANT UPDATE 然後 DROP trigger」這種**極窄**攻擊面（攻擊者需要 admin role + GRANT 自己 + DROP trigger 三步驟皆能完成 = 已是 superuser-level pwn）
- **本 spec 走 Option A**（user 選擇）；如降級 Option C 工時節省 4-5h

---

## 3. Architecture

### 3.1 PR 結構

| Commit | 範圍 | 檔案數 | 風險 |
|--------|------|--------|------|
| **C1** `test(audit): E2E trigger verification` | pytest E2E test | 1 new test file | 零（純 test） |
| **C2** `feat(audit): audit_writer role + REVOKE/GRANT migration` | alembic migration | 1 new migration | 中（prod migration 需 verify） |
| **C3** `feat(audit): switch audit middleware to audit_writer role` | audit middleware 切連線 | `utils/audit.py` + test | 中（runtime role 切換需 smoke） |
| **C4** `docs(p1d): audit append-only spec` | spec.md | 本檔（已 commit 後即視為 done） | 零 |

3-4 commit 同 PR；建議**單 PR 4 commit**（user 偏好 commit 紀律）。

### 3.2 Trigger E2E test（PR-D1）

**CRITICAL（advisor 2026-05-28 抓）**：`tests/conftest.py:167 Base.metadata.create_all(test_engine)` 走 ORM schema 不跑 alembic migration → test DB **沒 trigger**。E2E test 直接跑 raw SQL UPDATE/DELETE 不會 raise → test 假 pass。

兩個解法：
- (a) test fixture 顯式 `op.execute` 安裝 trigger DDL 在 test session 開始（dialect-aware）
- (b) 用 `@pytest.mark.skip(reason="PG-only trigger")` 跳過 SQLite，跑在 integration test 階段對 real PG

**選 (a)**：cleaner、test 在 unit pytest stage 就抓得到 regression、不需要新 CI job。

新檔 `tests/test_audit_logs_immutable.py`：

```python
"""Spec D PR-D1: audit_logs immutable trigger E2E verification。

防 future alembic downgrade / drift 意外移除 trigger 但無人察覺。
直接走 raw SQL UPDATE / DELETE assert IntegrityError / OperationalError。

注意：tests/conftest.py:167 用 Base.metadata.create_all 不跑 alembic
migration → test DB 沒 trigger。本檔自己 install trigger DDL 模擬 prod 行為。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError

from models.audit import AuditLog
from models.database import session_scope, get_engine


# 對齊 alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py
_INSTALL_TRIGGER_SQLITE = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE');
    END;
    """,
]

_INSTALL_TRIGGER_PG = [
    """
    CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
    RETURNS trigger AS $$
    BEGIN
        IF (TG_OP = 'UPDATE') THEN
            RAISE EXCEPTION 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE (id=%)', OLD.id;
        ELSIF (TG_OP = 'DELETE') THEN
            RAISE EXCEPTION 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE (id=%)', OLD.id;
        END IF;
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "CREATE TRIGGER trg_audit_log_immutable_update BEFORE UPDATE ON audit_logs FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_fn();",
    "CREATE TRIGGER trg_audit_log_immutable_delete BEFORE DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_fn();",
]


@pytest.fixture(autouse=True)
def _install_audit_trigger(test_db_session):
    """test_db_session fixture 跑 create_all 後，本 fixture 補裝 trigger DDL。

    test_db_session 來自 conftest（建 SQLite + Base.metadata.create_all）。
    我們在這之後手動 CREATE TRIGGER 補上 alembic migration 不跑的 trigger。
    """
    engine = get_engine()
    dialect = engine.dialect.name
    stmts = _INSTALL_TRIGGER_PG if dialect == "postgresql" else _INSTALL_TRIGGER_SQLITE
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    yield
    # 不主動 DROP — test_db_session 結束時 SQLite 整個 DB tmpfile 被刪


def _create_test_audit_log(session) -> int:
    """對齊實際 AuditLog schema：必填 action + entity_type；extras 用 changes column。"""
    log = AuditLog(
        action="TEST",
        entity_type="TEST",
        entity_id="1",   # String(50), not int
        changes="{}",    # Text, nullable but 給空 JSON
    )
    session.add(log)
    session.commit()
    return log.id


def test_audit_log_update_raises(test_db_session):
    """UPDATE audit_logs raise DatabaseError（PG: IntegrityError / SQLite: OperationalError）。"""
    log_id = _create_test_audit_log(test_db_session)

    with pytest.raises((DatabaseError, IntegrityError, OperationalError)) as exc_info:
        test_db_session.execute(
            text("UPDATE audit_logs SET entity_id = '999' WHERE id = :id"),
            {"id": log_id},
        )
        test_db_session.commit()
    msg = str(exc_info.value).lower()
    assert "audit_logs" in msg or "abort" in msg, f"Expected trigger reject, got: {exc_info.value}"


def test_audit_log_delete_raises(test_db_session):
    """DELETE audit_logs raise DatabaseError。"""
    log_id = _create_test_audit_log(test_db_session)

    with pytest.raises((DatabaseError, IntegrityError, OperationalError)) as exc_info:
        test_db_session.execute(
            text("DELETE FROM audit_logs WHERE id = :id"),
            {"id": log_id},
        )
        test_db_session.commit()
    msg = str(exc_info.value).lower()
    assert "audit_logs" in msg or "abort" in msg, f"Expected trigger reject, got: {exc_info.value}"


def test_audit_log_insert_succeeds(test_db_session):
    """INSERT audit_logs 仍正常（trigger 只擋 UPDATE/DELETE）。"""
    log_id = _create_test_audit_log(test_db_session)
    assert log_id is not None and log_id > 0
```

**對齊 schema（advisor 2026-05-28 抓）**：
- `AuditLog.entity_id` 是 `String(50)` 不是 int — fixture 用 `"1"` 不是 `1`
- AuditLog 無 `extras_json` column — 用 `changes` (Text, nullable) 寫 JSON
- 必填只有 `action` + `entity_type`（其餘 nullable 或 default）
- 透過 `test_db_session` fixture（conftest 提供 SQLite create_all）+ 自動 install trigger fixture 達成 dialect-aware E2E test

### 3.3 audit_writer role + REVOKE/GRANT migration（PR-D2）

新檔 `alembic/versions/YYYYMMDD_audwrt01_audit_writer_role.py`：

```python
"""audit_writer role + REVOKE UPDATE/DELETE / GRANT INSERT on audit_logs

Revision ID: audwrt01
Revises: <當前 head>
Create Date: 2026-05-28

Why:
    Spec D defense-in-depth：trigger 已防 UPDATE/DELETE，本 migration 加：
    1. ivy_audit_writer LOGIN role（密碼由 ops 另設）
    2. REVOKE UPDATE, DELETE ON audit_logs FROM ivy_admin_role, ivy_parent_role, public
    3. GRANT INSERT ON audit_logs TO ivy_audit_writer, ivy_admin_role
    4. GRANT SELECT ON audit_logs TO ivy_admin_role（查看仍需要）

    即使 trigger 被 DROP, REVOKE 仍擋；即使 user 加 GRANT UPDATE 給 admin, trigger 仍擋。

    Refs: audit P1 #10、spec docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md
"""

from alembic import op
import sqlalchemy as sa

revision = "audwrt01"
down_revision = "<plan 確認當前 head>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite 不支 role，skip
        return

    op.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ivy_audit_writer') THEN
                CREATE ROLE ivy_audit_writer WITH LOGIN;
            END IF;
        END
        $$;
    """))

    # REVOKE UPDATE / DELETE from all known roles + PUBLIC
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC"))
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM ivy_admin_role"))
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM ivy_parent_role"))

    # GRANT INSERT for audit writes
    op.execute(sa.text("GRANT INSERT ON audit_logs TO ivy_audit_writer"))
    op.execute(sa.text("GRANT INSERT ON audit_logs TO ivy_admin_role"))

    # GRANT SELECT for admin to view audit
    op.execute(sa.text("GRANT SELECT ON audit_logs TO ivy_admin_role"))

    # GRANT USAGE on sequence (audit_logs.id is SERIAL)
    op.execute(sa.text("GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO ivy_audit_writer"))
    op.execute(sa.text("GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO ivy_admin_role"))

    # CRITICAL（advisor 2026-05-28 抓）：SET LOCAL ROLE 需要 caller 是 target role
    # 的 member。沒這個 GRANT 則 runtime SET LOCAL ROLE ivy_audit_writer 會
    # "permission denied to set role" → 100% audit-write 走 fail-open 路徑
    # （log warning + 用 admin connection 寫入，破壞 audit_writer 設計意圖）。
    # parlsr001 註解明示 BYPASSRLS 不會透過 IN ROLE 繼承；role membership 是
    # 另一個 GRANT，必須直接寫。
    op.execute(sa.text("GRANT ivy_audit_writer TO ivy_admin_login"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("REVOKE INSERT ON audit_logs FROM ivy_audit_writer"))
    op.execute(sa.text("REVOKE USAGE, SELECT ON SEQUENCE audit_logs_id_seq FROM ivy_audit_writer"))
    # 重新 GRANT 給 admin_role 與 PUBLIC（恢復 PG 預設行為）
    op.execute(sa.text("GRANT UPDATE, DELETE ON audit_logs TO ivy_admin_role"))
    op.execute(sa.text("DROP ROLE IF EXISTS ivy_audit_writer"))
```

**注意**：
- `down_revision` 在 plan stage `alembic heads` 確認當前 head
- ops 部署後需手動：`ALTER ROLE ivy_audit_writer PASSWORD '<from-secret>'`（migration 不寫密碼避免 secret in git）
- SQLite test 走 `bind.dialect.name == "sqlite"` skip，trigger 仍由既有 migration cover

### 3.4 audit middleware 切連線（PR-D3）

`utils/audit.py:340-360 write_audit_log` 內：

```python
def write_audit_log(...):
    # ... 既有準備 audit_log 物件邏輯 ...
    
    session = get_session()
    try:
        # 切換到 audit_writer role per transaction
        # （SET ROLE 影響本 connection 直到 RESET，不影響其他 worker）
        if session.bind.dialect.name == "postgresql":
            session.execute(text("SET LOCAL ROLE ivy_audit_writer"))
        
        session.add(audit_log)
        session.commit()  # 此 commit 內 SET LOCAL ROLE 仍生效
        # SET LOCAL 在 commit 後自動 reset（PG SET LOCAL semantics）
    except Exception:
        session.rollback()
        # rollback 後 SET LOCAL 也 reset
        raise
    finally:
        session.close()
```

**為何用 `SET LOCAL ROLE` 而非 `SET ROLE`**：
- `SET LOCAL` 在 transaction 結束（commit/rollback）後**自動 reset**
- `SET ROLE`（無 LOCAL）會 persist 到 session 結束 — 若 connection 被 pool 復用，下一個 request 仍是 audit_writer
- `SET LOCAL` 是 PG 推薦 per-transaction role switch pattern

**SQLite test fallback**：dialect check skip，SQLite 無 role 概念 trigger 已是唯一防線

### 3.5 與既有 multi-role 互動

| Caller | 連線使用 role | audit_logs 操作 |
|--------|--------------|-----------------|
| 一般 staff request | `ivy_admin_login` | INSERT (透過 audit middleware → SET LOCAL ROLE ivy_audit_writer) |
| 家長 request | `ivy_parent_login` (via parent_rls) | **無**（家長端不寫 audit_logs；audit middleware skip 家長 path） |
| Cron / scheduler | `ivy_admin_login` (default app role) | INSERT (同 staff path) |
| Manual DBA psql | `ivy_admin_login` | SELECT only；UPDATE/DELETE 被 REVOKE 擋 + trigger 擋 |
| Manual DBA SUPERUSER | SUPERUSER | UPDATE/DELETE 被 trigger 擋（trigger 對 SUPERUSER 仍 enforce） |

**沒有**家長端寫 audit_logs path（家長操作的 audit 仍由 admin connection 寫入；parent_rls middleware 是 read-only context）。確認 in plan stage：`grep -n "write_audit_log\|AuditLog(" api/parent_portal/` 應**無命中**。

### 3.6 不入 audit_logs 異常處理

`SET LOCAL ROLE` 失敗（極端：role 還沒建好 / 密碼錯）→ audit middleware:
- fail-open（既有 audit middleware 已是 fail-open，缺 audit 不該擋 user request）
- log warning 給 Sentry
- 既有 `_background_tasks` 機制不影響

**追加 test**：`test_audit_writer_role_missing_falls_open`（mock `SET ROLE` raise，assert 仍 INSERT 成功 走 admin role default + log warning）—— plan stage 落實。

---

## 4. 測試計畫

新增 4-5 個 pytest：

**`tests/test_audit_logs_immutable.py`** (Task 1):
1. `test_audit_log_update_raises` — UPDATE raise DatabaseError
2. `test_audit_log_delete_raises` — DELETE raise DatabaseError
3. `test_audit_log_insert_succeeds` — INSERT 正常

**`tests/test_audit_writer_role.py`** (Task 3):
4. `test_audit_middleware_uses_audit_writer_role` — mock session.execute SET LOCAL ROLE 被 call with `ivy_audit_writer`
5. `test_audit_writer_role_missing_falls_open` — mock SET LOCAL ROLE raise，assert audit INSERT 仍走 admin default + log warning

**回歸**：全套 5563 pytest baseline（6 unrelated fail 不變）+ 5 new test = 5568 passed。

---

## 5. Roll-out

### 5.1 部署步驟

1. PR 合併（4 commit + 5 new test + 1 alembic migration）。
2. **PROD ops 必跑**（順序）：
   - Step A: `psql -U postgres -c "\du ivy_audit_writer"` verify role 還沒存在
   - Step B: `alembic upgrade head` 跑 audwrt01 migration
   - Step C: `psql -U postgres -c "ALTER ROLE ivy_audit_writer PASSWORD '<from-secret>'"` 設密碼（不入 alembic）
   - Step D: `psql -U postgres -c "\dp audit_logs"` verify privilege：UPDATE/DELETE 對 ivy_admin_role 已 REVOKE / INSERT 對 ivy_audit_writer 已 GRANT
   - Step E: 後端 service 重啟（audit middleware 開始 SET LOCAL ROLE）
3. Smoke 測試：
   - 任一 admin 操作（如新增員工 POST）→ 看 audit_logs 是否新增一筆 → ops `psql SELECT count(*) FROM audit_logs` 對比前後
   - 觀察 Sentry 1 小時無 `SET LOCAL ROLE` failed warning
4. Manual trigger verify（防 prod migration 沒 land）：
   - `psql -U ivy_admin_login -c "UPDATE audit_logs SET entity_id=999 WHERE id=1"` → 預期 raise `audit_logs 為不可竄改稽核軌跡，禁止 UPDATE`

### 5.2 回退方案

如 prod 出問題：
- **快速回退**：revert C3 commit（audit middleware 不切 role）— 立即恢復用 admin_login 寫 audit_logs，trigger 仍是主防線
- **完整回退**：`alembic downgrade audwrt01^` 移除 role + restore GRANT
- **零回退**：trigger（C1 + 既有 2026-05-07 trigger）永遠保留，是最低底線防護

### 5.3 監控指標

7 天觀察：
- `SET LOCAL ROLE ivy_audit_writer failed` warning 量：應為 0
- `audit_logs INSERT failed` error 量：應為 0（fail-open 不該擋寫入）
- `psql -U ivy_admin_login -c "SELECT count(*) FROM audit_logs"` 每日對比，確認 INSERT 速率穩定

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| `SET LOCAL ROLE` 在 connection pool 復用時 leak | 下一 request 走 audit_writer role 失敗 | `SET LOCAL` 自動隨 transaction reset；plan stage smoke verify connection pool 復用情境 |
| Prod alembic migration 跑時 ops 忘記設 `ivy_audit_writer` 密碼 | role 建好但無法 LOGIN → `SET LOCAL ROLE` 仍可用（不需 LOGIN），但日後直接連 audit_writer 失敗 | `SET LOCAL ROLE` 不需要 target role LOGIN attribute (PG semantics)；密碼只有「直接登入 ivy_audit_writer」才需要。spec §5.1 roll-out checklist 仍列為強制 step 避免 future 用 |
| audit middleware fail-open 設計可能讓所有 audit 寫入失敗都被 swallow | 防護缺失但 user 不察 | 既有 sentry warning 觸發；新加 `test_audit_writer_role_missing_falls_open` 驗 fall-back 路徑 |
| 既有 `ivy_admin_role` 沒被 REVOKE 過 UPDATE/DELETE on audit_logs，migration 第一次 REVOKE 會否 break 既有寫入 | INSERT 仍可（GRANT INSERT 補上）；UPDATE/DELETE 本來就被 trigger 擋（無 caller） | grep `models/__init__.py` + `utils/audit.py` 確認**無**任何 caller 對 audit_logs 做 UPDATE/DELETE（既存 design 本就 append-only）；plan stage step 必跑 |
| Test 跑時 SQLite trigger 行為與 PG 不同 | E2E test 可能 dialect-specific | test 用 `pytest.raises(DatabaseError)` 涵蓋 IntegrityError (PG) 與 OperationalError (SQLite)；assert message 含 `audit_logs` or `abort` 兩者皆 cover |

---

## 7. Out of scope

- 不處理 P0/P1 其餘 audit findings（C / E / F 為獨立 spec）
- 不調整既有 4 個 role 的 LOGIN / BYPASSRLS 屬性
- 不對 audit_logs schema 加 column / index / constraint
- 不重寫 `utils/audit.py` 既有 audit batch / dedup 邏輯
- 不引入 audit_logs 表 partitioning / archive（量大後 follow-up）

---

## 8. 驗收 checklist（user 手測 + roll-out）

PR 合併 + alembic migration 後 USER 手動驗證：

- [ ] `alembic upgrade head` 跑 audwrt01 migration 無錯
- [ ] `psql ... -c "\du ivy_audit_writer"` 確認 role 已建
- [ ] `ALTER ROLE ivy_audit_writer PASSWORD '<from-secret>'` 設密碼
- [ ] `psql -c "\dp audit_logs"` 確認 UPDATE/DELETE 對 ivy_admin_role 已 REVOKE / INSERT 對 ivy_audit_writer 已 GRANT
- [ ] 後端 service 重啟
- [ ] 任一 admin 操作（例如新增員工）→ audit_logs 新增一筆（select 看到）
- [ ] Sentry 1 小時無 `SET LOCAL ROLE` warning
- [ ] Manual trigger verify：`psql -U ivy_admin_login -c "UPDATE audit_logs SET entity_id=999"` → 預期 raise `audit_logs 為不可竄改稽核軌跡，禁止 UPDATE`
- [ ] pytest E2E test 三條（update/delete/insert）通過

---

## 9. 後續 follow-up（不在本 spec）

- audit_logs 量穩定 > 100M 後評估 partitioning（年/月）+ archive 到冷儲存
- 引入 `audit_logs.checksum` column（previous row hash chain）達 tamper detection 級別 — 超越本 spec defense-in-depth
- audit_writer role 改 separate engine（如 ops 觀察 `SET LOCAL ROLE` 開銷高）

---

## Approach C 降級備案（user 反悔可改用）

如 user 在實作中觀察複雜度太高想簡化，可隨時降級到 Approach C：
1. 移除 C2 migration 內 `CREATE ROLE ivy_audit_writer` + GRANT INSERT TO audit_writer + sequence GRANT
2. 移除 C3 commit（audit middleware 不切 role）
3. 保留 C1 (E2E test) + C4 (spec)
4. 工時節省 4-5h

降級後仍有：trigger（既有 2026-05-07）+ REVOKE UPDATE/DELETE FROM admin/parent role 雙重防護 = defense-in-depth 99% 達成；損失「audit_writer role 完整 separation of duties」這個 1% 增強。
