"""Compat shim — moved to services.finance.art_teacher_payroll. Remove in PR2."""

import sys

from services.finance import art_teacher_payroll as _impl

sys.modules[__name__] = _impl
