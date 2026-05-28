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


class TestSentryTaggedCapture:
    """Phase 1 P1 resilience：Supabase Storage exception 須呼叫 tagged_capture."""

    def test_save_exception_calls_tagged_capture(self, mock_supabase, monkeypatch):
        # Phase 4：save() 失敗後會嘗試 local fallback；patch _enqueue 跳 DB
        # 讓 tagged_capture 只被呼叫一次（主要失敗）
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = RuntimeError("bucket down")
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)
        monkeypatch.setattr(
            "utils.supabase_storage._enqueue_pending_upload", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "utils.supabase_storage._FALLBACK_ROOT",
            __import__("pathlib").Path("/tmp/test_fallback_root"),
        )
        import pathlib
        pathlib.Path("/tmp/test_fallback_root/activity_posters").mkdir(parents=True, exist_ok=True)
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            # With fallback enabled, save() no longer raises — it silently falls back
            backend.save("activity_posters", "x.png", b"X", "image/png")
            mock_capture.assert_called()
            # First call must be for supabase tag
            first_call = mock_capture.call_args_list[0]
            assert first_call.kwargs.get("tag") == "supabase" \
                or first_call.args[1] == "supabase"

    def test_read_exception_calls_tagged_capture(self, mock_supabase):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.download.side_effect = ConnectionError("net")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            with pytest.raises(ConnectionError):
                backend.read("activity_posters", "x.png")
            mock_capture.assert_called_once()

    def test_delete_exception_still_idempotent(self, mock_supabase):
        """delete 既有 idempotent 語意（不拋）保留，但仍呼叫 tagged_capture."""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.remove.side_effect = RuntimeError("net")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            # 既有行為：不拋；Phase 1 加 tagged_capture
            backend.delete("activity_posters", "x.png")
            mock_capture.assert_called_once()

    def test_signed_url_exception_calls_tagged_capture(self, mock_supabase):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.create_signed_url.side_effect = RuntimeError("auth")
        with patch("utils.supabase_storage.tagged_capture") as mock_capture:
            with pytest.raises(RuntimeError):
                backend.signed_url("leave_attachments", "x.pdf", 60)
            mock_capture.assert_called_once()


class TestPhase4Fallback:
    """Phase 4 P1 resilience：retry + local fallback tests."""

    def test_save_retries_then_fallback_writes_local(
        self, mock_supabase, tmp_path, monkeypatch
    ):
        """upload 持續失敗 → fallback 寫本機檔，呼叫端不 raise。"""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = ConnectionError("supabase down")
        # 把 fallback root 指向 tmp_path 避免真寫磁碟
        monkeypatch.setattr("utils.supabase_storage._FALLBACK_ROOT", tmp_path / "uploads_pending")
        # 跳 sleep（retry backoff）
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)
        # patch _enqueue_pending_upload 跳 DB（這個 test 只驗 local file）
        enqueued = []
        monkeypatch.setattr(
            "utils.supabase_storage._enqueue_pending_upload",
            lambda *a, **kw: enqueued.append(a),
        )

        # 不應 raise
        backend.save("activity_posters", "x.png", b"PNGDATA", "image/png")

        # local file 存在且內容正確
        files = list((tmp_path / "uploads_pending" / "activity_posters").iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == b"PNGDATA"
        # _enqueue 有被呼叫
        assert len(enqueued) == 1

    def test_save_fallback_disabled_raises(self, mock_supabase, monkeypatch):
        """STORAGE_LOCAL_FALLBACK_ENABLED=false → 失敗照樣 raise。"""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = ConnectionError("down")
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        from config import settings
        monkeypatch.setattr(settings.storage, "local_fallback_enabled", False, raising=False)

        with pytest.raises(ConnectionError):
            backend.save("activity_posters", "x.png", b"X", "image/png")

    def test_save_retry_succeeds_on_second_attempt(self, mock_supabase, monkeypatch):
        """第 1 次 fail 第 2 次 success → 不進 fallback。"""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        attempts = {"n": 0}

        def maybe_upload(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return None

        bucket.upload.side_effect = maybe_upload
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        # 不應 raise（第 2 次成功）
        backend.save("activity_posters", "y.png", b"Y", "image/png")
        assert attempts["n"] == 2

