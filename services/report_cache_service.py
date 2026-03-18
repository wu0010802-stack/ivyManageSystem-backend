"""高成本報表快取 service。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from models.database import ReportSnapshot, get_session

logger = logging.getLogger(__name__)


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
        self._persist_snapshot(
            cache_key=cache_key,
            category=category,
            serialized=serialized,
            computed_at=now,
            expires_at=expires_at,
        )
        return payload

    def invalidate_category(self, session, category: str) -> int:
        return self.invalidate_categories(session, category)

    def invalidate_categories(self, session, *categories: str) -> int:
        unique_categories = [category for category in dict.fromkeys(categories) if category]
        if not unique_categories:
            return 0

        cache_session = get_session()
        try:
            deleted = (
                cache_session.query(ReportSnapshot)
                .filter(ReportSnapshot.category.in_(unique_categories))
                .delete(synchronize_session=False)
            )
            cache_session.commit()
            return deleted
        except Exception:
            cache_session.rollback()
            logger.warning(
                "報表快取失效失敗: categories=%s",
                unique_categories,
                exc_info=True,
            )
            return 0
        finally:
            cache_session.close()

    def _persist_snapshot(
        self,
        *,
        cache_key: str,
        category: str,
        serialized: str,
        computed_at: datetime,
        expires_at: datetime,
    ) -> None:
        cache_session = get_session()
        try:
            snapshot = (
                cache_session.query(ReportSnapshot)
                .filter(ReportSnapshot.cache_key == cache_key)
                .first()
            )
            if snapshot is None:
                snapshot = ReportSnapshot(
                    cache_key=cache_key,
                    category=category,
                )
                cache_session.add(snapshot)

            snapshot.payload = serialized
            snapshot.computed_at = computed_at
            snapshot.expires_at = expires_at
            cache_session.commit()
        except Exception:
            cache_session.rollback()
            logger.warning(
                "報表快取寫入失敗: category=%s key=%s",
                category,
                cache_key,
                exc_info=True,
            )
        finally:
            cache_session.close()


report_cache_service = ReportCacheService()
