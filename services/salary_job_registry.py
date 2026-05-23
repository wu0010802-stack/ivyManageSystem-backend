"""Compat shim — moved to services.finance.salary_job_registry. Remove in PR2."""

import sys

from services.finance import salary_job_registry as _impl

sys.modules[__name__] = _impl
