"""seedgen 灌後驗證。

`summary(session)` 印每張被清/灌表的筆數,供人工核對各模組產出規模。
`check_consistency(session)` 回傳一致性問題清單(Phase 4 補強;目前回空 list,
但已可被 import 與呼叫,避免 orchestrator 串接時 NameError)。
"""

from __future__ import annotations

from .wipe import tables_to_wipe

try:  # pragma: no cover - 型別匯入失敗不影響 runtime
    from sqlalchemy.orm import Session
except Exception:  # pragma: no cover
    Session = object  # type: ignore[assignment,misc]


def summary(session: "Session") -> dict[str, int]:
    """印出每張 `tables_to_wipe()` 表的筆數並回傳 table → count dict。

    用原生 ``SELECT count(*)`` 逐表查詢(表名已由 metadata 提供,非使用者輸入,
    無注入風險)。查詢失敗(表不存在等)以 -1 記錄,不中斷整體 summary。
    """
    from sqlalchemy import text

    counts: dict[str, int] = {}
    print("=== seedgen 灌後筆數 summary ===")
    for table in tables_to_wipe():
        try:
            n = session.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        except Exception:  # noqa: BLE001 - 單表查詢失敗不應拖垮 summary
            n = -1
        counts[table] = int(n)
        print(f"  {table:<40} {n:>8}")
    print(f"=== 共 {len(counts)} 張表 ===")
    return counts


def check_consistency(session: "Session") -> list[str]:
    """回傳一致性問題清單(Phase 4 實作:薪資/年終/lifecycle/孤兒 FK)。

    目前為最小可用版,回空 list 代表「無已知問題」,使 orchestrator 與
    `--verify` 模式可正常串接呼叫。
    """
    problems: list[str] = []
    return problems
