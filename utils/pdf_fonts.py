"""PDF 中文字型統一註冊（reportlab）。

為什麼用 TTF embed 而非 reportlab 內建 CID font（如 STSong-Light/MSung-Light）：
    CID font 只是「stub reference」— PDF 內不嵌字型 outline，由 viewer 自行 fallback。
    Adobe Reader / macOS Preview 認得 Adobe-CNS1 集；**Chrome PDFium 不內建繁中 fallback**，
    缺 outline 的字會 silently 空白（典型症狀：邊框/checkbox 都在，文字節點消失）。
    embed TTF 把 glyph outline 直接放進 PDF，所有 viewer 都能正確 render。

字型：Noto Sans TC Regular（Google Fonts，SIL OFL 授權，可商用），
    bundled 於 assets/fonts/NotoSansTC-Regular.ttf。

子集化（subsetted）：原始 Noto Sans TC Regular VF 為 11.9 MB（包含全 CJK Ext B + variable
    font axes 等本系統用不到的資料）。bundle 版為 pyftsubset 處理後的 5.7 MB 子集，
    保留範圍涵蓋全 BMP CJK（U+4E00-9FFF + Ext A）含罕用人名字（例：凃/淼/婭/瑄）。
    重 subset 指令見 assets/fonts/README.md。
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

    Notes:
        會同時註冊 family mapping，讓 reportlab Paragraph 的 <b>/<i>/<bi>
        標籤不會 fallback 到 Helvetica-Bold（無 CJK glyph 會變方塊）。
        reportlab 4.x 的 TTFont 預設已自動填 family map，本呼叫為防禦性鎖定，
        避免 reportlab 版本變更或 ParagraphStyle parent 繼承的 corner case 翻車。
    """
    if CJK_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        if not os.path.isfile(_FONT_PATH):
            raise FileNotFoundError(
                f"找不到中文字型：{_FONT_PATH}\n"
                "請確認 assets/fonts/NotoSansTC-Regular.ttf 存在（repo 已 bundle）。"
            )
        pdfmetrics.registerFont(TTFont(CJK_FONT_NAME, _FONT_PATH))
        # 把同一個 TTF 註冊成 family 的 normal/bold/italic/boldItalic，
        # 避免 <b>...</b> 中文走 Helvetica-Bold fallback 失字。
        # 未來如要支援真正 bold weight，可另 bundle NotoSansTC-Bold.ttf。
        pdfmetrics.registerFontFamily(
            CJK_FONT_NAME,
            normal=CJK_FONT_NAME,
            bold=CJK_FONT_NAME,
            italic=CJK_FONT_NAME,
            boldItalic=CJK_FONT_NAME,
        )
    return CJK_FONT_NAME
