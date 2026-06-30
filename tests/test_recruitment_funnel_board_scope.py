"""招生 funnel /board 依「入學學期（target_school_year/target_semester）」圈定範圍。

Task 5 後：get_board 改以 target_school_year/target_semester 過濾（取代 month.in_）。
所有寫入路徑保證 target 有值；測試 fixture 須明確設 target_* 欄位。
"""

from api.recruitment.funnel import get_board
from models.recruitment import RecruitmentVisit
from services.recruitment_funnel import (
    school_term_to_roc_months,
    roc_month_to_school_term,
)


class TestSchoolTermToRocMonths:
    def test_semester_1_aug_to_next_jan(self):
        assert school_term_to_roc_months(114, 1) == [
            "114.08",
            "114.09",
            "114.10",
            "114.11",
            "114.12",
            "115.01",
        ]

    def test_semester_2_feb_to_jul(self):
        assert school_term_to_roc_months(114, 2) == [
            "115.02",
            "115.03",
            "115.04",
            "115.05",
            "115.06",
            "115.07",
        ]

    def test_whole_year_when_semester_none(self):
        months = school_term_to_roc_months(114, None)
        assert len(months) == 12
        assert months[0] == "114.08"
        assert months[-1] == "115.07"
        # 跨年邊界：上學期含隔年 1 月、下學期全在隔年
        assert "115.01" in months


def _names(out):
    return {c.child_name for bucket in out.stages.values() for c in bucket}


class TestGetBoardScopesToSchoolYear:
    def _seed(self, s):
        def _visit(month, name):
            """建立訪視並依 month 推導入學學期（Task 5：board 依 target 過濾）。"""
            sy, sem = roc_month_to_school_term(month)
            return RecruitmentVisit(
                month=month,
                child_name=name,
                target_school_year=sy,
                target_semester=sem,
            )

        s.add_all(
            [
                _visit("114.09", "甲"),  # → target 114/1 上學期
                _visit("115.05", "乙"),  # → target 114/2 下學期
                _visit("114.03", "丙"),  # → target 113/2 下學期
                _visit("115.10", "丁"),  # → target 115/1 上學期
            ]
        )
        s.commit()

    def test_filters_to_selected_school_year(self, test_db_session):
        self._seed(test_db_session)
        out = get_board(school_year=114, semester=None, session=test_db_session, _=None)
        assert _names(out) == {"甲", "乙"}
        assert out.summary.visited_count == 2  # 兩筆都無 deposit/student → visited

    def test_other_year_isolated(self, test_db_session):
        self._seed(test_db_session)
        out113 = get_board(
            school_year=113, semester=None, session=test_db_session, _=None
        )
        assert _names(out113) == {"丙"}
        out115 = get_board(
            school_year=115, semester=None, session=test_db_session, _=None
        )
        assert _names(out115) == {"丁"}

    def test_semester_filter(self, test_db_session):
        self._seed(test_db_session)
        sem1 = get_board(school_year=114, semester=1, session=test_db_session, _=None)
        assert _names(sem1) == {"甲"}  # 114.09 在上學期
        sem2 = get_board(school_year=114, semester=2, session=test_db_session, _=None)
        assert _names(sem2) == {"乙"}  # 115.05 在下學期

    def test_default_school_year_uses_current_term(self, test_db_session):
        """school_year=None → 預設當前學年（不再抓全表）。"""
        self._seed(test_db_session)
        out = get_board(
            school_year=None, semester=None, session=test_db_session, _=None
        )
        # 預設只圈當前學年，不會把四筆跨學年全撈進來
        assert len(_names(out)) < 4
