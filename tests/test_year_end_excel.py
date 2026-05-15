"""年終 Excel parser 對 fixture `year_end_114.xls` 的整合測試。

驗證三大區塊解析正確：
- settlements（「年終獎金」sheet 6-step 明細）
- special_bonuses（「年終獎金總表」8 種獎金）
- class_targets（「班級經營績效114.01.15」上下學期各 9 班）
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from models.year_end import SpecialBonusType
from services.year_end.excel_io import parse_year_end_excel


FIXTURE = (
    Path(__file__).parent / "fixtures" / "appraisal" / "year_end_114.xls"
)


@pytest.fixture(scope="module")
def parsed():
    return parse_year_end_excel(FIXTURE)


class TestParseHeader:
    def test_academic_year_114(self, parsed):
        assert parsed.academic_year == 114


class TestParseSettlements:
    def test_has_cai_yiqian(self, parsed):
        names = [s.name for s in parsed.settlements]
        assert "蔡宜倩" in names

    def test_cai_yiqian_full_pipeline(self, parsed):
        s = next(s for s in parsed.settlements if s.name == "蔡宜倩")
        assert s.base_salary == Decimal("36160")
        assert s.festival_total == Decimal("2000")
        assert s.avg_performance_rate == Decimal("97.0")
        assert s.gross_amount == Decimal("37015.2")
        # Excel 小計 = 30944.7072
        assert abs(s.subtotal - Decimal("30944.71")) <= Decimal("0.5")
        # payable 應領小計 = 29044.7072
        assert abs(s.payable - Decimal("29044.71")) <= Decimal("0.5")

    def test_guo_wenxiu_partial_year(self, parsed):
        s = next(s for s in parsed.settlements if s.name == "郭玟秀")
        assert s.total_in_year == Decimal("10")
        # payable Excel 14632.354933333329
        assert abs(s.payable - Decimal("14632.35")) <= Decimal("1.0")


class TestParseSpecialBonuses:
    def test_cai_yiqian_has_six_types(self, parsed):
        cai = [b for b in parsed.special_bonuses if b.name == "蔡宜倩"]
        types_present = {b.bonus_type for b in cai}
        # 113上考核 3312、113上紅利 1500、113下紅利 1000、114上鼓勵 1275、
        # 114上超額 2000、節慶差額 1975 = 6 種
        assert SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST in types_present
        assert SpecialBonusType.SEMESTER_DIVIDEND_FIRST in types_present
        assert SpecialBonusType.SEMESTER_DIVIDEND_SECOND in types_present
        assert SpecialBonusType.AFTER_CLASS_AWARD in types_present
        assert SpecialBonusType.EXCESS_ENROLLMENT in types_present
        assert SpecialBonusType.FESTIVAL_DIFF in types_present

    def test_cai_yiqian_appraisal_bonus_3312(self, parsed):
        cai = [
            b
            for b in parsed.special_bonuses
            if b.name == "蔡宜倩"
            and b.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST
        ]
        assert len(cai) == 1
        assert cai[0].amount == Decimal("3312")

    def test_cai_yiqian_after_class_award_1275(self, parsed):
        match = [
            b
            for b in parsed.special_bonuses
            if b.name == "蔡宜倩"
            and b.bonus_type == SpecialBonusType.AFTER_CLASS_AWARD
        ]
        assert match and match[0].amount == Decimal("1275")

    def test_negative_festival_diff_can_be_loaded(self, parsed):
        # 郭碧婷的節慶獎金比例差額為負（-363.33...）
        match = [
            b
            for b in parsed.special_bonuses
            if b.name == "郭碧婷"
            and b.bonus_type == SpecialBonusType.FESTIVAL_DIFF
        ]
        assert match
        assert match[0].amount < 0


class TestParseClassTargets:
    def test_first_semester_has_9_classes(self, parsed):
        first = [c for c in parsed.class_targets if c.semester_first]
        # Excel 114 上 9 班（天堂鳥、茉莉、牡丹、薔薇、百合、櫻花、芙蓉、向日葵、滿天星）
        assert len(first) == 9

    def test_top_class_tian_tang_niao(self, parsed):
        match = [
            c
            for c in parsed.class_targets
            if c.semester_first and c.class_name == "天堂鳥"
        ]
        assert match
        ct = match[0]
        assert ct.head_count_target == 24
        # 平均在籍 21.5
        assert abs(ct.avg_monthly_enrollment - Decimal("21.5")) <= Decimal("0.1")
        # 經營績效 89.58
        assert abs(ct.class_performance_rate - Decimal("89.58")) <= Decimal("0.1")
