"""scripts/encrypt_medical_fields.py — 既有 plaintext 醫療欄位加密 backfill.

P0d Phase 3a 法規/個資 sprint：把既有 plaintext 的
Student.allergy/medication/special_needs 一次性加密為 Fernet ciphertext。

執行：
    # Dry-run（不寫 DB，只列 row 數）
    python scripts/encrypt_medical_fields.py --dry-run

    # 真實 execute（每批 500 列 + commit）
    python scripts/encrypt_medical_fields.py --execute

設計重點：
- **Idempotent**：is_encrypted() 已加密則 skip，可重跑
- **Batched**：避免長交易鎖 students 表（每批 500 commit）
- **不走 ORM 加密路徑**：raw SQL UPDATE，避免 EncryptedText 透明加密又一輪
- **Migration window 安全**：執行前後既有 endpoint 都能讀（legacy plaintext fallback）

執行前必設 env：
    export MEDICAL_FIELD_ENCRYPTION_KEY="<your prod fernet key>"

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §3.6
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 讓 scripts 可 import backend 模組
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500

# 要 backfill 的欄位（皆位於 students 表）
_TARGET_FIELDS = ("allergy", "medication", "special_needs")


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _count_plaintext_rows(session) -> dict[str, int]:
    """計算每個欄位仍為 plaintext 的 row 數量（不寫 DB）。"""
    from utils.medical_encryption import is_encrypted

    counts: dict[str, int] = {}
    for field in _TARGET_FIELDS:
        rows = session.execute(
            text(
                f"SELECT id, {field} FROM students "
                f"WHERE {field} IS NOT NULL AND {field} != ''"
            )
        ).all()
        plaintext_count = sum(1 for r in rows if not is_encrypted(r[1]))
        counts[field] = plaintext_count
    return counts


def _backfill_field(session, field: str, dry_run: bool) -> dict[str, int]:
    """處理單一欄位：每批 _BATCH_SIZE 列 + commit。

    回傳 stats: {checked, encrypted_skipped, encrypted_now, errored}.
    """
    from utils.medical_encryption import encrypt_medical, is_encrypted

    stats = {
        "checked": 0,
        "encrypted_skipped": 0,
        "encrypted_now": 0,
        "errored": 0,
    }
    offset = 0

    while True:
        rows = session.execute(
            text(
                f"SELECT id, {field} FROM students "
                f"WHERE {field} IS NOT NULL AND {field} != '' "
                f"ORDER BY id LIMIT :batch OFFSET :offset"
            ),
            {"batch": _BATCH_SIZE, "offset": offset},
        ).all()
        if not rows:
            break

        for row_id, value in rows:
            stats["checked"] += 1
            try:
                if is_encrypted(value):
                    stats["encrypted_skipped"] += 1
                    continue

                # Plaintext → encrypt
                ciphertext = encrypt_medical(value)
                if dry_run:
                    stats["encrypted_now"] += 1
                    continue

                session.execute(
                    text(f"UPDATE students SET {field} = :ct WHERE id = :id"),
                    {"ct": ciphertext, "id": row_id},
                )
                stats["encrypted_now"] += 1
            except Exception as exc:
                logger.error("Backfill error id=%s field=%s: %s", row_id, field, exc)
                stats["errored"] += 1

        if not dry_run:
            session.commit()
        offset += _BATCH_SIZE
        logger.info(
            "%s batch offset=%s checked=%s encrypted_now=%s",
            field,
            offset,
            stats["checked"],
            stats["encrypted_now"],
        )

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Backfill 既有 plaintext 醫療欄位為 Fernet ciphertext (idempotent)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只計數不寫入（預設）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真實寫入。dry-run 與 execute 必擇一明確指定",
    )
    args = parser.parse_args()

    if args.dry_run and args.execute:
        print("錯誤：--dry-run 與 --execute 不可同時指定", file=sys.stderr)
        sys.exit(2)
    if not args.dry_run and not args.execute:
        print(
            "請明確指定 --dry-run 或 --execute（避免誤改 prod）",
            file=sys.stderr,
        )
        sys.exit(2)

    if not os.environ.get("MEDICAL_FIELD_ENCRYPTION_KEY"):
        print(
            "錯誤：未設 MEDICAL_FIELD_ENCRYPTION_KEY env，無法執行 backfill",
            file=sys.stderr,
        )
        sys.exit(2)

    _setup_logging()
    mode = "DRY-RUN" if args.dry_run else "EXECUTE"
    logger.info("=" * 50)
    logger.info("encrypt_medical_fields backfill %s", mode)
    logger.info("=" * 50)

    from models.base import session_scope

    with session_scope() as session:
        # 第一階段：列 plaintext 計數
        plaintext_counts = _count_plaintext_rows(session)
        logger.info("Plaintext 計數 (pre-backfill):")
        total_plaintext = 0
        for field, count in plaintext_counts.items():
            logger.info("  %-20s %d 列", field, count)
            total_plaintext += count
        if total_plaintext == 0:
            logger.info("沒有 plaintext rows 需 backfill（已全加密）")
            return

        # 第二階段：執行 backfill
        all_stats: dict[str, dict[str, int]] = {}
        for field in _TARGET_FIELDS:
            logger.info("\n處理欄位 %s ...", field)
            all_stats[field] = _backfill_field(session, field, args.dry_run)

    # 總結
    logger.info("\n%s 完成 %s", "=" * 20, "=" * 20)
    for field, stats in all_stats.items():
        logger.info(
            "%s: checked=%d encrypted_skipped=%d encrypted_now=%d errored=%d",
            field,
            stats["checked"],
            stats["encrypted_skipped"],
            stats["encrypted_now"],
            stats["errored"],
        )
    if args.dry_run:
        logger.info("DRY-RUN: 未實際寫入 DB。確認 stats 無誤後加 --execute 跑 backfill")


if __name__ == "__main__":
    main()
