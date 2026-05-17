"""PDF 中文字型統一註冊（reportlab）。

為什麼用 TTF embed 而非 reportlab 內建 CID font（如 STSong-Light/MSung-Light）：
    CID font 只是「stub reference」— PDF 內不嵌字型 outline，由 viewer 自行 fallback。
    Adobe Reader / macOS Preview 認得 Adobe-CNS1 集；**Chrome PDFium 不內建繁中 fallback**，
    缺 outline 的字會 silently 空白（典型症狀：邊框/checkbox 都在，文字節點消失）。
    embed TTF 把 glyph outline 直接放進 PDF，所有 viewer 都能正確 render。

字型：Noto Sans TC Regular（Google Fonts，SIL OFL 授權，可商用），
    bundled 於 assets/fonts/NotoSansTC-Regular.ttf。
"""

from __future__ import annotations

import os
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

CJK_FONT_NAME = "NotoSansTC"

_FONT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "fonts",
    "NotoSansTC-Regular.ttf",
)


def register_cjk_font() -> str:
    """註冊繁中字型（idempotent，可重複呼叫）。

    Returns:
        字型名稱字串（供 setFont / fontName 用）。
    """
    if CJK_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        if not os.path.isfile(_FONT_PATH):
            raise FileNotFoundError(
                f"找不到中文字型：{_FONT_PATH}\n"
                "請確認 assets/fonts/NotoSansTC-Regular.ttf 存在（repo 已 bundle）。"
            )
        pdfmetrics.registerFont(TTFont(CJK_FONT_NAME, _FONT_PATH))
    return CJK_FONT_NAME
