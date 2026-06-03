"""_build_funnel_card 純函式：保留座位欄位正確帶出。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.recruitment import RecruitmentVisit
from api.recruitment.funnel import _build_funnel_card


def test_card_exposes_reserved_seat():
    v = RecruitmentVisit(
        month="115.03",
        child_name="甲",
        has_deposit=True,
        provisional_grade_id=3,
        target_school_year=115,
        target_semester=1,
    )
    v.id = 1  # 未存檔，手動指定 id 供卡片使用
    card = _build_funnel_card(v, None, {3: "中班"})
    assert card.current_stage == "deposited"
    assert card.provisional_grade_id == 3
    assert card.provisional_grade_name == "中班"
    assert card.target_school_year == 115


def test_card_without_reservation():
    v = RecruitmentVisit(month="115.03", child_name="乙", has_deposit=False)
    v.id = 2
    card = _build_funnel_card(v, None, {})
    assert card.provisional_grade_id is None
    assert card.provisional_grade_name is None
