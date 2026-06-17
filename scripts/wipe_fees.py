"""scripts/wipe_fees.py — 學費系統本地 dev 清資料（c2 重構前用）。

c2 將 DROP TABLE fee_items；若 fee_items / student_fee_records 等表有 row，
alembic upgrade 會被 FK 卡住。此腳本提供有序刪除（refunds → payments →
records → items），並重置 PK 序列；僅在 localhost / 127.0.0.1 環境允許。

使用：
    python scripts/wipe_fees.py --confirm

安全閘：
- DATABASE_URL 需明確包含 localhost 或 127.0.0.1（其他環境一律 abort）
- 必須帶 --confirm 旗標（不接受互動式 prompt，避免誤入 prod CI 殺資料）
"""

import argparse
import os
import sys
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text  # noqa: E402

from models.base import DATABASE_URL, session_scope  # noqa: E402


def _redact_db_url(url: str) -> str:
    """遮罩 DB URL 的帳密，只保留 scheme/host/port/db。

    避免把 DATABASE_URL（可能含密碼）印進 stdout/stderr → 終端 scrollback /
    CI log / 截圖。無帳密的 URL 原樣回傳；解析失敗一律回 "<redacted>"。
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<redacted>"
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        parts = parts._replace(netloc=f"***@{host}" if host else "***")
    return urlunsplit(parts)


def _ensure_localhost() -> None:
    if not DATABASE_URL:
        print("[abort] DATABASE_URL 未設定，拒絕執行。", file=sys.stderr)
        sys.exit(2)
    if ("localhost" not in DATABASE_URL) and ("127.0.0.1" not in DATABASE_URL):
        print(
            f"[abort] DATABASE_URL='{_redact_db_url(DATABASE_URL)}' 不是 localhost；"
            "本腳本僅允許在本機 dev 環境執行。",
            file=sys.stderr,
        )
        sys.exit(2)


def wipe_fees() -> dict:
    """有序刪除學費四表並重置 PK 序列。回傳各表刪除筆數。"""
    counts: dict[str, int] = {}
    with session_scope() as session:
        # 順序：refunds / payments → records
        # （payments 與 refunds 都靠 record_id FK 到 records；
        #  c2 已 DROP TABLE fee_items；c3 已 DROP COLUMN fee_item_id）
        for table in (
            "student_fee_refunds",
            "student_fee_payments",
            "student_fee_records",
        ):
            try:
                res = session.execute(text(f"DELETE FROM {table}"))
                counts[table] = res.rowcount or 0
            except Exception as exc:  # 表可能不存在或已被前一輪 DROP
                counts[table] = -1
                print(f"[warn] DELETE FROM {table} 失敗：{exc}", file=sys.stderr)

        # 重置 PK 序列：PostgreSQL 自動命名規則 <table>_id_seq
        for table in (
            "student_fee_refunds",
            "student_fee_payments",
            "student_fee_records",
            "fee_items",
        ):
            seq = f"{table}_id_seq"
            try:
                session.execute(text(f"ALTER SEQUENCE {seq} RESTART WITH 1"))
            except Exception as exc:
                print(f"[warn] reset sequence {seq} 失敗：{exc}", file=sys.stderr)

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="必須帶此旗標才會實際執行刪除",
    )
    args = parser.parse_args()

    _ensure_localhost()

    if not args.confirm:
        print(
            "[abort] 未帶 --confirm 旗標；本腳本將清空所有學費資料，需明確確認。",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[info] DATABASE_URL={_redact_db_url(DATABASE_URL)}")
    print("[info] 開始清除學費資料...")
    counts = wipe_fees()
    print("[done] 刪除筆數摘要：")
    for tbl, cnt in counts.items():
        print(f"  - {tbl}: {cnt}")


if __name__ == "__main__":
    main()
