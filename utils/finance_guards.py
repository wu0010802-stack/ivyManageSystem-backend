"""Compat shim — moved to services.finance.finance_guards. Remove in PR2."""

import sys

from services.finance import finance_guards as _impl

sys.modules[__name__] = _impl
