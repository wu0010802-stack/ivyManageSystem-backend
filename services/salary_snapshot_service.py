"""Compat shim — moved to services.finance.salary_snapshot_service. Remove in PR2."""

import sys

from services.finance import salary_snapshot_service as _impl

sys.modules[__name__] = _impl
