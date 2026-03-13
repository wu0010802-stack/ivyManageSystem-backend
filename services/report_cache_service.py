"""高成本報表快取 service。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from models.database import ReportSnapshot


class ReportCacheService:
    def build_cache_key(self, category: str, params: dict | None = None) -> str:
        normalized = json.dumps(params or {}, ensure_ascii=False, sort_keys=True, default=str)
        return f"{category}:{normalized}"

    def get_or_build(
        self,
        session,
        *,
        category: str,
        ttl_seconds: int,
        builder,
        params: dict | None = None,
        force_refresh: bool = False,
    ):
        now = datetime.now()
        cache_key = self.build_cache_key(category, params)
        snapshot = session.query(ReportSnapshot).filter(ReportSnapshot.cache_key == cache_key).first()

        if (
            snapshot
            and not force_refresh
            and snapshot.expires_at
            and snapshot.expires_at > now
        ):
            return json.loads(snapshot.payload)

        payload = builder()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        expires_at = now + timedelta(seconds=ttl_seconds)

        if snapshot is None:
            snapshot = ReportSnapshot(
                cache_key=cache_key,
                category=category,
            )
            session.add(snapshot)

        snapshot.payload = serialized
        snapshot.computed_at = now
        snapshot.expires_at = expires_at
        session.commit()
        return payload

    def invalidate_category(self, session, category: str) -> int:
        deleted = (
            session.query(ReportSnapshot)
            .filter(ReportSnapshot.category == category)
            .delete(synchronize_session=False)
        )
        session.commit()
        return deleted


report_cache_service = ReportCacheService()
