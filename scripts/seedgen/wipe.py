"""業務資料清除(僅限 dev DB)。

`tables_to_wipe()` 自 `Base.metadata` 全表名出發,扣掉:
  1. PRESERVE:系統/RBAC 表(alembic_version / permission_definitions / roles),
     即使日後被 ORM 映射也絕不清。
  2. SKIP_SUBSTRINGS 命中:token / cache / rate-limit / staging / 排程 watermark /
     一次性碼 / 上傳暫存 / webhook / 非同步 job 等揮發性/基礎設施表,清空無意義
     甚至有害。

`wipe(session)` 在「單一交易」內以
``TRUNCATE <所有表> RESTART IDENTITY CASCADE`` 一次清空並重置序列。
CASCADE 處理 FK 相依(含 classrooms↔employees 循環),不需手動排序。
護欄(只對本機 dev DB 執行)由 CLI 端 `guard.assert_dev_db` 把關,本模組不重複判斷。
"""

from __future__ import annotations

# 觸發所有 model 載入,確保 Base.metadata 完整(避免漏表)。
import models.database  # noqa: F401
from models.base import Base

try:  # pragma: no cover - 型別匯入失敗不影響 runtime
    from sqlalchemy.orm import Session
except Exception:  # pragma: no cover
    Session = object  # type: ignore[assignment,misc]


# 永不清除:系統元資料與 RBAC 種子(以 DB 為單一來源,清掉會破壞登入/權限)。
PRESERVE: frozenset[str] = frozenset(
    {
        "alembic_version",
        "permission_definitions",
        "roles",
    }
)

# 表名命中任一子字串即跳過(揮發性/基礎設施表,清空無業務意義)。
SKIP_SUBSTRINGS: tuple[str, ...] = (
    "jwt_blocklist",
    "rate_limit",
    "_refresh_tokens",
    "_cache",
    "_staging",
    "_sync_states",
    "scheduler_heartbeat",
    "scheduler_watermark",
    "binding_codes",
    "device_setup_codes",
    "pending_uploads",
    "password_history",
    "line_webhook",
    "line_reply",
    "salary_calc_jobs",
    "data_quality_reports",
)


def _is_skipped(table_name: str) -> bool:
    """表名是否命中 SKIP_SUBSTRINGS(子字串比對)。"""
    return any(sub in table_name for sub in SKIP_SUBSTRINGS)


def tables_to_wipe() -> list[str]:
    """回傳要清除的表名清單。

    = Base.metadata 全表名 − PRESERVE − 命中 SKIP_SUBSTRINGS 的表。
    回傳依表名排序,讓輸出/TRUNCATE 列表穩定可預期。

    用 ``Base.metadata.tables``(name→Table 的 dict)而非 ``sorted_tables``:
    我們只需要表名集合,不需要拓撲排序;而 employees↔classrooms 互相依賴的
    FK 環會讓 ``sorted_tables`` 噴 SAWarning。改取 dict keys 可拿到相同的全表名
    集合且不觸發排序。
    """
    all_names = set(Base.metadata.tables.keys())
    keep = {
        name for name in all_names if name not in PRESERVE and not _is_skipped(name)
    }
    return sorted(keep)


def wipe(session: "Session") -> None:
    """在單一交易內清空所有 `tables_to_wipe()` 表並重置序列。

    用一條 ``TRUNCATE ... RESTART IDENTITY CASCADE`` 同時列出所有表;
    CASCADE 解除 FK 相依,RESTART IDENTITY 重置自增序列。
    呼叫端負責 commit/rollback(本函式不自行 commit)。
    """
    from sqlalchemy import text

    targets = tables_to_wipe()
    if not targets:
        return
    quoted = ", ".join(f'"{name}"' for name in targets)
    session.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
