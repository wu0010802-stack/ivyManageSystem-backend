"""FastAPI dependencies for parent_portal routers.

Bridges `models.parent_db` (env-driven, RLS-enforced engine) with the existing
parent role guard (`utils.auth.require_parent_role`). Routers that have been
migrated to RLS use `Depends(get_parent_db)` instead of imperatively calling
`get_session()`.

Phase 1 (2026-05-18): only `attendance.py` uses this; remaining routers stay
on the legacy admin engine until their tables get GRANT + ENABLE RLS + POLICY
in a future migration.
"""

from __future__ import annotations

from typing import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from models.parent_db import get_parent_session_dep
from utils.auth import require_parent_role


def get_parent_db(
    current_user: dict = Depends(require_parent_role()),
) -> Generator[Session, None, None]:
    """Yield a Session bound to the RLS-enforced parent engine, with
    `app.current_user_id` set to the caller's user_id for the duration of
    one transaction.

    Inside handlers:
    - Use `session.flush()` if you need an ID before the request ends.
    - **Never call `session.commit()`** — it would end the tx the dependency
      owns, dropping `SET LOCAL` and turning subsequent queries into 0 rows.
    - The dep commits + closes the session when the handler returns.

    Fail-loud invariant: if `PARENT_DB_USER` / `PARENT_DB_PASSWORD` env vars
    are unset, `get_parent_session_dep` raises RuntimeError — the request
    fails 500 rather than silently falling back to the admin (BYPASSRLS)
    engine, which would defeat the whole point of RLS.
    """
    yield from get_parent_session_dep(current_user["user_id"])
