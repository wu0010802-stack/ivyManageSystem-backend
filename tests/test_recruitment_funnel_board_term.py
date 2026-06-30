"""招生 funnel /board 改依「入學學期」（target_school_year/target_semester）圈定範圍。

Task 5：取代原本依訪視月份（month.in_）的過濾邏輯。
所有寫入路徑（create 預設當前學期、import 由 month 推導、enrterm01 backfill）
皆保證 target 有值，因此 board 可直接以 target 過濾，無需 month fallback。
"""

from api.recruitment.funnel import get_board
from models.recruitment import RecruitmentVisit


def _names(out):
    return {c.child_name for bucket in out.stages.values() for c in bucket}


def test_board_groups_by_enrollment_term_not_visit_month(test_db_session):
    """訪視月份屬 114 上學期（114.09），但入學學期設為 115 上 → 查 115 上才出現。

    此為 Task 5 核心行為：board 依 target_school_year/target_semester 過濾，
    而非依 visit month 所屬學期。
    """
    # 訪視月份屬 114 上學期(114.09)，但入學學期設為 115 上學期
    test_db_session.add(
        RecruitmentVisit(
            month="114.09",
            child_name="跨期童",
            has_deposit=False,
            target_school_year=115,
            target_semester=1,
        )
    )
    test_db_session.commit()

    # 查 115 上學期 → 應出現（依 target，而非依 visit month 的 114 上）
    out_115 = get_board(school_year=115, semester=1, session=test_db_session, _=None)
    assert "跨期童" in _names(out_115), "依入學學期 115/1 應能找到跨期童"

    # 查 114 上學期 → 不應出現（訪視月份雖屬 114 上，但 target 為 115 上）
    out_114 = get_board(school_year=114, semester=1, session=test_db_session, _=None)
    assert "跨期童" not in _names(
        out_114
    ), "114/1 看板不應出現 target 為 115/1 的跨期童"
