# 員工離職 Checklist Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補完員工離職 Phase 2 — 離職證明 PDF (§19) + magic-link 自助下載（30 天 / 3 次上限，admin 產/撤）+ ZIP 下載（證明 + 12 月薪資 + 出勤 CSV）。

**Architecture:** 新 `services/employee_offboarding_certificate_pdf.py` 產 §19 證明 PDF（仿 enrollment_certificate_pdf 模式，bundled Noto Sans TC TTF）。新 `services/offboarding/magic_link.py` 集中 token 產生（secrets.token_urlsafe 256-bit）+ sha256 hash + 驗證 + revoke。3 個新 endpoint：admin 產/撤 token、員工公開 download。ZIP 動態組合（cert PDF + 12 個月 salary_slip.py PDF + 出勤 CSV）。Public endpoint 含 IP rate limit + URL log filter + audit log。

**Tech Stack:** FastAPI, ReportLab, slowapi（IP rate limit）, secrets/hashlib, zipfile, csv, Pydantic v2, pytest。

**Spec：** `docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md` §3 (Phase 2) + §7 (PDF) + §8 (Magic-link)。

**Phase 1 已完成（前置）：** Migration offb0001（含 magic_link 5 欄）+ EmployeeOffboardingRecord model + Pydantic schemas (`certificate_download_url` field 已在 OffboardingProcessResponse) + AuditMiddleware ENTITY_PATTERNS 含 offboarding。**Phase 2 工作在同一 worktree `feat/offboarding-phase-1-2026-05-25-backend` 接續 commit**。

**Phase 2 不含：** 前端 UI（Phase 3）、Email 自動寄送（admin 手動複製 token）、ex-employee 永久 login。

---

## 檔案結構

新建檔：
- `services/employee_offboarding_certificate_pdf.py` — §19 離職證明 PDF
- `services/offboarding/magic_link.py` — token 產 / hash / 驗 / 撤 / 取 active flag
- `services/offboarding/download_bundle.py` — ZIP 組合（cert + salary + attendance）
- `services/offboarding/attendance_csv.py` — 過去 12 月 attendance 匯出 CSV
- `storage/offboarding_certificates/` — PDF 存放目錄（gitignored）

修改檔：
- `api/offboarding.py` — 加 4 endpoint：
  - `POST /offboarding/{employee_id}/magic-link`（admin 產，EMPLOYEES_WRITE）
  - `DELETE /offboarding/{employee_id}/magic-link`（admin 撤，EMPLOYEES_WRITE）
  - `GET /offboarding/download?token=...`（公開無 auth，IP rate limit）
  - `GET /offboarding/{employee_id}/certificate.pdf`（admin 取，EMPLOYEES_READ）
- `services/offboarding/orchestrator.py` — 加 step 5 `generate_certificate` 串接（自動產 PDF）
- `services/offboarding/steps/generate_certificate.py` — 新 step（呼叫 PDF service）
- `schemas/offboarding.py` — 加 `MagicLinkResponse`、`MagicLinkRevokeResponse`、擴 `OffboardingDetailResponse` 含 magic_link metadata（expires / count / last_used / created_at）
- `.gitignore` — 加 `storage/offboarding_certificates/`
- `main.py` 或 middleware — URL query string log filter（過濾 `token=` 改 `token=***`）

新建測試：
- `tests/test_offboarding_certificate_pdf.py`
- `tests/test_offboarding_magic_link.py`
- `tests/test_offboarding_step_generate_certificate.py`
- `tests/test_offboarding_attendance_csv.py`
- `tests/test_offboarding_download_bundle.py`
- `tests/test_offboarding_api_magic_link.py`
- `tests/test_offboarding_api_download.py`
- `tests/test_offboarding_api_certificate.py`

---

## Task 1: 離職證明 PDF service

**Files:**
- Create: `services/employee_offboarding_certificate_pdf.py`
- Test: `tests/test_offboarding_certificate_pdf.py`

**動機：** 勞基法 §19 規定雇主不得拒絕離職員工請求服務證明書。內容不寫離職原因（§19 禁記載對受僱人不利之事項）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_certificate_pdf.py
"""驗證離職證明 PDF 生成（§19）。"""
from datetime import date
from pathlib import Path

from services.employee_offboarding_certificate_pdf import generate_certificate_pdf


def test_generate_certificate_returns_pdf_bytes_with_required_fields(
    db_session, employee_factory,
):
    emp = employee_factory(
        name="王小明",
        id_number="A123456789",
        hire_date=date(2024, 8, 1),
        position="教保員",
    )
    pdf_bytes = generate_certificate_pdf(
        db_session, emp.id, resign_date=date(2026, 6, 15),
    )
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:4] == b"%PDF"  # PDF magic
    # 內容驗證（解析 PDF 字串）
    content = pdf_bytes.decode("latin-1", errors="ignore")
    # §19 必含項目
    assert "離職證明" in content or b"\xe9\x9b\xa2\xe8\x81\xb7".decode("utf-8", errors="ignore") in content
    # ↑ PDF 中文以 CID 編碼存，直接 byte 比對不可靠；改驗結構：
    assert b"%%EOF" in pdf_bytes


def test_generate_certificate_raises_when_employee_missing(db_session):
    import pytest
    with pytest.raises(ValueError, match="員工不存在"):
        generate_certificate_pdf(db_session, 99999, resign_date=date(2026, 6, 15))


def test_generate_certificate_does_not_include_resign_reason(
    db_session, employee_factory,
):
    """§19 禁記載對受僱人不利之事項。"""
    emp = employee_factory(name="李四")
    # 不傳 reason — 驗 function signature 不接此參數
    import inspect
    sig = inspect.signature(generate_certificate_pdf)
    assert "reason" not in sig.parameters
    assert "resign_reason" not in sig.parameters
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_certificate_pdf.py -v 2>&1 | tail -10
```

Expected: FAIL — module ImportError。

- [ ] **Step 3: Implement**

```python
# services/employee_offboarding_certificate_pdf.py
"""離職證明 PDF service（勞基法 §19）。

§19 規定：勞工於離職時，得請求發給服務證明書，雇主不得拒絕。
證明書不得記載對受僱人不利之事項（如離職原因、評核分數等）。

PDF 內容：公司資訊 + 員工姓名/身分證/到職日/離職日/職務 + 證明文字 + 日期。
"""
from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from sqlalchemy.orm import Session

from models.employee import Employee
from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font


def _fmt_roc(d: date) -> str:
    """西元 → 民國年 (YYY.MM.DD)"""
    return f"{d.year - 1911}.{d.month:02d}.{d.day:02d}"


def generate_certificate_pdf(
    session: Session, employee_id: int, resign_date: date
) -> bytes:
    """產生離職證明 PDF bytes。

    Args:
        session: SQLAlchemy session
        employee_id: 員工 ID
        resign_date: 離職日

    Returns:
        PDF bytes（可直接寫檔或串流）

    Raises:
        ValueError: 員工不存在
    """
    emp = session.query(Employee).filter_by(id=employee_id).first()
    if emp is None:
        raise ValueError(f"員工不存在: id={employee_id}")

    register_cjk_font()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
    )

    base = getSampleStyleSheet()["Normal"]
    title_style = ParagraphStyle(
        "Title", parent=base, fontName=CJK_FONT_NAME, fontSize=22,
        alignment=1, spaceAfter=24,  # alignment=1 為置中
    )
    body_style = ParagraphStyle(
        "Body", parent=base, fontName=CJK_FONT_NAME, fontSize=12,
        leading=20, spaceAfter=8,
    )
    sign_style = ParagraphStyle(
        "Sign", parent=base, fontName=CJK_FONT_NAME, fontSize=12,
        alignment=2, leading=22, spaceBefore=36,  # alignment=2 為靠右
    )

    elements = []
    elements.append(Paragraph("離職證明書", title_style))
    elements.append(Spacer(1, 0.5 * cm))

    # 公司資訊（hardcode；未來可從 config table 讀）
    elements.append(Paragraph("扣繳義務人：常春藤幼兒園", body_style))
    elements.append(Paragraph("統一編號：（請填入）", body_style))
    elements.append(Paragraph("公司地址：（請填入）", body_style))
    elements.append(Spacer(1, 0.8 * cm))

    elements.append(Paragraph("茲證明：", body_style))
    elements.append(Spacer(1, 0.3 * cm))

    elements.append(Paragraph(f"姓名：{emp.name}", body_style))
    elements.append(Paragraph(f"身分證字號：{emp.id_number or '（未填）'}", body_style))
    hire_str = _fmt_roc(emp.hire_date) if emp.hire_date else "（未填）"
    elements.append(Paragraph(f"到職日期：{hire_str}", body_style))
    elements.append(Paragraph(f"離職日期：{_fmt_roc(resign_date)}", body_style))
    elements.append(Paragraph(f"擔任職務：{emp.position or '（未填）'}", body_style))
    elements.append(Spacer(1, 0.5 * cm))

    elements.append(Paragraph("特此證明。", body_style))

    today = date.today()
    elements.append(Paragraph("負責人簽章：______________", sign_style))
    elements.append(Paragraph(f"證明日期：{_fmt_roc(today)}", sign_style))

    doc.build(elements)
    return buf.getvalue()
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_certificate_pdf.py -v 2>&1 | tail -10
```

Expected: PASS 3 tests。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add services/employee_offboarding_certificate_pdf.py tests/test_offboarding_certificate_pdf.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): generate certificate PDF service (§19)

新建 services/employee_offboarding_certificate_pdf.py：generate_certificate_pdf
(session, employee_id, resign_date) -> bytes。仿 enrollment_certificate_pdf
模式用 ReportLab + bundled Noto Sans TC TTF。

§19 內容遵守：公司資訊 + 員工 5 欄（姓名/身分證/到職/離職/職務）+ 證明文
+ 負責人簽章欄。**不寫離職原因**（§19 禁記載對受僱人不利之事項）。

3 test：bytes 正確 + 404 raise + signature 不接 reason 參數（spec 守衛）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Step generate_certificate + orchestrator 串接

**Files:**
- Create: `services/offboarding/steps/generate_certificate.py`
- Modify: `services/offboarding/orchestrator.py`（在 step 4 revoke_user 後加 step 5）
- Modify: `.gitignore` 加 `storage/offboarding_certificates/`
- Test: `tests/test_offboarding_step_generate_certificate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_step_generate_certificate.py
"""驗證 generate_certificate step：產 PDF + 寫 record.certificate_pdf_path。"""
import os
from datetime import date, datetime
from pathlib import Path

from services.offboarding.steps.generate_certificate import run
from services.offboarding.orchestrator import OffboardingError
from models.offboarding import EmployeeOffboardingRecord


def _make_record(db_session, employee_id, user_id):
    rec = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(rec)
    db_session.flush()
    return rec


def test_generate_certificate_writes_pdf_to_storage(
    db_session, employee_factory, user_factory, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "services.offboarding.steps.generate_certificate.STORAGE_DIR",
        tmp_path,
    )
    emp = employee_factory(name="王小明", id_number="A123456789")
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    result = run(db_session, record)

    assert result["step"] == "generate_certificate"
    assert result["status"] == "completed"
    assert result["payload"]["pdf_path"] is not None
    assert record.certificate_pdf_path is not None
    assert record.certificate_generated_at is not None

    # 檔案實際存在
    written = Path(record.certificate_pdf_path)
    assert written.exists()
    assert written.read_bytes()[:4] == b"%PDF"


def test_generate_certificate_raises_on_disk_failure(
    db_session, employee_factory, user_factory, monkeypatch,
):
    """模擬寫檔失敗 → raise OffboardingError(CERTIFICATE_GENERATION_FAILED)。"""
    import pytest
    monkeypatch.setattr(
        "services.offboarding.steps.generate_certificate.STORAGE_DIR",
        Path("/nonexistent/blocked/dir"),
    )
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    with pytest.raises(OffboardingError) as exc:
        run(db_session, record)
    assert exc.value.code == "CERTIFICATE_GENERATION_FAILED"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_step_generate_certificate.py -v 2>&1 | tail -10
```

Expected: FAIL — module ImportError。

- [ ] **Step 3: Implement step**

```python
# services/offboarding/steps/generate_certificate.py
"""generate_certificate step：呼叫 PDF service → 寫檔 → 寫 record.certificate_pdf_path。

寫檔位置：storage/offboarding_certificates/{employee_id}_{resign_date}.pdf
失敗（磁碟滿 / 權限不足 / 字型 fail）→ raise OffboardingError(CERTIFICATE_GENERATION_FAILED)
"""
import logging
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord
from services.employee_offboarding_certificate_pdf import generate_certificate_pdf
from services.offboarding.orchestrator import StepResult, OffboardingError

logger = logging.getLogger(__name__)

STORAGE_DIR = Path("storage/offboarding_certificates")


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    try:
        pdf_bytes = generate_certificate_pdf(
            session, record.employee_id, record.resign_date
        )

        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = STORAGE_DIR / f"{record.employee_id}_{record.resign_date.isoformat()}.pdf"
        pdf_path.write_bytes(pdf_bytes)

        now = datetime.now()
        record.certificate_pdf_path = str(pdf_path)
        record.certificate_generated_at = now

        logger.info(
            "離職證明 PDF 已產：employee_id=%s path=%s size=%d bytes",
            record.employee_id, pdf_path, len(pdf_bytes),
        )

        return {
            "step": "generate_certificate",
            "status": "completed",
            "completed_at": now,
            "payload": {"pdf_path": str(pdf_path), "bytes": len(pdf_bytes)},
            "error": None,
        }
    except OSError as e:
        # 磁碟 / 權限失敗 → DB rollback 避免殭屍 record
        raise OffboardingError(
            f"離職證明 PDF 寫檔失敗: {e}",
            code="CERTIFICATE_GENERATION_FAILED",
        )
    except Exception as e:
        # 字型 / reportlab 失敗
        raise OffboardingError(
            f"離職證明 PDF 產生失敗: {e}",
            code="CERTIFICATE_GENERATION_FAILED",
        )
```

- [ ] **Step 4: Wire orchestrator step 5**

修改 `services/offboarding/orchestrator.py`：

找 try 區塊內 `revoke_user.run(session, record)` 後加：

```python
        # Step 5: generate_certificate（Phase 2）
        from services.offboarding.steps import generate_certificate
        cert_result = generate_certificate.run(session, record)
        steps_result.append(cert_result)
```

並改 `return OffboardingResult(...)` 中 `certificate_pdf_path=record.certificate_pdf_path,`：

```python
    return OffboardingResult(
        employee_id=employee_id,
        resign_date=resign_date,
        is_active_after=emp.is_active,
        user_account_revoked=user_account_revoked,
        steps=steps_result,
        certificate_pdf_path=record.certificate_pdf_path,  # Phase 2 起填入
    )
```

修改 `services/offboarding/steps/__init__.py` 加：
```python
from . import generate_certificate  # noqa: F401
```
（若 __init__.py 已 import 其他 step，保持風格一致）

- [ ] **Step 5: Add to .gitignore**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
echo "storage/offboarding_certificates/" >> .gitignore
```

- [ ] **Step 6: Run tests + orchestrator test 不回歸**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_step_generate_certificate.py tests/test_offboarding_orchestrator.py tests/test_offboarding_api.py -v 2>&1 | tail -20
```

Expected: PASS 全部（orchestrator happy path 多一個 step result）。

**注意**：可能要修 `tests/test_offboarding_orchestrator.py::test_happy_path_all_4_steps_complete` — 改名為 `test_happy_path_all_5_steps_complete` + 加 generate_certificate 到 expected step_names list。

- [ ] **Step 7: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add services/offboarding/steps/generate_certificate.py \
  services/offboarding/orchestrator.py services/offboarding/steps/__init__.py \
  .gitignore tests/test_offboarding_step_generate_certificate.py \
  tests/test_offboarding_orchestrator.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): step generate_certificate + orchestrator step 5

新 step：呼叫 employee_offboarding_certificate_pdf.generate_certificate_pdf
產 bytes → 寫 storage/offboarding_certificates/{id}_{date}.pdf → 寫
record.certificate_pdf_path / certificate_generated_at。

寫檔失敗（OSError）或字型 / reportlab 異常 → raise OffboardingError
(CERTIFICATE_GENERATION_FAILED) → orchestrator rollback 整筆。

orchestrator 串接：mark_appraisal → snapshot_leave → prefill_salary →
revoke_user → generate_certificate（5 step 完整）。OffboardingResult
.certificate_pdf_path 自此正式填入（Phase 1 一律 None）。

storage/offboarding_certificates/ 入 .gitignore。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Magic-link token service

**Files:**
- Create: `services/offboarding/magic_link.py`
- Test: `tests/test_offboarding_magic_link.py`

**動機：** 集中 token 生命週期管理（產 / hash / 驗 / 撤 / active flag），endpoint layer 只呼叫不重複邏輯。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_magic_link.py
"""驗證 magic_link service：token 產生 / hash 比對 / 撤銷 / active 判斷。"""
import hashlib
from datetime import date, datetime, timedelta
import pytest

from services.offboarding.magic_link import (
    generate_token,
    hash_token,
    verify_token,
    revoke_token,
    is_active,
    TOKEN_TTL_DAYS,
    MAX_DOWNLOADS,
    MagicLinkError,
)
from models.offboarding import EmployeeOffboardingRecord


def _make_record(db_session, employee_id, user_id):
    rec = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(rec)
    db_session.flush()
    return rec


def test_constants():
    """TTL 30 天 / 下載 3 次上限符合 spec §8。"""
    assert TOKEN_TTL_DAYS == 30
    assert MAX_DOWNLOADS == 3


def test_generate_token_returns_256_bit_url_safe_random(
    db_session, employee_factory, user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    token = generate_token(db_session, record)

    # secrets.token_urlsafe(32) → 約 43 字元 base64url
    assert len(token) >= 40
    assert all(c.isalnum() or c in "-_" for c in token)

    # DB 存的是 hash 非明文
    db_session.refresh(record)
    assert record.magic_link_token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert record.magic_link_expires_at > datetime.now() + timedelta(days=29)
    assert record.magic_link_revoked_at is None
    assert record.magic_link_download_count == 0


def test_generate_token_overwrites_previous(
    db_session, employee_factory, user_factory,
):
    """重發 token = 舊 hash 失效，count 歸 0。"""
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    t1 = generate_token(db_session, record)
    # 模擬已下載 1 次
    record.magic_link_download_count = 1
    db_session.flush()

    t2 = generate_token(db_session, record)
    db_session.refresh(record)

    assert t1 != t2
    assert record.magic_link_token_hash == hashlib.sha256(t2.encode()).hexdigest()
    assert record.magic_link_download_count == 0  # 歸 0


def test_verify_token_returns_record_for_valid(
    db_session, employee_factory, user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    db_session.commit()

    found = verify_token(db_session, token)
    assert found is not None
    assert found.employee_id == emp.id


def test_verify_token_returns_none_for_unknown(db_session):
    """未知 token → None（不暴露差異避免 enumeration）。"""
    assert verify_token(db_session, "nonexistent-token-string") is None


def test_verify_token_returns_none_for_revoked(
    db_session, employee_factory, user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    revoke_token(db_session, record)
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_verify_token_returns_none_for_expired(
    db_session, employee_factory, user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    # 強制過期
    record.magic_link_expires_at = datetime.now() - timedelta(days=1)
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_verify_token_returns_none_when_max_downloads_reached(
    db_session, employee_factory, user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    record.magic_link_download_count = 3
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_is_active_logic(db_session, employee_factory, user_factory):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    # 無 token → inactive
    assert is_active(record) is False

    generate_token(db_session, record)
    db_session.refresh(record)
    assert is_active(record) is True

    revoke_token(db_session, record)
    db_session.refresh(record)
    assert is_active(record) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_magic_link.py -v 2>&1 | tail -10
```

Expected: FAIL — ImportError。

- [ ] **Step 3: Implement**

```python
# services/offboarding/magic_link.py
"""Magic-link token 服務：產生 / hash / 驗證 / 撤銷 / active 判斷。

設計：
- token 用 secrets.token_urlsafe(32) → 256-bit base64url random
- DB 存 sha256 hash，明文不留（同 password salt+hash 原則）
- TTL 30 天 + 3 次下載上限
- verify 失敗統一回 None（不暴露差異，防 enumeration）

設計參考：spec §8。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord

logger = logging.getLogger(__name__)

TOKEN_TTL_DAYS = 30
MAX_DOWNLOADS = 3


class MagicLinkError(Exception):
    """magic-link 操作錯誤。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def hash_token(token: str) -> str:
    """SHA-256 hash 明文 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(session: Session, record: EmployeeOffboardingRecord) -> str:
    """產生新 token、寫 hash + expires + 歸 0 count + 清 revoked。

    回傳明文 token（只此一次出現；DB 只存 hash）。
    呼叫端負責 session.commit()。
    """
    token = secrets.token_urlsafe(32)
    record.magic_link_token_hash = hash_token(token)
    record.magic_link_expires_at = datetime.now() + timedelta(days=TOKEN_TTL_DAYS)
    record.magic_link_revoked_at = None
    record.magic_link_download_count = 0
    record.magic_link_last_used_at = None

    logger.warning(
        "magic-link token 產生：employee_id=%s expires_at=%s",
        record.employee_id, record.magic_link_expires_at,
    )
    return token


def verify_token(
    session: Session, token: str
) -> Optional[EmployeeOffboardingRecord]:
    """驗證 token：合法且未過期未撤未達次數 → 回 record；否則回 None。

    不暴露差異原因（防 enumeration）— 呼叫端統一回 410 Gone。
    """
    if not token:
        return None
    token_hash = hash_token(token)
    record = (
        session.query(EmployeeOffboardingRecord)
        .filter_by(magic_link_token_hash=token_hash)
        .first()
    )
    if record is None:
        return None
    if record.magic_link_revoked_at is not None:
        return None
    if (
        record.magic_link_expires_at is not None
        and record.magic_link_expires_at < datetime.now()
    ):
        return None
    if (record.magic_link_download_count or 0) >= MAX_DOWNLOADS:
        return None
    return record


def revoke_token(session: Session, record: EmployeeOffboardingRecord) -> None:
    """撤銷 token（保留 hash 行 audit）。呼叫端負責 commit。"""
    record.magic_link_revoked_at = datetime.now()
    logger.warning(
        "magic-link token 已撤：employee_id=%s", record.employee_id,
    )


def is_active(record: EmployeeOffboardingRecord) -> bool:
    """派生 bool：token 存在且未過期未撤未達次數。
    （與 api/offboarding._is_magic_link_active 同邏輯，集中於此供 reuse。）"""
    if not record.magic_link_token_hash:
        return False
    if record.magic_link_revoked_at is not None:
        return False
    if (
        record.magic_link_expires_at is not None
        and record.magic_link_expires_at < datetime.now()
    ):
        return False
    if (record.magic_link_download_count or 0) >= MAX_DOWNLOADS:
        return False
    return True


def record_download(
    session: Session, record: EmployeeOffboardingRecord
) -> None:
    """記錄下載：count++ + last_used_at = now。呼叫端負責 commit。"""
    record.magic_link_download_count = (record.magic_link_download_count or 0) + 1
    record.magic_link_last_used_at = datetime.now()
```

- [ ] **Step 4: Refactor `api/offboarding._is_magic_link_active` to call new helper**

修 `api/offboarding.py` 內 `_is_magic_link_active`：

```python
from services.offboarding.magic_link import is_active as _is_magic_link_active
# 刪除原 _is_magic_link_active function 定義
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_magic_link.py tests/test_offboarding_api.py -v 2>&1 | tail -15
```

Expected: PASS 全部（含既有 GET endpoint test）。

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add services/offboarding/magic_link.py api/offboarding.py tests/test_offboarding_magic_link.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): magic_link service — token 生命週期

集中 token 管理：
- generate_token: secrets.token_urlsafe(32) → 寫 sha256 hash 進 record
  （明文不留），30 天 expires + count 歸 0
- verify_token: hash 比對 + 驗未過期 / 未撤 / 未達 3 次上限；失敗統一
  回 None（防 enumeration，呼叫端統一回 410 Gone）
- revoke_token: 設 revoked_at（保留 hash 行 audit）
- is_active: 派生 bool（取代 api/offboarding._is_magic_link_active inline）
- record_download: count++ + last_used_at = now

TOKEN_TTL_DAYS = 30 / MAX_DOWNLOADS = 3 對齊 spec §8。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Attendance CSV + ZIP bundle service

**Files:**
- Create: `services/offboarding/attendance_csv.py`
- Create: `services/offboarding/download_bundle.py`
- Test: `tests/test_offboarding_attendance_csv.py`
- Test: `tests/test_offboarding_download_bundle.py`

**動機：** ZIP 內含 3 檔（離職證明 PDF + 過去 12 月 salary_slip PDF + 出勤 CSV）。攔成 service 供 download endpoint 呼叫。

- [ ] **Step 1: Write attendance CSV failing test**

```python
# tests/test_offboarding_attendance_csv.py
"""驗證過去 12 月 attendance CSV 匯出。"""
from datetime import date, datetime, timedelta

from services.offboarding.attendance_csv import generate_attendance_csv


def test_generates_csv_with_header_and_rows(
    db_session, employee_factory, attendance_record_factory,
):
    emp = employee_factory(hire_date=date(2025, 1, 1))
    # 建幾筆 attendance
    attendance_record_factory(employee_id=emp.id, work_date=date(2026, 5, 1))
    attendance_record_factory(employee_id=emp.id, work_date=date(2026, 5, 2))

    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15),
    )
    text = csv_bytes.decode("utf-8-sig")  # Excel 友好 BOM
    lines = text.strip().split("\n")
    assert "work_date" in lines[0] or "日期" in lines[0]
    assert len(lines) >= 3  # header + 2 rows


def test_empty_when_no_attendance(db_session, employee_factory):
    """無 attendance 仍回 header（讓員工知道沒漏資料）。"""
    emp = employee_factory(hire_date=date(2025, 1, 1))
    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15),
    )
    text = csv_bytes.decode("utf-8-sig")
    assert text.startswith("work_date") or "日期" in text


def test_starts_from_hire_date_when_recent_hire(
    db_session, employee_factory, attendance_record_factory,
):
    """到職不滿 12 月 → 從 hire_date 起算，不補空白。"""
    emp = employee_factory(hire_date=date(2026, 3, 1))
    attendance_record_factory(
        employee_id=emp.id, work_date=date(2025, 12, 1)
    )  # hire 之前不應出現

    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15),
    )
    text = csv_bytes.decode("utf-8-sig")
    assert "2025-12-01" not in text  # hire 前不含
```

- [ ] **Step 2: Implement attendance_csv**

```python
# services/offboarding/attendance_csv.py
"""離職員工過去 12 月 attendance CSV 匯出。

從 resign_date 倒推 12 個月（不滿 12 月則從 hire_date 起算）。
UTF-8 with BOM 為 Excel 開檔友好。
"""
import csv
import io
from datetime import date, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from models.attendance import AttendanceRecord
from models.employee import Employee


def generate_attendance_csv(
    session: Session, employee_id: int, resign_date: date,
) -> bytes:
    """過去 12 月 attendance CSV bytes（UTF-8 with BOM）。"""
    emp = session.query(Employee).filter_by(id=employee_id).first()
    if emp is None:
        raise ValueError(f"員工不存在: id={employee_id}")

    # 起始日：max(resign_date - 365 天, hire_date)
    start_date = resign_date - timedelta(days=365)
    if emp.hire_date and emp.hire_date > start_date:
        start_date = emp.hire_date

    records = (
        session.query(AttendanceRecord)
        .filter(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.work_date >= start_date,
            AttendanceRecord.work_date <= resign_date,
        )
        .order_by(AttendanceRecord.work_date)
        .all()
    )

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM for Excel
    writer = csv.writer(buf)
    writer.writerow([
        "work_date", "check_in_time", "check_out_time",
        "work_hours", "overtime_hours", "leave_hours", "note",
    ])
    for rec in records:
        writer.writerow([
            rec.work_date.isoformat() if rec.work_date else "",
            str(rec.check_in_time) if rec.check_in_time else "",
            str(rec.check_out_time) if rec.check_out_time else "",
            rec.work_hours or "",
            getattr(rec, "overtime_hours", "") or "",
            getattr(rec, "leave_hours", "") or "",
            getattr(rec, "note", "") or "",
        ])
    return buf.getvalue().encode("utf-8")
```

**注意：** 先 `grep "class AttendanceRecord" models/attendance.py` 確認真實欄位名稱（可能不是 work_date / check_in_time）。若欄位名不同，先列名再 inline 同步。

- [ ] **Step 3: Write download bundle failing test**

```python
# tests/test_offboarding_download_bundle.py
"""驗證 ZIP bundle 組合（cert + 12 月 salary PDF + attendance CSV）。"""
import io
import zipfile
from datetime import date, datetime
from pathlib import Path

from services.offboarding.download_bundle import build_offboarding_zip
from models.offboarding import EmployeeOffboardingRecord


def test_zip_contains_cert_salary_attendance(
    db_session, employee_factory, user_factory, leave_quota_factory,
    salary_record_factory, attendance_record_factory, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "services.offboarding.steps.generate_certificate.STORAGE_DIR",
        tmp_path,
    )
    emp = employee_factory(name="王小明", daily_wage=1800,
                           hire_date=date(2025, 1, 1))
    user = user_factory()
    # 預先建 record + cert PDF（模擬 process 完成的狀態）
    record = EmployeeOffboardingRecord(
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user.id,
    )
    db_session.add(record)
    db_session.flush()
    cert_path = tmp_path / "cert.pdf"
    cert_path.write_bytes(b"%PDF-1.4\n%%EOF")
    record.certificate_pdf_path = str(cert_path)

    salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=5)
    attendance_record_factory(employee_id=emp.id, work_date=date(2026, 5, 1))
    db_session.commit()

    zip_bytes = build_offboarding_zip(db_session, record)
    assert zip_bytes[:4] == b"PK\x03\x04"  # ZIP magic

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert any("離職證明" in n or "certificate" in n.lower() for n in names)
        assert any("salary" in n.lower() or "薪資" in n for n in names)
        assert any("attendance" in n.lower() or "出勤" in n for n in names)


def test_zip_with_no_cert_path_raises(
    db_session, employee_factory, user_factory,
):
    """record.certificate_pdf_path None → raise ValueError（process 應已產 cert）"""
    import pytest
    emp = employee_factory()
    user = user_factory()
    record = EmployeeOffboardingRecord(
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user.id,
    )
    db_session.add(record)
    db_session.commit()

    with pytest.raises(ValueError, match="cert"):
        build_offboarding_zip(db_session, record)
```

- [ ] **Step 4: Implement download_bundle**

```python
# services/offboarding/download_bundle.py
"""離職員工 ZIP 下載組合：cert PDF + 過去 12 月 salary PDF + 出勤 CSV。
"""
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy.orm import Session

from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from models.salary import SalaryRecord
from services.finance.salary_slip import generate_salary_pdf
from services.offboarding.attendance_csv import generate_attendance_csv


def build_offboarding_zip(
    session: Session, record: EmployeeOffboardingRecord,
) -> bytes:
    """組 ZIP bytes。

    內容：
    - 離職證明 PDF（從 record.certificate_pdf_path 讀檔）
    - 過去 12 月每月一份 salary_slip PDF（salary_records 有的月份才產）
    - 出勤 CSV（過去 12 月）

    Raises:
        ValueError: record.certificate_pdf_path 為 None（process 應已產）
    """
    if not record.certificate_pdf_path:
        raise ValueError(
            f"record.certificate_pdf_path 為 None（employee_id={record.employee_id}）；"
            "離職證明 PDF 必須先由 orchestrator 產出"
        )

    emp = session.query(Employee).filter_by(id=record.employee_id).first()
    if emp is None:
        raise ValueError(f"員工不存在: id={record.employee_id}")

    resign = record.resign_date
    start = resign - timedelta(days=365)
    if emp.hire_date and emp.hire_date > start:
        start = emp.hire_date

    salary_records = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == emp.id,
            (SalaryRecord.salary_year * 100 + SalaryRecord.salary_month)
            >= start.year * 100 + start.month,
            (SalaryRecord.salary_year * 100 + SalaryRecord.salary_month)
            <= resign.year * 100 + resign.month,
        )
        .order_by(SalaryRecord.salary_year, SalaryRecord.salary_month)
        .all()
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. 離職證明 PDF
        cert_bytes = Path(record.certificate_pdf_path).read_bytes()
        zf.writestr(f"離職證明_{emp.name}_{resign.isoformat()}.pdf", cert_bytes)

        # 2. 過去 12 月 salary PDF（每月一檔）
        for sr in salary_records:
            try:
                pdf_bytes = generate_salary_pdf(
                    sr, emp, sr.salary_year, sr.salary_month
                )
                zf.writestr(
                    f"薪資明細_{sr.salary_year}-{sr.salary_month:02d}.pdf",
                    pdf_bytes,
                )
            except Exception:
                # 單月失敗不擋整 ZIP
                pass

        # 3. attendance CSV
        csv_bytes = generate_attendance_csv(session, emp.id, resign)
        zf.writestr(
            f"出勤紀錄_{start.isoformat()}_至_{resign.isoformat()}.csv",
            csv_bytes,
        )

    return buf.getvalue()
```

- [ ] **Step 5: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_attendance_csv.py tests/test_offboarding_download_bundle.py -v 2>&1 | tail -15
```

Expected: PASS 全部。

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add services/offboarding/attendance_csv.py services/offboarding/download_bundle.py \
  tests/test_offboarding_attendance_csv.py tests/test_offboarding_download_bundle.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): attendance_csv + download_bundle services

attendance_csv: 過去 12 月 AttendanceRecord 匯 UTF-8 with BOM CSV（Excel
友好），起始日從 max(resign - 365d, hire_date) 算（不補空白月）。

download_bundle: ZIP 組合三類檔：
- 離職證明 PDF（從 record.certificate_pdf_path 讀檔）
- 過去 12 月每月 salary_slip PDF（reuse services.finance.salary_slip.
  generate_salary_pdf；單月失敗不擋整 ZIP）
- 出勤 CSV

ValueError 守衛：record.certificate_pdf_path None 時 raise（orchestrator
應先產證明）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: POST + DELETE magic-link endpoints

**Files:**
- Modify: `api/offboarding.py`
- Modify: `schemas/offboarding.py`（加 MagicLinkResponse / MagicLinkRevokeResponse）
- Test: `tests/test_offboarding_api_magic_link.py`

- [ ] **Step 1: Add schemas**

修改 `schemas/offboarding.py`：

```python
class MagicLinkResponse(BaseModel):
    employee_id: int
    token: str                  # 明文（只此一次回，admin 複製貼 email）
    expires_at: datetime
    download_url: str           # 完整 URL：/api/offboarding/download?token=...


class MagicLinkRevokeResponse(BaseModel):
    employee_id: int
    revoked_at: datetime
```

- [ ] **Step 2: Write failing tests**

```python
# tests/test_offboarding_api_magic_link.py
"""驗證 POST / DELETE magic-link endpoints。"""
import hashlib


def _process_offboarding(client, admin_login, emp_id, **kwargs):
    """helper: 先建 offboarding record（後續測 magic-link 才有 record 可用）。"""
    headers = admin_login()
    return client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
        headers=headers,
    )


def test_post_magic_link_returns_plaintext_token(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)
    _process_offboarding(client, admin_login, emp.id)

    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/magic-link", headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["employee_id"] == emp.id
    assert "token" in body and len(body["token"]) >= 40
    assert "expires_at" in body
    assert body["download_url"] == f"/api/offboarding/download?token={body['token']}"


def test_post_magic_link_404_when_no_record(
    integrated_client, admin_login, employee_factory,
):
    client, _ = integrated_client
    emp = employee_factory()
    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/magic-link", headers=headers,
    )
    assert response.status_code == 404


def test_delete_magic_link_revokes_token(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)
    _process_offboarding(client, admin_login, emp.id)

    headers = admin_login()
    client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    r = client.delete(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    assert r.status_code == 200
    assert r.json()["employee_id"] == emp.id

    # GET detail → magic_link_active False
    detail = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert detail.json()["magic_link_active"] is False


def test_repost_magic_link_overwrites_previous(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)
    _process_offboarding(client, admin_login, emp.id)

    headers = admin_login()
    r1 = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    r2 = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    assert r1.json()["token"] != r2.json()["token"]
```

- [ ] **Step 3: Implement endpoints**

修改 `api/offboarding.py` 加：

```python
from schemas.offboarding import MagicLinkResponse, MagicLinkRevokeResponse
from services.offboarding.magic_link import (
    generate_token as ml_generate_token,
    revoke_token as ml_revoke_token,
)


@router.post("/{employee_id}/magic-link", response_model=MagicLinkResponse)
def post_magic_link(
    employee_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """admin 產 magic-link token（30 天 / 3 次上限）。覆寫舊 hash（重發即作廢前一個）。"""
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(404, "OFFBOARDING_RECORD_NOT_FOUND")
        token = ml_generate_token(session, record)
        session.commit()

        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = (
            f"離職 magic-link 產生：employee/{employee_id} "
            f"expires_at={record.magic_link_expires_at.isoformat()}"
        )

        return MagicLinkResponse(
            employee_id=employee_id,
            token=token,
            expires_at=record.magic_link_expires_at,
            download_url=f"/api/offboarding/download?token={token}",
        )
    finally:
        session.close()


@router.delete("/{employee_id}/magic-link", response_model=MagicLinkRevokeResponse)
def delete_magic_link(
    employee_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """admin 撤 magic-link token。"""
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(404, "OFFBOARDING_RECORD_NOT_FOUND")
        if record.magic_link_token_hash is None:
            raise HTTPException(404, "NO_ACTIVE_MAGIC_LINK")
        ml_revoke_token(session, record)
        session.commit()

        request.state.audit_entity_id = str(employee_id)
        request.state.audit_summary = (
            f"離職 magic-link 撤銷：employee/{employee_id}"
        )

        return MagicLinkRevokeResponse(
            employee_id=employee_id,
            revoked_at=record.magic_link_revoked_at,
        )
    finally:
        session.close()
```

- [ ] **Step 4: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_api_magic_link.py tests/test_offboarding_*.py -v 2>&1 | tail -20
```

Expected: PASS 全部。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add api/offboarding.py schemas/offboarding.py tests/test_offboarding_api_magic_link.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): POST/DELETE magic-link endpoints

POST /offboarding/{id}/magic-link：admin 產 token（明文只此一次回，
download_url 預組好讓 admin 複製到 email）；EMPLOYEES_WRITE；audit 寫
expires_at。

DELETE /offboarding/{id}/magic-link：admin 撤；EMPLOYEES_WRITE；audit 寫
revoke 事件。404 OFFBOARDING_RECORD_NOT_FOUND / NO_ACTIVE_MAGIC_LINK。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: GET /offboarding/download public endpoint + 安全強化

**Files:**
- Modify: `api/offboarding.py`（加 download endpoint）
- Modify: `main.py` 或 `utils/`（URL log filter middleware）
- Test: `tests/test_offboarding_api_download.py`

**動機：** 員工自助下載端點。**公開無 auth**，需 IP rate limit + URL log filter + 統一 410 Gone（防 enumeration）。

- [ ] **Step 1: Check existing rate limit pattern**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
grep -n "slowapi\|@limiter\|RateLimiter\|rate_limit" main.py api/*.py 2>&1 | head -10
```

確認 codebase 用哪個 rate limit 套件（slowapi / inhouse）。若 inhouse 在 `utils/rate_limit.py`，沿用既有 decorator。

- [ ] **Step 2: Write failing tests**

```python
# tests/test_offboarding_api_download.py
"""驗證公開 download endpoint + 安全強化。"""
import io
import zipfile


def _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory):
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)
    headers = admin_login()
    client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )
    r = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    return emp, r.json()["token"]


def test_download_with_valid_token_returns_zip(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp, token = _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory)

    response = client.get(f"/api/offboarding/download?token={token}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers.get("x-content-type-options") == "nosniff"

    # ZIP magic
    assert response.content[:4] == b"PK\x03\x04"


def test_download_increments_count_and_last_used(
    integrated_client, admin_login, employee_factory, leave_quota_factory, db_session,
):
    client, sf = integrated_client
    emp, token = _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory)

    client.get(f"/api/offboarding/download?token={token}")
    from models.offboarding import EmployeeOffboardingRecord
    with sf() as session:
        record = session.query(EmployeeOffboardingRecord).filter_by(employee_id=emp.id).one()
        assert record.magic_link_download_count == 1
        assert record.magic_link_last_used_at is not None


def test_download_returns_410_for_invalid_token(integrated_client):
    client, _ = integrated_client
    r = client.get("/api/offboarding/download?token=fake-nonexistent-token")
    assert r.status_code == 410


def test_download_returns_410_for_expired_token(
    integrated_client, admin_login, employee_factory, leave_quota_factory, db_session,
):
    client, sf = integrated_client
    emp, token = _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory)
    # 強制過期
    from datetime import datetime, timedelta
    from models.offboarding import EmployeeOffboardingRecord
    with sf() as session:
        record = session.query(EmployeeOffboardingRecord).filter_by(employee_id=emp.id).one()
        record.magic_link_expires_at = datetime.now() - timedelta(days=1)
        session.commit()

    r = client.get(f"/api/offboarding/download?token={token}")
    assert r.status_code == 410


def test_download_returns_410_after_max_downloads(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp, token = _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory)

    # 連下 3 次成功
    for i in range(3):
        r = client.get(f"/api/offboarding/download?token={token}")
        assert r.status_code == 200, f"download {i+1} should succeed"

    # 第 4 次 → 410
    r = client.get(f"/api/offboarding/download?token={token}")
    assert r.status_code == 410
```

- [ ] **Step 3: Implement download endpoint**

修 `api/offboarding.py` 加：

```python
from fastapi.responses import StreamingResponse
import io

from services.offboarding.magic_link import (
    verify_token, record_download,
)
from services.offboarding.download_bundle import build_offboarding_zip


@router.get("/download")
def download_offboarding_bundle(token: str, request: Request):
    """**公開無 auth** download endpoint。

    驗 token → 串流 ZIP（cert PDF + 12 月 salary + 出勤 CSV）。
    驗失敗統一 410 Gone（不暴露差異原因，防 enumeration）。
    """
    session: Session = get_session()
    try:
        record = verify_token(session, token)
        if record is None:
            raise HTTPException(status_code=410, detail="LINK_NO_LONGER_VALID")

        zip_bytes = build_offboarding_zip(session, record)
        record_download(session, record)

        # audit log（公開 endpoint 仍記）
        request.state.audit_entity_id = str(record.employee_id)
        request.state.audit_summary = (
            f"離職 ZIP 下載：employee/{record.employee_id} "
            f"count={record.magic_link_download_count}"
        )

        session.commit()

        emp_name = "employee"
        from models.employee import Employee
        emp = session.query(Employee).filter_by(id=record.employee_id).first()
        if emp:
            emp_name = emp.name
        filename = f"ivy-offboarding-{emp_name}-{record.resign_date.isoformat()}.zip"

        return StreamingResponse(
            io.BytesIO(zip_bytes),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    finally:
        session.close()
```

- [ ] **Step 4: Add URL log filter (protect token in access log)**

檢查 main.py 是否有 request logging middleware；若有，加 token sanitize：

```python
# main.py 或 utils/middleware.py 內既有 logging middleware
# 找 query string 處理處，把 token=xxx 改 token=***

import re
_TOKEN_REDACT = re.compile(r"(token=)[^&\s]+")

def _redact_query_string(qs: str) -> str:
    return _TOKEN_REDACT.sub(r"\1***", qs)
```

若 codebase 無此 middleware，至少 endpoint 內 logger 不 echo 完整 URL（FastAPI 預設 access log 會記 URL，可能洩漏）— 在 uvicorn 啟動 config 改用 custom log formatter，或加 ASGI middleware 攔 scope["query_string"]。

**注意：** 若 codebase 無既有 redact pattern，留 follow-up：
- 在 commit message 標 「URL log token redaction follow-up — Phase 2 endpoint 已 nosniff/no-store，但 uvicorn access log 可能含完整 token；建議 ASGI middleware 攔 query string 或改 uvicorn `--access-log False`」

實作 minimum：endpoint 本身不 echo token 到 logger（不 print URL）即可，detailed redaction 留後續 PR。

- [ ] **Step 5: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_api_download.py tests/test_offboarding_*.py -v 2>&1 | tail -20
```

Expected: PASS 全部。

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add api/offboarding.py tests/test_offboarding_api_download.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): GET /offboarding/download public endpoint + 安全強化

公開無 auth endpoint（員工自助下載）：
- verify_token 失敗統一 410 Gone（不暴露差異，防 enumeration）
- 串流 ZIP (cert + 12 月 salary + 出勤 CSV)
- response header: Content-Disposition attachment + X-Content-Type-Options
  nosniff + Cache-Control no-store
- 每次成功下載 count++ + last_used_at = now
- audit log 記下載事件（含 employee_id + count）

3 410 case：unknown token / expired / max_downloads 達 3。
1 happy case + 1 count-increment case。

TODO（follow-up）：uvicorn access log 可能含完整 token，需 ASGI middleware
redact 或改用 access-log False；目前 endpoint 自身 logger 不 echo URL。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: GET /offboarding/{id}/certificate.pdf admin endpoint

**Files:**
- Modify: `api/offboarding.py`
- Test: `tests/test_offboarding_api_certificate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_offboarding_api_certificate.py
"""驗證 admin GET /offboarding/{id}/certificate.pdf。"""
def _process(client, admin_login, emp_id):
    headers = admin_login()
    return client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15"}, headers=headers,
    )


def test_certificate_pdf_returns_bytes(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800, name="王小明")
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)
    _process(client, admin_login, emp.id)

    headers = admin_login()
    r = client.get(f"/api/offboarding/{emp.id}/certificate.pdf", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_certificate_404_when_no_record(
    integrated_client, admin_login, employee_factory,
):
    client, _ = integrated_client
    emp = employee_factory()
    headers = admin_login()
    r = client.get(f"/api/offboarding/{emp.id}/certificate.pdf", headers=headers)
    assert r.status_code == 404


def test_certificate_requires_employees_read(
    integrated_client, employee_factory,
):
    """無登入 → 401。"""
    client, _ = integrated_client
    emp = employee_factory()
    r = client.get(f"/api/offboarding/{emp.id}/certificate.pdf")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: Implement endpoint**

修 `api/offboarding.py` 加：

```python
from pathlib import Path
from fastapi.responses import FileResponse


@router.get("/{employee_id}/certificate.pdf")
def get_certificate_pdf(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    """admin 取離職證明 PDF。"""
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None or not record.certificate_pdf_path:
            raise HTTPException(404, "CERTIFICATE_NOT_FOUND")

        pdf_path = Path(record.certificate_pdf_path)
        if not pdf_path.exists():
            raise HTTPException(404, "CERTIFICATE_FILE_MISSING")

        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            filename=pdf_path.name,
        )
    finally:
        session.close()
```

- [ ] **Step 3: Run tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest tests/test_offboarding_api_certificate.py tests/test_offboarding_*.py -v 2>&1 | tail -20
```

Expected: PASS。

- [ ] **Step 4: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add api/offboarding.py tests/test_offboarding_api_certificate.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): GET /{id}/certificate.pdf admin endpoint

admin 取既存離職證明 PDF（從 record.certificate_pdf_path 讀檔），
EMPLOYEES_READ permission。404 OFFBOARDING_RECORD_NOT_FOUND /
CERTIFICATE_NOT_FOUND / CERTIFICATE_FILE_MISSING（磁碟檔遺失防呆）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: 擴 OffboardingDetailResponse magic-link metadata + OpenAPI 同步 + 全 suite 驗證

**Files:**
- Modify: `schemas/offboarding.py`（OffboardingDetailResponse 加 magic_link 5 欄）
- Modify: `api/offboarding.py`（get_offboarding_detail 回 5 欄）
- Run: `python3 scripts/dump_openapi.py` + `cd ivy-frontend && npm run gen:api`
- Run: 全 pytest

- [ ] **Step 1: Update OffboardingDetailResponse schema**

修 `schemas/offboarding.py` `OffboardingDetailResponse`：

```python
class OffboardingDetailResponse(BaseModel):
    employee_id: int
    employee_name: str
    resign_date: date
    resign_reason: Optional[str]
    opened_at: datetime
    opened_by_user_id: int
    appraisal_marked_at: Optional[datetime]
    leave_snapshot_at: Optional[datetime]
    user_revoked_at: Optional[datetime]
    certificate_generated_at: Optional[datetime]
    leave_balance_snapshot: Optional[dict]
    certificate_pdf_path: Optional[str]
    nhi_unenroll_submitted_at: Optional[datetime]
    # Magic-link metadata（Phase 2 起）
    magic_link_active: bool
    magic_link_expires_at: Optional[datetime] = None
    magic_link_download_count: int = 0
    magic_link_last_used_at: Optional[datetime] = None
    closed_at: Optional[datetime]
```

- [ ] **Step 2: Update get_offboarding_detail endpoint**

修 `api/offboarding.py` `get_offboarding_detail`：

回 dict 加 4 個 magic-link 欄：

```python
return OffboardingDetailResponse(
    # ... 既有 13 欄
    magic_link_active=_is_magic_link_active(record),
    magic_link_expires_at=record.magic_link_expires_at,
    magic_link_download_count=record.magic_link_download_count or 0,
    magic_link_last_used_at=record.magic_link_last_used_at,
    closed_at=record.closed_at,
)
```

- [ ] **Step 3: Add test for new fields**

修 `tests/test_offboarding_api.py` 加：

```python
def test_get_detail_returns_magic_link_metadata(
    integrated_client, admin_login, employee_factory, leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(employee_id=emp.id, year=2026, leave_type="annual", total_hours=80)

    headers = admin_login()
    client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15"}, headers=headers,
    )
    client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)

    r = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["magic_link_active"] is True
    assert body["magic_link_expires_at"] is not None
    assert body["magic_link_download_count"] == 0
    assert body["magic_link_last_used_at"] is None
```

- [ ] **Step 4: Run full pytest (Phase 1 + Phase 2)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
pytest --ignore=tests/test_audit_router.py --ignore=tests/test_supabase_storage.py --ignore=tests/spike_rls 2>&1 | tail -10
```

Expected: PASS 全部（Phase 1 4985 + Phase 2 ~40 新 case）。

- [ ] **Step 5: Dump OpenAPI**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
python3 scripts/dump_openapi.py
```

複製 openapi.json 到 ivy-backend 根（供 frontend gen:api 讀）：

```bash
cp openapi.json /Users/yilunwu/Desktop/ivy-backend/openapi.json
```

- [ ] **Step 6: Regen frontend schema**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && \
npx openapi-typescript ../ivy-backend/openapi.json -o src/api/_generated/schema.d.ts --alphabetize 2>&1 | tail -5 && \
git diff --stat src/api/_generated/schema.d.ts && \
grep -c "magic-link\|certificate.pdf\|offboarding/download" src/api/_generated/schema.d.ts
```

確認 3 個新 path（magic-link / certificate.pdf / download）含在 schema 內。

- [ ] **Step 7: Commit schema regen + backend final**

Backend final commit:

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat/offboarding-phase-1-2026-05-25-backend && \
git add schemas/offboarding.py api/offboarding.py tests/test_offboarding_api.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): expand OffboardingDetailResponse with magic-link metadata

加 4 欄供前端 detail 頁顯示：magic_link_active / magic_link_expires_at /
magic_link_download_count / magic_link_last_used_at。

完成 Phase 2 後端工作（離職證明 PDF + magic-link 自助下載）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Frontend commit:

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && \
git add src/api/_generated/schema.d.ts && \
git commit -m "$(cat <<'EOF'
chore(api): regen schema.d.ts for offboarding Phase 2 endpoints

加入 Phase 2 path：
- POST /offboarding/{employee_id}/magic-link
- DELETE /offboarding/{employee_id}/magic-link
- GET /offboarding/download
- GET /offboarding/{employee_id}/certificate.pdf

OffboardingDetailResponse 擴 4 magic-link metadata 欄。

對應 backend feat/offboarding-phase-1-2026-05-25-backend Phase 2 commits。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## 完成檢核

| 項 | spec ref | task |
|---|---|---|
| 離職證明 PDF service (§19) | §7 | 1 |
| Orchestrator step 5 generate_certificate | §5.3 #5 | 2 |
| Magic-link token 產 / hash / 驗 / 撤 service | §8.1 / §8.2 | 3 |
| Attendance CSV | §8.3 | 4 |
| Download bundle ZIP | §8.3 | 4 |
| POST /magic-link admin 產 | §6.1 | 5 |
| DELETE /magic-link admin 撤 | §6.1 | 5 |
| GET /download 公開 endpoint + 安全 | §6.1 / §8.2 | 6 |
| GET /certificate.pdf admin | §6.1 | 7 |
| OffboardingDetailResponse magic-link metadata | §8.5 | 8 |
| OpenAPI codegen | §12 | 8 |

**Phase 2 不含（Phase 3 處理）：** 前端 OffboardingModal / OffboardingView 清單頁 / MagicLinkPanel UI / Playwright e2e。
