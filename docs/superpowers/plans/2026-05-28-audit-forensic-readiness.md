# Audit Forensic Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補強 `audit_logs` 對「家長帳號被盜」forensic readiness — 加 `user_agent_hash` + `session_id`（從現有 JWT `jti` 取），並補 4 條家長端敏感 detail GET 的 audit 痕跡。

**Architecture:** 既有 `utils/audit.py` 包含 `AuditMiddleware`（write actions）與 `write_explicit_audit`（GET 敏感讀取）。本 plan 在 `models/audit.py` schema 加 2 欄，於 `utils/audit.py` payload 組裝處取出 UA hash + JWT jti，並於 4 條家長端 endpoint 加 `write_explicit_audit` 呼叫。**JWT jti 已存在**（`utils/auth.py:210` `to_encode.setdefault("jti", ...)`），無須改動 token 生成。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、PyJWT、pytest

**Spec:** `docs/superpowers/specs/2026-05-28-observability-forensic-and-design-tokens-design.md` Ch1

---

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `alembic/versions/auditfor01_audit_logs_ua_and_session.py` | Create | Alembic migration：`audit_logs` 加 `user_agent_hash` (String(64), nullable) + `session_id` (String(64), nullable, indexed) |
| `models/audit.py` | Modify | `AuditLog` 加 2 個 Column |
| `utils/audit.py` | Modify | (a) 新增 `_extract_session_id_from_header(request) -> str \| None`（讀 JWT jti）；(b) 新增 `_compute_ua_hash(request) -> str \| None`；(c) 於 `write_audit_in_session`、`write_explicit_audit`、`_extract_user_from_header` 呼叫處的 payload 注入兩欄 |
| `utils/auth.py` | Modify | `decode_token_for_audit` 確認回傳 dict 含 `jti`（既有 jti 已在 payload，只需確認 helper 不過濾掉） |
| `api/parent_portal/medications.py` | Modify | `GET /medication-orders/{order_id}` (line 246) 加 `write_explicit_audit(..., dedup=True)` |
| `api/parent_portal/growth_reports.py` | Modify | `GET /growth-reports/{report_id}/download` (line 85) 加 `write_explicit_audit(..., dedup=False)`（下載類） |
| `api/parent_portal/contact_book.py` | Modify | `GET /contact-book/{entry_id}` (line 294) 加 `write_explicit_audit(..., dedup=True)` |
| `api/parent_portal/parent_downloads.py` | Modify | `GET /uploads/portfolio/{key:path}` (line 150) 加 `write_explicit_audit(..., dedup=False)` |
| `tests/test_audit_forensic.py` | Create | 5 個 pytest（schema / jti 取值 / ua_hash 計算 / write_explicit_audit 寫 2 欄 / parent endpoint 觸發 audit） |

---

## Task 1: Migration auditfor01 + 模型加 2 欄

**Files:**
- Create: `alembic/versions/auditfor01_audit_logs_ua_and_session.py`
- Modify: `models/audit.py`
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認當前 alembic head**

Run:
```bash
alembic heads
```
Expected: single head（spec ship 時點之後最新 head；e.g., `intghealth01` 或更新）。**將該 head 記下** 作為 `down_revision`。若有多 head，要先寫 merge head 而非直接 chain（不在本 plan 範圍 — 停下回報 user）。

- [ ] **Step 2: 寫 failing test — 確認 model 暴露兩欄**

Create `tests/test_audit_forensic.py`：

```python
"""tests/test_audit_forensic.py — Ch1 AuditLog forensic readiness."""

from sqlalchemy import inspect

from models.audit import AuditLog
from models.base import get_engine


def test_audit_log_model_has_ua_hash_and_session_id():
    """AuditLog ORM 模型必須暴露 user_agent_hash 與 session_id 兩欄。"""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert "user_agent_hash" in cols, f"missing user_agent_hash, got {cols}"
    assert "session_id" in cols, f"missing session_id, got {cols}"


def test_audit_logs_table_has_session_id_index():
    """session_id 必須有 index（forensic 查詢 'find all activity of same session')."""
    indexes = {idx.name for idx in AuditLog.__table__.indexes}
    assert any("session_id" in i.lower() for i in indexes), (
        f"no index on session_id; got indexes {indexes}"
    )
```

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_audit_log_model_has_ua_hash_and_session_id -xvs
```
Expected: FAIL — `AssertionError: missing user_agent_hash`

- [ ] **Step 4: 加欄位到 `models/audit.py`**

Modify `models/audit.py`，在 `acknowledged_by` 行之後加入：

```python
    user_agent_hash = Column(
        String(64),
        nullable=True,
        comment="SHA256(UA)[:32]，避免直存 device PII",
    )
    session_id = Column(
        String(64),
        nullable=True,
        index=True,
        comment="JWT jti claim — forensic 用，stateless 無伺服端 session",
    )
```

並更新 `__table_args__` 加入 session_id 的 explicit Index（`index=True` 已建但 explicit 命名更安全）：

```python
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_logs_ack_created", "acknowledged_at", "created_at"),
        Index("ix_audit_session", "session_id"),
    )
```

並把 column 上的 `index=True` 移除（避免雙重 index）：

```python
    session_id = Column(
        String(64),
        nullable=True,
        comment="JWT jti claim — forensic 用，stateless 無伺服端 session",
    )
```

- [ ] **Step 5: Run model tests**

Run:
```bash
pytest tests/test_audit_forensic.py::test_audit_log_model_has_ua_hash_and_session_id tests/test_audit_forensic.py::test_audit_logs_table_has_session_id_index -xvs
```
Expected: PASS

- [ ] **Step 6: 寫 Alembic migration**

Create `alembic/versions/auditfor01_audit_logs_ua_and_session.py`：

```python
"""audit_logs add user_agent_hash + session_id

Revision ID: auditfor01
Revises: <CURRENT_HEAD_FROM_STEP_1>
Create Date: 2026-05-28

Ch1 of observability-forensic-and-design-tokens spec.
新增兩欄供「家長帳號被盜」forensic：
- user_agent_hash: SHA256(UA)[:32]，hash 化避免直存 device PII
- session_id: JWT jti claim（stateless，無伺服端 session 表）
"""

from alembic import op
import sqlalchemy as sa


revision = "auditfor01"
down_revision = "<CURRENT_HEAD_FROM_STEP_1>"  # 替換成 step 1 抓到的 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_audit_session", "audit_logs", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_session", table_name="audit_logs")
    op.drop_column("audit_logs", "session_id")
    op.drop_column("audit_logs", "user_agent_hash")
```

**重要**：替換 `<CURRENT_HEAD_FROM_STEP_1>` 為 step 1 抓到的實際 head ID。

- [ ] **Step 7: 驗 migration 跑得起來**

Run:
```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```
Expected: 全部成功，最終 head 為 `auditfor01`。

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/auditfor01_audit_logs_ua_and_session.py models/audit.py tests/test_audit_forensic.py
git commit -m "feat(audit): 加 user_agent_hash + session_id 欄位 (auditfor01)

audit_logs 新增 2 欄供「家長帳號被盜」forensic：
- user_agent_hash: SHA256(UA)[:32]，hash 化避 PII
- session_id: JWT jti（stateless），加 ix_audit_session
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1"
```

---

## Task 2: 在 utils/audit.py 加 UA hash + session_id 取值

**Files:**
- Modify: `utils/audit.py`
- Modify: `utils/auth.py`（確認 decode_token_for_audit 保留 jti）
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認 decode_token_for_audit 不會吃掉 jti**

Run:
```bash
grep -A 20 "def decode_token_for_audit" utils/auth.py | head -25
```
Expected: payload 是 `jwt.decode(...)` 回傳的 dict，含所有 claim 包含 jti。**若 helper 做了 key filter**（例如只回 `user_id` + `name`），改為直接回完整 payload；否則跳到 Step 3。

如果該函式只回特定欄，修改其回傳為完整 payload dict（保留所有 claim）：

```python
def decode_token_for_audit(token: str) -> dict | None:
    """解 token，給 audit / 其他內部 helper 用。回傳完整 payload（含 jti、user_id 等）。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None
```

（若已是 full payload 回傳，跳過此修改。）

- [ ] **Step 2: 寫 failing test — extract jti from JWT**

加入 `tests/test_audit_forensic.py`：

```python
import secrets

from utils.audit import _extract_session_id_from_request
from utils.auth import create_access_token


def _build_request_with_token(token: str):
    """Helper: 造一個帶 Authorization header 的假 Request。"""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    return Request(scope)


def test_extract_session_id_returns_jti_from_token():
    explicit_jti = secrets.token_urlsafe(16)
    token = create_access_token({"user_id": 1, "name": "alice", "jti": explicit_jti})
    request = _build_request_with_token(token)

    session_id = _extract_session_id_from_request(request)

    assert session_id == explicit_jti


def test_extract_session_id_returns_none_when_no_header():
    from starlette.requests import Request

    request = Request({"type": "http", "headers": []})
    assert _extract_session_id_from_request(request) is None


def test_extract_session_id_returns_none_on_bad_token():
    request = _build_request_with_token("not-a-jwt-blah")
    assert _extract_session_id_from_request(request) is None
```

- [ ] **Step 3: Run failing tests**

Run:
```bash
pytest tests/test_audit_forensic.py -k "extract_session_id" -xvs
```
Expected: FAIL — `cannot import name '_extract_session_id_from_request'`

- [ ] **Step 4: 實作 _extract_session_id_from_request + _compute_ua_hash**

在 `utils/audit.py`，於 `_extract_user_from_header` 同區域加：

```python
import hashlib


def _extract_session_id_from_request(request: Request) -> str | None:
    """從 Authorization Bearer JWT 取 jti claim（forensic session 識別）。

    失敗（無 header、bad token、無 jti）一律回 None — audit 寫入應該繼續，
    session_id 為 NULL 是預期過渡狀態（既有 token 過期後自然填補）。
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    from utils.auth import decode_token_for_audit

    payload = decode_token_for_audit(token) or {}
    jti = payload.get("jti")
    if jti and isinstance(jti, str):
        return jti
    return None


def _compute_ua_hash(request: Request) -> str | None:
    """SHA256(User-Agent)[:32]。raw UA 不存（含 device PII），hash 化保留 forensic diff。"""
    ua = request.headers.get("user-agent", "")
    if not ua:
        return None
    return hashlib.sha256(ua.encode("utf-8", errors="ignore")).hexdigest()[:32]
```

- [ ] **Step 5: Run tests pass**

Run:
```bash
pytest tests/test_audit_forensic.py -k "extract_session_id or compute_ua" -xvs
```
Expected: PASS（3 test）

- [ ] **Step 6: 寫 failing test — write_explicit_audit 注入兩欄**

加入 `tests/test_audit_forensic.py`：

```python
from models.audit import AuditLog
from models.base import get_session
from utils.audit import write_explicit_audit


def test_write_explicit_audit_persists_ua_hash_and_session_id(tmp_session_factory):
    """呼叫 write_explicit_audit 後 audit_logs row 含 ua_hash + session_id。"""
    explicit_jti = secrets.token_urlsafe(16)
    token = create_access_token({"user_id": 1, "name": "alice", "jti": explicit_jti})

    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"user-agent", b"TestUA/1.0 (purpose=forensic-test)"),
        ],
    }
    request = Request(scope)

    write_explicit_audit(
        request,
        action="READ",
        entity_type="student",
        summary="forensic test write",
        entity_id="42",
        dedup=False,
    )

    # write_explicit_audit 走 fire-and-forget → 同 thread 同步 fallback（無 event loop）
    session = get_session()
    try:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.entity_type == "student", AuditLog.entity_id == "42")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.session_id == explicit_jti
        expected_ua_hash = hashlib.sha256(
            b"TestUA/1.0 (purpose=forensic-test)"
        ).hexdigest()[:32]
        assert row.user_agent_hash == expected_ua_hash
    finally:
        session.close()
```

**注意**：`tmp_session_factory` fixture 不是預設，這裡用 `get_session()` 直連 test DB。確保 conftest.py 已配置 test DB。

- [ ] **Step 7: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_write_explicit_audit_persists_ua_hash_and_session_id -xvs
```
Expected: FAIL — `row.session_id == None`（既有 helper 未填）

- [ ] **Step 8: 修 write_explicit_audit 與 _write_audit_sync 注入兩欄**

`utils/audit.py` 找到 `write_explicit_audit` 函式內組 payload 處（約 line 533+ 區段），加入兩欄：

```python
    payload = {
        "user_id": user_id,
        "username": username,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "summary": summary,
        "changes": changes_json,
        "ip_address": ip,
        "user_agent_hash": _compute_ua_hash(request),    # NEW
        "session_id": _extract_session_id_from_request(request),  # NEW
    }
    _schedule_audit_write(payload)
```

同時找 `write_audit_in_session`（line 475+）與 `AuditMiddleware.dispatch` 內組 payload 處（line ~700+），同樣加入兩欄。**逐處 grep 確認 payload dict 全都加到了**：

```bash
grep -n '"action":' utils/audit.py
```

- [ ] **Step 9: Run test pass**

Run:
```bash
pytest tests/test_audit_forensic.py::test_write_explicit_audit_persists_ua_hash_and_session_id -xvs
```
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add utils/audit.py utils/auth.py tests/test_audit_forensic.py
git commit -m "feat(audit): payload 注入 user_agent_hash + session_id (jti)

新增 _extract_session_id_from_request、_compute_ua_hash 兩 helper。
write_explicit_audit / write_audit_in_session / AuditMiddleware 三條
寫入路徑統一注入。session_id 為 NULL 在既有 token 過期前是預期過渡。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1"
```

---

## Task 3: parent medication detail GET 補 audit

**Files:**
- Modify: `api/parent_portal/medications.py`（line 246 `get_medication_order`）
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認端點現狀**

Run:
```bash
sed -n '246,260p' api/parent_portal/medications.py
```
Expected: 看到 `@router.get("/{order_id}")` 與 `def get_medication_order(...)` 簽章，**未** 含 `write_explicit_audit` 呼叫。

- [ ] **Step 2: 寫 failing test**

加入 `tests/test_audit_forensic.py`（如有既有 parent_portal fixture 則沿用；否則用 sub-app TestClient）：

```python
def test_parent_get_medication_detail_emits_audit(parent_client, parent_token, sample_medication_order):
    """家長 GET /medication-orders/{order_id} 應寫 audit_logs row。"""
    headers = {"Authorization": f"Bearer {parent_token}", "User-Agent": "PtestUA/1"}
    resp = parent_client.get(
        f"/parent/medication-orders/{sample_medication_order.id}",
        headers=headers,
    )
    assert resp.status_code == 200

    session = get_session()
    try:
        row = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "medication_order",
                AuditLog.entity_id == str(sample_medication_order.id),
                AuditLog.action == "READ",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.session_id is not None  # jti propagated
        assert row.user_agent_hash is not None
    finally:
        session.close()
```

**Fixture 提示**：`parent_client`、`parent_token`、`sample_medication_order` 通常在 `tests/conftest.py` 或 `tests/parent_portal/conftest.py` 已存在。若無，**先 grep 看現有 parent_portal 測試怎麼造 client**：

```bash
grep -rn "parent_client\|parent_token" tests/ | head -10
```

若 codebase 未提供，採 dependency-override + TestClient 方式（參考 `tests/test_parent_portal_auth.py` 或類似）。

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_get_medication_detail_emits_audit -xvs
```
Expected: FAIL — `row is None`（endpoint 未寫 audit）

- [ ] **Step 4: 在 endpoint 加 write_explicit_audit**

Modify `api/parent_portal/medications.py`，在 `get_medication_order`（line 246+）回傳 dict 之前加：

```python
from utils.audit import write_explicit_audit


@router.get("/{order_id}")
def get_medication_order(
    order_id: int,
    request: Request,                                 # NEW（如果尚未注入）
    parent=Depends(get_current_parent),
    session: Session = Depends(get_session_dep),
):
    order = _get_order_for_parent(session, parent, order_id)
    # ... 既有取資料邏輯 ...

    write_explicit_audit(
        request,
        action="READ",
        entity_type="medication_order",
        summary=f"家長 {parent.id} 查看 學生 {order.student_id} 用藥單 #{order.id}",
        entity_id=str(order.id),
        dedup=True,  # 同家長同 order 60s 內 dedup（避免 polling 灌爆）
    )

    return _order_to_dict(order, ...)
```

**重要**：`Request` import 與 dep 注入若已存在則不重複；用 `parent.id`（user_id）與 `order.student_id` 不放姓名（PII 控管，與 CLAUDE.md #9 對齊）。

- [ ] **Step 5: Run test pass**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_get_medication_detail_emits_audit -xvs
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api/parent_portal/medications.py tests/test_audit_forensic.py
git commit -m "feat(audit): parent GET /medication-orders/{id} 補 audit (forensic)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1.4"
```

---

## Task 4: parent contact-book detail GET 補 audit

**Files:**
- Modify: `api/parent_portal/contact_book.py`（line 294 `get_detail`）
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認端點現狀**

Run:
```bash
sed -n '294,335p' api/parent_portal/contact_book.py
```
Expected: 看 `@router.get("/{entry_id}")` 與 `def get_detail(...)`。**注意該檔已 import** `write_explicit_audit`（line 33）— 但只用於 POST，GET detail 沒呼叫。

- [ ] **Step 2: 寫 failing test**

加入 `tests/test_audit_forensic.py`：

```python
def test_parent_get_contact_book_detail_emits_audit(parent_client, parent_token, sample_contact_book_entry):
    headers = {"Authorization": f"Bearer {parent_token}", "User-Agent": "PtestUA/1"}
    resp = parent_client.get(
        f"/parent/contact-book/{sample_contact_book_entry.id}",
        headers=headers,
    )
    assert resp.status_code == 200

    session = get_session()
    try:
        row = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "contact_book_entry",
                AuditLog.entity_id == str(sample_contact_book_entry.id),
                AuditLog.action == "READ",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.session_id is not None
    finally:
        session.close()
```

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_get_contact_book_detail_emits_audit -xvs
```
Expected: FAIL — `row is None`

- [ ] **Step 4: 加 write_explicit_audit 呼叫**

Modify `api/parent_portal/contact_book.py:get_detail`：

```python
@router.get("/{entry_id}")
def get_detail(
    entry_id: int,
    request: Request,                                 # 既有 / 新增
    parent=Depends(get_current_parent),
    session: Session = Depends(get_session_dep),
):
    entry = _get_entry_for_parent(session, parent, entry_id)
    # ... 既有邏輯 ...

    write_explicit_audit(
        request,
        action="READ",
        entity_type="contact_book_entry",
        summary=f"家長 {parent.id} 查看 學生 {entry.student_id} 聯絡簿 #{entry.id}",
        entity_id=str(entry.id),
        dedup=True,
    )

    return _entry_to_dict(entry, ...)
```

- [ ] **Step 5: Run test pass**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_get_contact_book_detail_emits_audit -xvs
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api/parent_portal/contact_book.py tests/test_audit_forensic.py
git commit -m "feat(audit): parent GET /contact-book/{id} 補 audit (forensic)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1.4"
```

---

## Task 5: parent growth-report download 補 audit（下載類，dedup=False）

**Files:**
- Modify: `api/parent_portal/growth_reports.py`（line 85 `/download`）
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認端點現狀**

Run:
```bash
sed -n '85,110p' api/parent_portal/growth_reports.py
```

- [ ] **Step 2: 寫 failing test**

加入 `tests/test_audit_forensic.py`：

```python
def test_parent_growth_report_download_emits_audit_no_dedup(parent_client, parent_token, sample_growth_report):
    """連續兩次 download 應寫兩筆 audit（dedup=False，下載軌跡完整）。"""
    headers = {"Authorization": f"Bearer {parent_token}", "User-Agent": "PtestUA/1"}
    url = f"/parent/growth-reports/{sample_growth_report.id}/download"

    for _ in range(2):
        resp = parent_client.get(url, headers=headers)
        assert resp.status_code == 200

    session = get_session()
    try:
        count = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "growth_report",
                AuditLog.entity_id == str(sample_growth_report.id),
                AuditLog.action == "READ",
            )
            .count()
        )
        assert count == 2
    finally:
        session.close()
```

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_growth_report_download_emits_audit_no_dedup -xvs
```
Expected: FAIL

- [ ] **Step 4: 加 write_explicit_audit 呼叫（dedup=False）**

Modify `api/parent_portal/growth_reports.py`：

```python
from utils.audit import write_explicit_audit


@router.get("/{report_id}/download")
def download_growth_report(
    report_id: int,
    request: Request,
    parent=Depends(get_current_parent),
    session: Session = Depends(get_session_dep),
):
    report = _get_report_for_parent(session, parent, report_id)
    # ... 既有取檔 / streaming 邏輯 ...

    write_explicit_audit(
        request,
        action="READ",
        entity_type="growth_report",
        summary=f"家長 {parent.id} 下載 學生 {report.student_id} 成長報告 #{report.id} PDF",
        entity_id=str(report.id),
        dedup=False,  # 下載類：每次下載保留軌跡
    )

    return StreamingResponse(...)
```

- [ ] **Step 5: Run test pass**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_growth_report_download_emits_audit_no_dedup -xvs
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api/parent_portal/growth_reports.py tests/test_audit_forensic.py
git commit -m "feat(audit): parent growth-report download 補 audit (dedup=False)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1.4"
```

---

## Task 6: parent portfolio download 補 audit

**Files:**
- Modify: `api/parent_portal/parent_downloads.py`（line 150 `/portfolio/{key:path}`）
- Test: `tests/test_audit_forensic.py`

- [ ] **Step 1: 確認端點現狀**

Run:
```bash
sed -n '150,180p' api/parent_portal/parent_downloads.py
```

- [ ] **Step 2: 寫 failing test**

加入 `tests/test_audit_forensic.py`：

```python
def test_parent_portfolio_download_emits_audit(parent_client, parent_token, sample_portfolio_key):
    headers = {"Authorization": f"Bearer {parent_token}", "User-Agent": "PtestUA/1"}
    resp = parent_client.get(
        f"/parent/uploads/portfolio/{sample_portfolio_key}",
        headers=headers,
    )
    assert resp.status_code == 200

    session = get_session()
    try:
        row = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "portfolio_download",
                AuditLog.action == "READ",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert sample_portfolio_key in (row.entity_id or "")
        assert row.session_id is not None
    finally:
        session.close()
```

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_portfolio_download_emits_audit -xvs
```
Expected: FAIL

- [ ] **Step 4: 加 write_explicit_audit 呼叫**

Modify `api/parent_portal/parent_downloads.py:download_parent_portfolio`：

```python
from utils.audit import write_explicit_audit


@router.get("/portfolio/{key:path}")
def download_parent_portfolio(
    key: str,
    request: Request,
    parent=Depends(get_current_parent),
    session: Session = Depends(get_session_dep),
):
    # ... 既有 ACL 與檔案取邏輯 ...

    write_explicit_audit(
        request,
        action="READ",
        entity_type="portfolio_download",
        summary=f"家長 {parent.id} 下載 學習歷程檔 {key}",
        entity_id=key[:50],  # 截斷避免 String(50) 滿位
        dedup=False,  # 下載類
    )

    return StreamingResponse(...)
```

- [ ] **Step 5: Run test pass**

Run:
```bash
pytest tests/test_audit_forensic.py::test_parent_portfolio_download_emits_audit -xvs
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api/parent_portal/parent_downloads.py tests/test_audit_forensic.py
git commit -m "feat(audit): parent portfolio download 補 audit

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1.4"
```

---

## Task 7: 全套 pytest 確認零 regression + 量級觀察

**Files:**（無變動，純驗證）

- [ ] **Step 1: 跑 audit 相關 narrow 測試**

Run:
```bash
pytest tests/test_audit_forensic.py -xvs
pytest tests/ -k "audit" -x --tb=short
```
Expected: 全 PASS。新增 `test_audit_forensic.py` 共 9 個 test 全綠。

- [ ] **Step 2: 跑全套 pytest 確認零 regression**

Run:
```bash
pytest tests/ --tb=short 2>&1 | tail -40
```
Expected:
- 既有測試數量不變或新增 9（test_audit_forensic.py）
- 零 NEW failure（既有 fail 不受影響可以接受）
- 任何因「audit_log 表多欄」造成的測試失敗都需修

若有失敗：grep 失敗 trace 確認是否與 audit_logs 兩新欄相關（可能是 fixture 寫死 INSERT 沒列新欄，或 ORM 反射檢查失敗）。

- [ ] **Step 3: 量級觀察（local dev DB）**

Run:
```bash
psql ivymanagement -c "SELECT COUNT(*) FROM audit_logs WHERE created_at > NOW() - INTERVAL '1 day';"
```

確認 baseline，本 PR 上線後 1 週再跑一次比較，符合 spec 量級評估（~3,840 row/週）。

- [ ] **Step 4: Final review commit（如有任何 fix）**

如 step 2 有調 fixture / model_dump 等，commit：

```bash
git add -A
git commit -m "fix(audit): 補齊新欄位對既有 fixture / 反射檢查的影響

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch1"
```

---

## Self-Review Checklist

- [x] **Spec coverage**：Ch1 全 5 section (1.1 schema / 1.2 jti / 1.3 middleware / 1.4 4 endpoint / 1.5 量級 / 1.6 測試) 都對應到 task
  - 1.1 → Task 1
  - 1.2 → Task 2（jti 已存在，只取值）
  - 1.3 → Task 2
  - 1.4 → Task 3-6（4 endpoint × 4 task）
  - 1.5 量級 → Task 7 step 3
  - 1.6 測試 → 散在各 task
- [x] **Placeholder scan**：無 TBD / TODO；migration `down_revision` 為「替換成 step 1 抓到的實際 head」是設計上必要動作（不是 placeholder）
- [x] **Type consistency**：`_extract_session_id_from_request` / `_compute_ua_hash` 函式簽章兩處（Task 2 step 4 定義、各 caller 用法）一致
- [x] **PII 控管**：summary 全用 `parent.id` 與 `student_id`，不放姓名（CLAUDE.md #9）

## 風險與緩解（plan 層）

| 風險 | 緩解 |
|---|---|
| Alembic head 在 step 1 與 step 6 之間漂移（多人並行 push） | Step 6 重 grep head；若已變需重做 down_revision |
| `decode_token_for_audit` 既有實作不回 jti | Task 2 Step 1 明確 grep + 視情況改 |
| 4 個 parent endpoint fixture 不存在 | Task 3 Step 2 已標註「先 grep 既有 parent test 怎麼造 client」 |
| Audit 寫入 fire-and-forget 導致測試 race | `_schedule_audit_write` 在無 event loop 時 fallback 同步寫；TestClient 走 sync stack 應 OK，若不穩可在 test 內加 `await asyncio.sleep(0.05)` |
