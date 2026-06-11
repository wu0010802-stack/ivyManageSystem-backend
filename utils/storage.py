# utils/storage.py
"""
集中管理使用者上傳檔案的儲存後端（local filesystem / Supabase Storage）。

設計目標：
- adapter pattern：`StorageBackend` Protocol 抽象、`LocalStorage` 與 `SupabaseStorage` 兩實作
- 透過 `STORAGE_BACKEND` env var 切換（local | supabase），預設 local
- `get_backend()` 提供 singleton 取用，避免重複建構

向下相容：保留 `get_storage_path(module)` 為 legacy shim，回傳本機 module 目錄。
新程式請改用 `get_backend().save() / .read() / .public_url()`。
"""

from pathlib import Path
from typing import Protocol

from config import settings

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads"


def get_storage_root() -> Path:
    """取得本機儲存根目錄（不建立）。僅 LocalStorage 與 legacy shim 用。"""
    root = settings.storage.root
    if root is None:
        return _DEFAULT_ROOT
    return root.expanduser().resolve()


def get_storage_path(module: str) -> Path:
    """Legacy shim：回傳 `<STORAGE_ROOT>/<module>`，自動建立目錄。

    新程式應改用 `get_backend().save(module, key, data, content_type)`。
    此函式保留是為了不破壞既有測試與尚未遷移的 caller。
    """
    path = get_storage_root() / module
    path.mkdir(parents=True, exist_ok=True)
    return path


class StorageBackend(Protocol):
    """上傳檔案儲存後端抽象介面。

    參數約定：
    - `module`：邏輯模組名（cloud 對應 bucket，local 對應子目錄）
        例：`"activity_posters"`、`"leave_attachments"`、`"attendance_imports"`
    - `key`：bucket 內的物件路徑（含子目錄），例：`"42/photo.jpg"`
    - 公開 module 可用 `public_url()` 取 CDN URL；私有 module 用 `signed_url()`
    """

    def save(self, module: str, key: str, data: bytes, content_type: str) -> None: ...

    def read(self, module: str, key: str) -> bytes: ...

    def delete(self, module: str, key: str) -> None: ...

    def exists(self, module: str, key: str) -> bool: ...

    def public_url(self, module: str, key: str) -> str: ...

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str: ...


class LocalStorage:
    """本機檔案系統實作。

    寫入根目錄由 `STORAGE_ROOT` env var 控制，預設 `<repo>/data/uploads/`。
    `public_url` / `signed_url` 對 caller 透明回傳原本的 API 路徑
    （供 router 維持「對外 URL 不改變」的相容性）。
    """

    # local 模式下，cloud 不存在的概念用內建 API 路徑代替，讓 caller 不必區分後端
    _API_PATH_PREFIX = {
        "activity_posters": "/api/activity/public/poster",
        "leave_attachments": "/api/leaves",  # admin
        "leave_attachments_portal": "/api/portal/leaves",  # portal（不同 prefix）
        "attendance_imports": None,  # 內部用，不對外 URL
    }

    def _path(self, module: str, key: str) -> Path:
        base = get_storage_root() / module
        full = (base / key).resolve()
        # 路徑穿越守衛（雙保險）：須用 trailing separator，否則 sibling-prefix
        # （base_evil）會通過 startswith(base) 繞過（Finding 檔Low-2）。
        if not str(full).startswith(str(base.resolve()) + "/"):
            raise ValueError(f"不合法的 key: {key}")
        return full

    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        full = self._path(module, key)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def read(self, module: str, key: str) -> bytes:
        full = self._path(module, key)
        return full.read_bytes()

    def delete(self, module: str, key: str) -> None:
        full = self._path(module, key)
        try:
            full.unlink()
        except FileNotFoundError:
            pass  # idempotent

    def exists(self, module: str, key: str) -> bool:
        full = self._path(module, key)
        return full.is_file()

    def public_url(self, module: str, key: str) -> str:
        # local 模式下，沿用既有對外 API 路徑（保留向下相容）
        prefix = self._API_PATH_PREFIX.get(module)
        if prefix is None:
            raise ValueError(f"module {module} 不支援 public_url")
        return f"{prefix}/{key}"

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str:
        # local 模式下，signed 與 public 等價（仍走後端 endpoint，由 JWT 守衛）
        return self.public_url(module, key)


_BACKEND_SINGLETON: StorageBackend | None = None


def get_backend() -> StorageBackend:
    """取得目前環境的 StorageBackend，singleton。

    依 `STORAGE_BACKEND` env var：
    - `local`（預設）→ `LocalStorage`
    - `supabase` → `SupabaseStorage`（從 `utils.supabase_storage` 延遲載入）
    """
    global _BACKEND_SINGLETON
    if _BACKEND_SINGLETON is not None:
        return _BACKEND_SINGLETON

    name = settings.storage.backend.lower()
    if name == "supabase":
        from utils.supabase_storage import SupabaseStorage

        _BACKEND_SINGLETON = SupabaseStorage()
    elif name == "local":
        _BACKEND_SINGLETON = LocalStorage()
    else:
        raise ValueError(f"未知的 STORAGE_BACKEND: {name}")

    return _BACKEND_SINGLETON
