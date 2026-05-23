"""Compat shim — moved to services.finance.finance_cache. Remove in PR2."""

import sys

from services.finance import finance_cache as _impl

sys.modules[__name__] = _impl
