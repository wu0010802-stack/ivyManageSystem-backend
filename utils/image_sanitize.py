"""utils/image_sanitize.py — 影像 metadata 清洗（EXIF / XMP / IPTC / ICC profile）。

P0a 兒童照片位置個資保護：iPhone 拍照預設嵌入 GPS / 相機序號 / 拍攝時間，
家長/教師上傳到 portfolio 後若以原檔提供下載，將形成跨家庭位置外洩通道
（個資法 §6 兒童特種個資、COPPA §312.4(b)、GDPR Recital 51）。

設計：純函式 + caller-side 透明處理。
- ImageOps.exif_transpose 把 Orientation tag 套到像素後丟棄 EXIF
- JPEG 用 quality=95 重 encode（無法用 "keep"：exif_transpose 會建立新 Image
  失去原 JPEG 格式 hint，quality="keep" 需 image.format == "JPEG"）。
  品質差異對家長端展示視覺不可分辨，trade-off 可接受。
- WebP 用 quality=85（業界 default）
- PNG 不指定 quality，optimize=False 保 chunk 結構
- ICC profile / XMP / IPTC 一律捨棄（profile 可能含 device id）

Refs: spec docs/superpowers/specs/2026-05-28-image-exif-strip-design.md
"""

from __future__ import annotations

import io
import logging

from fastapi import HTTPException
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# 第一版支援清洗的副檔名集合（小寫含點）。
# HEIC/HEIF/GIF 第一版不處理：HEIC 走 portfolio variants transcode 為 JPG 等同 strip；
# GIF 罕見 GPS tag 不在主要威脅面。列為 follow-up。
IMAGE_EXTENSIONS_TO_SANITIZE: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp"}
)


def strip_image_metadata(content: bytes, ext: str) -> bytes:
    """清除影像 metadata，回傳乾淨 bytes。

    Orientation 透過 ImageOps.exif_transpose 套到像素後丟棄 tag，
    使客戶端任何時候 view 都是正向（不需依賴 EXIF Orientation）。

    Args:
        content: 影像原始 bytes
        ext:     副檔名含點（如 ".jpg"），會 lower()

    Returns:
        清洗後的 bytes。若 ext 不在 IMAGE_EXTENSIONS_TO_SANITIZE 直接回原 content。

    Raises:
        HTTPException(400): PIL 解析失敗或 DecompressionBomb
        HTTPException(500): 重 encode 失敗（不靜默回原檔，否則漏網）
    """
    ext_lower = ext.lower()
    if ext_lower not in IMAGE_EXTENSIONS_TO_SANITIZE:
        return content

    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Image.DecompressionBombError as exc:
        logger.warning(
            "image_sanitize 拒絕 decompression bomb（%s）：%s", ext_lower, exc
        )
        raise HTTPException(
            status_code=400, detail="影像尺寸超過上限，請壓縮後重新上傳"
        )
    except Exception as exc:
        logger.warning("image_sanitize 解析失敗（%s）：%s", ext_lower, exc)
        raise HTTPException(status_code=400, detail="影像格式不支援或損毀")

    # 把 Orientation tag 套到像素 → 丟掉 EXIF（含 GPS / Make / Model / Software / DateTime）
    image = ImageOps.exif_transpose(image)

    # 重 encode 為 BytesIO，不寫 EXIF/XMP/IPTC/ICC profile
    out = io.BytesIO()
    save_kwargs: dict = {}
    fmt: str

    if ext_lower in (".jpg", ".jpeg"):
        fmt = "JPEG"
        # quality="keep" 需要原 image.format == "JPEG"，但 ImageOps.exif_transpose
        # 會建立新 Image 物件失去 format hint → 改用 quality=95 重 encode；
        # 視覺差異對家長端不可分辨，metadata 安全優先。
        save_kwargs = {"quality": 95}
        # 確保非 RGB（如 P / RGBA / CMYK）能存成 JPEG
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        elif image.mode not in ("RGB", "L", "CMYK"):
            image = image.convert("RGB")
    elif ext_lower == ".png":
        fmt = "PNG"
        save_kwargs = {"optimize": False}
    elif ext_lower == ".webp":
        fmt = "WebP"
        save_kwargs = {"quality": 85}
    else:
        # 不會到這（IMAGE_EXTENSIONS_TO_SANITIZE 已 guard）
        return content

    try:
        # 明確不傳 exif / icc_profile / xmp → 全部丟棄
        image.save(out, format=fmt, **save_kwargs)
    except Exception as exc:
        logger.error(
            "image_sanitize 重 encode 失敗（%s）：%s", ext_lower, exc, exc_info=True
        )
        raise HTTPException(status_code=500, detail="影像處理失敗，請稍後再試")

    return out.getvalue()
