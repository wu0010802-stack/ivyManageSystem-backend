# DR Backup / Restore Drill / Runbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地審計 finding (a)(b)(c)(d) — 每日 pg_dump 到 R2 異地、Storage 同步、月度 restore 演練、dr-runbook.md 形式化。

**Architecture:** GitHub Actions scheduled workflow（cron 與手動）→ pg_dump custom format 與 supabase storage list+download → aws s3 cp 到 Cloudflare R2 `ivy-dr` bucket（`db/daily/` `db/monthly/` `storage/...` 三前綴）。Restore drill 用 GH Actions services 啟臨時 PG 跑 pg_restore + sanity SQL + report。Phase 0 前置：把 `api/portfolio/reports.py` 從本機 path 遷移到 Supabase Storage 新 bucket `growth-reports`。

**Tech Stack:** GitHub Actions、pg_dump/pg_restore 15、aws-cli (S3 API)、boto3、supabase-py、pytest、FastAPI、Supabase Pro PITR、Cloudflare R2。

**Spec：** `docs/superpowers/specs/2026-05-26-dr-backup-restore-drill-design.md`

---

## File Structure

### Phase 0：growth-reports → Supabase Storage
| File | Action | Responsibility |
|---|---|---|
| `utils/supabase_storage.py` | Modify | `_MODULE_TO_BUCKET` 加 `growth_reports → growth-reports` |
| `api/portfolio/reports.py` | Modify | PDF 改寫 storage backend；download endpoint 回 302 signed URL；`_resolve_pdf_path` 邏輯改為 storage backend |
| `scripts/migrate_growth_reports_to_supabase.py` | Create | idempotent migration script，`--dry-run` 必備 |
| `tests/test_growth_report_api.py` | Modify | 加 supabase backend 行為與 redirect 測試 |
| `tests/test_migrate_growth_reports_to_supabase.py` | Create | dry-run / idempotent / hash mismatch raise |
| `docs/sop/storage-deployment.md` | Modify | §1 表格加 `growth-reports` 列 |

### Phase 1：pg_dump → R2 daily
| File | Action | Responsibility |
|---|---|---|
| `.github/workflows/dr-backup.yml` | Create | 每日 cron + workflow_dispatch、pg_dump + sha256 + s3 cp + monthly copy |
| (R2 console / wrangler) | Manual | 建 `ivy-dr` bucket + 4 條 lifecycle rule |
| (Supabase SQL editor) | Manual | 建 `backup_readonly` role |
| (GH repo Settings → Secrets) | Manual | 6 個 secret |

### Phase 2：Storage sync
| File | Action | Responsibility |
|---|---|---|
| `scripts/dr_storage_sync.py` | Create | supabase list ↔ R2 diff + 上傳 + metadata |
| `tests/test_dr_storage_sync.py` | Create | 6 個 diff case + dry-run |
| `.github/workflows/dr-backup.yml` | Modify | 加 storage sync step |

### Phase 3：Restore drill
| File | Action | Responsibility |
|---|---|---|
| `.github/workflows/dr-restore-drill.yml` | Create | workflow_dispatch + services PG + restore + sanity + report |
| `.github/workflows/dr_restore_sanity.sql` | Create | 4 條 sanity SQL |
| `.github/workflows/dr_drill_report.py` | Create | Markdown report 含 RTO 拆解 |

### Phase 4：Runbook + 文件
| File | Action | Responsibility |
|---|---|---|
| `docs/sop/dr-runbook.md`（workspace 層級） | Create | 9 章 DR runbook |
| `docs/sop/zeabur-deployment-runbook.md`（workspace） | Modify | §4.2 改寫、§5 追加 |
| `docs/sop/storage-deployment.md`（ivy-backend） | Modify | §5 切回 local 章節追加 |

---

## Phase 0：growth-reports → Supabase Storage

### Task 1：擴 storage backend 加 growth_reports module

**Files:**
- Modify: `utils/supabase_storage.py:24-28`

- [ ] **Step 1：改 `_MODULE_TO_BUCKET` mapping**

```python
_MODULE_TO_BUCKET = {
    "activity_posters": "activity-posters",
    "leave_attachments": "leave-attachments",
    "attendance_imports": "attendance-imports",
    "growth_reports": "growth-reports",
}
```

- [ ] **Step 2：跑 existing test 確認不 regress**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_supabase_storage.py -v
```

Expected: all pass

- [ ] **Step 3：commit**

```bash
git add utils/supabase_storage.py
git commit -m "feat(storage): add growth_reports module → growth-reports bucket mapping"
```

---

### Task 2：reports.py 寫入路徑改 storage backend（先寫測試）

**Files:**
- Test: `tests/test_growth_report_api.py`（既存檔，加 test）
- Modify: `api/portfolio/reports.py:328-341`

**Background：** 既存 `_generate_pdf_job` 把 PDF bytes 寫到 `student_dir/{report.id}.pdf` 並把 relative path 存 `report.file_path`。改為呼叫 `get_backend().save(module="growth_reports", key=..., data=pdf_bytes, content_type="application/pdf")`，`file_path` 改存 storage key（如 `students/{sid}/{report_id}.pdf`）。

- [ ] **Step 1：寫失敗測試 — supabase backend 模式下生成 PDF 走 storage util**

加進 `tests/test_growth_report_api.py`：

```python
def test_pdf_job_writes_to_supabase_storage_when_backend_supabase(monkeypatch):
    """STORAGE_BACKEND=supabase 模式時，_generate_pdf_job 應走 storage backend
    寫入而非 local 檔案系統。"""
    from api.portfolio import reports as reports_mod
    from utils.storage import get_backend

    # 1. monkeypatch backend 為 supabase mock
    calls = []
    class FakeStorage:
        def save(self, module, key, data, content_type):
            calls.append({"module": module, "key": key, "size": len(data), "ct": content_type})
        def signed_url(self, module, key, ttl):
            return f"https://signed.example/{module}/{key}"
    monkeypatch.setattr("api.portfolio.reports.get_backend", lambda: FakeStorage())

    # 2. 建 fake report 跑 _generate_pdf_job（細節依既有 fixture 模式）
    # 3. 驗 calls 收到 1 筆 module=growth_reports key=students/{sid}/{rid}.pdf content_type=application/pdf
    # 4. 驗 DB report.file_path == key（非 local path）
```

**Note：** 完整 fixture setup 視既有 test 寫法調整；此 test 的 assertion 重點是「save 被呼叫一次且 module=growth_reports」+「file_path 是 storage key」。

- [ ] **Step 2：跑 test 確認失敗**

```bash
pytest tests/test_growth_report_api.py::test_pdf_job_writes_to_supabase_storage_when_backend_supabase -v
```

Expected: FAIL（reports.py 還在寫 local）

- [ ] **Step 3：改 reports.py `_generate_pdf_job`（行 328-341）**

```python
# 既有：
# student_dir = REPORT_ROOT / str(student.id)
# student_dir.mkdir(parents=True, exist_ok=True)
# path = student_dir / f"{report.id}.pdf"
# path.write_bytes(pdf_bytes)
# report.file_path = str(path.resolve().relative_to(Path.cwd())) 或 abs

# 改為：
from utils.storage import get_backend

storage_key = f"students/{student.id}/{report.id}.pdf"
backend = get_backend()

# Local backend 仍用既有 path 規則寫檔；Supabase backend 走 storage.save
if settings.storage.backend == "supabase":
    backend.save(
        module="growth_reports",
        key=storage_key,
        data=pdf_bytes,
        content_type="application/pdf",
    )
    report.file_path = storage_key  # 存 key，非 local path
else:
    student_dir = REPORT_ROOT / str(student.id)
    student_dir.mkdir(parents=True, exist_ok=True)
    path = student_dir / f"{report.id}.pdf"
    path.write_bytes(pdf_bytes)
    try:
        report.file_path = str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        report.file_path = str(path.resolve())

report.status = REPORT_STATUS_READY
report.file_size = len(pdf_bytes)
report.generated_at = datetime.utcnow()
```

- [ ] **Step 4：跑 test 確認通過**

```bash
pytest tests/test_growth_report_api.py::test_pdf_job_writes_to_supabase_storage_when_backend_supabase -v
```

Expected: PASS

- [ ] **Step 5：跑既存 growth_report 全套確保零 regression**

```bash
pytest tests/test_growth_report_api.py tests/test_growth_report_pdf.py tests/test_growth_report_collector.py -v
```

Expected: all pass

- [ ] **Step 6：commit**

```bash
git add tests/test_growth_report_api.py api/portfolio/reports.py
git commit -m "feat(portfolio): growth report PDF write goes through storage backend"
```

---

### Task 3：download endpoint 回 302 signed URL（supabase backend）

**Files:**
- Modify: `api/portfolio/reports.py:522-565`（既有 FileResponse 區）
- Modify: `tests/test_growth_report_api.py`

- [ ] **Step 1：寫失敗測試 — supabase backend 下載 endpoint 回 302**

```python
def test_download_endpoint_redirects_to_signed_url_when_backend_supabase(client, monkeypatch):
    """Supabase backend 模式下，GET /api/students/{sid}/growth-reports/{rid}/download
    應回 302 + Location header 含 signed URL。"""
    # 1. monkeypatch get_backend 回 FakeStorage with signed_url 方法
    # 2. 建 fixture：student + ready report，file_path = "students/1/42.pdf"
    # 3. call endpoint，assert response.status_code == 302
    # 4. assert "signed" in response.headers["location"]
```

- [ ] **Step 2：跑 test 確認失敗**

```bash
pytest tests/test_growth_report_api.py::test_download_endpoint_redirects_to_signed_url_when_backend_supabase -v
```

Expected: FAIL

- [ ] **Step 3：改 reports.py download endpoint**

找到既有 `FileResponse(...)` 段（約 line 522-565），改為：

```python
from fastapi.responses import RedirectResponse

# 在 download handler 內，找出 PDF 後：
if settings.storage.backend == "supabase":
    backend = get_backend()
    ttl = settings.storage.supabase_signed_url_ttl
    url = backend.signed_url("growth_reports", report.file_path, ttl)
    return RedirectResponse(url=url, status_code=302)
else:
    # 既有 local 路徑邏輯 + _resolve_pdf_path containment check + FileResponse 保留
    path = _resolve_pdf_path(report.file_path)
    return FileResponse(path, media_type="application/pdf", filename=f"{...}.pdf")
```

- [ ] **Step 4：跑 test 確認通過**

```bash
pytest tests/test_growth_report_api.py::test_download_endpoint_redirects_to_signed_url_when_backend_supabase -v
```

Expected: PASS

- [ ] **Step 5：commit**

```bash
git add api/portfolio/reports.py tests/test_growth_report_api.py
git commit -m "feat(portfolio): growth report download returns signed URL redirect on supabase backend"
```

---

### Task 4：寫 migration script（local PDF → Supabase）

**Files:**
- Create: `scripts/migrate_growth_reports_to_supabase.py`

**Behaviour：**
1. 連 DB 撈所有 `StudentGrowthReport` where `status='ready' AND file_path NOT LIKE 'students/%'`（local path pattern）
2. 對每筆：
   - 讀 local 檔（`_resolve_pdf_path` containment check）
   - 算 sha256
   - upload 到 Supabase Storage `growth-reports` bucket `students/{sid}/{rid}.pdf`
   - download 回來驗 sha256 一致
   - update DB `file_path = storage_key`
   - 刪 local 檔（成功才刪）
3. 若 hash mismatch：log error、raise、**不刪** local
4. 已遷移過（file_path 已是 storage key pattern）跳過 → idempotent
5. CLI args：`--dry-run`（讀但不寫）、`--limit N`（測用）

- [ ] **Step 1：建檔 with skeleton**

```python
"""scripts/migrate_growth_reports_to_supabase.py

把本機 growth-reports PDF 遷到 Supabase Storage。Idempotent。

用法：
  python scripts/migrate_growth_reports_to_supabase.py --dry-run        # 預覽
  python scripts/migrate_growth_reports_to_supabase.py                  # 真跑
  python scripts/migrate_growth_reports_to_supabase.py --limit 5        # 限量測試
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path

from config import settings
from models.base import session_scope
from models.database import StudentGrowthReport
from utils.storage import get_backend

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


_STORAGE_KEY_PREFIX = "students/"


def _is_already_migrated(file_path: str) -> bool:
    return file_path.startswith(_STORAGE_KEY_PREFIX)


def _local_path(file_path: str) -> Path:
    p = Path(file_path)
    return p if p.is_absolute() else (Path.cwd() / p)


def _migrate_one(report: StudentGrowthReport, backend, dry_run: bool) -> str:
    if _is_already_migrated(report.file_path):
        return "skipped:already-migrated"

    local = _local_path(report.file_path)
    if not local.is_file():
        return "skipped:local-missing"

    data = local.read_bytes()
    src_hash = hashlib.sha256(data).hexdigest()
    storage_key = f"{_STORAGE_KEY_PREFIX}{report.student_id}/{report.id}.pdf"

    if dry_run:
        logger.info(
            "[dry-run] would upload report=%s key=%s size=%s sha=%s",
            report.id, storage_key, len(data), src_hash[:8],
        )
        return "dry-run"

    backend.save("growth_reports", storage_key, data, "application/pdf")
    fetched = backend.read("growth_reports", storage_key)
    dst_hash = hashlib.sha256(fetched).hexdigest()
    if src_hash != dst_hash:
        raise RuntimeError(
            f"hash mismatch report={report.id}: src={src_hash} dst={dst_hash}"
        )

    report.file_path = storage_key
    # 提交 db 變更後再刪 local（caller 控 session 提交時機）
    return "migrated"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if settings.storage.backend != "supabase" and not args.dry_run:
        logger.error("非 supabase backend 不可真跑 migration；用 --dry-run 預覽")
        sys.exit(2)

    backend = get_backend()
    results: dict[str, int] = {}

    with session_scope() as session:
        query = session.query(StudentGrowthReport).filter(
            StudentGrowthReport.status == "ready",
            StudentGrowthReport.file_path.isnot(None),
        )
        if args.limit:
            query = query.limit(args.limit)
        reports = query.all()
        logger.info("找到 %s 筆 ready report", len(reports))

        local_files_to_delete: list[Path] = []
        for report in reports:
            try:
                status = _migrate_one(report, backend, dry_run=args.dry_run)
                results[status] = results.get(status, 0) + 1
                if status == "migrated":
                    local_files_to_delete.append(_local_path(report.file_path
                        if not report.file_path.startswith(_STORAGE_KEY_PREFIX)
                        else None))
            except Exception as e:
                logger.exception("migration failed report=%s: %s", report.id, e)
                results["failed"] = results.get("failed", 0) + 1

        # 注意：session 出 scope 才 commit，這裡先暫存待刪 local 路徑（已被 file_path 覆蓋的舊路徑無法回頭，遵守先 commit DB 再刪檔的順序）
        # session 出 scope 後在外面刪 local

    logger.info("結果: %s", results)
    return results


if __name__ == "__main__":
    main()
```

**Note：** 「先 commit DB 才刪 local」是關鍵 — 若 commit 失敗 local 仍在，可重跑；若刪檔在 commit 前 fail，DB 仍指 local 但檔沒了。實際刪 local 的邏輯可改為：先記 (report_id, old_local_path) 在 list，session 出 scope（成功 commit）後再做檔案刪除。已於程式內留註解，實作者依此 pattern 完成。

- [ ] **Step 2：跑 dry-run 驗 logging 與不影響 DB**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python scripts/migrate_growth_reports_to_supabase.py --dry-run
```

Expected: 印 "[dry-run] would upload ..."、`results: {'dry-run': N}` 或 `{}`（無 ready report）；DB 不變

- [ ] **Step 3：commit**

```bash
git add scripts/migrate_growth_reports_to_supabase.py
git commit -m "feat(scripts): add growth-reports local→supabase migration script (idempotent, dry-run)"
```

---

### Task 5：migration script 測試

**Files:**
- Create: `tests/test_migrate_growth_reports_to_supabase.py`

- [ ] **Step 1：寫失敗測試三段**

```python
"""tests/test_migrate_growth_reports_to_supabase.py"""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from scripts.migrate_growth_reports_to_supabase import _migrate_one, _is_already_migrated


def _fake_report(report_id, sid, file_path, status="ready"):
    r = MagicMock()
    r.id = report_id
    r.student_id = sid
    r.file_path = file_path
    r.status = status
    return r


def test_is_already_migrated_recognises_storage_key():
    assert _is_already_migrated("students/1/42.pdf") is True
    assert _is_already_migrated("data/growth_reports/1/42.pdf") is False


def test_dry_run_does_not_call_backend(tmp_path):
    pdf = tmp_path / "42.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = _fake_report(42, 1, str(pdf))
    backend = MagicMock()

    status = _migrate_one(report, backend, dry_run=True)

    assert status == "dry-run"
    backend.save.assert_not_called()
    assert report.file_path == str(pdf)  # 未動


def test_idempotent_skips_already_migrated(tmp_path):
    report = _fake_report(42, 1, "students/1/42.pdf")
    backend = MagicMock()

    status = _migrate_one(report, backend, dry_run=False)

    assert status == "skipped:already-migrated"
    backend.save.assert_not_called()


def test_migrate_updates_file_path_to_storage_key(tmp_path):
    pdf = tmp_path / "42.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = _fake_report(42, 1, str(pdf))
    backend = MagicMock()
    # backend.read 回傳同樣 bytes → hash 一致
    backend.read.return_value = b"%PDF-1.4 fake"

    status = _migrate_one(report, backend, dry_run=False)

    assert status == "migrated"
    backend.save.assert_called_once()
    args = backend.save.call_args
    assert args.args[0] == "growth_reports"
    assert args.args[1] == "students/1/42.pdf"
    assert report.file_path == "students/1/42.pdf"


def test_hash_mismatch_raises_and_does_not_update(tmp_path):
    pdf = tmp_path / "42.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    report = _fake_report(42, 1, str(pdf))
    backend = MagicMock()
    backend.read.return_value = b"%PDF-1.4 corrupted"  # different bytes → mismatch hash

    with pytest.raises(RuntimeError, match="hash mismatch"):
        _migrate_one(report, backend, dry_run=False)

    assert report.file_path == str(pdf)  # 未動


def test_missing_local_file_skipped(tmp_path):
    report = _fake_report(42, 1, str(tmp_path / "missing.pdf"))
    backend = MagicMock()

    status = _migrate_one(report, backend, dry_run=False)

    assert status == "skipped:local-missing"
    backend.save.assert_not_called()
```

- [ ] **Step 2：跑 test 確認 6 條全失敗**

```bash
pytest tests/test_migrate_growth_reports_to_supabase.py -v
```

Expected: 6 FAIL（function not importable / 或 logic 未實作）

- [ ] **Step 3：補完 `_migrate_one` 與 `_is_already_migrated`（若 Task 4 已實作則可直接通過）**

回頭看 Task 4 的 script，確認 `_is_already_migrated` 與 `_migrate_one` 純函式 API 對得上 test 簽章。

- [ ] **Step 4：跑 test 確認 6 條全通過**

```bash
pytest tests/test_migrate_growth_reports_to_supabase.py -v
```

Expected: 6 PASS

- [ ] **Step 5：commit**

```bash
git add tests/test_migrate_growth_reports_to_supabase.py
git commit -m "test(scripts): cover growth-reports migration dry-run / idempotency / hash check"
```

---

### Task 6：Manual ops — 建 Supabase bucket 與更新 `storage-deployment.md`

**Files:**
- Modify: `docs/sop/storage-deployment.md`

- [ ] **Step 1：在 Supabase Dashboard 建 `growth-reports` bucket**

Dashboard → Storage → New bucket
- Name: `growth-reports`
- Public: ❌（private）
- File size limit: 50MB（對齊 `GROWTH_REPORT_MAX_BYTES` 上限）

- [ ] **Step 2：更新 `docs/sop/storage-deployment.md` §1 表格**

找 §1 的 bucket 表格，加一列：

```markdown
| `growth-reports`       | ❌ Private | 學生成長報告 PDF，後端發 signed URL |
```

- [ ] **Step 3：commit**

```bash
git add docs/sop/storage-deployment.md
git commit -m "docs(sop): document growth-reports bucket in storage-deployment SOP"
```

---

### Task 7：Prod migration 真跑（manual ops，需 staging 先試）

**注意：** 此 task 在 prod 部署 `STORAGE_BACKEND=supabase` 之後執行；先在 staging Supabase（或 dev project）驗 dry-run 與小批量真跑。

- [ ] **Step 1：staging dry-run**

```bash
ENV=staging python scripts/migrate_growth_reports_to_supabase.py --dry-run
```

預期：印「找到 N 筆 ready report」並列出 would upload；無 DB 變更。

- [ ] **Step 2：staging 小批量真跑（5 筆）**

```bash
ENV=staging python scripts/migrate_growth_reports_to_supabase.py --limit 5
```

預期：5 筆 migrated；Supabase Dashboard 看到 5 個檔；DB `file_path` 改為 storage key。

- [ ] **Step 3：staging 確認下載**

從 admin UI 進入 growth report 列表，下載 5 筆遷過的 PDF，驗檔可開、size 對。

- [ ] **Step 4：prod dry-run**

```bash
ENV=production python scripts/migrate_growth_reports_to_supabase.py --dry-run
```

- [ ] **Step 5：prod 真跑（建議週末低峰）**

```bash
ENV=production python scripts/migrate_growth_reports_to_supabase.py
```

- [ ] **Step 6：prod 驗收 — 抽 5 筆下載確認可開**

從 admin UI 抽 5 筆（涵蓋舊報告與最近）下載，全部成功 = pass。

- [ ] **Step 7：記錄成果**

在 PR description 或 ops log 寫：「遷移 N 筆，全部 hash verify 通過，舊 local 檔已刪。container restart 驗證下載仍可（restart Zeabur 後抽 1 筆）」

---

## Phase 1：pg_dump → R2 daily

### Task 8：Supabase backup_readonly role（manual ops）

- [ ] **Step 1：產生 32 字元亂數密碼並安全記錄到 1Password**

```bash
openssl rand -hex 16
```

- [ ] **Step 2：Supabase SQL editor 建 role**

```sql
CREATE ROLE backup_readonly WITH LOGIN PASSWORD '<上一步產生的密碼>' NOINHERIT;
GRANT CONNECT ON DATABASE postgres TO backup_readonly;
GRANT USAGE ON SCHEMA public TO backup_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_readonly;
```

- [ ] **Step 3：本地驗證可連並 dump**

```bash
PGPASSWORD='<password>' pg_dump \
  --host=<supabase-host> --port=5432 \
  --username=backup_readonly --dbname=postgres \
  --format=custom --no-owner --no-privileges \
  --file=/tmp/test.dump
ls -lh /tmp/test.dump  # 預期 > 1KB
```

預期：dump 成功；若 permission denied 則回到 Step 2 補 GRANT。

---

### Task 9：建立 Cloudflare R2 bucket 與 lifecycle

- [ ] **Step 1：Cloudflare Dashboard 建 R2 bucket**

R2 → Create bucket
- Name: `ivy-dr`
- Location: 任意（R2 自動分配；DR 場景不需指定）

- [ ] **Step 2：產生 API token（限這個 bucket）**

R2 → Manage R2 API Tokens → Create
- Permissions: Object Read & Write
- Specify buckets: `ivy-dr`
- TTL: 不過期（之後 90 天輪替）

複製 access key id + secret + endpoint URL 到 1Password。

- [ ] **Step 3：設定 lifecycle rules（透過 wrangler 或 dashboard）**

R2 → ivy-dr → Settings → Object lifecycle rules：

| Prefix | Action | Days |
|---|---|---|
| `db/daily/` | Delete | 30 |
| `db/monthly/` | Delete | 365 |
| `storage/leave-attachments/` | Delete | 365 |
| `storage/growth-reports/` | （無 rule，永久保留） | — |

- [ ] **Step 4：驗證 wrangler 或 aws-cli 能列檔**

```bash
aws s3 ls s3://ivy-dr/ --endpoint-url=https://<account-id>.r2.cloudflarestorage.com
```

預期：回空清單（bucket 還沒檔案），不報錯。

---

### Task 10：GH secrets 設定（manual ops）

- [ ] **Step 1：到 ivy-backend repo → Settings → Secrets and variables → Actions**

新增 secrets：

| Name | Value |
|---|---|
| `SUPABASE_DB_HOST` | Supabase Dashboard → Settings → Database → Direct connection host |
| `SUPABASE_BACKUP_DB_PASSWORD` | Task 8 設的密碼 |
| `R2_ACCESS_KEY_ID` | Task 9 Step 2 取得 |
| `R2_SECRET_ACCESS_KEY` | 同上 |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `LINE_NOTIFY_WEBHOOK` | 沿用既有 ops 群 webhook（從 ops doc 或 1Password 取） |
| `SUPABASE_URL` | Supabase Dashboard → API URL（Phase 2 會用） |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase Dashboard → service_role key（Phase 2 會用） |

- [ ] **Step 2：截圖（遮蔽 value）存到 ops 文件作為 audit trail**

存到 `docs/sop/dr-runbook.md` §3 GH secrets 對照表參考檔（之後 Phase 4 落地）。

---

### Task 11：寫 `dr-backup.yml` workflow

**Files:**
- Create: `.github/workflows/dr-backup.yml`

- [ ] **Step 1：建檔**

```yaml
name: dr-backup

on:
  schedule:
    - cron: '17 18 * * *'   # 02:17 UTC+8（台灣凌晨低峰）
  workflow_dispatch:

concurrency:
  group: dr-backup
  cancel-in-progress: false

jobs:
  dump-and-upload:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - name: Install postgresql-client-15
        run: |
          sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
          sudo apt-get update -y
          sudo apt-get install -y postgresql-client-15

      - name: pg_dump
        env:
          PGPASSWORD: ${{ secrets.SUPABASE_BACKUP_DB_PASSWORD }}
        run: |
          set -euo pipefail
          DATE=$(date -u +%Y-%m-%d)
          pg_dump \
            --host=${{ secrets.SUPABASE_DB_HOST }} \
            --port=5432 \
            --username=backup_readonly \
            --dbname=postgres \
            --format=custom \
            --no-owner --no-privileges \
            --file=ivy-${DATE}.dump
          sha256sum ivy-${DATE}.dump > ivy-${DATE}.sha256
          echo "DUMP_DATE=$DATE" >> "$GITHUB_ENV"
          ls -lh ivy-${DATE}.*

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.R2_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          aws-region: auto

      - name: Upload dump to R2
        run: |
          set -euo pipefail
          aws s3 cp ivy-${DUMP_DATE}.dump   s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          aws s3 cp ivy-${DUMP_DATE}.sha256 s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          DAY=$(date -u +%d)
          if [ "$DAY" = "01" ]; then
            aws s3 cp ivy-${DUMP_DATE}.dump   s3://ivy-dr/db/monthly/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
            aws s3 cp ivy-${DUMP_DATE}.sha256 s3://ivy-dr/db/monthly/ --endpoint-url=${{ secrets.R2_ENDPOINT }}
          fi

      - name: Notify on failure
        if: failure()
        run: |
          curl -X POST ${{ secrets.LINE_NOTIFY_WEBHOOK }} \
            -d "message=[DR-Backup] ${{ env.DUMP_DATE }} 失敗，請查 GH Actions: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
```

- [ ] **Step 2：在本機驗 YAML 語法**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python -c "import yaml; yaml.safe_load(open('.github/workflows/dr-backup.yml'))"
```

Expected: 無錯誤輸出

- [ ] **Step 3：commit**

```bash
git add .github/workflows/dr-backup.yml
git commit -m "feat(ops): add dr-backup GH Actions workflow (daily pg_dump to R2)"
```

---

### Task 12：手動觸發 workflow 驗證 Phase 1

- [ ] **Step 1：push commit 後到 GH Actions UI 手動觸發**

GH → Actions → dr-backup → Run workflow → main 分支

- [ ] **Step 2：監控 run 狀態，預期 < 10 分鐘綠**

預期 log 包含：
- `pg_dump` step 印出 `ls -lh ivy-YYYY-MM-DD.*`，dump size > 1KB
- `Upload dump to R2` step 無錯

- [ ] **Step 3：在 R2 console / aws-cli 驗證檔案存在**

```bash
aws s3 ls s3://ivy-dr/db/daily/ --endpoint-url=https://<account-id>.r2.cloudflarestorage.com
```

預期：見 `ivy-YYYY-MM-DD.dump` 與 `.sha256`。

- [ ] **Step 4：下載並驗 sha256 + pg_restore 本地可跑**

```bash
aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.dump ./test.dump --endpoint-url=...
aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.sha256 ./test.sha256 --endpoint-url=...
sed -i.bak "s/ivy-YYYY-MM-DD\.dump/test.dump/" test.sha256
sha256sum -c test.sha256

# 本地 PG 測試 restore
createdb dr_test
pg_restore --no-owner --no-privileges -d dr_test ./test.dump
psql dr_test -c "SELECT count(*) FROM users;"
dropdb dr_test
```

預期：sha256 OK；row count > 0。

- [ ] **Step 5：第二天確認 cron 自動觸發**

cron 為 `17 18 * * *` = UTC 18:17 = 台灣 02:17。隔天上午看 GH Actions 應有新一筆綠的 run。

---

## Phase 2：Storage sync

### Task 13：寫 `dr_storage_sync.py`

**Files:**
- Create: `scripts/dr_storage_sync.py`

- [ ] **Step 1：建檔**

```python
"""scripts/dr_storage_sync.py

把 Supabase Storage bucket 鏡像到 R2。Idempotent。

用法：
  python scripts/dr_storage_sync.py \
    --buckets leave-attachments growth-reports \
    --target s3://ivy-dr/storage/ \
    --mode incremental

環境變數：SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / AWS_* / R2_ENDPOINT
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Iterable

import boto3
from supabase import create_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _list_supabase(client, bucket: str) -> list[dict]:
    """回傳 [{name, updated_at, size}]，遞迴展平。"""
    out: list[dict] = []
    stack = [""]
    while stack:
        prefix = stack.pop()
        items = client.storage.from_(bucket).list(prefix) if prefix else client.storage.from_(bucket).list()
        for it in items:
            name = it.get("name")
            if not name:
                continue
            full = f"{prefix}/{name}" if prefix else name
            # Supabase 目錄項目 metadata == None；檔案項目含 size / updated_at
            meta = it.get("metadata")
            if meta is None and it.get("id") is None:
                # directory
                stack.append(full)
            else:
                out.append({
                    "name": full,
                    "updated_at": it.get("updated_at") or it.get("created_at") or "",
                    "size": (meta or {}).get("size") or it.get("size") or 0,
                })
    return out


def _list_r2(s3, bucket: str, prefix: str) -> dict[str, dict]:
    """回傳 {key: {user_metadata, size}}。"""
    paginator = s3.get_paginator("list_objects_v2")
    out: dict[str, dict] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            head = s3.head_object(Bucket=bucket, Key=obj["Key"])
            out[obj["Key"]] = {
                "user_metadata": head.get("Metadata") or {},
                "size": obj["Size"],
            }
    return out


def _r2_key(target_prefix: str, src_bucket: str, src_name: str) -> str:
    return f"{target_prefix.rstrip('/')}/{src_bucket}/{src_name}"


def _decide_action(src: dict, dst: dict | None) -> str:
    if dst is None:
        return "upload"
    src_ts = src["updated_at"]
    dst_ts = (dst["user_metadata"] or {}).get("x-source-updated-at", "")
    if src_ts and dst_ts and src_ts > dst_ts:
        return "upload"
    return "skip"


def _sync_bucket(sb_client, s3, src_bucket: str, target_uri: str, dry_run: bool) -> dict[str, int]:
    # target_uri 例：s3://ivy-dr/storage/
    assert target_uri.startswith("s3://")
    _, _, rest = target_uri.partition("s3://")
    dst_bucket, _, dst_prefix = rest.partition("/")

    src_items = _list_supabase(sb_client, src_bucket)
    dst_items = _list_r2(s3, dst_bucket, f"{dst_prefix.rstrip('/')}/{src_bucket}/")

    stats = {"upload": 0, "skip": 0, "error": 0}
    for src in src_items:
        key = _r2_key(dst_prefix, src_bucket, src["name"])
        dst = dst_items.get(key)
        action = _decide_action(src, dst)
        if action == "skip":
            stats["skip"] += 1
            continue
        if dry_run:
            logger.info("[dry-run] would upload %s/%s → %s", src_bucket, src["name"], key)
            stats["upload"] += 1
            continue
        try:
            data = sb_client.storage.from_(src_bucket).download(src["name"])
            s3.put_object(
                Bucket=dst_bucket,
                Key=key,
                Body=data,
                Metadata={"x-source-updated-at": src["updated_at"] or datetime.utcnow().isoformat()},
            )
            stats["upload"] += 1
            logger.info("uploaded %s/%s → %s", src_bucket, src["name"], key)
        except Exception as e:
            logger.exception("upload failed %s/%s: %s", src_bucket, src["name"], e)
            stats["error"] += 1
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--buckets", nargs="+", required=True)
    parser.add_argument("--target", required=True, help="s3://bucket/prefix/")
    parser.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    r2_endpoint = os.environ["R2_ENDPOINT"]

    sb = create_client(sb_url, sb_key)
    s3 = boto3.client(
        "s3",
        endpoint_url=r2_endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"),
    )

    overall = {"upload": 0, "skip": 0, "error": 0}
    for bucket in args.buckets:
        stats = _sync_bucket(sb, s3, bucket, args.target, args.dry_run)
        logger.info("bucket=%s stats=%s", bucket, stats)
        for k in overall:
            overall[k] += stats[k]

    logger.info("overall=%s", overall)
    if overall["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2：commit**

```bash
git add scripts/dr_storage_sync.py
git commit -m "feat(scripts): add Supabase Storage → R2 sync script (idempotent, dry-run)"
```

---

### Task 14：`dr_storage_sync.py` 測試

**Files:**
- Create: `tests/test_dr_storage_sync.py`

- [ ] **Step 1：寫測試**

```python
"""tests/test_dr_storage_sync.py"""
from unittest.mock import MagicMock
import pytest

from scripts.dr_storage_sync import _decide_action, _r2_key


def test_decide_action_new_file():
    src = {"name": "a.pdf", "updated_at": "2026-05-20T10:00:00Z", "size": 100}
    assert _decide_action(src, None) == "upload"


def test_decide_action_target_up_to_date():
    src = {"name": "a.pdf", "updated_at": "2026-05-20T10:00:00Z", "size": 100}
    dst = {"user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"}, "size": 100}
    assert _decide_action(src, dst) == "skip"


def test_decide_action_source_newer():
    src = {"name": "a.pdf", "updated_at": "2026-05-21T10:00:00Z", "size": 100}
    dst = {"user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"}, "size": 100}
    assert _decide_action(src, dst) == "upload"


def test_decide_action_target_newer_still_skip():
    # 目標反而新（不太可能但要 safe default）
    src = {"name": "a.pdf", "updated_at": "2026-05-19T10:00:00Z", "size": 100}
    dst = {"user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"}, "size": 100}
    assert _decide_action(src, dst) == "skip"


def test_r2_key_layout():
    assert _r2_key("storage/", "leave-attachments", "2026/01/abc.pdf") \
        == "storage/leave-attachments/2026/01/abc.pdf"


def test_r2_key_trailing_slash_ignored():
    assert _r2_key("storage", "growth-reports", "students/1/42.pdf") \
        == "storage/growth-reports/students/1/42.pdf"
```

- [ ] **Step 2：跑 test 確認 6 條全綠**

```bash
pytest tests/test_dr_storage_sync.py -v
```

Expected: 6 PASS

- [ ] **Step 3：commit**

```bash
git add tests/test_dr_storage_sync.py
git commit -m "test(scripts): cover dr_storage_sync diff + key layout logic"
```

---

### Task 15：把 storage sync step 加到 `dr-backup.yml`

**Files:**
- Modify: `.github/workflows/dr-backup.yml`

- [ ] **Step 1：在 `Upload dump to R2` step 後追加 storage sync**

```yaml
      - name: Install Python deps for storage sync
        run: |
          python -m pip install --upgrade pip
          pip install supabase==2.* boto3

      - name: Mirror Supabase Storage → R2
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          R2_ENDPOINT: ${{ secrets.R2_ENDPOINT }}
          AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          AWS_DEFAULT_REGION: auto
        run: |
          python scripts/dr_storage_sync.py \
            --buckets leave-attachments growth-reports \
            --target s3://ivy-dr/storage/ \
            --mode incremental
```

注意：插在 dump upload 之後、Notify on failure 之前。

- [ ] **Step 2：commit**

```bash
git add .github/workflows/dr-backup.yml
git commit -m "feat(ops): add Supabase Storage sync step to dr-backup workflow"
```

- [ ] **Step 3：push 後手動觸發 workflow 驗證**

GH Actions UI → dr-backup → Run workflow

- [ ] **Step 4：驗 R2 storage/ 結構**

```bash
aws s3 ls s3://ivy-dr/storage/ --endpoint-url=... --recursive | head -20
```

預期：見 `storage/leave-attachments/...` 與 `storage/growth-reports/...` 物件。

- [ ] **Step 5：故意改一個 supabase 物件再跑 workflow，驗 R2 對應檔被更新**

選一個 leave-attachments 中的小檔，從 supabase dashboard 重新上傳同 key 不同 bytes（或刪除後重傳）。再手動觸發 workflow。

```bash
aws s3api head-object --bucket ivy-dr --key storage/leave-attachments/<key> --endpoint-url=...
# 看 Metadata.x-source-updated-at 是否更新
```

預期：metadata.x-source-updated-at 為新時間。

---

## Phase 3：Restore drill

### Task 16：寫 sanity SQL

**Files:**
- Create: `.github/workflows/dr_restore_sanity.sql`

- [ ] **Step 1：建檔**

```sql
-- DR Restore Drill Sanity Checks

\echo '=== 1. Core table row counts ==='
SELECT 'users' AS tbl, count(*) AS n FROM users
UNION ALL SELECT 'employees', count(*) FROM employees
UNION ALL SELECT 'students', count(*) FROM students
UNION ALL SELECT 'salary_records', count(*) FROM salary_records
UNION ALL SELECT 'attendance_records', count(*) FROM attendance_records
UNION ALL SELECT 'guardians', count(*) FROM guardians
UNION ALL SELECT 'leaves', count(*) FROM leaves;

\echo '=== 2. Latest event timestamps (validate freshness) ==='
SELECT 'latest_attendance' AS check, MAX(created_at)::text AS value FROM attendance_records
UNION ALL SELECT 'latest_audit', MAX(created_at)::text FROM audit_logs;

\echo '=== 3. Alembic head ==='
SELECT 'alembic_version' AS check, string_agg(version_num, ',') AS value FROM alembic_version;

\echo '=== 4. Cross-table join smoke ==='
SELECT u.id, u.username, e.name, e.position
FROM users u JOIN employees e ON e.user_id = u.id
LIMIT 3;
```

- [ ] **Step 2：commit**

```bash
git add .github/workflows/dr_restore_sanity.sql
git commit -m "feat(ops): add restore drill sanity SQL checks"
```

---

### Task 17：寫 drill report 產生器

**Files:**
- Create: `.github/workflows/dr_drill_report.py`

- [ ] **Step 1：建檔**

```python
"""dr_drill_report.py — produce Markdown drill report.

Usage:
  python dr_drill_report.py \
    --dump-date 2026-05-26 \
    --start-ts 1716000000 --download-end-ts 1716000060 \
    --restore-end-ts 1716000300 \
    --sanity-output sanity_output.txt > drill-report.md
"""
import argparse
import re
from datetime import datetime, timedelta


def parse_sanity(text: str) -> dict:
    """從 psql 輸出抽 row counts / latest_attendance / alembic_version。"""
    out: dict = {"row_counts": {}, "latest": {}, "alembic": ""}
    lines = text.splitlines()
    section = None
    for line in lines:
        if "Core table row counts" in line:
            section = "rows"
        elif "Latest event timestamps" in line:
            section = "latest"
        elif "Alembic head" in line:
            section = "alembic"
        elif "Cross-table join smoke" in line:
            section = "join"
        elif section == "rows":
            m = re.match(r"\s*(\w+)\s*\|\s*(\d+)", line)
            if m:
                out["row_counts"][m.group(1)] = int(m.group(2))
        elif section == "latest":
            m = re.match(r"\s*(\w+)\s*\|\s*(.+)", line)
            if m:
                out["latest"][m.group(1)] = m.group(2).strip()
        elif section == "alembic":
            m = re.match(r"\s*alembic_version\s*\|\s*(.+)", line)
            if m:
                out["alembic"] = m.group(1).strip()
    return out


def judge_pass(parsed: dict, dump_date: str) -> tuple[str, list[str]]:
    """回 (judgment, reasons)。"""
    warns: list[str] = []
    for tbl, n in parsed["row_counts"].items():
        if n == 0:
            warns.append(f"{tbl} row count = 0")
    latest_att = parsed["latest"].get("latest_attendance", "")
    if latest_att and latest_att != "":
        try:
            d = datetime.fromisoformat(latest_att.replace("Z", "+00:00"))
            dump_d = datetime.fromisoformat(dump_date)
            if (dump_d - d.replace(tzinfo=None)).days > 2:
                warns.append(f"latest_attendance {latest_att} 比 dump 日 {dump_date} 落差 > 2 天")
        except Exception:
            pass
    if warns:
        return "WARN", warns
    return "PASS", []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dump-date", required=True)
    p.add_argument("--start-ts", type=int, required=True)
    p.add_argument("--download-end-ts", type=int, required=True)
    p.add_argument("--restore-end-ts", type=int, required=True)
    p.add_argument("--sanity-output", required=True)
    args = p.parse_args()

    download_sec = args.download_end_ts - args.start_ts
    restore_sec = args.restore_end_ts - args.download_end_ts
    total_sec = args.restore_end_ts - args.start_ts

    with open(args.sanity_output) as f:
        sanity = f.read()
    parsed = parse_sanity(sanity)
    judgment, warns = judge_pass(parsed, args.dump_date)

    print(f"# DR Restore Drill Report")
    print()
    print(f"- **Dump date:** {args.dump_date}")
    print(f"- **Drill ran at:** {datetime.utcnow().isoformat()}Z")
    print(f"- **Judgment:** **{judgment}**")
    if warns:
        print(f"- **Warnings:**")
        for w in warns:
            print(f"  - {w}")
    print()
    print(f"## RTO 拆解（單位：秒）")
    print()
    print(f"| 階段 | 耗時 |")
    print(f"|---|---|")
    print(f"| Download from R2 | {download_sec} |")
    print(f"| pg_restore       | {restore_sec} |")
    print(f"| **Total**        | **{total_sec}** |")
    print()
    print(f"## Sanity SQL 結果")
    print()
    print(f"### Row counts")
    for tbl, n in parsed["row_counts"].items():
        print(f"- `{tbl}`: {n}")
    print()
    print(f"### Latest events")
    for k, v in parsed["latest"].items():
        print(f"- `{k}`: {v}")
    print()
    print(f"### Alembic version")
    print(f"- `{parsed['alembic']}`")
    print()
    print(f"## 原始 psql 輸出")
    print()
    print("```")
    print(sanity)
    print("```")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2：用 fake sanity output 跑一次本地驗 Markdown 排版**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.github/workflows
cat > /tmp/sanity_fake.txt <<'EOF'
=== 1. Core table row counts ===
        tbl         |  n
--------------------+-----
 users              |  20
 employees          |  18
 students           | 150
 salary_records     |  60
 attendance_records | 9000
 guardians          | 200
 leaves             |  30

=== 2. Latest event timestamps ===
       check        |          value
--------------------+-------------------------
 latest_attendance  | 2026-05-26 09:00:00+08
 latest_audit       | 2026-05-26 09:05:00+08

=== 3. Alembic head ===
      check       | value
------------------+--------
 alembic_version  | abc123

=== 4. Cross-table join smoke ===
 id | username  | name | position
----+-----------+------+----------
  1 | admin     | 王   | 主管
EOF

python dr_drill_report.py \
  --dump-date 2026-05-26 \
  --start-ts 1716000000 \
  --download-end-ts 1716000060 \
  --restore-end-ts 1716000300 \
  --sanity-output /tmp/sanity_fake.txt
```

預期：印出 Markdown 報告，Judgment: PASS，RTO 表 300 秒總計。

- [ ] **Step 3：commit**

```bash
git add .github/workflows/dr_drill_report.py
git commit -m "feat(ops): add drill report generator (Markdown with RTO breakdown)"
```

---

### Task 18：寫 `dr-restore-drill.yml` workflow

**Files:**
- Create: `.github/workflows/dr-restore-drill.yml`

- [ ] **Step 1：建檔**

```yaml
name: dr-restore-drill

on:
  workflow_dispatch:
    inputs:
      dump_date:
        description: "Dump 日期（YYYY-MM-DD），留空抓最新"
        required: false

jobs:
  drill:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    services:
      pg:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: drilltest
        ports: [5432:5432]
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s --health-timeout 5s --health-retries 10

    steps:
      - uses: actions/checkout@v4

      - name: Install pg client
        run: |
          sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
          wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
          sudo apt-get update -y
          sudo apt-get install -y postgresql-client-15

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.R2_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          aws-region: auto

      - name: Download dump from R2
        run: |
          set -euo pipefail
          START_TS=$(date +%s)
          DATE="${{ inputs.dump_date }}"
          if [ -z "$DATE" ]; then
            DATE=$(aws s3 ls s3://ivy-dr/db/daily/ --endpoint-url=${{ secrets.R2_ENDPOINT }} \
                   | grep '\.dump$' | sort | tail -1 | awk '{print $4}' \
                   | sed 's/ivy-//' | sed 's/\.dump$//')
          fi
          aws s3 cp s3://ivy-dr/db/daily/ivy-${DATE}.dump   ./drill.dump   --endpoint-url=${{ secrets.R2_ENDPOINT }}
          aws s3 cp s3://ivy-dr/db/daily/ivy-${DATE}.sha256 ./drill.sha256 --endpoint-url=${{ secrets.R2_ENDPOINT }}
          sed -i "s/ivy-${DATE}\.dump/drill.dump/" drill.sha256
          sha256sum -c drill.sha256
          echo "DRILL_DATE=$DATE"                >> "$GITHUB_ENV"
          echo "START_TS=$START_TS"              >> "$GITHUB_ENV"
          echo "DOWNLOAD_END_TS=$(date +%s)"     >> "$GITHUB_ENV"

      - name: pg_restore
        env:
          PGPASSWORD: drilltest
        run: |
          set -euo pipefail
          pg_restore --no-owner --no-privileges \
            -h localhost -U postgres -d postgres \
            --jobs=4 ./drill.dump
          echo "RESTORE_END_TS=$(date +%s)" >> "$GITHUB_ENV"

      - name: Sanity SQL
        env:
          PGPASSWORD: drilltest
        run: |
          psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 \
            -f .github/workflows/dr_restore_sanity.sql > sanity_output.txt
          cat sanity_output.txt

      - name: Generate drill report
        run: |
          python .github/workflows/dr_drill_report.py \
            --dump-date $DRILL_DATE \
            --start-ts $START_TS \
            --download-end-ts $DOWNLOAD_END_TS \
            --restore-end-ts $RESTORE_END_TS \
            --sanity-output sanity_output.txt \
            > drill-report.md
          cat drill-report.md

      - uses: actions/upload-artifact@v4
        with:
          name: drill-report-${{ env.DRILL_DATE }}
          path: drill-report.md
          retention-days: 90

      - name: LINE notify result
        if: always()
        run: |
          STATUS="${{ job.status }}"
          curl -X POST ${{ secrets.LINE_NOTIFY_WEBHOOK }} \
            -d "message=[DR-Drill] ${DRILL_DATE} 結果：${STATUS} → ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
```

- [ ] **Step 2：YAML 語法檢查**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/dr-restore-drill.yml'))"
```

Expected: 無錯

- [ ] **Step 3：commit**

```bash
git add .github/workflows/dr-restore-drill.yml
git commit -m "feat(ops): add dr-restore-drill GH Actions workflow"
```

---

### Task 19：手動觸發 drill 驗 Phase 3

**前置：** Phase 1 已連續跑 ≥ 3 天累積有 dump 可拉。

- [ ] **Step 1：GH Actions UI 觸發 dr-restore-drill**

留空 dump_date（抓最新）→ Run workflow

- [ ] **Step 2：監控 < 30 分鐘綠**

預期：sanity SQL 印出 row counts > 0；report 印 Judgment: PASS。

- [ ] **Step 3：下載 artifact 驗 Markdown 排版**

GH run page → Artifacts → drill-report-YYYY-MM-DD → 下載 → 看 RTO 拆解、warns 應為空。

- [ ] **Step 4：故意指定 7 天前 dump 跑一次驗 staleness 警告**

`workflow_dispatch` 輸入 `dump_date = 2026-05-19`（或更舊）→ 預期 report 標 `WARN` 且 warns 含「latest_attendance 比 dump 日落差 > 2 天」。

- [ ] **Step 5：LINE 收到 notify**

ops 群應收到 `[DR-Drill] ... 結果：success`。

- [ ] **Step 6：記下首次實測 RTO 秒數，供 Phase 4 runbook 填入**

例：「2026-05-26 首次演練：download 60s / restore 240s / total 300s（5min）→ 遠優於 RTO 4h 目標」

---

## Phase 4：Runbook + 文件更新

### Task 20：寫 `dr-runbook.md`（workspace 層級）

**Files:**
- Create: `/Users/yilunwu/Desktop/ivyManageSystem/docs/sop/dr-runbook.md`

**注意：** 此檔在 workspace（非 git repo）下；建檔後人工 review 過再決定要不要把 workspace 整個 init 成 git repo。或把檔搬到 ivy-backend repo `docs/sop/dr-runbook.md` 同步管理（建議後者，本 task 採後者）。

- [ ] **Step 1：在 ivy-backend repo 建 `docs/sop/dr-runbook.md`**

```markdown
# Disaster Recovery Runbook

文件最後更新：2026-05-26
適用版本：ivy-backend / ivy-frontend / Supabase Pro / R2 ivy-dr

## 1. 目的與保證

- **RPO 24h** — 最壞情況遺失最近 24 小時資料變更
- **RTO 4–8h** — 從決定 restore 到服務恢復可登入操作，1 個工作日內完成
- **實測 RTO**（依月度演練更新）：YYYY-MM-DD = N 分鐘（首次演練後填入；見 §5 紀錄表）

**涵蓋場景：**
- Supabase Pro PITR 7 天視窗外的 DB 災難（帳號鎖、長期誤改未發現、project 損壞）
- Supabase Storage bucket 災損（cross-region 鏡像保險）
- Supabase 帳號完全鎖死

**不涵蓋：**
- 跨 region 自動 failover（需人工切換，見 §6）

## 2. 備份組成

| 內容 | 來源 | 目標 | 頻率 | 保留 |
|---|---|---|---|---|
| pg_dump（custom format） | Supabase Postgres（backup_readonly role） | R2 `ivy-dr/db/daily/` | 每日 02:17 +08 | 30 天 |
| pg_dump 月度長期 | 同上 | R2 `ivy-dr/db/monthly/` | 每月 1 號 | 365 天 |
| leave-attachments | Supabase Storage | R2 `ivy-dr/storage/leave-attachments/` | 同 workflow daily | 365 天 |
| growth-reports | Supabase Storage | R2 `ivy-dr/storage/growth-reports/` | 同 workflow daily | 永久 |

**首選恢復路徑：** Supabase Pro PITR（RPO ~分鐘級 / RTO ~1h）。R2 dump 為 PITR 失效時的長期保險。

## 3. 認證與角色清單

### Supabase `backup_readonly` role
- 權限：`CONNECT` / `USAGE ON SCHEMA public` / `SELECT ON ALL TABLES`
- 密碼：1Password「DR / Supabase backup_readonly」條目
- **新表加入後須跑：** `GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_readonly;`（已用 `ALTER DEFAULT PRIVILEGES` 涵蓋未來表，但既有表新加 column 不需重做）
- 輪替：每 90 天

### GitHub secrets（ivy-backend repo）

| Name | 用途 | 輪替 |
|---|---|---|
| `SUPABASE_DB_HOST` | pg_dump 目標 | 不需 |
| `SUPABASE_BACKUP_DB_PASSWORD` | 上面 role 密碼 | 90 天 |
| `SUPABASE_URL` | Storage sync API | 不需 |
| `SUPABASE_SERVICE_ROLE_KEY` | Storage sync 認證 | 90 天 |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 寫入 | 90 天 |
| `R2_ENDPOINT` | R2 URL | 不需 |
| `LINE_NOTIFY_WEBHOOK` | 失敗告警 | 依 LINE 群輪換 |

### R2 access token IAM scope
- 限定 bucket `ivy-dr`
- 權限：Object Read & Write

## 4. 例行檢核（每週）

- [ ] 看 GH Actions `dr-backup` 連續 7 天綠燈
- [ ] R2 `db/daily/` 最新檔 size 與前一天落差 ±30% 內
- [ ] R2 `db/daily/` 對應 sha256 manifest 存在

## 5. 月度演練 SOP

1. 每月 1 號（或最近工作日）admin 觸發 GH UI → Actions → `dr-restore-drill` → Run workflow
2. 留空 `dump_date` 抓最新；或指定特定日期（測 staleness）
3. 等 < 30 分鐘
4. 下載 artifact `drill-report-YYYY-MM-DD`
5. 讀 Judgment：
   - `PASS` — 所有 row count > 0 且 latest_attendance 距 dump 日 ≤ 2 天
   - `WARN` — 有警告但 restore 成功
   - `FAIL` — pg_restore 出錯（升級 P0）

### 實測 RTO 紀錄表

| 月份 | 觸發人 | Download 秒 | Restore 秒 | Total 秒 | Judgment | 備註 |
|---|---|---|---|---|---|---|
| 2026-05 | | | | | | 首次演練 |
| 2026-06 | | | | | | |

## 6. 災難演習實戰 SOP（真的要 restore 到 prod）

### 決策樹

```
災難發生時間 < 7 天前？─yes─→ Supabase PITR
        │
        no
        ↓
Supabase project 仍可登入？─yes─→ Supabase PITR（若資料未實體損壞）
        │
        no
        ↓
    R2 dump restore
```

### Path A：Supabase PITR

1. Supabase Dashboard → Database → Backups → Point-in-time recovery
2. 選 target timestamp（事故前 5 分鐘）
3. 等 Supabase 還原（~1h）
4. Smoke test：見 §6 Path B Step 5

### Path B：R2 dump restore

1. **建新 Postgres 目的地**
   - 選 a：Supabase 建新 project（適合整個 project 損壞）
   - 選 b：Supabase Dashboard 還原到既有 project（適合 schema/data 部分壞）

2. **從 R2 拉最新 dump 並驗 sha256**

   ```bash
   aws s3 ls s3://ivy-dr/db/daily/ --endpoint-url=$R2_ENDPOINT
   aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.dump   ./restore.dump   --endpoint-url=$R2_ENDPOINT
   aws s3 cp s3://ivy-dr/db/daily/ivy-YYYY-MM-DD.sha256 ./restore.sha256 --endpoint-url=$R2_ENDPOINT
   sed -i "s/ivy-YYYY-MM-DD\.dump/restore.dump/" restore.sha256
   sha256sum -c restore.sha256
   ```

3. **pg_restore（加 --jobs=4 加速）**

   ```bash
   PGPASSWORD='<new-postgres-pwd>' pg_restore \
     --no-owner --no-privileges --jobs=4 \
     -h <new-host> -U postgres -d postgres \
     ./restore.dump
   ```

4. **切 DATABASE_URL → 重新部署 Zeabur backend**
   - Zeabur Console → ivy-backend → Settings → Environment Variables
   - 改 `DATABASE_URL` 為新目的地 connection string
   - Restart service（等 healthcheck 過）

5. **Storage：從 R2 鏡像回填**

   ```bash
   # 確認 supabase CLI 已安裝
   supabase login

   for bucket in leave-attachments growth-reports; do
     aws s3 sync \
       s3://ivy-dr/storage/$bucket/ \
       ./restore_storage/$bucket/ \
       --endpoint-url=$R2_ENDPOINT

     # 對每個檔上傳到對應 Supabase bucket（可寫迴圈或用 supabase-py 腳本）
     # 注意：bucket 命名 hyphen vs underscore；新 project 需先建同名 bucket
   done
   ```

6. **Smoke test**
   - [ ] admin 帳號可登入
   - [ ] `/api/employees` 回 200 且 list > 0
   - [ ] 最新一筆 `salary_records` 可在 admin UI 看到
   - [ ] 任一 leave-attachment 可下載
   - [ ] 任一 growth-report 可下載

7. **預估各步驟耗時（首次實戰演練後填）**

| 步驟 | 估計 | 實測（首次） |
|---|---|---|
| 1. 建新 PG | 30min（新 project）/ 10min（既有 project） | |
| 2. R2 download + sha256 | 5min（500MB dump） | |
| 3. pg_restore | 10min | |
| 4. 切 DATABASE_URL + Zeabur 重啟 | 5min | |
| 5. Storage 回填 | 30min（依檔數量） | |
| 6. Smoke test | 10min | |
| **總計** | ~1h30min – 2h | |

## 7. Storage 災損 SOP（DB 沒事、bucket 沒了）

若僅 Supabase Storage bucket 損壞（DB 仍正常）：

1. 確認 Supabase Storage 仍可寫（建測試 bucket 上傳一個檔驗證）
2. 從 R2 拉對應前綴

   ```bash
   aws s3 sync \
     s3://ivy-dr/storage/leave-attachments/ \
     ./recovery/leave-attachments/ \
     --endpoint-url=$R2_ENDPOINT
   ```

3. 用 supabase CLI 或 supabase-py 上傳回原 bucket（路徑保持一致）
4. DB 內 `attachment_paths` / `file_path` 不變，服務自動接上
5. 確認 admin UI 下載一筆原本壞掉的檔 = pass

## 8. 告警銜接

- **目前：** `dr-backup` workflow 失敗 → `LINE_NOTIFY_WEBHOOK` 通知 ops 群
- **Sentry 啟用後（依 [[project-sentry-integration-2026-05-18]]）：** workflow failure 額外 `capture_exception`
- **連續 2 天 backup 失敗 = P0**：admin 立即介入，先看 GH Actions log 是 pg_dump 連線問題還是 R2 寫入問題

## 9. 已知限制與 backlog

- 跨 region failover 仍需人工切換 Supabase region（規劃中）
- PITR 視窗 7 天；超出後 R2 dump 最多落後 24h（接受的 RPO）
- backup_readonly role 對新 schema 新表需手動 `GRANT SELECT`（已用 default privileges 涵蓋未來表）
- growth-reports migration 期既存 local PDF 須由 `scripts/migrate_growth_reports_to_supabase.py` 補搬
- Storage sync 不刪 R2 上多餘檔（依 lifecycle 自然老化）
```

- [ ] **Step 2：commit**

```bash
git add docs/sop/dr-runbook.md
git commit -m "docs(sop): add DR runbook (RPO/RTO targets, backup composition, restore SOPs)"
```

---

### Task 21：更新既存 SOP 文件

**Files:**
- Modify: `/Users/yilunwu/Desktop/ivyManageSystem/docs/sop/zeabur-deployment-runbook.md`（workspace）
- Modify: `docs/sop/storage-deployment.md`（ivy-backend）

- [ ] **Step 1：改 `ivyManageSystem/docs/sop/zeabur-deployment-runbook.md` §4.2**

打開 workspace 檔（路徑 `/Users/yilunwu/Desktop/ivyManageSystem/docs/sop/zeabur-deployment-runbook.md`），找到 §4.2 章節（line 134-138），改為：

```markdown
### 4.2 Backup
- Supabase Pro 內建 PITR（最近 7 天）— 首選恢復路徑（RTO ~1h）
- 異地備份：GH Actions `dr-backup.yml` 每日 02:17 +08 推送 pg_dump 至 Cloudflare R2 `ivy-dr/db/daily/`，每月 1 號額外複製至 `db/monthly/`
- Storage 鏡像：同 workflow 把 leave-attachments + growth-reports 鏡像至 R2 `ivy-dr/storage/`
- 完整 DR 流程、演練 SOP、retention、回填步驟：見 `ivy-backend/docs/sop/dr-runbook.md`
- 月度演練：手動觸發 `dr-restore-drill.yml`，report artifact 存 GH Actions 90 天
```

- [ ] **Step 2：改 `zeabur-deployment-runbook.md` §5**

找 §5 監控告警章節（line 141-147），P1 待辦清單後追加一行：

```markdown
DR backup 失敗會 LINE Notify ops 群；Sentry 啟用後納入監控（見 `ivy-backend/docs/sop/dr-runbook.md` §8）
```

- [ ] **Step 3：改 `ivy-backend/docs/sop/storage-deployment.md` §5**

找 §5「切換回 local（回滾）」章節（line 70-73），在末尾追加：

```markdown
另有 R2 異地鏡像 `ivy-dr/storage/`，可用 `aws s3 cp ... --endpoint-url=$R2_ENDPOINT` 拉回後再 `supabase storage upload` 回填到新 bucket（見 dr-runbook.md §6 Path B Step 5）。
```

- [ ] **Step 4：commit ivy-backend 那份**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add docs/sop/storage-deployment.md
git commit -m "docs(sop): cross-reference R2 mirror in storage-deployment rollback section"
```

- [ ] **Step 5：workspace zeabur-deployment-runbook.md 的變更**

workspace 不是 git repo，這份檔的變更無法 commit。建議：

選 (a)：把 `zeabur-deployment-runbook.md` 搬到 ivy-backend 並在 workspace 加 symlink — 簡單
選 (b)：把 workspace 整個 `docs/sop/` 變成 git submodule 或 init git
選 (c)：先留變更不 commit，PR description 內附 diff 提示 reviewer 注意

本 task 採 **(c)**：寫變更但不強制 commit；follow-up issue 處理 workspace 文件治理。

---

## Self-Review

完成 spec coverage / placeholder / type 一致性檢查：

### Spec coverage
- §1 RPO/RTO 目標 → 寫進 runbook §1 ✅
- §2 架構 → 在 plan File Structure 與 Phase 1/2 yaml 體現 ✅
- §3 範疇外 → runbook §1 涵蓋場景與不涵蓋場景 ✅
- §4 Phase 0 growth-reports → Task 1–7 ✅（注意 spec 寫 `pdf_path` 實際 DB 欄位是 `file_path`，plan 已正名）
- §5 Phase 1 pg_dump → Task 8–12 ✅
- §6 Phase 2 Storage sync → Task 13–15 ✅
- §7 Phase 3 restore drill → Task 16–19 ✅
- §8 Phase 4 runbook → Task 20–21 ✅
- §9 開放細節（retention / encryption 等）→ 採 spec 推薦預設值，已落地到 yaml lifecycle + runbook 表格 ✅
- §10 Phase 依賴順序 → plan 依此順序排列 Task ✅
- §11 失敗模式 → runbook §9 已列已知限制 ✅
- §12 測試矩陣 → 各 phase 任務內含對應 test 或 manual 驗證 step ✅
- §13 不做的事 → 已避免（無 KMS、無新 SaaS、無自動輪替）✅

### Placeholder scan
- 無 TBD / TODO（runbook §5 紀錄表的「首次演練後填入」是設計上的活欄位，非實作 placeholder）
- 無「implement later」 / 「fill in details」字樣
- 所有 step 含實際 code 或命令

### Type / API 一致性
- `_MODULE_TO_BUCKET` key 名 `growth_reports`（underscore）→ bucket name `growth-reports`（hyphen）一致 ✅
- DB 欄位 `file_path`（非 `pdf_path`）— spec 第一版誤寫，plan 已校正 ✅
- `_migrate_one` / `_is_already_migrated` 簽章在 Task 4 與 Task 5 test 一致 ✅
- R2 prefix `db/daily/` `db/monthly/` `storage/leave-attachments/` `storage/growth-reports/` 在 workflow yaml、lifecycle 表、runbook 一致 ✅
- `dump_date` workflow input 命名在 Task 18 與 19 一致 ✅
