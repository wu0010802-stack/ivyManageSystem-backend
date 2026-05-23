"""Compat shim — moved to services.finance.monthly_pnl_service. Remove in PR2."""

import sys

from services.finance import monthly_pnl_service as _impl

sys.modules[__name__] = _impl
