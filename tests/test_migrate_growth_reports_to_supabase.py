"""tests/test_migrate_growth_reports_to_supabase.py

Test suite for growth-reports local→Supabase migration script.
Covers dry-run, idempotency, hash verification, and edge cases.
"""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.migrate_growth_reports_to_supabase import (
    _is_already_migrated,
    _local_path,
    _migrate_one,
)


def _fake_report(report_id, sid, file_path, status="ready"):
    """Factory: create a mock StudentGrowthReport."""
    r = MagicMock()
    r.id = report_id
    r.student_id = sid
    r.file_path = file_path
    r.status = status
    return r


class TestIsAlreadyMigrated:
    """_is_already_migrated() recognizes storage key prefix."""

    def test_storage_key_recognized(self):
        """file_path starting with 'students/' is already migrated."""
        assert _is_already_migrated("students/1/42.pdf") is True
        assert _is_already_migrated("students/99/999.pdf") is True

    def test_local_path_not_migrated(self):
        """Relative or absolute local paths are not migrated."""
        assert _is_already_migrated("data/growth_reports/1/42.pdf") is False
        assert _is_already_migrated("/tmp/growth_reports/1/42.pdf") is False


class TestLocalPath:
    """_local_path() returns absolute Path."""

    def test_relative_path_made_absolute(self, tmp_path):
        """Relative paths are resolved relative to cwd."""
        rel = Path("some/file.pdf")
        result = _local_path(str(rel))
        assert result.is_absolute()
        assert result == Path.cwd() / rel

    def test_absolute_path_unchanged(self, tmp_path):
        """Absolute paths are returned as-is."""
        abs_path = tmp_path / "file.pdf"
        result = _local_path(str(abs_path))
        assert result == abs_path


class TestMigrateOne:
    """_migrate_one() orchestrates upload, hash check, and DB update."""

    def test_dry_run_does_not_write(self, tmp_path):
        """dry_run=True logs intent but makes no backend calls."""
        pdf = tmp_path / "42.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        report = _fake_report(42, 1, str(pdf))
        backend = MagicMock()

        status = _migrate_one(report, backend, dry_run=True)

        assert status == "dry-run"
        backend.save.assert_not_called()
        backend.read.assert_not_called()
        assert report.file_path == str(pdf)  # unchanged

    def test_already_migrated_skipped(self):
        """file_path already in storage key format is skipped."""
        report = _fake_report(42, 1, "students/1/42.pdf")
        backend = MagicMock()

        status = _migrate_one(report, backend, dry_run=False)

        assert status == "skipped:already-migrated"
        backend.save.assert_not_called()
        backend.read.assert_not_called()

    def test_local_file_missing_skipped(self, tmp_path):
        """Missing local file is skipped (idempotent fallback)."""
        report = _fake_report(42, 1, str(tmp_path / "nonexistent.pdf"))
        backend = MagicMock()

        status = _migrate_one(report, backend, dry_run=False)

        assert status == "skipped:local-missing"
        backend.save.assert_not_called()
        backend.read.assert_not_called()

    def test_successful_migration_updates_file_path(self, tmp_path):
        """Successful upload updates report.file_path to storage key."""
        pdf = tmp_path / "42.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        report = _fake_report(42, 1, str(pdf))
        backend = MagicMock()
        # backend.read returns same bytes → hash match
        backend.read.return_value = b"%PDF-1.4 fake"

        status = _migrate_one(report, backend, dry_run=False)

        assert status == "migrated"
        # Verify backend.save was called with correct args
        backend.save.assert_called_once()
        call_args = backend.save.call_args
        assert call_args.args[0] == "growth_reports"
        assert call_args.args[1] == "students/1/42.pdf"
        assert call_args.args[2] == b"%PDF-1.4 fake"
        assert call_args.args[3] == "application/pdf"
        # Verify report.file_path was updated to storage key
        assert report.file_path == "students/1/42.pdf"

    def test_hash_mismatch_raises_and_does_not_update(self, tmp_path):
        """Hash mismatch between uploaded and downloaded bytes raises RuntimeError."""
        pdf = tmp_path / "42.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        report = _fake_report(42, 1, str(pdf))
        backend = MagicMock()
        # backend.read returns different bytes → hash mismatch
        backend.read.return_value = b"%PDF-1.4 corrupted"

        with pytest.raises(RuntimeError, match="hash mismatch"):
            _migrate_one(report, backend, dry_run=False)

        # report.file_path should not be updated
        assert report.file_path == str(pdf)
        assert not report.file_path.startswith("students/")
