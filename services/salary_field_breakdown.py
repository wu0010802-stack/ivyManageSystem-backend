"""Compat shim — moved to services.finance.salary_field_breakdown. Remove in PR2."""

import sys

from services.finance import salary_field_breakdown as _impl

sys.modules[__name__] = _impl
