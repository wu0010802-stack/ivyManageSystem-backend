"""tests/test_data_quality_rules.py — Ch2 data quality rules + schema."""

from sqlalchemy import inspect

from models.data_quality import DataQualityReport
from models.base import Base


def test_data_quality_report_columns():
    cols = {c.name for c in DataQualityReport.__table__.columns}
    expected = {
        "id",
        "rule_code",
        "severity",
        "entity_type",
        "entity_id",
        "summary",
        "detected_at",
        "last_seen_at",
        "dedup_key",
        "status",
        "ack_by",
        "ack_at",
        "resolved_at",
        "resolution_note",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_data_quality_report_registered_in_metadata():
    """CLAUDE.md #5：必須在 models/__init__.py 中央 import 才能進 metadata。"""
    assert "data_quality_reports" in Base.metadata.tables


def test_data_quality_report_has_partial_unique_index():
    """ix_dqr_dedup_open 應為 partial unique (status='open')。"""
    indexes = [idx for idx in DataQualityReport.__table__.indexes]
    open_idx = next(
        (idx for idx in indexes if idx.name == "ix_dqr_dedup_open"),
        None,
    )
    assert open_idx is not None
    assert open_idx.unique is True
