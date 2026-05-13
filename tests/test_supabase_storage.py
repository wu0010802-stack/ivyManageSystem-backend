"""
測試 SupabaseStorage：mock storage3 client，不打真 Supabase。

策略：
- 用 unittest.mock 替換 supabase.create_client，回傳 MagicMock
- 驗證 save/delete/public_url/signed_url 傳給 client 的參數正確
- 驗證 module → bucket 名稱對應正確
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_supabase(monkeypatch, tmp_path):
    """準備乾淨的 SupabaseStorage 實例，內部 client 為 MagicMock。"""
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")

    import utils.storage as storage_mod

    storage_mod._BACKEND_SINGLETON = None

    with patch("utils.supabase_storage.create_client") as mock_create:
        client = MagicMock()
        mock_create.return_value = client
        from utils.supabase_storage import SupabaseStorage

        backend = SupabaseStorage()
        yield backend, client


def test_save_calls_upload_with_correct_bucket_and_key(mock_supabase):
    backend, client = mock_supabase
    backend.save("activity_posters", "abc.png", b"PNGDATA", "image/png")

    client.storage.from_.assert_called_with("activity-posters")
    bucket = client.storage.from_.return_value
    bucket.upload.assert_called_once()
    call_args = bucket.upload.call_args
    # supabase-py 接受 (path, file, file_options) — 驗證 path 與 file 內容
    assert call_args.kwargs.get("path") == "abc.png" or call_args.args[0] == "abc.png"


def test_save_uses_correct_bucket_for_leave_attachments(mock_supabase):
    backend, client = mock_supabase
    backend.save("leave_attachments", "42/photo.jpg", b"X", "image/jpeg")

    client.storage.from_.assert_called_with("leave-attachments")


def test_delete_calls_remove(mock_supabase):
    backend, client = mock_supabase
    backend.delete("activity_posters", "old.png")

    bucket = client.storage.from_.return_value
    bucket.remove.assert_called_once_with(["old.png"])


def test_public_url_returns_supabase_cdn_url(mock_supabase):
    backend, client = mock_supabase
    bucket = client.storage.from_.return_value
    bucket.get_public_url.return_value = (
        "https://example.supabase.co/storage/v1/object/public/activity-posters/abc.png"
    )

    url = backend.public_url("activity_posters", "abc.png")

    client.storage.from_.assert_called_with("activity-posters")
    bucket.get_public_url.assert_called_once_with("abc.png")
    assert url.startswith("https://example.supabase.co/")


def test_signed_url_passes_ttl_to_supabase(mock_supabase):
    backend, client = mock_supabase
    bucket = client.storage.from_.return_value
    bucket.create_signed_url.return_value = {
        "signedURL": "https://example.supabase.co/storage/v1/object/sign/leave-attachments/42/x.jpg?token=xxx"
    }

    url = backend.signed_url("leave_attachments", "42/x.jpg", ttl_seconds=3600)

    bucket.create_signed_url.assert_called_once_with("42/x.jpg", 3600)
    assert "token=" in url


def test_unknown_module_raises(mock_supabase):
    backend, _ = mock_supabase
    with pytest.raises(ValueError, match="未知 module"):
        backend.save("unknown_module", "x", b"X", "application/octet-stream")
