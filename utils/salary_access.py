"""Compat shim — moved to services.finance.salary_access. Remove in PR2."""

import sys

from services.finance import salary_access as _impl

sys.modules[__name__] = _impl
