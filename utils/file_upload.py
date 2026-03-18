"""上傳檔案共用工具：大小限制、magic bytes 驗證與內容讀取。"""
from fastapi import HTTPException, UploadFile

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# ── Magic bytes 白名單 ─────────────────────────────────────────────────────
# 格式：副檔名（小寫） → (比對起始偏移量, 期望的位元組序列)
# 值為 None 表示該格式無 magic bytes（如 CSV），直接略過驗證。
MAGIC_SIGNATURES: dict[str, tuple[int, bytes] | None] = {
    ".jpg":  (0, b"\xff\xd8\xff"),
    ".jpeg": (0, b"\xff\xd8\xff"),
    ".png":  (0, b"\x89PNG\r\n\x1a\n"),
    ".gif":  (0, b"GIF8"),
    ".pdf":  (0, b"%PDF"),
    # HEIC/HEIF 使用 ISO Base Media File Format，"ftyp" 位於 offset 4
    ".heic": (4, b"ftyp"),
    ".heif": (4, b"ftyp"),
    # XLSX 本質是 ZIP archive
    ".xlsx": (0, b"PK\x03\x04"),
    # XLS 為 OLE2 Compound Document
    ".xls":  (0, b"\xd0\xcf\x11\xe0"),
    # CSV 為純文字，無 magic bytes
    ".csv":  None,
}


def validate_file_signature(content: bytes, extension: str) -> None:
    """驗證檔案內容的 magic bytes 是否符合副檔名所聲稱的格式。

    - 副檔名不在 MAGIC_SIGNATURES 白名單中：直接略過（不封鎖未知類型）
    - 副檔名對應的簽名為 None（如 CSV）：直接略過
    - 內容長度不足以涵蓋簽名位置，或實際 bytes 不符：raise HTTPException(400)

    Args:
        content:   已讀取的完整檔案內容。
        extension: 包含點號的副檔名，如 ".jpg"（大小寫均可）。
    """
    ext = extension.lower()
    sig = MAGIC_SIGNATURES.get(ext)
    if sig is None:
        return  # 無 magic bytes 定義或不在白名單，略過

    offset, expected = sig
    end = offset + len(expected)
    actual = content[offset:end] if len(content) >= end else b""
    if actual != expected:
        raise HTTPException(
            status_code=400,
            detail=f"檔案內容與副檔名（{extension}）不符，請確認上傳的檔案未損壞或遭竄改",
        )


async def read_upload_with_size_check(file: UploadFile) -> bytes:
    """讀取上傳檔案內容，超過 10 MB 則回傳 400。"""
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="檔案超過 10MB 限制")
    return content
