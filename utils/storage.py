"""
集中管理使用者上傳檔案的實體儲存路徑。

預設寫到 `backend/data/uploads/<module>/`，可用環境變數 `STORAGE_ROOT` 覆寫，
production 通常指到容器外掛載磁碟（例如 `/var/lib/ivy/uploads`）。
"""

import os
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads"


def get_storage_root() -> Path:
    """取得儲存根目錄（不建立）。"""
    raw = os.getenv("STORAGE_ROOT")
    return Path(raw).expanduser().resolve() if raw else _DEFAULT_ROOT


def get_storage_path(module: str) -> Path:
    """回傳 `<STORAGE_ROOT>/<module>`，自動建立目錄。"""
    path = get_storage_root() / module
    path.mkdir(parents=True, exist_ok=True)
    return path
