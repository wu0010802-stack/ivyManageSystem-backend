"""HTTP 標頭組裝小工具。

集中處理 Content-Disposition 的 RFC 5987 編碼，避免各匯出端點各自手寫
f"...filename*=UTF-8''{filename}" 時漏掉 quote()，導致中文檔名在 Starlette
以 latin-1 編碼 raw header 時 UnicodeEncodeError → 500。
"""

from urllib.parse import quote


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """回傳 latin-1 安全的 Content-Disposition 值（支援中文檔名）。

    一律走 RFC 5987 `filename*=UTF-8''<percent-encoded>`；filename 不論是否含
    非 ASCII 字元都先 quote()，確保產出的 header 值可被 latin-1 編碼。

    Args:
        filename: 原始檔名（可含中文）。
        inline: True → `inline`（瀏覽器直接顯示）；False → `attachment`（下載）。
    """
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename*=UTF-8''{quote(filename)}"
