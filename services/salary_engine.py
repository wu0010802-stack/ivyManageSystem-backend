"""Compat shim — moved to services.finance.salary_engine.

This shim replaces itself in sys.modules with the implementation module so that
patch.object / setattr / module-level globals are shared between old and new
import paths. Remove in PR2 (post-merge of 3 parallel worktrees).
"""

import sys

from services.finance import salary_engine as _impl

sys.modules[__name__] = _impl
