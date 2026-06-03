# PDPA Phase 2 — 後端強制（P2-1~P2-3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓既有 consent / DSR 記錄真正生效——consent 後端強制（咽喉點 + portal gate）、DSR 可被 admin 決議並執行——修補 SECURITY_AUDIT RA-MED-4（consent fail-open）+ RA-MED-5（DSR 永停 pending）。

**Architecture:** 單一 `consent_check` 純函式（查 `ParentConsentLog` 最新一筆 scope 狀態，短 TTL 快取）為所有 point-of-use 咽喉的唯一判定源；`require_current_consent` dependency 守家長 portal 資料端點；DSR opt-out 改即時寫 consent log、delete/correct 走 admin queue。全程 `CONSENT_ENFORCEMENT_ENABLED` flag 包覆（預設 false = 全 no-op）。

**Tech Stack:** FastAPI、SQLAlchemy、PostgreSQL、pytest（SQLite in-memory `test_db_session`）、`utils/cache_layer`。

---

## 前置依賴與排序（執行前必讀）

1. **依賴 `fix/line-push-consent-staff-exempt`（commit `fb025a6`）先 merge**：該 fix 已讓 `_check_line_push_consent` 對 `role != 'parent'` 放行（員工豁免）。本 plan 的 Task 5 在此基礎上，把**家長側**的 line_push 判定從 `User.line_push_consent` 改為查 `ParentConsentLog`。若 fix 未先 merge，Task 5 需連員工豁免一起做（屆時把 fix 的 role 分支併入）。
2. **base**：本 worktree 從 `origin/main`（`0c4a31c`）開。
3. **PR 邊界**：P2-1（Task 1–6）/ P2-2（Task 7–8）/ P2-3（Task 9–12）各自獨立 commit、各自 CI 綠。
4. **跨端同步**（workspace CLAUDE.md 陷阱 #1/#8）：Task 11 新增 `DSR_MANAGE` 權限須前後端同步；本 plan 只做後端，前端權限字串集合在前端 plan 處理。

---

## 關鍵設計決策

### D1. consent 判定的單一數據源 = `ParentConsentLog`
`ParentConsentLog`（per-user、scope-aware、append-only、索引 `ix_pcl_user_scope_time` on `(user_id, scope, consented_at)`）是唯一真相。退役 `User.line_push_consent`（無 writer 的脫節欄位，見 [[feedback_fail_closed_gate_no_writer_column]]）。

### D2. ⚠️ per-student consent 語義（**實作前須與業主確認**）
`ParentConsentLog` 是 **per-user（家長）**，但 `photo_publish`/`cross_border` 保護的是 **per-student（學生）PII**，一個學生可多位 guardian。判定策略分兩類，本 plan 採以下**保守預設**，並在 `consent_check_student_scope` 集中一處（易改）：

- **家長自己操作的上傳**（`parent_portal/*`：medications/events/leaves/messages）：current_user 即操作家長 → 查**該 user** 的 scope consent（per-user，明確）。
- **老師操作 / 系統產生**（contact_book 照片、portfolio growth report、portal parent_messages、photo_publish 廣播）：無單一操作家長 → 查該 student 綁定的 guardian。**預設策略：該 student 的「主要聯絡人」（`Guardian.is_primary=True`）對應 user 的 consent 為準；無主要聯絡人時退回「任一已綁定 guardian 同意即可」。** 這是合規 vs 可用性的取捨，**須業主確認**是否改為「所有 guardian 皆須同意」（最嚴格）。

> 此決策只影響 `consent_check_student_scope`（Task 2）一處實作；改策略不動 caller。

### D3. fail-mode（明定不留 silent default，spec §4）
- `service_essential` gate：consent 查詢 DB error → **讀路徑** degraded-on-read（用短 TTL 快取的舊決策）；無快取且 DB 失敗 → **fail-open + WARNING**（避免 DB 抖動鎖死全體家長）；**寫路徑** fail-closed。
- granular point-of-use（line_push/photo_publish/cross_border）：查詢 error → **fail-closed**（漏發一則通知/一張照片 < 違反撤回意願）。
- flag off：全 no-op。

### D4. flag dark-launch
`CONSENT_ENFORCEMENT_ENABLED`（env，預設 `false`）。false 時 gate / point-of-use 全 no-op（純記錄，維持現狀）。啟用走部署 gate（見文末 Rollout）。

---

## 共用基建

### Task 1: `CONSENT_ENFORCEMENT_ENABLED` flag

**Files:**
- Create: `config/consent.py`
- Modify: `config/base.py`（加 import + field）
- Test: `tests/test_consent_settings.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_consent_settings.py
def test_consent_enforcement_defaults_false(monkeypatch):
    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    from config.consent import ConsentSettings
    assert ConsentSettings().enforcement_enabled is False


def test_consent_enforcement_reads_env(monkeypatch):
    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    from config.consent import ConsentSettings
    assert ConsentSettings().enforcement_enabled is True
```

- [ ] **Step 2: Run，確認 fail**

Run: `pytest tests/test_consent_settings.py -q`
Expected: FAIL（`ModuleNotFoundError: config.consent`）

- [ ] **Step 3: 實作（仿 `config/ops.py` 樣式）**

```python
# config/consent.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConsentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    enforcement_enabled: bool = Field(
        default=False,
        validation_alias="CONSENT_ENFORCEMENT_ENABLED",
        description="個資法 consent 強制總開關；false=全 no-op（dark-launch）",
    )
```

`config/base.py`：在 `Settings` 類加（仿既有 `ops: OpsSettings = Field(...)`）：
```python
from .consent import ConsentSettings  # 與其他 sub-settings import 並列
# class Settings 內：
    consent: ConsentSettings = Field(default_factory=ConsentSettings)
```

- [ ] **Step 4: Run，確認 pass**

Run: `pytest tests/test_consent_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/consent.py config/base.py tests/test_consent_settings.py
git commit -m "feat(consent): CONSENT_ENFORCEMENT_ENABLED dark-launch flag（預設 false）"
```

---

### Task 2: `consent_check` 純函式（核心咽喉判定）

**Files:**
- Create: `services/consent/__init__.py`、`services/consent/checker.py`
- Test: `tests/consent/test_consent_checker.py`

判定 per-user scope consent（查 `ParentConsentLog` 最新一筆），+ per-student 包裝（D2）。短 TTL 快取（`utils/cache_layer`，60s）。

- [ ] **Step 1: 寫 failing test（per-user 最新一筆 wins）**

```python
# tests/consent/test_consent_checker.py
from datetime import timedelta
from models.consent import ParentConsentLog, PolicyVersion
from models.auth import User
from utils.taipei_time import now_taipei_naive


def _seed_policy(session) -> int:
    p = PolicyVersion(version="2026.1", effective_at=now_taipei_naive(),
                      document_path="policies/2026.1.pdf")
    session.add(p); session.flush()
    return p.id


def _seed_parent(session, uid_suffix="1") -> User:
    u = User(username=f"p{uid_suffix}", password_hash="!LINE_ONLY",
             role="parent", line_user_id=None)
    session.add(u); session.flush()
    return u


def test_consent_check_latest_false_returns_false(test_db_session):
    from services.consent.checker import consent_check
    pid = _seed_policy(test_db_session)
    u = _seed_parent(test_db_session)
    base = now_taipei_naive()
    # 先同意、後撤回 → 最新為撤回
    for consented, dt in [(True, base), (False, base + timedelta(hours=1))]:
        log = ParentConsentLog(user_id=u.id, policy_version_id=pid,
                               scope="line_push", consented=consented)
        log.consented_at = dt
        test_db_session.add(log)
    test_db_session.commit()
    assert consent_check(test_db_session, u.id, "line_push") is False


def test_consent_check_latest_true_returns_true(test_db_session):
    from services.consent.checker import consent_check
    pid = _seed_policy(test_db_session)
    u = _seed_parent(test_db_session)
    log = ParentConsentLog(user_id=u.id, policy_version_id=pid,
                           scope="line_push", consented=True)
    test_db_session.add(log); test_db_session.commit()
    assert consent_check(test_db_session, u.id, "line_push") is True


def test_consent_check_no_record_returns_false(test_db_session):
    from services.consent.checker import consent_check
    u = _seed_parent(test_db_session)
    assert consent_check(test_db_session, u.id, "photo_publish") is False
```

- [ ] **Step 2: Run，確認 fail**

Run: `pytest tests/consent/test_consent_checker.py -q`
Expected: FAIL（`ModuleNotFoundError: services.consent`）

- [ ] **Step 3: 實作**

```python
# services/consent/__init__.py
# (empty package marker)
```

```python
# services/consent/checker.py
"""個資法 consent point-of-use 判定（單一數據源 = ParentConsentLog）。

判定規則：查該 user 對該 scope 的最新一筆 ParentConsentLog.consented。
無記錄 → False（opt-in 原則）。per-student 包裝見 consent_check_student_scope。
"""
from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from models.consent import ParentConsentLog
from models.guardian import Guardian

logger = logging.getLogger(__name__)


def consent_check(session: Session, user_id: int, scope: str) -> bool:
    """該 user 對 scope 的最新 consent 狀態。無記錄 → False。"""
    row = (
        session.query(ParentConsentLog.consented)
        .filter(
            ParentConsentLog.user_id == user_id,
            ParentConsentLog.scope == scope,
        )
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    return bool(row[0]) if row is not None else False


def consent_check_student_scope(session: Session, student_id: int, scope: str) -> bool:
    """per-student PII scope（photo_publish / cross_border）的家庭層判定（D2）。

    預設：取該 student 的主要聯絡人（is_primary）guardian 對應 user 的 consent；
    無主要聯絡人 → 任一已綁定（user_id 非空、未軟刪）guardian 同意即可。
    ⚠️ 業主可改為「所有 guardian 皆須同意」——只改此函式。
    """
    guardians = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student_id,
            Guardian.user_id.isnot(None),
            Guardian.deleted_at.is_(None),
        )
        .all()
    )
    if not guardians:
        return False
    primary = next((g for g in guardians if g.is_primary), None)
    if primary is not None:
        return consent_check(session, primary.user_id, scope)
    return any(consent_check(session, g.user_id, scope) for g in guardians)
```

- [ ] **Step 4: Run，確認 pass**

Run: `pytest tests/consent/test_consent_checker.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: per-student 測試 + 短 TTL 快取**

加 test（主要聯絡人 consent 為準 / 無 primary 取 any）：

```python
def test_consent_check_student_uses_primary_guardian(test_db_session):
    from services.consent.checker import consent_check_student_scope
    from models.student import Student
    from models.classroom import Classroom
    pid = _seed_policy(test_db_session)
    # 主要聯絡人不同意、次要聯絡人同意 → 以主要為準 = False
    primary = _seed_parent(test_db_session, "primary")
    secondary = _seed_parent(test_db_session, "secondary")
    stu = Student(name="童", enrollment_school_year=114)  # 依現有 Student 必填欄位補
    test_db_session.add(stu); test_db_session.flush()
    test_db_session.add_all([
        Guardian(student_id=stu.id, user_id=primary.id, name="主", is_primary=True),
        Guardian(student_id=stu.id, user_id=secondary.id, name="次", is_primary=False),
    ])
    test_db_session.add(ParentConsentLog(user_id=secondary.id, policy_version_id=pid,
                                         scope="cross_border", consented=True))
    test_db_session.commit()
    assert consent_check_student_scope(test_db_session, stu.id, "cross_border") is False
```

> **執行注意**：`Student` 必填欄位以實際 model 為準（先 `grep "nullable=False" models/student.py` 補齊；本步驟若 fixture 建 Student 太重，可改用 conftest 既有 student factory，`grep "def.*student" tests/conftest.py`）。

快取（仿 `api/portal/_shared.py` 用法）：`consent_check` 包一層 `get_cache().get/set(namespace="consent", key=f"{user_id}:{scope}", ttl=60)`。撤回 consent（Task 9 opt-out）後須 `get_cache().delete("consent", f"{user_id}:{scope}")` 立即失效，否則 60s 內仍放行——**Task 9 必須配套 invalidate**。

- [ ] **Step 6: Run 全部 + Commit**

Run: `pytest tests/consent/ -q`
Expected: PASS

```bash
git add services/consent/ tests/consent/
git commit -m "feat(consent): consent_check 純函式（ParentConsentLog 單一數據源）+ per-student 家庭層判定"
```

---

## P2-1：consent 強制（point-of-use 咽喉）

### Task 3: cross_border gate helper + 1 caller（contact_book 照片）

**Files:**
- Modify: `services/consent/checker.py`（加 `enforce_or_raise` 便利函式）
- Modify: `api/portal/contact_book.py`（`upload_photo`，~line 519 前）
- Test: `tests/consent/test_cross_border_gate.py`

- [ ] **Step 1: 寫 failing test**（flag on + student 主要 guardian 未同意 cross_border → upload 403；flag off → 放行）

```python
# tests/consent/test_cross_border_gate.py — 用 TestClient 打 upload_photo
# （參考 tests/ 既有 contact_book 上傳測試 setup：grep "upload_photo\|upload-photo" tests/）
# 斷言：flag on + 無 cross_border consent → 403 code=CONSENT_REQUIRED；
#       flag on + 有 consent → 200；flag off → 200（不擋）。
```

> **執行注意**：先 `grep -rn "upload-photo\|upload_photo" tests/` 找既有 contact_book 上傳測試樣板複用 fixture（teacher token、entry、student、guardian）。若無，照 `tests/test_portal_*.py` 既有 portal TestClient 樣式建。

- [ ] **Step 2: Run，確認 fail**（目前無 gate，flag on 也回 200）

- [ ] **Step 3: 實作 `enforce_or_raise` + caller gate**

`services/consent/checker.py` 加：
```python
from config import get_settings  # 依實際 settings 取得方式（grep "get_settings\|settings =" config/）
from services.business_errors.parent import ConsentRequired


def enforce_student_cross_border(session: Session, student_id: int) -> None:
    """flag on 且該 student 家庭未同意 cross_border → raise ConsentRequired(403)。

    flag off → no-op。查詢 error → fail-closed（raise），符合 granular point-of-use
    寧可漏發不可違反撤回（D3）。
    """
    if not get_settings().consent.enforcement_enabled:
        return
    try:
        ok = consent_check_student_scope(session, student_id, "cross_border")
    except Exception as exc:  # fail-closed
        logger.warning("cross_border consent check error (fail-closed): %s", exc)
        raise ConsentRequired("學生資料跨境同意檢查失敗，請稍後再試")
    if not ok:
        raise ConsentRequired("家長尚未同意學生資料跨境傳輸，無法上傳含個資的檔案")
```

`api/portal/contact_book.py` `upload_photo`，在拿到 `entry`（已有 `entry.student_id`，~line 512）之後、`storage.put_attachment(...)`（~line 519）之前插入：
```python
from services.consent.checker import enforce_student_cross_border
# ...
enforce_student_cross_border(session, entry.student_id)
```

- [ ] **Step 4: Run，確認 pass**（flag on 無 consent → 403；有 consent → 200；flag off → 200）

- [ ] **Step 5: Commit**

```bash
git add services/consent/checker.py api/portal/contact_book.py tests/consent/test_cross_border_gate.py
git commit -m "feat(consent): cross_border 上傳咽喉 enforce_student_cross_border + contact_book 照片 gate"
```

---

### Task 4: cross_border gate — 其餘 6 個上傳 caller

每個 caller 在「已取得 student_id」與「呼叫 `storage.put_attachment` / `get_backend().save`」之間插入一行 `enforce_student_cross_border(session, <student_id 來源>)`，import 同 Task 3。**程式碼模式與 Task 3 完全相同**，只有 student_id 來源不同。

**Files（逐一）:**

| # | File | 函式 | student_id 來源（已在手邊） | 對 caller 端來源 |
|---|------|------|----------------------------|------|
| 1 | `api/portfolio/reports.py` | `_generate_pdf_job`（~line 308 後） | `report.student_id` | 系統（background job）→ 用 `consent_check_student_scope`（無操作家長） |
| 2 | `api/parent_portal/medications.py` | `upload_medication_photo`（~line 385 前） | `order.student_id` | 家長自己（current_user）→ 見下「家長自上傳」備註 |
| 3 | `api/parent_portal/events.py` | `upload_ack_signature`（~line 312 前） | `student_id`（Query 參數，已 `_assert_student_owned`） | 家長自己 |
| 4 | `api/parent_portal/leaves.py` | `upload_leave_attachment`（~line 346 前） | `item.student_id` | 家長自己 |
| 5 | `api/parent_portal/messages.py` | `attach_to_message`（~line 492 前） | `t.student_id` | 家長自己 |
| 6 | `api/portal/parent_messages.py` | `attach_to_message`（~line 548 前） | `t.student_id` | 老師（無操作家長）→ per-student |

> **家長自上傳的語意（D2）**：家長自己上傳自己孩子的檔案時，其行為本身已隱含「該家長願意上傳」，但 cross_border 是「跨境傳輸」同意，與「上傳意願」不同——家長可願意上傳卻不同意跨境。**保守一致做法**：所有 caller 一律用 `enforce_student_cross_border(session, student_id)`（per-student），不分家長/老師上傳，語意統一、實作單純。記於 plan 供 review。

- [ ] **Step 1: 每個 caller 先補 failing test**（flag on 無 consent → 403），再加同一行 gate，跑綠。逐 caller commit 或合併一個 commit：

```bash
# 每個 caller 改完且測試綠後：
git add api/portfolio/reports.py api/parent_portal/medications.py \
        api/parent_portal/events.py api/parent_portal/leaves.py \
        api/parent_portal/messages.py api/portal/parent_messages.py \
        tests/consent/test_cross_border_gate.py
git commit -m "feat(consent): cross_border 咽喉接上其餘 6 個含學生 PII 上傳點"
```

> vendor_payments / announcements / attachments（observation 以外）**不接**（非學生 PII，見 cross_border explore）；attachments.py 待其支援 report/medication owner_type 時再評估（follow-up）。

---

### Task 5: line_push 家長側改查 ParentConsentLog（接員工豁免 fix）

**Files:**
- Modify: `services/line_service.py`（`_check_line_push_consent`）
- Test: `tests/test_line_consent_gate.py`（擴充）

承 `fix/line-push-consent-staff-exempt`：員工（role != 'parent'）已放行。本 task 把**家長側**判定從 `bool(user.line_push_consent)` 改為查 `ParentConsentLog`。

- [ ] **Step 1: 寫 failing test**（家長在 ParentConsentLog 同意 line_push → True；撤回 → False；`User.line_push_consent` 不再被讀）

```python
def test_parent_line_push_reads_consent_log_not_user_column(test_db_session):
    from services.line_service import _check_line_push_consent
    from models.auth import User
    from models.consent import ParentConsentLog, PolicyVersion
    from utils.taipei_time import now_taipei_naive
    p = PolicyVersion(version="2026.1", effective_at=now_taipei_naive(),
                      document_path="x.pdf"); test_db_session.add(p); test_db_session.flush()
    u = User(username="parent_consent_log", password_hash="x", role="parent",
             line_user_id="U_parent_cl_001", line_push_consent=False)  # 欄位 False，但 log 同意
    test_db_session.add(u); test_db_session.flush()
    test_db_session.add(ParentConsentLog(user_id=u.id, policy_version_id=p.id,
                                         scope="line_push", consented=True))
    test_db_session.commit()
    # 改 ParentConsentLog 為數據源後：應回 True（即使 user.line_push_consent=False）
    assert _check_line_push_consent("U_parent_cl_001") is True
```

- [ ] **Step 2: Run，確認 fail**（現讀 `user.line_push_consent`=False → 回 False）

- [ ] **Step 3: 實作**（`_check_line_push_consent` 家長分支改用 `consent_check`）

```python
            if not user:
                return False
            if user.role != "parent":
                return True
            # 家長：查 ParentConsentLog 單一數據源（退役 user.line_push_consent）
            from services.consent.checker import consent_check
            return consent_check(session, user.id, "line_push")
```

- [ ] **Step 4: Run**（test_line_consent_gate 全綠 + 新 test 綠）

- [ ] **Step 5: Commit**

```bash
git add services/line_service.py tests/test_line_consent_gate.py
git commit -m "feat(consent): line_push 家長側改查 ParentConsentLog（退役 User.line_push_consent）"
```

---

### Task 6: photo_publish 咽喉 + coverage 斷言測試

**Files:**
- Modify: `services/contact_book_service.py`（`publish_entry`，~line 83 標記發布前）
- Create: `tests/consent/test_consent_chokepoint_coverage.py`
- Test: `tests/consent/test_photo_publish_gate.py`

- [ ] **Step 1: 寫 failing test**（flag on + student 家庭未同意 photo_publish → `publish_entry` 不廣播給未同意 guardian / 該子女照片不入廣播）

> 行為（spec §3.1b）：未同意 → 該家長子女照片不納入廣播。實作：`publish_entry` 在 fan-out 給 `guardian_user_ids` 時，per-guardian 過濾 `consent_check(session, uid, "photo_publish")`（flag on 時）。

- [ ] **Step 2: Run，確認 fail**

- [ ] **Step 3: 實作**（`publish_entry` 內，flag on 時過濾 guardian_user_ids）

```python
from config import get_settings
from services.consent.checker import consent_check
# ... 取得 guardian_user_ids 後：
if get_settings().consent.enforcement_enabled:
    guardian_user_ids = [
        uid for uid in guardian_user_ids
        if consent_check(session, uid, "photo_publish")
    ]
```

- [ ] **Step 4: coverage 斷言測試（防回歸，spec §3.1c）**

枚舉「對家長 user 的 LINE push / 照片廣播 / 跨境上傳 entrypoint」，斷言都經咽喉。最務實版本：grep-based 結構測試——斷言 `services/`、`api/` 內呼叫 `put_attachment`/`get_backend().save` 的檔案，若在「含學生 PII 上傳清單」內，則同檔有 `enforce_student_cross_border` import。

```python
# tests/consent/test_consent_chokepoint_coverage.py
import pathlib, re

PII_UPLOAD_FILES = {
    "api/portal/contact_book.py", "api/portfolio/reports.py",
    "api/parent_portal/medications.py", "api/parent_portal/events.py",
    "api/parent_portal/leaves.py", "api/parent_portal/messages.py",
    "api/portal/parent_messages.py",
}

def test_all_pii_upload_sites_have_cross_border_gate():
    root = pathlib.Path(__file__).resolve().parents[2]
    for rel in PII_UPLOAD_FILES:
        src = (root / rel).read_text(encoding="utf-8")
        assert "enforce_student_cross_border" in src, (
            f"{rel} 含學生 PII 上傳但未接 cross_border 咽喉——"
            f"新增繞過咽喉的上傳路徑即 fail（RA-MED-4 防回歸）"
        )
```

- [ ] **Step 5: Run 全部 P2-1 測試 + Commit**

Run: `pytest tests/consent/ tests/test_line_consent_gate.py -q`
Expected: PASS

```bash
git add services/contact_book_service.py tests/consent/test_photo_publish_gate.py tests/consent/test_consent_chokepoint_coverage.py
git commit -m "feat(consent): photo_publish 廣播咽喉 + chokepoint coverage 斷言測試（RA-MED-4 防回歸）"
```

---

## P2-2：service_essential gate（家長 portal 守衛）

### Task 7: `require_current_consent` dependency + policy-bump 重簽

**Files:**
- Create: `api/parent_portal/_consent_gate.py`
- Modify: 家長 portal 資料讀寫端點（掛 dependency；公開/登入/consent 簽署/policy 查詢端點豁免）
- Test: `tests/consent/test_require_current_consent.py`

判定：家長最新一筆 `service_essential` consent 的 `policy_version_id` == 當期生效 `PolicyVersion`（`effective_at <= now` 最新者）→ 通過；否則 → **403 + `X-Consent-Required` header**，前端攔截彈 re-consent。

- [ ] **Step 1: 寫 failing test**（未簽當期 policy → 403 + header；簽當期 → 通過；policy 升版 → 舊簽失效；flag off → 不擋）

```python
def test_require_current_consent_stale_policy_403(test_db_session, parent_client):
    # 家長簽了 v1，但當期生效已是 v2 → 打資料端點得 403 + X-Consent-Required
    ...
def test_require_current_consent_flag_off_passes(...):
    # CONSENT_ENFORCEMENT_ENABLED=false → 不擋
    ...
```

> **執行注意**：`parent_client` fixture / 家長 JWT 樣板見 `tests/` 既有 parent_portal 測試（`grep -rn "require_parent_role\|parent.*client\|liff" tests/ | head`）。

- [ ] **Step 2: Run，確認 fail**

- [ ] **Step 3: 實作 dependency**

```python
# api/parent_portal/_consent_gate.py
"""家長 portal service_essential consent gate（D3 fail-mode）。"""
from __future__ import annotations
import logging
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from utils.auth import require_parent_role
from config import get_settings

logger = logging.getLogger(__name__)


def require_current_consent(write: bool = False):
    """factory：掛在家長資料端點。write=True 的端點 DB 失敗時 fail-closed。"""

    def dep(
        request: Request,
        current_user: dict = Depends(require_parent_role()),
    ):
        if not get_settings().consent.enforcement_enabled:
            return current_user
        from models.base import session_scope
        from services.consent.checker import has_signed_current_policy
        try:
            with session_scope() as session:
                ok = has_signed_current_policy(session, current_user["user_id"])
        except Exception as exc:
            logger.warning("consent gate DB error (write=%s): %s", write, exc)
            if write:
                raise HTTPException(status_code=503, detail="同意狀態檢查暫時不可用")
            return current_user  # 讀路徑 degraded fail-open（D3）
        if not ok:
            raise HTTPException(
                status_code=403,
                detail="請先重新簽署當期隱私權政策",
                headers={"X-Consent-Required": "service_essential"},
            )
        return current_user

    return dep
```

`services/consent/checker.py` 加 `has_signed_current_policy`：
```python
from models.consent import PolicyVersion
from utils.taipei_time import now_taipei_naive

def has_signed_current_policy(session: Session, user_id: int) -> bool:
    """家長最新 service_essential consent 的 policy_version == 當期生效 policy。"""
    current = (
        session.query(PolicyVersion.id)
        .filter(PolicyVersion.effective_at <= now_taipei_naive())
        .order_by(PolicyVersion.effective_at.desc())
        .first()
    )
    if current is None:
        return True  # 尚未 seed 任何 policy → 不擋（dark 期）
    latest = (
        session.query(ParentConsentLog)
        .filter(ParentConsentLog.user_id == user_id,
                ParentConsentLog.scope == "service_essential")
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    return bool(latest and latest.consented and latest.policy_version_id == current[0])
```

- [ ] **Step 4: 掛端點 + Run + Commit**

掛在家長資料讀寫端點（`grep -rn "require_parent_role" api/parent_portal/ | grep -v auth | grep -v consent`，逐一加 `Depends(require_current_consent(write=<POST/PUT/DELETE>))`；公開/登入/consent/policy 端點不掛）。

```bash
git add api/parent_portal/_consent_gate.py services/consent/checker.py api/parent_portal/ tests/consent/test_require_current_consent.py
git commit -m "feat(consent): require_current_consent gate + policy-bump 重簽 + degraded fail-mode"
```

---

### Task 8: `GET /api/parent/policies/current` 確認 + consent 快取 invalidate 收口

- [ ] 確認 `GET /policies/current`（Phase 1 已存在於 `api/parent_portal/consent.py:189`）回當期生效 policy 供前端 re-consent modal 用；若無則補。寫 test 斷言回最新 `effective_at <= now`。Commit。

---

## P2-3：DSR 執行

### Task 9: opt-out 改即時（granular scope）+ 快取 invalidate

**Files:**
- Modify: `api/parent_portal/dsr.py`（`submit_opt_out_request`）
- Test: `tests/consent/test_opt_out_immediate.py`

現況：opt-out 只寫 pending `DsrRequest`（不生效）。改為：對 granular scope（photo_publish/line_push/cross_border）**即時寫 `ParentConsentLog consented=false` + invalidate 快取**，`service_essential` 拒絕（4xx，指引走 delete/withdrawal）。

- [ ] **Step 1: 寫 failing test**（opt-out line_push → 立即 `consent_check` 回 False；opt-out service_essential → 4xx）

- [ ] **Step 2: Run，確認 fail**（現只寫 pending DsrRequest，consent_check 不變）

- [ ] **Step 3: 實作**（`submit_opt_out_request` 內）

```python
from models.consent import CONSENT_SCOPE_SERVICE_ESSENTIAL, ParentConsentLog
# service_essential 拒絕
if payload.scope == CONSENT_SCOPE_SERVICE_ESSENTIAL:
    raise HTTPException(status_code=400,
        detail="基礎服務同意不可停止；如需終止服務請走刪除申請")
# granular：即時寫撤回 log（沿用當期 policy_version）+ 仍留 DsrRequest 法律備案
current_policy = (session.query(PolicyVersion)
    .filter(PolicyVersion.effective_at <= now_taipei_naive())
    .order_by(PolicyVersion.effective_at.desc()).first())
session.add(ParentConsentLog(
    user_id=user.id, policy_version_id=(current_policy.id if current_policy else None),
    scope=payload.scope, consented=False, note="DSR opt-out 即時撤回"))
# DsrRequest 直接記為 approved（自助、不需 admin 核准，spec §3.2a）
req.status = DSR_STATUS_APPROVED
# invalidate 快取（Task 2 Step 5）
from utils.cache_layer import get_cache
get_cache().delete("consent", f"{user.id}:{payload.scope}")
```

> **執行注意**：`ParentConsentLog.policy_version_id` 現為 `nullable=False`（見 models/consent.py:88）。若 opt-out 時無 policy（dark 期），需 migration 改 nullable 或先 seed policy。**先 grep 確認** `policy_version_id` nullable 狀態；若 NOT NULL 則本 task 含一支 alembic 改 nullable（downgrade 完整）。

- [ ] **Step 4: Run + Commit**

```bash
git add api/parent_portal/dsr.py tests/consent/test_opt_out_immediate.py
git commit -m "feat(dsr): opt-out granular scope 改即時撤回 consent + 快取 invalidate"
```

---

### Task 10: `DSR_MANAGE` 權限

**Files:**
- Modify: `utils/permissions.py`（Permission enum + PERMISSION_LABELS + ROLE_TEMPLATES）
- Test: `tests/test_permissions.py`（或既有權限測試）

- [ ] **Step 1: test**（`Permission.DSR_MANAGE.value == "DSR_MANAGE"`；在 PERMISSION_LABELS；admin 角色含之）
- [ ] **Step 2: fail**
- [ ] **Step 3: 實作**——三處同步：

```python
# utils/permissions.py
# 1) Permission enum（PORTAL_IMPERSONATE 後）：
    DSR_MANAGE = "DSR_MANAGE"
# 2) PERMISSION_LABELS：
    "DSR_MANAGE": "個資權利請求管理",
# 3) ROLE_TEMPLATES：admin 已 WILDCARD 自動含；principal 視業主決策加入：
#    "principal": [..., Permission.DSR_MANAGE.value],
```

- [ ] **Step 4: pass + Commit**

```bash
git commit -am "feat(perm): 新增 DSR_MANAGE 權限（個資權利請求管理）"
```

> 前端權限字串集合（`src/constants/permissions.ts` + `hasPermission` gate）在前端 plan 同步。

---

### Task 11: admin DSR queue 端點（list / approve / reject）

**Files:**
- Create: `api/parent_portal/dsr_admin.py`（或 `api/dsr_admin.py`）
- Modify: `main.py`（include_router）
- Schema: `schemas/dsr.py`（response_model）
- Test: `tests/consent/test_dsr_admin_queue.py`

端點：`GET /api/admin/dsr-requests`（list，filter status）、`POST /api/admin/dsr-requests/{id}/approve`、`/reject`。皆 gated by `Permission.DSR_MANAGE`。approve/reject 寫 `decided_at`/`decided_by`/`decision_note` + AuditLog。ownership 重驗（approve 前確認 subject 合法）。

- [ ] **Step 1: 寫 failing test**（無 DSR_MANAGE → 403；list 回 pending；approve reject 寫 decision 欄位）
- [ ] **Step 2: fail**
- [ ] **Step 3: 實作**（response_model 必填，避免前端 codegen 拿 unknown）：

```python
# schemas/dsr.py
from pydantic import BaseModel
class DsrRequestAdminOut(BaseModel):
    id: int
    user_id: int
    request_type: str
    status: str
    subject_entity_type: str | None
    subject_entity_id: int | None
    scope: str | None
    field_name: str | None
    reason: str | None
    submitted_at: str
    decided_at: str | None
    decision_note: str | None
class DsrDecisionIn(BaseModel):
    decision_note: str
```

```python
# api/parent_portal/dsr_admin.py（骨架）
router = APIRouter(prefix="/admin/dsr-requests", tags=["dsr-admin"])

@router.get("", response_model=list[DsrRequestAdminOut])
def list_dsr(status: str | None = None,
             current_user: dict = Depends(require_permission(Permission.DSR_MANAGE)),
             session: Session = Depends(get_session_dep)):
    q = session.query(DsrRequest)
    if status: q = q.filter(DsrRequest.status == status)
    return [_to_admin_out(r) for r in q.order_by(DsrRequest.submitted_at.desc()).all()]

@router.post("/{req_id}/reject", response_model=DsrRequestAdminOut)
def reject_dsr(req_id: int, payload: DsrDecisionIn, request: Request,
               current_user: dict = Depends(require_permission(Permission.DSR_MANAGE)),
               session: Session = Depends(get_session_dep)):
    req = session.query(DsrRequest).filter(DsrRequest.id == req_id).first()
    if not req or req.status != DSR_STATUS_PENDING:
        raise HTTPException(404, "申請不存在或已決議")
    req.status = DSR_STATUS_REJECTED
    req.decided_at = now_taipei_naive(); req.decided_by = current_user["user_id"]
    req.decision_note = payload.decision_note
    write_explicit_audit(request, action="UPDATE", entity_type="dsr_request",
                         entity_id=str(req.id), summary="DSR 駁回",
                         changes={"status": "rejected"})
    session.commit()
    return _to_admin_out(req)
```

> approve 端點按 request_type 分派到 Task 12（delete→GC / correct→手動）。

- [ ] **Step 4: main.py include_router + Run + Commit**

```bash
git add api/parent_portal/dsr_admin.py schemas/dsr.py main.py tests/consent/test_dsr_admin_queue.py
git commit -m "feat(dsr): admin DSR queue 端點（list/approve/reject）+ DSR_MANAGE gate + audit"
```

> **執行注意**：`DsrRequest` 現有欄位是否含 `decided_at`/`decided_by`/`decision_note`？`grep -n "decided\|decision" models/dsr.py`。Phase 1 model 片段未見這些欄位——**若缺，本 task 含一支 alembic 加欄（nullable，downgrade 完整）**。

---

### Task 12: approve 執行——delete→lifecycle GC / correct→手動

**Files:**
- Modify: `api/parent_portal/dsr_admin.py`（approve 分派）
- Test: `tests/consent/test_dsr_execute.py`

- [ ] **delete approve**：走既有 `services.student_lifecycle.transition(session, student, to_status=LIFECYCLE_WITHDRAWN, ...)` → 既有 365d PII GC 接手（**不建平行刪除**）。保留出席/費用/薪資（法定保存）。法定保存內或涉未結帳務 → admin 可 reject 註明法源。
- [ ] **correct approve**：**不自動套用** new_value——只記 `decision_note` + AuditLog，admin 用既有編輯工具手動更正（避免套用引擎/IDOR）。
- [ ] **opt-out**：已於 Task 9 即時自助，不進此 queue。
- [ ] ownership 重驗：approve 前確認 `subject_entity` 與申請人關係。
- [ ] test：approve delete → student 終態 + GC 排程（保留項不動，mock GC 斷言被呼叫）；approve correct → 稽核紀錄 + 欄位未被系統改。
- [ ] Commit：`feat(dsr): DSR delete 走 lifecycle GC + correct 手動更正（ownership 重驗）`

---

## Self-Review（plan 對 spec 覆蓋）

- spec §3.1a service_essential gate → Task 7 ✓
- spec §3.1b granular point-of-use（line_push/photo_publish/cross_border）→ Task 3/4/5/6 ✓
- spec §3.1c chokepoint coverage 測試 → Task 6 ✓
- spec §3.1d fail-mode → D3 + Task 3/7 ✓
- spec §3.1e dark-launch flag → Task 1 ✓
- spec §3.2a opt-out 即時 → Task 9 ✓
- spec §3.2b delete→GC → Task 12 ✓
- spec §3.2c correct 手動 → Task 12 ✓
- spec §3.2d admin queue + DSR_MANAGE + ownership → Task 10/11/12 ✓
- spec §3.3 endpoints → Task 7/8/9/11 ✓

**Flagged（執行前須確認）：**
1. **D2 per-student consent 策略**（主要 guardian vs 所有 guardian）——業主/法律決策。
2. `ParentConsentLog.policy_version_id` 與 `DsrRequest.decided_*` 欄位 nullable/存在性——Task 9/11 可能各含一支小 alembic。
3. Task 5 依賴 `fix/line-push-consent-staff-exempt` 先 merge。

## Rollout / 部署 gate（spec §6）
1. prod 上傳政策文件 + seed `PolicyVersion` v1。
2. `DSR_MANAGE` seed 給 admin/園長。
3. `CONSENT_ENFORCEMENT_ENABLED` 先 false（dark）→ 驗證重簽 + 咽喉 → LINE 廣播預告 → 刻意設 true。
4. 監測重簽率（<80% 提醒）+ watch fail-closed 誤擋。
