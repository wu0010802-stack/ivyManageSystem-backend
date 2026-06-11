"""tests/test_image_sanitize.py — P0a EXIF strip 純函式單元測試。

Refs: docs/superpowers/specs/2026-05-28-image-exif-strip-design.md §4.2
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from PIL.ExifTags import Base as ExifBase

from utils.image_sanitize import (
    IMAGE_EXTENSIONS_TO_SANITIZE,
    strip_image_metadata,
)

FIXTURES = Path(__file__).parent / "fixtures" / "exif"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _exif_keys(content: bytes) -> set[int]:
    """回傳影像 top-level EXIF tag id 集合（不含 GPS sub-IFD）"""
    img = Image.open(io.BytesIO(content))
    return set(img.getexif().keys())


def _gps_keys(content: bytes) -> set[int]:
    """回傳影像 GPS IFD tag id 集合"""
    img = Image.open(io.BytesIO(content))
    return set(img.getexif().get_ifd(ExifBase.GPSInfo.value).keys())


# ── 主路徑：GPS / Make / Model 清除 ──


def test_strip_removes_gps_tags_from_jpeg():
    """清洗後 EXIF GPSInfo tag 應消失"""
    original = _load("with_gps.jpg")
    assert ExifBase.GPSInfo.value in _exif_keys(original)
    assert _gps_keys(original), "fixture 應含 GPS sub-IFD"

    cleaned = strip_image_metadata(original, ".jpg")

    assert ExifBase.GPSInfo.value not in _exif_keys(cleaned)
    assert not _gps_keys(cleaned), "GPS sub-IFD 應為空"


def test_strip_removes_make_model_software():
    """清洗後 Make / Model / Software / DateTime 應消失"""
    original = _load("with_gps.jpg")
    cleaned = strip_image_metadata(original, ".jpg")
    keys = _exif_keys(cleaned)
    assert ExifBase.Make.value not in keys
    assert ExifBase.Model.value not in keys
    assert ExifBase.Software.value not in keys
    assert ExifBase.DateTime.value not in keys


# ── Orientation 套到像素 ──


def test_orientation_is_applied_to_pixels_and_tag_dropped():
    """Orientation=6 (CW 90°) 樣本清洗後：像素應已旋轉、Orientation tag 不應殘留"""
    original = _load("with_orientation_6.jpg")
    original_img = Image.open(io.BytesIO(original))
    assert original_img.getexif().get(ExifBase.Orientation.value) == 6
    # fixture 為橫長 100x60；Orientation=6 表示 view 時應旋轉成 60x100
    assert original_img.size == (100, 60)

    cleaned = strip_image_metadata(original, ".jpg")

    cleaned_img = Image.open(io.BytesIO(cleaned))
    # exif_transpose 應已把 Orientation=6 套到像素 → size 變 60x100（viewer 原本看到的形狀）
    assert cleaned_img.size == (60, 100)
    # Orientation tag 不應殘留（或為 1=normal）
    orientation = cleaned_img.getexif().get(ExifBase.Orientation.value)
    assert orientation in (None, 1)


# ── 不在白名單的 ext → 不處理 ──


def test_strip_passes_through_pdf_unchanged():
    pdf = b"%PDF-1.4\n%random binary"
    out = strip_image_metadata(pdf, ".pdf")
    assert out == pdf


def test_strip_passes_through_heic_unchanged():
    # v1 不處理 HEIC，直接回原 content
    heic_bytes = b"\x00\x00\x00\x20ftypheic"
    out = strip_image_metadata(heic_bytes, ".heic")
    assert out == heic_bytes


def test_strip_passes_through_gif_unchanged():
    gif_bytes = b"GIF89a" + b"\x00" * 30
    out = strip_image_metadata(gif_bytes, ".gif")
    assert out == gif_bytes


# ── 錯誤處理 ──


def test_strip_raises_400_on_corrupted_jpeg():
    with pytest.raises(HTTPException) as exc_info:
        strip_image_metadata(b"not an image at all", ".jpg")
    assert exc_info.value.status_code == 400
    assert "格式不支援或損毀" in exc_info.value.detail


def test_strip_raises_400_on_empty_bytes_for_image_ext():
    with pytest.raises(HTTPException) as exc_info:
        strip_image_metadata(b"", ".png")
    assert exc_info.value.status_code == 400


def test_strip_raises_400_on_fake_webp():
    """Finding 檔Low-1：.webp 無 magic bytes 條目，validate_file_signature 會略過；
    唯一防線是 strip 的 PIL 重解碼。假 webp（HTML 內容）須被拒，海報端點才安全。"""
    with pytest.raises(HTTPException) as exc_info:
        strip_image_metadata(b"<html>not an image</html>", ".webp")
    assert exc_info.value.status_code == 400


# ── PNG / WebP 路徑 ──


def test_strip_png_clean_passthrough_remains_valid_png():
    """無 metadata 的 PNG 清洗後仍為合法 PNG。"""
    original = _load("clean.png")
    cleaned = strip_image_metadata(original, ".png")
    img = Image.open(io.BytesIO(cleaned))
    img.verify()
    assert img.format == "PNG"


def test_strip_webp_basic():
    """生成一個 WebP 嵌入 EXIF，清洗後 metadata 應已清。"""
    src = Image.new("RGB", (40, 40), color=(10, 20, 30))
    exif = src.getexif()
    exif[ExifBase.Make.value] = "TestMake"
    buf = io.BytesIO()
    src.save(buf, format="WebP", exif=exif, quality=90)
    original = buf.getvalue()

    cleaned = strip_image_metadata(original, ".webp")
    cleaned_img = Image.open(io.BytesIO(cleaned))
    assert cleaned_img.format == "WEBP"  # Pillow uppercase
    # WebP 清洗後不應殘留 Make
    assert ExifBase.Make.value not in _exif_keys(cleaned)


# ── 尺寸保留（無 Orientation 時） ──


def test_strip_preserves_dimensions_for_non_rotated_image():
    original = _load("with_gps.jpg")  # Orientation 未設定，預設 1
    original_img = Image.open(io.BytesIO(original))
    cleaned = strip_image_metadata(original, ".jpg")
    cleaned_img = Image.open(io.BytesIO(cleaned))
    assert original_img.size == cleaned_img.size


# ── 白名單常數 ──


def test_image_extensions_to_sanitize_constant_is_lowercase_with_dot():
    for ext in IMAGE_EXTENSIONS_TO_SANITIZE:
        assert ext.startswith(".")
        assert ext == ext.lower()
    # 預期 4 個 v1 ext
    assert IMAGE_EXTENSIONS_TO_SANITIZE == {".jpg", ".jpeg", ".png", ".webp"}


# ── 大小寫 ext robustness ──


def test_strip_handles_uppercase_ext():
    original = _load("with_gps.jpg")
    cleaned = strip_image_metadata(original, ".JPG")
    # 應仍套 strip（內部 lower）
    assert ExifBase.GPSInfo.value not in _exif_keys(cleaned)
