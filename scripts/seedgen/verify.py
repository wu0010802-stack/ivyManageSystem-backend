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
    """回傳一致性問題清單:薪資/年終/lifecycle/孤兒 FK/請假額度。

    空 list 代表無已知問題。每項檢查獨立 try/except,單項失敗(表不存在等)
    記為一條問題而非中斷,確保 `--verify` 永遠跑完。
    """
    from sqlalchemy import text

    problems: list[str] = []

    def check(label: str, sql: str, bad_if_positive: bool = True) -> None:
        try:
            n = int(session.execute(text(sql)).scalar() or 0)
        except Exception as exc:  # noqa: BLE001 - 單項失敗不中斷整體
            problems.append(f"[檢查失敗] {label}: {exc}")
            return
        if bad_if_positive and n > 0:
            problems.append(f"{label}: {n} 筆")

    # 1) closed 月薪資 net_salary 應全為正。
    check(
        "薪資 net_salary <= 0(應全為正)",
        "SELECT count(*) FROM salary_records WHERE net_salary <= 0",
    )
    # 2) 年終金額應在 ±100 萬 CHECK 內。
    check(
        "年終 total_amount 越界(±100萬)",
        "SELECT count(*) FROM year_end_settlements WHERE abs(total_amount) > 1000000",
    )
    # 3) lifecycle_status 合法值域。
    check(
        "students.lifecycle_status 非法值",
        "SELECT count(*) FROM students WHERE lifecycle_status NOT IN "
        "('prospect','enrolled','active','on_leave','transferred','withdrawn','graduated')",
    )
    # 4) 孤兒 FK 抽查。
    check(
        "salary_records 無對應員工",
        "SELECT count(*) FROM salary_records s LEFT JOIN employees e ON s.employee_id=e.id WHERE e.id IS NULL",
    )
    check(
        "guardians 無對應學生",
        "SELECT count(*) FROM guardians g LEFT JOIN students s ON g.student_id=s.id "
        "WHERE g.student_id IS NOT NULL AND s.id IS NULL",
    )
    check(
        "active 學生無班級",
        "SELECT count(*) FROM students WHERE lifecycle_status='active' AND classroom_id IS NULL",
    )
    # 5) 月薪員工應有請假額度(>0)。
    check(
        "無任何請假額度(leave_quotas 為空)",
        "SELECT CASE WHEN count(*)=0 THEN 1 ELSE 0 END FROM leave_quotas",
    )

    return problems
