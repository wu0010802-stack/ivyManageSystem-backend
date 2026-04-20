"""
共用搜尋工具：SQL LIKE / ILIKE 安全組合。
"""


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
