"""utils/search.py 純函式單元測試。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Student
from utils.search import (  # noqa: E402
    build_search_filter,
    normalize_query,
    relevance_key,
    tokenize_query,
)


def test_normalize_collapses_and_trims_whitespace():
    # 全形空白 U+3000 → 半形 + 連續空白收斂 + 去頭尾
    assert normalize_query("  王　小明 ") == "王 小明"


def test_normalize_fullwidth_alnum_to_halfwidth():
    assert normalize_query("ＡＢＣ１２３") == "ABC123"


def test_normalize_keeps_cjk_and_handles_none():
    assert normalize_query("王小明") == "王小明"
    assert normalize_query(None) == ""


def test_tokenize_splits_on_space_including_fullwidth():
    assert tokenize_query("大班 王") == ["大班", "王"]
    assert tokenize_query("王　小明") == ["王", "小明"]


def test_tokenize_blank_returns_empty():
    assert tokenize_query("   ") == []
    assert tokenize_query("") == []


def test_build_search_filter_none_when_empty():
    assert build_search_filter([], [Student.name]) is None
    assert build_search_filter(["王"], []) is None


def test_build_search_filter_escapes_wildcards():
    clause = build_search_filter(["%"], [Student.name])
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "\\%" in compiled  # % 被跳脫為 \%，不會當萬用字元


def test_build_search_filter_and_across_tokens():
    clause = build_search_filter(["大班", "王"], [Student.name])
    compiled = str(clause.compile()).upper()
    assert compiled.count(" LIKE ") == 2  # 兩 token 各一 LIKE
    assert " AND " in compiled  # token 之間 AND


def test_relevance_key_exact_prefix_contains():
    assert relevance_key("王", "王") == 0
    assert relevance_key("王小明", "王") == 1
    assert relevance_key("小王", "王") == 2


def test_relevance_key_casefold_and_empty():
    assert relevance_key("ABC", "abc") == 0  # 大小寫不敏感
    assert relevance_key("王", "") == 2  # 空 query 一律視為包含級
    assert relevance_key(None, "王") == 2
