"""招生 funnel /board 依「訪視月份所屬學年」圈定範圍。

原本 get_board 抓全表 → 切學年看板不變、隨年度無上限累積、summary 全歷史。
target_school_year 多為 NULL 不能當過濾依據，改由 RecruitmentVisit.month（民國月份）
推導所屬學年（學年 N = 8 月~隔年 7 月，對齊 utils.academic.term_bounds）。
"""

from api.recruitment.funnel import get_board
from models.recruitment import RecruitmentVisit
from services.recruitment_funnel import school_term_to_roc_months


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
        s.add_all(
            [
                RecruitmentVisit(month="114.09", child_name="甲"),  # 學年114 上學期
                RecruitmentVisit(month="115.05", child_name="乙"),  # 學年114 下學期
                RecruitmentVisit(month="114.03", child_name="丙"),  # 學年113 下學期
                RecruitmentVisit(month="115.10", child_name="丁"),  # 學年115 上學期
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
