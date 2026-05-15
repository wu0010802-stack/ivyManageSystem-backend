"""半年考核 Excel parser 對真實 Excel fixture 的整合測試。

使用 `tests/fixtures/appraisal/half_year_114_first.xls`（114(上)考核統計表）作 golden。
驗證 parse_half_year_excel 正確抽出：
- 學年/學期
- 基礎分數
- 14 位員工 + 4 位「不計算考核」
- 每位員工的 score_items / total_score / grade / bonus_amount
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from models.appraisal import Grade, Semester
from services.appraisal.excel_io import parse_half_year_excel


FIXTURE = (
    Path(__file__).parent / "fixtures" / "appraisal" / "half_year_114_first.xls"
)


@pytest.fixture(scope="module")
def parsed():
    return parse_half_year_excel(FIXTURE)


class TestParseHeader:
    def test_academic_year_114(self, parsed):
        assert parsed.academic_year == 114

    def test_first_semester(self, parsed):
        assert parsed.semester == Semester.FIRST

    def test_base_score_75_6(self, parsed):
        # 9/15 全園 121/160 = 75.6%
        assert parsed.base_score == Decimal("75.6")


class TestParseParticipants:
    def test_total_participants_at_least_14(self, parsed):
        # Excel 主表共 14 位實際考核員工 + 4 位「不計算考核」
        assert len(parsed.participants) >= 14

    def test_has_wang_yaling(self, parsed):
        names = [p.name for p in parsed.participants]
        assert "王雅玲" in names

    def test_excluded_pan_yuhui(self, parsed):
        # 潘諭慧 114.12.22 到職、115.01.19 簽約 → 不計算
        pans = [p for p in parsed.participants if p.name == "潘諭慧"]
        assert len(pans) == 1
        assert pans[0].is_excluded is True
        assert "不計算考核" in (pans[0].exclude_reason or "")

    def test_wang_yaling_grade_pass(self, parsed):
        # 王雅玲 71.15 乙等
        row = next(p for p in parsed.participants if p.name == "王雅玲")
        assert row.total_score == Decimal("71.15")
        assert row.grade == Grade.PASS
        assert row.bonus_amount in (None, Decimal("0"), Decimal("0.00"))

    def test_cai_yiqian_grade_good_bonus_3294(self, parsed):
        # 蔡宜倩 82.35 甲等 獎金 3294
        row = next(p for p in parsed.participants if p.name == "蔡宜倩")
        assert row.total_score == Decimal("82.35")
        assert row.grade == Grade.GOOD
        assert row.bonus_amount == Decimal("3294.00")

    def test_chen_pinfen_score_items_include_3_15_returning(self, parsed):
        # 陳品棻 col 12 (3/15 舊生註冊率) = 6.0
        row = next(p for p in parsed.participants if p.name == "陳品棻")
        item_codes = {s.item_code: s.score_delta for s in row.score_items}
        assert item_codes.get("RETURNING_RATE_0315") == Decimal("6.00")
        assert row.bonus_amount == Decimal("3324.00")

    def test_cai_peiwen_grade_fail(self, parsed):
        # 蔡佩汶 57.10 丁等
        row = next(p for p in parsed.participants if p.name == "蔡佩汶")
        assert row.total_score == Decimal("57.10")
        assert row.grade == Grade.FAIL
