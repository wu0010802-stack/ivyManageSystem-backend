"""
Supabase Storage 實作，作為 utils.storage.StorageBackend 的雲端版本。

依 module 切到對應 bucket：
- activity_posters    → bucket "activity-posters"（公開）
- leave_attachments   → bucket "leave-attachments"（私有，需 signed URL）
- attendance_imports  → bucket "attendance-imports"（私有，僅後端用）

環境變數：
- SUPABASE_URL：Supabase project URL
- SUPABASE_SERVICE_ROLE_KEY：後端專用 service role key（絕對勿外洩）
"""

import logging

from supabase import create_client

from config import settings

logger = logging.getLogger(__name__)

# module 邏輯名稱 → Supabase bucket 名稱
# 注意：bucket 名只能 lowercase + hyphen，module 用 underscore，這裡做映射
_MODULE_TO_BUCKET = {
    "activity_posters": "activity-posters",
    "leave_attachments": "leave-attachments",
    "attendance_imports": "attendance-imports",
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
        bucket.upload(
            path=key,
            file=data,
            file_options={"content-type": content_type, "upsert": "true"},
        )

    def read(self, module: str, key: str) -> bytes:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        return bucket.download(key)

    def delete(self, module: str, key: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            bucket.remove([key])
        except Exception as e:
            # idempotent：物件已不存在不 raise
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
                items = bucket.list()
                filename = key
            else:
                items = bucket.list(parent[0])
                filename = parent[1]
            return any(item.get("name") == filename for item in items)
        except Exception:
            return False

    def public_url(self, module: str, key: str) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        return bucket.get_public_url(key)

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        res = bucket.create_signed_url(key, ttl_seconds)
        # supabase-py 2.x 回 dict {"signedURL": "..."}
        return res.get("signedURL") or res.get("signed_url") or ""
