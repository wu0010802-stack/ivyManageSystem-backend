"""Compat shim — moved to services.finance.finance_reconciliation_service. Remove in PR2."""

import sys

from services.finance import finance_reconciliation_service as _impl

sys.modules[__name__] = _impl
