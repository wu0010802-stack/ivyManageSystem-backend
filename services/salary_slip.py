"""Compat shim — moved to services.finance.salary_slip. Remove in PR2."""

import sys

from services.finance import salary_slip as _impl

sys.modules[__name__] = _impl
