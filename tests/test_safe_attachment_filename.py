"""驗證 safe_attachment_filename：阻擋雙副檔名攻擊與路徑穿越在 original_filename 上的影響。

威脅：原 attach 端點直接用 file.filename 寫進 Attachment.original_filename。
家長上傳 `payload.pdf.exe` 時，雖然 splitext 取最後 ext = .exe 已被白名單擋下，
但若改名為 `evil.exe.pdf`：splitext 給 .pdf（白名單通過）+ magic bytes 若是 PDF 簽名
（polyglot file 可能達成）也通過 → 結果 original_filename 真的存成 `evil.exe.pdf`。
下載時 Content-Disposition 帶這串名稱，在 Windows 上可能誤導使用者。

修法：basename → 內嵌 . 換 _ → 用驗證過的 ext 接尾。

Refs: 資安掃描 2026-05-07 P1。
"""

import pytest

from utils.file_upload import safe_attachment_filename


class TestSafeAttachmentFilename:
    def test_strips_double_extension(self):
        """payload.pdf.exe → 假設 ext 已驗證為 .pdf，basename 內的 .pdf 會被換 _"""
        assert safe_attachment_filename("payload.pdf.exe", ".pdf") == "payload_pdf.pdf"

    def test_strips_double_extension_when_inner_is_dangerous(self):
        """evil.exe.pdf → 即使 ext 是 .pdf，原檔名 .exe 也會被中和"""
        assert safe_attachment_filename("evil.exe.pdf", ".pdf") == "evil_exe.pdf"

    def test_strips_directory_traversal(self):
        """../../etc/passwd 風格 → 只取 basename，且裡面的 . 換 _"""
        assert safe_attachment_filename("../../etc/passwd", ".pdf") == "passwd.pdf"

    def test_strips_windows_path_components(self):
        """C:\\Users\\name\\photo.jpg → 只取 basename，丟掉 drive letter 與資料夾"""
        assert (
            safe_attachment_filename("C:\\Users\\name\\photo.jpg", ".jpg")
            == "photo.jpg"
        )

    def test_strips_windows_path_with_double_extension(self):
        """C:\\windows\\evil.exe.pdf 偽裝雙副檔名 → basename 內嵌 . 換 _"""
        assert (
            safe_attachment_filename("C:\\windows\\evil.exe.pdf", ".pdf")
            == "evil_exe.pdf"
        )

    def test_handles_control_chars(self):
        """null byte / 控制字元 → 底線"""
        assert safe_attachment_filename("foo\x00bar.jpg", ".jpg") == "foo_bar.jpg"

    def test_empty_filename_falls_back(self):
        assert safe_attachment_filename("", ".pdf") == "attachment.pdf"

    def test_dotfile_strips_leading_dot(self):
        """`.env` 視為 dotfile：splitext 給 stem='.env'，內嵌 . → _，前置底線剝除"""
        # 攻擊面：避免 .htaccess / .env 等 Unix 隱藏檔語意被保留
        assert safe_attachment_filename(".env", ".pdf") == "env.pdf"

    def test_normalizes_extension_case(self):
        assert safe_attachment_filename("photo.JPG", ".JPG").endswith(".jpg") is True

    def test_preserves_chinese_filename(self):
        """中文檔名應保留"""
        result = safe_attachment_filename("聯絡簿照片.heic", ".heic")
        assert result == "聯絡簿照片.heic"

    def test_truncates_overly_long_basename(self):
        long_name = "a" * 200 + ".pdf"
        result = safe_attachment_filename(long_name, ".pdf")
        # basename 部分截到 100 字 + .pdf
        assert len(result) <= 104
        assert result.endswith(".pdf")

    def test_unsafe_chars_replaced(self):
        assert (
            safe_attachment_filename("foo<bar>:baz.jpg", ".jpg") == "foo_bar__baz.jpg"
        )
