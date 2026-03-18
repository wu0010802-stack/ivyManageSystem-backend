"""
utils/file_upload.validate_file_signature 單元測試。

涵蓋：
- 各合法格式的 magic bytes 通過驗證
- 內容與副檔名不符時 raise HTTPException(400)
- 內容長度不足時 raise HTTPException(400)
- 無 magic bytes 定義的類型（CSV、未知副檔名）直接略過
"""

import pytest
from fastapi import HTTPException

from utils.file_upload import validate_file_signature


# ── 測試用 magic bytes 資料 ────────────────────────────────────────────────

JPEG_BYTES  = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG_BYTES   = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
GIF_BYTES   = b"GIF89a" + b"\x00" * 100
PDF_BYTES   = b"%PDF-1.7" + b"\x00" * 100
# HEIC/HEIF: ISO base media format — "ftyp" 位於 offset 4，brand "heic" 於 offset 8
HEIC_BYTES  = b"\x00\x00\x00\x1c" + b"ftyp" + b"heic" + b"\x00" * 100
XLSX_BYTES  = b"PK\x03\x04" + b"\x00" * 100
XLS_BYTES   = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
BOGUS_BYTES = b"\x00\x01\x02\x03" * 30


# ── 合法格式：應通過驗證 ───────────────────────────────────────────────────

class TestValidFileSignatures:
    def test_jpeg_jpg(self):
        validate_file_signature(JPEG_BYTES, ".jpg")

    def test_jpeg_jpeg(self):
        validate_file_signature(JPEG_BYTES, ".jpeg")

    def test_png(self):
        validate_file_signature(PNG_BYTES, ".png")

    def test_gif(self):
        validate_file_signature(GIF_BYTES, ".gif")

    def test_pdf(self):
        validate_file_signature(PDF_BYTES, ".pdf")

    def test_heic(self):
        validate_file_signature(HEIC_BYTES, ".heic")

    def test_heif(self):
        validate_file_signature(HEIC_BYTES, ".heif")

    def test_xlsx(self):
        validate_file_signature(XLSX_BYTES, ".xlsx")

    def test_xls(self):
        validate_file_signature(XLS_BYTES, ".xls")

    def test_csv_no_magic_bytes_always_passes(self):
        """CSV 為純文字，無 magic bytes 定義，應直接通過"""
        validate_file_signature(b"name,age\nAlice,30\n", ".csv")

    def test_unknown_extension_passes(self):
        """不在白名單的副檔名不做驗證，直接通過"""
        validate_file_signature(BOGUS_BYTES, ".foo")

    def test_uppercase_extension_normalised(self):
        """副檔名大寫應被正規化為小寫後驗證"""
        validate_file_signature(JPEG_BYTES, ".JPG")

    def test_uppercase_png(self):
        validate_file_signature(PNG_BYTES, ".PNG")


# ── 非法格式：內容與副檔名不符 → HTTPException(400) ─────────────────────────

class TestSignatureMismatch:
    def test_jpeg_content_with_png_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(JPEG_BYTES, ".png")
        assert exc.value.status_code == 400

    def test_pdf_content_with_jpg_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(PDF_BYTES, ".jpg")
        assert exc.value.status_code == 400

    def test_png_content_with_pdf_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(PNG_BYTES, ".pdf")
        assert exc.value.status_code == 400

    def test_bogus_content_with_xlsx_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(BOGUS_BYTES, ".xlsx")
        assert exc.value.status_code == 400

    def test_bogus_content_with_xls_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(BOGUS_BYTES, ".xls")
        assert exc.value.status_code == 400

    def test_xlsx_content_with_jpg_ext(self):
        """XLSX（ZIP）偽裝成圖片"""
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(XLSX_BYTES, ".jpg")
        assert exc.value.status_code == 400

    def test_heic_content_with_png_ext(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(HEIC_BYTES, ".png")
        assert exc.value.status_code == 400


# ── 內容長度不足：應視為不符 → HTTPException(400) ────────────────────────────

class TestInsufficientContent:
    def test_empty_content_with_jpg(self):
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(b"", ".jpg")
        assert exc.value.status_code == 400

    def test_too_short_for_png_signature(self):
        """PNG magic bytes 需要 8 bytes，只給 4 bytes 應失敗"""
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(b"\x89PNG", ".png")
        assert exc.value.status_code == 400

    def test_too_short_for_heic_offset(self):
        """HEIC magic 在 offset 4，只給 3 bytes 應失敗"""
        with pytest.raises(HTTPException) as exc:
            validate_file_signature(b"\x00\x00\x00", ".heic")
        assert exc.value.status_code == 400

    def test_empty_content_csv_passes(self):
        """CSV 無 magic bytes 定義，空內容也應通過"""
        validate_file_signature(b"", ".csv")
