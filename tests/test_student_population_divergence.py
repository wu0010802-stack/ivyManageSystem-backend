"""Characterization test：在學學生「current population」口徑分歧。

目的：pin 住現況——多個 surface 各自手刻 lifecycle filter，對「同一批學生」回不同
population。並標出「同稱『在學人數』卻不同答案」的治理 bug（dashboard vs 教育部月報）。

本測試**不主張**全部該統一（churn/appraisal/fees 各有合理的不同問題），而是：
1. 證明分歧存在（同學生、不同數字）。
2. 鎖定 dashboard 在學 ≠ gov_moe 月報在學（兩者皆名為「在學」、後者法定申報）。

對應 spec：docs/superpowers/specs/2026-05-29-student-population-vocabulary-design.md
未來抽 services/analytics/student_population.py named vocabulary 後，本測試改為斷言
各 named population 的明確成員。
"""

from __future__ import annotations

from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_PROSPECT,
    LIFECYCLE_WITHDRAWN,
)
from services.gov_moe.monthly_calculator import EXCLUDED_LIFECYCLE

# transferred 也是終態之一（models/classroom.py:150 註解列出）
LIFECYCLE_TRANSFERRED = "transferred"

ALL_STATES = [
    LIFECYCLE_PROSPECT,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    LIFECYCLE_GRADUATED,
]

# is_active 由 set_lifecycle_status 連動（services/student_lifecycle.py）：
# active + on_leave(休學仍在讀) → True；其餘 → False。
IS_ACTIVE_TRUE_STATES = {LIFECYCLE_ACTIVE, LIFECYCLE_ON_LEAVE}


def _fixture_students() -> list[dict]:
    """每個 lifecycle 狀態各 1 名學生，is_active 依 sync 規則填。"""
    return [
        {"lifecycle_status": s, "is_active": s in IS_ACTIVE_TRUE_STATES}
        for s in ALL_STATES
    ]


# ── 各 surface 的 current-population predicate（忠實重現 + 標來源）──────────────


def _pop_dashboard(stu: dict) -> bool:
    # services/dashboard_query_service.py:199 首頁「在學人數」：Student.is_active == True
    return stu["is_active"] is True


def _pop_gov_moe(stu: dict) -> bool:
    # services/gov_moe/monthly_calculator.py:179 教育部月報「在學」：~lifecycle_status.in_(EXCLUDED_LIFECYCLE)
    return stu["lifecycle_status"] not in EXCLUDED_LIFECYCLE


def _pop_churn_active_only(stu: dict) -> bool:
    # services/analytics/churn_service.py:104：lifecycle_status == 'active'
    return stu["lifecycle_status"] == LIFECYCLE_ACTIVE


def _pop_churn_active_on_leave(stu: dict) -> bool:
    # services/analytics/churn_service.py:211：lifecycle_status.in_(('active','on_leave'))
    return stu["lifecycle_status"] in (LIFECYCLE_ACTIVE, LIFECYCLE_ON_LEAVE)


def _pop_fees_billable(stu: dict) -> bool:
    # services/fees/generation.py:90：lifecycle_status.in_([active, enrolled])
    return stu["lifecycle_status"] in (LIFECYCLE_ACTIVE, LIFECYCLE_ENROLLED)


def _count(pred) -> int:
    return sum(1 for s in _fixture_students() if pred(s))


def _members(pred) -> set:
    return {s["lifecycle_status"] for s in _fixture_students() if pred(s)}


class TestCurrentPopulationDivergence:
    """同一批學生，各 surface 的「當前在學」口徑不一致。"""

    def test_dashboard_counts_active_and_on_leave(self):
        assert _members(_pop_dashboard) == {LIFECYCLE_ACTIVE, LIFECYCLE_ON_LEAVE}
        assert _count(_pop_dashboard) == 2

    def test_gov_moe_counts_everyone_except_prospect(self):
        # 法定月報「在學」幾乎涵蓋全部（含 enrolled/graduated/withdrawn/transferred）
        assert _count(_pop_gov_moe) == len(ALL_STATES) - 1  # 排除 prospect
        assert LIFECYCLE_GRADUATED in _members(_pop_gov_moe)

    def test_dashboard_and_gov_moe_disagree_on_zaixue(self):
        """治理 bug：兩者皆名「在學人數」，但對同一批學生回不同數字。"""
        dash = _count(_pop_dashboard)
        gov = _count(_pop_gov_moe)
        assert dash != gov, "若相等表示口徑已對齊（spec follow-up 已落地）"
        # gov 月報（法定申報）比園長首頁多算了非 prospect 的所有狀態
        assert gov > dash

    def test_same_count_different_members(self):
        """dashboard 與 fees 都 = 2 但成員不同（on_leave vs enrolled）→ 大小相同不代表一致。"""
        assert _count(_pop_dashboard) == _count(_pop_fees_billable) == 2
        assert _members(_pop_dashboard) != _members(_pop_fees_billable)

    def test_churn_active_only_excludes_on_leave(self):
        """合理的不同問題：churn 率不含休學（非 bug，記錄以免被誤統一）。"""
        assert _members(_pop_churn_active_only) == {LIFECYCLE_ACTIVE}
        assert LIFECYCLE_ON_LEAVE in _members(_pop_churn_active_on_leave)
        assert _members(_pop_churn_active_only) != _members(_pop_dashboard)

    def test_at_least_four_distinct_populations_exist(self):
        """≥4 種不同 population 並存，無共用詞彙 → spec 提案抽 student_population。"""
        distinct = {
            frozenset(_members(p))
            for p in (
                _pop_dashboard,
                _pop_gov_moe,
                _pop_churn_active_only,
                _pop_fees_billable,
            )
        }
        assert len(distinct) >= 4
