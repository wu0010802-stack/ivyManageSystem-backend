# tests/test_storage_backend.py
"""
測試 utils/storage.py 的 StorageBackend Protocol 與 LocalStorage 實作。

設計目標：
- LocalStorage 完整覆蓋 save / read / delete / exists / public_url / signed_url
- get_backend() 依 STORAGE_BACKEND env var 切換、singleton 快取
"""

import os
from pathlib import Path

import pytest

from utils.storage import LocalStorage, StorageBackend, get_backend


@pytest.fixture
def local_root(tmp_path, monkeypatch):
    """每測試一個獨立的 STORAGE_ROOT，避免污染。"""
    root = tmp_path / "uploads"
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    # 重置 backend singleton，確保新 env 生效
    import utils.storage as storage_mod

    storage_mod._BACKEND_SINGLETON = None
    return root


def test_local_save_and_read_round_trip(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "abc.png", b"PNGDATA", "image/png")

    assert backend.read("activity_posters", "abc.png") == b"PNGDATA"
    assert (local_root / "activity_posters" / "abc.png").read_bytes() == b"PNGDATA"


def test_local_save_creates_subdirectories(local_root):
    backend = LocalStorage()
    backend.save("leave_attachments", "42/photo.jpg", b"JPGDATA", "image/jpeg")

    assert backend.read("leave_attachments", "42/photo.jpg") == b"JPGDATA"
    assert (local_root / "leave_attachments" / "42" / "photo.jpg").exists()


def test_local_exists_true_and_false(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "exists.png", b"X", "image/png")

    assert backend.exists("activity_posters", "exists.png") is True
    assert backend.exists("activity_posters", "missing.png") is False


def test_local_delete_removes_file(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "doomed.png", b"X", "image/png")

    backend.delete("activity_posters", "doomed.png")

    assert backend.exists("activity_posters", "doomed.png") is False


def test_local_delete_missing_file_is_idempotent(local_root):
    backend = LocalStorage()
    # 應該不 raise
    backend.delete("activity_posters", "never_existed.png")


def test_local_public_url_activity_poster(local_root):
    backend = LocalStorage()
    url = backend.public_url("activity_posters", "abc123.png")
    assert url == "/api/activity/public/poster/abc123.png"


def test_local_public_url_leave_attachment(local_root):
    backend = LocalStorage()
    url = backend.public_url("leave_attachments", "42/photo.jpg")
    assert url == "/api/leaves/42/photo.jpg"


def test_local_signed_url_returns_public_path(local_root):
    """local 模式下 signed 與 public 等價（後端 JWT 守衛）。"""
    backend = LocalStorage()
    url = backend.signed_url("leave_attachments", "42/photo.jpg", ttl_seconds=3600)
    assert url == "/api/leaves/42/photo.jpg"


def test_local_public_url_rejects_unknown_module(local_root):
    backend = LocalStorage()
    with pytest.raises(ValueError):
        backend.public_url("attendance_imports", "x.xlsx")


def test_get_backend_default_is_local(local_root):
    backend = get_backend()
    assert isinstance(backend, LocalStorage)


def test_get_backend_singleton_within_process(local_root):
    b1 = get_backend()
    b2 = get_backend()
    assert b1 is b2


def test_get_backend_invalid_value_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "not_a_real_backend")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    import utils.storage as storage_mod

    storage_mod._BACKEND_SINGLETON = None

    with pytest.raises(ValueError, match="未知的 STORAGE_BACKEND"):
        get_backend()
