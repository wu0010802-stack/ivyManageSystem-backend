"""
Supabase Storage 實作，作為 utils.storage.StorageBackend 的雲端版本。

依 module 切到對應 bucket：
- activity_posters    → bucket "activity-posters"（公開）
- leave_attachments   → bucket "leave-attachments"（私有，需 signed URL）
- attendance_imports  → bucket "attendance-imports"（私有，僅後端用）
- growth_reports      → bucket "growth-reports"（私有，需 signed URL）

環境變數：
- SUPABASE_URL：Supabase project URL
- SUPABASE_SERVICE_ROLE_KEY：後端專用 service role key（絕對勿外洩）
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supabase import create_client

from config import settings
from utils.external_calls import retry_with_backoff, tagged_capture
from utils.circuit_breaker import SUPABASE_BREAKER, BreakerOpenError

logger = logging.getLogger(__name__)

# module 邏輯名稱 → Supabase bucket 名稱
# 注意：bucket 名只能 lowercase + hyphen，module 用 underscore，這裡做映射
_MODULE_TO_BUCKET = {
    "activity_posters": "activity-posters",
    "leave_attachments": "leave-attachments",
    "attendance_imports": "attendance-imports",
    "growth_reports": "growth-reports",
}


def _resolve_bucket(module: str) -> str:
    bucket = _MODULE_TO_BUCKET.get(module)
    if bucket is None:
        raise ValueError(f"未知 module: {module}")
    return bucket


class SupabaseStorage:
    """Supabase Storage backend。"""

    def __init__(self) -> None:
        url = settings.storage.supabase_url
        key = settings.storage.supabase_service_role_key
        if not url or not key:
            raise RuntimeError(
                "STORAGE_BACKEND=supabase 需要設定 SUPABASE_URL 與 SUPABASE_SERVICE_ROLE_KEY"
            )
        self._client = create_client(url, key)

    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        # upsert=true：若同 key 存在則覆蓋（呼叫端通常用 uuid filename 不會撞）
        # Phase 4：retry_with_backoff(3) + local fallback on persistent failure
        try:
            SUPABASE_BREAKER.call(
                lambda: retry_with_backoff(
                    lambda: bucket.upload(
                        path=key,
                        file=data,
                        file_options={"content-type": content_type, "upsert": "true"},
                    ),
                    attempts=3,
                    base_seconds=1.0,
                    cap_seconds=8.0,
                )
            )
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            from config import settings as _settings
            if not getattr(_settings.storage, "local_fallback_enabled", True):
                raise
            # Local fallback：寫 data/uploads_pending + DB row；caller 視為 save 成功
            try:
                local_path = _stash_locally(module, key, data)
                _enqueue_pending_upload(module, key, content_type, local_path, str(exc))
            except Exception as fallback_exc:
                tagged_capture(fallback_exc, tag="supabase", level="error")
                raise exc  # fallback 也炸 → 還原原本 raise

    def read(self, module: str, key: str) -> bytes:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            return SUPABASE_BREAKER.call(lambda: bucket.download(key))
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            raise

    def delete(self, module: str, key: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            SUPABASE_BREAKER.call(lambda: bucket.remove([key]))
        except Exception as e:
            # idempotent：物件已不存在不 raise；保留既有行為
            tagged_capture(e, tag="supabase", level="warning")
            logger.warning(
                "Supabase Storage delete 失敗（忽略）：module=%s key=%s err=%s",
                module,
                key,
                e,
            )

    def exists(self, module: str, key: str) -> bool:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            parent = key.rsplit("/", 1)
            if len(parent) == 1:
                items = SUPABASE_BREAKER.call(lambda: bucket.list())
                filename = key
            else:
                items = SUPABASE_BREAKER.call(lambda p=parent[0]: bucket.list(p))
                filename = parent[1]
            return any(item.get("name") == filename for item in items)
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="warning")
            return False  # 既有行為：例外視為「不存在」

    def public_url(self, module: str, key: str) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        return bucket.get_public_url(key)

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            res = SUPABASE_BREAKER.call(lambda: bucket.create_signed_url(key, ttl_seconds))
        except Exception as exc:
            tagged_capture(exc, tag="supabase", level="error")
            raise
        # supabase-py 2.x 回 dict {"signedURL": "..."}
        return res.get("signedURL") or res.get("signed_url") or ""


# ── Phase 4 fallback helpers ──────────────────────────────────────

_FALLBACK_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads_pending"


def _stash_locally(module: str, key: str, data: bytes) -> str:
    """寫 fallback bytes 到本機 data/uploads_pending/<module>/<uuid>.bin，回 path string."""
    folder = _FALLBACK_ROOT / module
    folder.mkdir(parents=True, exist_ok=True)
    # 用 uuid 避撞名（同 key 可能多次失敗）；保留原 key 在 DB row
    fname = f"{uuid.uuid4().hex}.bin"
    local_path = folder / fname
    local_path.write_bytes(data)
    return str(local_path)


def _enqueue_pending_upload(
    module: str,
    key: str,
    content_type: str,
    local_path: str,
    error: str,
) -> None:
    """寫 pending_uploads DB row（scheduler tick 會撈）."""
    from models.base import get_session_factory
    from models.pending_uploads import PendingUpload

    session = get_session_factory()()
    try:
        row = PendingUpload(
            module=module,
            key=key,
            content_type=content_type,
            local_path=local_path,
            attempts=0,
            next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            last_error=error[:500],
        )
        session.add(row)
        session.commit()
    finally:
        session.close()

