"""Compat shim — moved to services.finance.salary_logic_info. Remove in PR2."""

import sys

from services.finance import salary_logic_info as _impl

sys.modules[__name__] = _impl
