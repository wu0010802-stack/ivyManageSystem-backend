"""tests/test_pending_uploads_scheduler.py — Phase 4 P1 resilience unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestTickPendingUploads:
    def test_picks_pending_row_and_uploads_succeeds(
        self, test_db_session, tmp_path, monkeypatch
    ):
        """到期 row 被撈到、上傳成功 → succeeded_at 設定、本機檔刪除。"""
        from models.pending_uploads import PendingUpload
        from services.notification.pending_uploads_scheduler import tick_pending_uploads

        # 建本機假檔
        local_file = tmp_path / "test.bin"
        local_file.write_bytes(b"TESTDATA")

        now = datetime.now(timezone.utc)
        row = PendingUpload(
            module="activity_posters",
            key="test/abc.png",
            content_type="image/png",
            local_path=str(local_file),
            attempts=0,
            next_retry_at=now - timedelta(seconds=10),
        )
        test_db_session.add(row)
        test_db_session.commit()

        # mock get_backend() → SupabaseStorage-like
        fake_bucket = MagicMock()
        fake_bucket.upload.return_value = None
        fake_client = MagicMock()
        fake_client.storage.from_.return_value = fake_bucket
        fake_backend = MagicMock()
        fake_backend.__class__.__name__ = "SupabaseStorage"
        fake_backend._client = fake_client

        monkeypatch.setattr(
            "utils.storage.get_backend",
            lambda: fake_backend,
        )

        result = tick_pending_uploads(now_provider=lambda: now)
        test_db_session.refresh(row)

        assert result["attempted"] == 1
        assert result["succeeded"] == 1
        assert row.succeeded_at is not None
        # local file should be removed
        assert not local_file.exists()

    def test_tick_increments_attempts_on_failure(
        self, test_db_session, tmp_path, monkeypatch
    ):
        """上傳失敗 → attempts+1、next_retry_at 延後、last_error 設定。"""
        from models.pending_uploads import PendingUpload
        from services.notification.pending_uploads_scheduler import tick_pending_uploads

        local_file = tmp_path / "fail.bin"
        local_file.write_bytes(b"DATA")

        now = datetime.now(timezone.utc)
        row = PendingUpload(
            module="activity_posters",
            key="fail/x.png",
            content_type="image/png",
            local_path=str(local_file),
            attempts=0,
            next_retry_at=now - timedelta(seconds=5),
        )
        test_db_session.add(row)
        test_db_session.commit()

        fake_bucket = MagicMock()
        fake_bucket.upload.side_effect = ConnectionError("timeout")
        fake_client = MagicMock()
        fake_client.storage.from_.return_value = fake_bucket
        fake_backend = MagicMock()
        fake_backend.__class__.__name__ = "SupabaseStorage"
        fake_backend._client = fake_client

        monkeypatch.setattr(
            "utils.storage.get_backend",
            lambda: fake_backend,
        )

        result = tick_pending_uploads(now_provider=lambda: now)
        test_db_session.refresh(row)

        assert result["failed"] == 1
        assert row.attempts == 1
        assert row.succeeded_at is None
        # SQLite 回 naive datetime；轉成 utc 再比或直接比 naive now
        retry_at = row.next_retry_at
        if retry_at is not None and retry_at.tzinfo is None:
            now_naive = now.replace(tzinfo=None)
            assert retry_at > now_naive
        else:
            assert retry_at > now
        assert "timeout" in (row.last_error or "")

    def test_fifth_attempt_marks_final(
        self, test_db_session, tmp_path, monkeypatch
    ):
        """第 5 次嘗試失敗 → attempts==5、last_error 以 'final:' 開頭、final_failed 計數。"""
        from models.pending_uploads import PendingUpload
        from services.notification.pending_uploads_scheduler import (
            tick_pending_uploads,
            _MAX_ATTEMPTS,
        )

        local_file = tmp_path / "final.bin"
        local_file.write_bytes(b"X")

        now = datetime.now(timezone.utc)
        row = PendingUpload(
            module="activity_posters",
            key="final/x.png",
            content_type="image/png",
            local_path=str(local_file),
            attempts=_MAX_ATTEMPTS - 1,  # 4 → next fail = final
            next_retry_at=now - timedelta(seconds=1),
        )
        test_db_session.add(row)
        test_db_session.commit()

        fake_bucket = MagicMock()
        fake_bucket.upload.side_effect = RuntimeError("permanent")
        fake_client = MagicMock()
        fake_client.storage.from_.return_value = fake_bucket
        fake_backend = MagicMock()
        fake_backend.__class__.__name__ = "SupabaseStorage"
        fake_backend._client = fake_client

        monkeypatch.setattr(
            "utils.storage.get_backend",
            lambda: fake_backend,
        )

        result = tick_pending_uploads(now_provider=lambda: now)
        test_db_session.refresh(row)

        assert result["final_failed"] == 1
        assert row.attempts == _MAX_ATTEMPTS
        assert row.last_error is not None
        assert row.last_error.startswith("final:")

    def test_local_backend_skips_silently(
        self, test_db_session, tmp_path, monkeypatch
    ):
        """backend 不是 SupabaseStorage → tick 回空 metric（不處理）。"""
        from models.pending_uploads import PendingUpload
        from services.notification.pending_uploads_scheduler import tick_pending_uploads

        local_file = tmp_path / "noop.bin"
        local_file.write_bytes(b"X")

        now = datetime.now(timezone.utc)
        row = PendingUpload(
            module="activity_posters",
            key="noop/x.png",
            content_type="image/png",
            local_path=str(local_file),
            attempts=0,
            next_retry_at=now - timedelta(seconds=1),
        )
        test_db_session.add(row)
        test_db_session.commit()

        fake_backend = MagicMock()
        fake_backend.__class__.__name__ = "LocalStorage"  # 非 SupabaseStorage

        monkeypatch.setattr(
            "utils.storage.get_backend",
            lambda: fake_backend,
        )

        result = tick_pending_uploads(now_provider=lambda: now)

        # attempted == 1（找到了 row），但 succeeded/failed 皆 0（未處理）
        assert result["attempted"] == 1
        assert result["succeeded"] == 0
        assert result["failed"] == 0
