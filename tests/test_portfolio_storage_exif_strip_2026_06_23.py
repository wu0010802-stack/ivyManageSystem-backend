"""P2-4 回歸（2026-06-23 全系統資安掃描）：put_attachment 原檔落盤須去 EXIF。

put_attachment 為 10 個附件 caller（vendor_payments / announcements / contact_book /
parent_messages / medications / leaves / events / consent…）共用的原檔落盤點。
原本 storage_key 對應的原檔 raw write_bytes(content) 未清洗 → 下載端點原樣回傳，
保留 iPhone HEIC/JPEG 的 GPS/相機序號。此處統一 strip 覆蓋全部 caller。

DB 無關，純檔案 IO（tmp_path）。
"""

import io

import pytest
from PIL import Image
from PIL.ExifTags import Base as ExifBase

from utils.portfolio_storage import LocalStorage


def _exif_keys(content: bytes) -> set[int]:
    return set(Image.open(io.BytesIO(content)).getexif().keys())


def _jpeg_with_make(make: str = "TestPhone") -> bytes:
    img = Image.new("RGB", (20, 20), color=(1, 2, 3))
    exif = img.getexif()
    exif[ExifBase.Make.value] = make
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif, quality=90)
    return buf.getvalue()


def test_put_attachment_strips_exif_from_jpeg_original(tmp_path):
    storage = LocalStorage(tmp_path)
    original = _jpeg_with_make()
    assert ExifBase.Make.value in _exif_keys(original), "fixture 應含 Make"

    stored = storage.put_attachment(original, ".jpg")
    raw = storage.read(stored.storage_key)

    assert ExifBase.Make.value not in _exif_keys(raw), "原檔落盤須去 EXIF"


def test_put_attachment_strips_exif_from_heic_original(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    img = Image.new("RGB", (30, 30), color=(5, 5, 5))
    exif = img.getexif()
    exif[ExifBase.Make.value] = "HeicPhone"
    buf = io.BytesIO()
    img.save(buf, format="HEIF", exif=exif)
    original = buf.getvalue()
    assert ExifBase.Make.value in _exif_keys(original), "fixture 應含 Make"

    storage = LocalStorage(tmp_path)
    stored = storage.put_attachment(original, ".heic")
    raw = storage.read(stored.storage_key)

    assert ExifBase.Make.value not in _exif_keys(raw), "HEIC 原檔落盤須去 EXIF/GPS"
    # 清洗後仍可解碼
    Image.open(io.BytesIO(raw)).load()
