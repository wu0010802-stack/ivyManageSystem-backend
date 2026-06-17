"""上傳檔案共用工具：大小限制、magic bytes 驗證與內容讀取。"""

import os
import re

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB（預設 / 影像 / 文件）
MAX_VIDEO_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB（影片）

# 影片副檔名集合
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}

# ── Magic bytes 白名單 ─────────────────────────────────────────────────────
# 格式：副檔名（小寫） → (比對起始偏移量, 期望的位元組序列)
# 值為 None 表示該格式無 magic bytes（如 CSV），直接略過驗證。
MAGIC_SIGNATURES: dict[str, tuple[int, bytes] | None] = {
    ".jpg": (0, b"\xff\xd8\xff"),
    ".jpeg": (0, b"\xff\xd8\xff"),
    ".png": (0, b"\x89PNG\r\n\x1a\n"),
    ".gif": (0, b"GIF8"),
    ".pdf": (0, b"%PDF"),
    # HEIC/HEIF 使用 ISO Base Media File Format，"ftyp" 位於 offset 4
    ".heic": (4, b"ftyp"),
    ".heif": (4, b"ftyp"),
    # XLSX 本質是 ZIP archive
    ".xlsx": (0, b"PK\x03\x04"),
    # XLS 為 OLE2 Compound Document
    ".xls": (0, b"\xd0\xcf\x11\xe0"),
    # CSV 為純文字，無 magic bytes
    ".csv": None,
    # MP4 / MOV 同屬 ISO Base Media File Format（QuickTime / ISO/IEC 14496-12）
    # 第 4 byte 起是 "ftyp" box；檢查這段即可過濾非影片檔（實際品牌因裝置而異）
    ".mp4": (4, b"ftyp"),
    ".mov": (4, b"ftyp"),
    # WebM 為 Matroska 變體，首 4 byte 為 EBML header "\x1A\x45\xDF\xA3"
    ".webm": (0, b"\x1a\x45\xdf\xa3"),
}


def is_video_extension(extension: str) -> bool:
    """判斷副檔名是否為影片（決定 size limit）。"""
    return extension.lower() in VIDEO_EXTENSIONS


def max_upload_size_for(extension: str) -> int:
    """依副檔名取得 size limit。影片 50MB，其他 10MB。"""
    return MAX_VIDEO_UPLOAD_SIZE if is_video_extension(extension) else MAX_UPLOAD_SIZE


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


_UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB


async def read_upload_with_size_check(
    file: UploadFile,
    *,
    extension: str | None = None,
    max_bytes: int | None = None,
    size_error_detail: str | None = None,
) -> bytes:
    """以 chunked 方式讀取上傳內容，累計超過 size limit 立即中止避免 OOM。

    P0a 落地後行為（2026-05-28）：若 extension 在已知白名單，將同步在 helper 內
    執行 magic_bytes validate + image EXIF strip，確保平台基線清洗。既有 caller
    在外部仍 call validate_file_signature(content, ext) 是冗餘但無害（idempotent）。
    Refs: docs/superpowers/specs/2026-05-28-image-exif-strip-design.md

    Args:
        file:      FastAPI UploadFile
        extension: 若提供，依副檔名套用對應 size limit（影片 50MB / 其他 10MB）
                   未提供時沿用 MAX_UPLOAD_SIZE (10MB)，維持舊呼叫者向後相容
                   且不會觸發 validate / strip（行為等同 helper 落地前）
        max_bytes: 若提供，覆寫 size limit（優先於 extension 推得的上限）。用於
                   小於預設 10MB 的自訂上限，例如簽名圖 200KB / 教師請假附件 5MB。
                   讓這些端點也走 chunked 早停，而非先 read() 全檔再比大小（DoS 韌性）。
        size_error_detail: 若提供，超限時用此訊息。預設訊息以 MB 表示，對 <1MB 的
                   上限會顯示「0MB」不直觀，故允許呼叫端自訂。
    """
    if max_bytes is not None:
        limit = max_bytes
    elif extension:
        limit = max_upload_size_for(extension)
    else:
        limit = MAX_UPLOAD_SIZE
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            if size_error_detail is not None:
                raise HTTPException(status_code=400, detail=size_error_detail)
            mb = limit // (1024 * 1024)
            raise HTTPException(status_code=400, detail=f"檔案超過 {mb}MB 限制")
        chunks.append(chunk)
    content = b"".join(chunks)

    if extension:
        # magic_bytes 驗證（從外部 caller 移進來；舊 caller 在外仍 call 一次冗餘但無害）
        validate_file_signature(content, extension)

        # image 進入點 EXIF 清洗（必在 validate 之後；strip 內部對非 image ext no-op）
        from utils.image_sanitize import (
            IMAGE_EXTENSIONS_TO_SANITIZE,
            strip_image_metadata,
        )

        if extension.lower() in IMAGE_EXTENSIONS_TO_SANITIZE:
            content = strip_image_metadata(content, extension)

    return content


# 資安掃描 2026-05-07 P1：原始 filename 可能含「雙副檔名」(payload.pdf.exe) 或路徑
# 字元；雖 storage_key 用 UUID 不受影響，但 original_filename 會在 download
# Content-Disposition 與 UI 顯示時被使用，留下被誤導執行 / 路徑穿越的可能性。
# 一律 sanitize：strip 路徑成分、把所有內嵌 dot 換成底線、用驗證過的 ext 接尾。
_FILENAME_UNSAFE_CHARS = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")
_MAX_BASENAME_LEN = 100


def safe_attachment_filename(raw_name: str, validated_ext: str) -> str:
    """產生安全的 original_filename。

    - 跨平台剝除目錄成分（split on `/` 與 `\\`，避免 Windows 上傳路徑殘留）
    - 控制字元 / 平台禁字 → 底線
    - basename 內所有 `.` 都換成底線（避免 .exe.pdf 雙副檔名 / .htaccess 隱藏檔）
    - 最終長度限制 100 字
    - 用 validated_ext 接尾（呼叫端已對 ext 套白名單）

    輸入空字串或全部被剝光時，回傳 `attachment<ext>`。
    """
    if not raw_name:
        return f"attachment{validated_ext.lower()}"
    # Windows 路徑用 \、Unix 用 /；Python os.path.basename 在 Unix 不切 \，手動處理
    base = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _ = os.path.splitext(base)
    cleaned = _FILENAME_UNSAFE_CHARS.sub("_", stem)
    cleaned = cleaned.replace(".", "_").strip("._-")
    if not cleaned:
        cleaned = "attachment"
    cleaned = cleaned[:_MAX_BASENAME_LEN]
    return f"{cleaned}{validated_ext.lower()}"
