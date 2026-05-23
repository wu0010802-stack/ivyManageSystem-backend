"""Tests for scripts/lint_alembic_symmetry.py."""

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable for tests
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lint_alembic_symmetry import classify_and_lint  # noqa: E402


def _write_migration(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body).lstrip())
    return f


def test_create_table_with_matching_drop_passes(tmp_path):
    f = _write_migration(
        tmp_path,
        "m_pass.py",
        '''
        """add foo"""
        revision = "abc123"
        down_revision = "xyz789"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            op.drop_table("foo")
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_create_table_without_drop_fails(tmp_path):
    f = _write_migration(
        tmp_path,
        "m_fail.py",
        '''
        """add foo"""
        revision = "abc123"
        down_revision = "xyz789"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            pass
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert len(violations) == 1
    assert "create_table" in violations[0]
    assert "drop_table" in violations[0]


def test_count_mismatch_passes(tmp_path):
    """First-version intentional weakness: 2 add_column + 1 drop_column passes."""
    f = _write_migration(
        tmp_path,
        "m_count.py",
        """
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.add_column("t", sa.Column("a"))
            op.add_column("t", sa.Column("b"))

        def downgrade():
            op.drop_column("t", "a")
    """,
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_skip_marker_in_header_skips(tmp_path):
    f = _write_migration(
        tmp_path,
        "m_skip.py",
        '''
        """add foo"""
        # alembic-lint: skip-symmetry (data-only cleanup)
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            pass
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "skipped:marker"
    assert violations == []


def test_merge_migration_with_tuple_down_revision_skips(tmp_path):
    f = _write_migration(
        tmp_path,
        "m_merge.py",
        '''
        """merge heads"""
        revision = "merge1"
        down_revision = ("head_a", "head_b")

        def upgrade():
            pass

        def downgrade():
            pass
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "skipped:merge"
    assert violations == []


def test_alter_column_not_checked_passes(tmp_path):
    """op.alter_column is intentionally out of scope (too complex to symmetric-check)."""
    f = _write_migration(
        tmp_path,
        "m_alter.py",
        """
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.alter_column("t", "c", type_=sa.String(64))

        def downgrade():
            pass
    """,
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_drop_table_without_create_fails_reverse(tmp_path):
    """Reverse rule: if downgrade has drop_X, upgrade should have create_X."""
    f = _write_migration(
        tmp_path,
        "m_reverse.py",
        """
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            pass

        def downgrade():
            op.drop_table("foo")
    """,
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert len(violations) == 1
    assert "drop_table" in violations[0]
    assert "create_table" in violations[0]


def test_create_index_with_drop_index_passes(tmp_path):
    f = _write_migration(
        tmp_path,
        "m_index.py",
        """
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_index("ix_foo", "foo", ["a"])

        def downgrade():
            op.drop_index("ix_foo", "foo")
    """,
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_create_fk_with_drop_constraint_passes(tmp_path):
    """All create_foreign_key/unique/check map to op.drop_constraint."""
    f = _write_migration(
        tmp_path,
        "m_fk.py",
        """
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_foreign_key("fk_a", "t1", "t2", ["a"], ["id"])

        def downgrade():
            op.drop_constraint("fk_a", "t1")
    """,
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_missing_downgrade_function_with_create_table_fails(tmp_path):
    """Migration with op.create_table in upgrade() but no downgrade() function
    at all should still emit a violation — the original bug was that the lint
    bailed early when either function was missing."""
    f = _write_migration(
        tmp_path,
        "m_nodown.py",
        '''
        """add foo"""
        revision = "abc123"
        down_revision = "xyz789"

        def upgrade():
            op.create_table("foo")
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert len(violations) == 1
    assert "create_table" in violations[0]
    assert "drop_table" in violations[0]


def test_skip_marker_inside_docstring_does_not_skip(tmp_path):
    """The skip marker must be a `#` comment. A docstring (or any non-comment
    text) that happens to contain the marker substring must NOT silence the lint."""
    f = _write_migration(
        tmp_path,
        "m_docskip.py",
        '''
        """Some doc mentioning alembic-lint: skip-symmetry as plain text"""
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            pass
    ''',
    )
    status, violations = classify_and_lint(f)
    assert status == "checked"  # NOT skipped
    assert len(violations) == 1
    assert "create_table" in violations[0]
