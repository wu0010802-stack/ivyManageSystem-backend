import importlib.util
from datetime import date
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parent.parent
    / "alembic/versions/enrterm01_add_enrollment_semester_and_backfill.py"
)
_spec = importlib.util.spec_from_file_location("enrterm01_mig", _MIG)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def test_roc_month_to_term_matches_service():
    from services.recruitment_funnel import roc_month_to_school_term

    for label in ("114.09", "115.01", "115.02", "115.03", "115.08"):
        assert mig._roc_month_to_term(label) == roc_month_to_school_term(label)


def test_roc_month_to_term_invalid_returns_none():
    assert mig._roc_month_to_term("") == (None, None)
    assert mig._roc_month_to_term("115") == (None, None)
    assert mig._roc_month_to_term("115.13") == (None, None)


def test_date_to_term_boundaries():
    assert mig._date_to_term(date(2025, 8, 1)) == (114, 1)
    assert mig._date_to_term(date(2026, 1, 31)) == (114, 1)
    assert mig._date_to_term(date(2026, 2, 1)) == (114, 2)
