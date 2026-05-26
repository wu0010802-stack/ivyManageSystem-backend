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
    """嘗試把單筆 report 從本機遷到 Supabase Storage。

    回傳狀態字串：
    - "skipped:already-migrated"  — file_path 已是 storage key，直接跳過
    - "skipped:local-missing"     — 本機檔不存在，跳過（不報錯，讓 caller 計數）
    - "dry-run"                   — dry-run 模式，未實際寫入
    - "migrated"                  — 成功上傳並驗 hash、更新 DB file_path

    注意：status=="migrated" 時 report.file_path 已改為 storage_key，
    caller 必須在呼叫本函式**之前**先記下 old_path，供後續刪除本機檔用。
    """
    if _is_already_migrated(report.file_path):
        return "skipped:already-migrated"

    local = _local_path(report.file_path)
    if not local.is_file():
        logger.warning("report=%s 本機檔不存在，跳過: %s", report.id, local)
        return "skipped:local-missing"

    data = local.read_bytes()
    src_hash = hashlib.sha256(data).hexdigest()
    storage_key = f"{_STORAGE_KEY_PREFIX}{report.student_id}/{report.id}.pdf"

    if dry_run:
        logger.info(
            "[dry-run] would upload report=%s key=%s size=%s sha=%s",
            report.id,
            storage_key,
            len(data),
            src_hash[:8],
        )
        return "dry-run"

    backend.save("growth_reports", storage_key, data, "application/pdf")
    fetched = backend.read("growth_reports", storage_key)
    dst_hash = hashlib.sha256(fetched).hexdigest()
    if src_hash != dst_hash:
        raise RuntimeError(
            f"hash mismatch report={report.id}: src={src_hash} dst={dst_hash}"
        )

    # 更新 DB（session 由 caller 透過 session_scope 管理，出 scope 才 commit）
    report.file_path = storage_key
    return "migrated"


def main() -> dict[str, int]:
    parser = argparse.ArgumentParser(
        description="把本機 growth-reports PDF 遷到 Supabase Storage（idempotent）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只預覽，不寫入 DB / Storage"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="限制處理筆數（測試用）"
    )
    args = parser.parse_args()

    if settings.storage.backend != "supabase" and not args.dry_run:
        logger.error("非 supabase backend 不可真跑 migration；用 --dry-run 預覽")
        sys.exit(2)

    backend = get_backend()
    results: dict[str, int] = {}

    # 先記 (old_local_path) 給已成功 migrated 的 report，
    # 待 session_scope 出 scope（DB commit 成功）後才刪本機檔。
    # 這樣做可保證「DB commit 失敗 → local 仍在 → 可重跑」。
    local_files_to_delete: list[Path] = []

    with session_scope() as session:
        query = session.query(StudentGrowthReport).filter(
            StudentGrowthReport.status == "ready",
            StudentGrowthReport.file_path.isnot(None),
        )
        if args.limit:
            query = query.limit(args.limit)
        reports = query.all()
        logger.info("找到 %s 筆 ready report", len(reports))

        for report in reports:
            # ★ 關鍵 bug fix：在呼叫 _migrate_one 之前先記下 old_path。
            # _migrate_one 若成功會把 report.file_path 覆蓋成 storage_key，
            # 之後再讀 report.file_path 就拿不到原始 local 路徑了。
            old_path = (
                _local_path(report.file_path)
                if not _is_already_migrated(report.file_path)
                else None
            )

            try:
                status = _migrate_one(report, backend, dry_run=args.dry_run)
                results[status] = results.get(status, 0) + 1
                if status == "migrated" and old_path is not None:
                    local_files_to_delete.append(old_path)
            except Exception as e:
                logger.exception("migration failed report=%s: %s", report.id, e)
                results["failed"] = results.get("failed", 0) + 1

        # session_scope 出 with 區塊時才 commit（見 models/base.py:session_scope）

    # ★ DB commit 成功後（session_scope 已正常 exit），才刪本機檔。
    # 若 session_scope 因異常 rollback，不會到達這裡，local 檔安全保留。
    deleted = 0
    for local in local_files_to_delete:
        try:
            local.unlink(missing_ok=True)
            logger.info("刪除本機檔: %s", local)
            deleted += 1
        except OSError as e:
            logger.warning("刪除本機檔失敗（非致命）: %s — %s", local, e)

    if deleted:
        results["local_deleted"] = deleted

    logger.info("結果: %s", results)
    return results


if __name__ == "__main__":
    main()
