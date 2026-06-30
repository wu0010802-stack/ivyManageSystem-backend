"""
共用搜尋工具：SQL LIKE / ILIKE 安全組合。
"""

from __future__ import annotations

import unicodedata
from typing import Optional, Sequence

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement


def escape_like_pattern(keyword: str, escape_char: str = "\\") -> str:
    """逸出 SQL LIKE 萬用字元，防止 `%` 與 `_` 被當成萬用字元造成過度匹配。

    使用端搭配 `.ilike(like_pattern(kw), escape='\\\\')` 或在 ORM 層指定相同 escape。

    範例：
        kw = escape_like_pattern(user_input)
        q = q.filter(Model.name.ilike(f"%{kw}%", escape=LIKE_ESCAPE_CHAR))
    """
    if not isinstance(keyword, str):
        return keyword
    return (
        keyword.replace(escape_char, escape_char + escape_char)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


LIKE_ESCAPE_CHAR = "\\"


def normalize_query(raw: Optional[str]) -> str:
    """正規化搜尋字串：全形→半形（NFKC，CJK 不受影響）、收斂空白。

    - NFKC 把全形英數/標點/全形空白（U+3000）轉半形；中日韓統一表意文字不變。
    - 連續空白（含 tab、全形空白轉成的半形空白）收斂為單一半形空白並去頭尾。
    """
    if not isinstance(raw, str):
        return ""
    s = unicodedata.normalize("NFKC", raw)
    return " ".join(s.split())


def tokenize_query(raw: Optional[str]) -> list[str]:
    """正規化後按空白分詞，去除空 token。"""
    normalized = normalize_query(raw)
    return normalized.split(" ") if normalized else []


def build_search_filter(
    tokens: Sequence[str], columns: Sequence[ColumnElement]
) -> Optional[ColumnElement]:
    """組多關鍵字 ILIKE 過濾：每 token 對 columns 做 OR，token 之間 AND。

    - 每個 token 經 escape_like_pattern 跳脫 % / _，防萬用字元注入。
    - tokens 或 columns 為空 → 回 None（呼叫端據此不加 filter）。
    """
    if not tokens or not columns:
        return None
    per_token_clauses = []
    for tok in tokens:
        pat = f"%{escape_like_pattern(tok)}%"
        per_token_clauses.append(
            or_(*[col.ilike(pat, escape=LIKE_ESCAPE_CHAR) for col in columns])
        )
    return and_(*per_token_clauses)


def relevance_key(text: Optional[str], normalized_query: str) -> int:
    """相關性排序鍵（越小越相關）：0=完全符合、1=前綴、2=包含/其他。

    比對採 casefold（與 ILIKE 大小寫不敏感一致）；text 先 normalize 對齊。
    normalized_query 須為已 normalize_query 過的字串。
    """
    if not normalized_query:
        return 2
    t = normalize_query(text).casefold()
    nq = normalized_query.casefold()
    if t == nq:
        return 0
    if t.startswith(nq):
        return 1
    return 2
