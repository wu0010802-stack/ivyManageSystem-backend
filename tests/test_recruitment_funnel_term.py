from services.recruitment_funnel import roc_month_to_school_term


def test_roc_month_to_school_term_basic():
    assert roc_month_to_school_term("114.09") == (114, 1)  # 9月 → 上學期
    assert roc_month_to_school_term("115.01") == (114, 1)  # 1月 → 前一年上學期
    assert roc_month_to_school_term("115.02") == (114, 2)  # 2月 → 下學期
    assert roc_month_to_school_term("115.03") == (114, 2)  # 3月 → 下學期
    assert roc_month_to_school_term("115.08") == (115, 1)  # 8月 → 下個學年上學期


def test_roc_month_to_school_term_invalid():
    import pytest

    for bad in ("", "115", "115.13", "abc.0a"):
        with pytest.raises(ValueError):
            roc_month_to_school_term(bad)
