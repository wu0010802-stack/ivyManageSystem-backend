"""Compat shim — moved to services.finance.fee_refund_calculator. Remove in PR2."""

import sys

from services.finance import fee_refund_calculator as _impl

sys.modules[__name__] = _impl
