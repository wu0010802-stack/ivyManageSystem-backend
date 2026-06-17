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

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
GIF_BYTES = b"GIF89a" + b"\x00" * 100
PDF_BYTES = b"%PDF-1.7" + b"\x00" * 100
# HEIC/HEIF: ISO base media format — "ftyp" 位於 offset 4，brand "heic" 於 offset 8
HEIC_BYTES = b"\x00\x00\x00\x1c" + b"ftyp" + b"heic" + b"\x00" * 100
XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 100
XLS_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100
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


# ── read_upload_with_size_check：chunked 早停 ─────────────────────────────────

import asyncio

from utils.file_upload import (
    MAX_UPLOAD_SIZE,
    MAX_VIDEO_UPLOAD_SIZE,
    read_upload_with_size_check,
)


class _FakeUpload:
    """模擬 UploadFile.read(chunk_size) 行為，並追蹤實際讀取量。

    P0a 落地後（2026-05-28）read_upload_with_size_check 會在 ext 已知時
    內部 call validate_file_signature，所以需要正確 magic bytes prefix。
    """

    def __init__(
        self, total_size: int, chunk_size: int = 64 * 1024, prefix: bytes = b""
    ):
        self._remaining = total_size
        self._chunk_size = chunk_size
        self.bytes_read = 0
        self._pos = 0
        self._prefix = prefix

    async def read(self, n: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        size = self._remaining if n < 0 else min(n, self._remaining)
        self._remaining -= size
        # 從 prefix 取（位元組序），prefix 用完則填 null
        out = bytearray(size)
        for i in range(size):
            if self._pos < len(self._prefix):
                out[i] = self._prefix[self._pos]
            else:
                out[i] = 0
            self._pos += 1
        self.bytes_read += size
        return bytes(out)


# MP4 ISO base media format magic bytes：offset 4 起 "ftyp"
_MP4_PREFIX = b"\x00\x00\x00\x20ftypmp42\x00" * 4  # 80 bytes prefix


class TestReadUploadChunked:
    def test_returns_full_content_under_limit(self):
        """無 ext 路徑：保持向後相容，不觸發 validate。"""
        f = _FakeUpload(total_size=1024)
        out = asyncio.run(read_upload_with_size_check(f))
        assert len(out) == 1024
        assert f.bytes_read == 1024

    def test_aborts_early_when_exceeds_default_limit(self):
        """1.1 GB body 應在略超過 10MB 後立即中止，而非先全載入。"""
        f = _FakeUpload(total_size=1100 * 1024 * 1024)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(read_upload_with_size_check(f))
        assert exc.value.status_code == 400
        # 早停：實際讀取量不應超過 limit + 1 個 chunk
        assert f.bytes_read <= MAX_UPLOAD_SIZE + 64 * 1024

    def test_video_extension_uses_video_limit(self):
        """MP4 ext + 真實 magic bytes prefix → 通過 size + validate。"""
        f = _FakeUpload(total_size=20 * 1024 * 1024, prefix=_MP4_PREFIX)
        out = asyncio.run(read_upload_with_size_check(f, extension=".mp4"))
        assert len(out) == 20 * 1024 * 1024

    def test_aborts_early_when_exceeds_video_limit(self):
        f = _FakeUpload(total_size=200 * 1024 * 1024, prefix=_MP4_PREFIX)
        with pytest.raises(HTTPException):
            asyncio.run(read_upload_with_size_check(f, extension=".mp4"))
        assert f.bytes_read <= MAX_VIDEO_UPLOAD_SIZE + 64 * 1024

    # ── max_bytes 自訂上限（簽名圖 200KB / 教師請假附件 5MB 等小於預設者）────────
    def test_max_bytes_override_under_limit_returns_content(self):
        """max_bytes 自訂上限：未超過時回完整內容。"""
        f = _FakeUpload(total_size=100 * 1024)
        out = asyncio.run(read_upload_with_size_check(f, max_bytes=200 * 1024))
        assert len(out) == 100 * 1024
        assert f.bytes_read == 100 * 1024

    def test_max_bytes_override_aborts_early(self):
        """max_bytes 自訂上限：超過時 chunked 早停，不先全載入避免 OOM/DoS。"""
        f = _FakeUpload(total_size=50 * 1024 * 1024)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(read_upload_with_size_check(f, max_bytes=200 * 1024))
        assert exc.value.status_code == 400
        # 早停：實際讀取量不應超過 max_bytes + 1 個 chunk
        assert f.bytes_read <= 200 * 1024 + 64 * 1024

    def test_max_bytes_custom_error_detail(self):
        """size_error_detail 提供時，超限錯誤訊息用呼叫端指定字串。"""
        f = _FakeUpload(total_size=50 * 1024 * 1024)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                read_upload_with_size_check(
                    f, max_bytes=200 * 1024, size_error_detail="簽名圖過大"
                )
            )
        assert exc.value.detail == "簽名圖過大"


# ── P0a 落地：integration tests for image strip 透明清洗 ───────────────────


class TestReadUploadImageSanitize:
    """integration: read_upload_with_size_check 對 image ext 自動 strip EXIF。"""

    def _fixture(self, name: str) -> bytes:
        from pathlib import Path

        return (Path(__file__).parent / "fixtures" / "exif" / name).read_bytes()

    def _upload(self, content: bytes) -> _FakeUpload:
        f = _FakeUpload(total_size=len(content), prefix=content)
        return f

    def test_image_with_gps_is_sanitized_on_read(self):
        """上傳含 GPS 的 JPEG 經 helper 讀取後 metadata 應已清。"""
        import io

        from PIL.ExifTags import Base as ExifBase
        from PIL import Image

        original = self._fixture("with_gps.jpg")
        f = self._upload(original)
        out = asyncio.run(read_upload_with_size_check(f, extension=".jpg"))
        # 內容應已被替換（非原始 bytes）
        assert out != original
        img = Image.open(io.BytesIO(out))
        assert ExifBase.GPSInfo.value not in img.getexif().keys()
        assert ExifBase.Make.value not in img.getexif().keys()

    def test_non_image_pdf_passes_through_unchanged(self):
        """PDF 不在 image 白名單 → strip 不觸發；只走 size + validate。"""
        pdf = b"%PDF-1.4\n" + b"\x00" * 2048
        f = self._upload(pdf)
        out = asyncio.run(read_upload_with_size_check(f, extension=".pdf"))
        # PDF 不被 strip，bytes 應完全一致
        assert out == pdf

    def test_jpeg_signature_mismatch_raises_400(self):
        """副檔名 .jpg 但內容是 PNG → 內部 validate 應 raise 400。"""
        png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        f = self._upload(png_content)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(read_upload_with_size_check(f, extension=".jpg"))
        assert exc.value.status_code == 400

    def test_orientation_applied_to_pixels_through_helper(self):
        """Orientation=6 樣本經 helper 後像素應已旋轉。"""
        import io

        from PIL import Image

        original = self._fixture("with_orientation_6.jpg")
        orig_img = Image.open(io.BytesIO(original))
        assert orig_img.size == (100, 60)  # raw 橫長

        f = self._upload(original)
        out = asyncio.run(read_upload_with_size_check(f, extension=".jpg"))

        cleaned_img = Image.open(io.BytesIO(out))
        # 套到像素後 view dimension 變成 (60, 100)
        assert cleaned_img.size == (60, 100)
