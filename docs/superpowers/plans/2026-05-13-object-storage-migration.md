# 物件儲存遷移實作計畫（Supabase Storage）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 backend 所有 user 上傳檔（活動海報、假單附件、考勤匯入暫存）改為走 Storage adapter，可在 local filesystem 與 Supabase Storage 之間切換，prod 用 Supabase 達成容器無狀態。

**Architecture:** `utils/storage.py` 新增 `StorageBackend` Protocol + `LocalStorage` 實作；`utils/supabase_storage.py` 新增 `SupabaseStorage`。各 router 改透過 `get_backend()` 取 backend，呼叫 `save / read / delete / public_url / signed_url`。DB schema 不變（仍存邏輯 key 字串）。env var `STORAGE_BACKEND=local|supabase` 切換，預設 local，既有測試完全不動。

**Tech Stack:** FastAPI、SQLAlchemy、`supabase>=2.0.0`（含 storage3 client）、pytest、`unittest.mock`。

**設計文件：** `docs/superpowers/specs/2026-05-13-object-storage-migration-design.md`

---

## 工作分支

```bash
cd ~/Desktop/ivy-backend
git checkout -b feat/object-storage-migration-backend
```

前端如需驗證 / 微調，另開 `feat/object-storage-migration-frontend` 於 `~/Desktop/ivy-frontend`。

---

## 檔案結構總覽

| 動作 | 檔案 | 用途 |
|------|------|------|
| 改寫 | `utils/storage.py` | 新增 Protocol、`LocalStorage`、`get_backend()`；保留 `get_storage_path()` 為 legacy shim |
| 新增 | `utils/supabase_storage.py` | `SupabaseStorage` 實作 |
| 新增 | `tests/test_storage_backend.py` | LocalStorage + get_backend 測試 |
| 新增 | `tests/test_supabase_storage.py` | SupabaseStorage mock 測試 |
| 修改 | `requirements.txt` | 加 `supabase>=2.0.0` |
| 修改 | `.env.example` | 加 STORAGE_BACKEND / SUPABASE_* 變數 |
| 修改 | `api/activity/settings.py` | poster 上傳改用 backend |
| 修改 | `api/activity/public.py` | poster 讀取改 redirect 或代理 |
| 修改 | `api/leaves.py` | admin 假單附件 download 改 redirect signed_url |
| 修改 | `api/portal/leaves.py` | portal 假單附件 upload/delete/download 改用 backend |
| 修改 | `api/attendance/upload.py` | 考勤匯入改用 backend；處理完即刪除 |
| 新增 | `docs/sop/storage-deployment.md` | 部署 SOP（Supabase bucket / Zeabur env vars） |
| 修改 | `SECURITY_AUDIT.md` | 加 Service Role Key 機敏處理 finding |

---

## 約定

- 每 task 完成後跑指定測試，全綠才 commit。
- commit message 用繁體中文、Conventional Commits 風格。
- TDD：先寫測試、跑 → FAIL、寫實作、跑 → PASS、commit。
- 測試檔位於 `tests/`。
- 既有 815+ 條 backend 測試在 Phase 1~5 各 phase 結尾都要 `pytest -q` 全綠。

---

## Phase 1：抽象層基建（不影響任何 router）

### Task 1.1：寫 `LocalStorage.save / read / delete / exists` 失敗測試

**Files:**
- Create: `tests/test_storage_backend.py`

- [ ] **Step 1：建立測試檔，寫 4 個失敗測試**

```python
# tests/test_storage_backend.py
"""
測試 utils/storage.py 的 StorageBackend Protocol 與 LocalStorage 實作。

設計目標：
- LocalStorage 完整覆蓋 save / read / delete / exists / public_url / signed_url
- get_backend() 依 STORAGE_BACKEND env var 切換、singleton 快取
"""

import os
from pathlib import Path

import pytest

from utils.storage import LocalStorage, StorageBackend, get_backend


@pytest.fixture
def local_root(tmp_path, monkeypatch):
    """每測試一個獨立的 STORAGE_ROOT，避免污染。"""
    root = tmp_path / "uploads"
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    # 重置 backend singleton，確保新 env 生效
    import utils.storage as storage_mod

    storage_mod._BACKEND_SINGLETON = None
    return root


def test_local_save_and_read_round_trip(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "abc.png", b"PNGDATA", "image/png")

    assert backend.read("activity_posters", "abc.png") == b"PNGDATA"
    assert (local_root / "activity_posters" / "abc.png").read_bytes() == b"PNGDATA"


def test_local_save_creates_subdirectories(local_root):
    backend = LocalStorage()
    backend.save("leave_attachments", "42/photo.jpg", b"JPGDATA", "image/jpeg")

    assert backend.read("leave_attachments", "42/photo.jpg") == b"JPGDATA"
    assert (local_root / "leave_attachments" / "42" / "photo.jpg").exists()


def test_local_exists_true_and_false(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "exists.png", b"X", "image/png")

    assert backend.exists("activity_posters", "exists.png") is True
    assert backend.exists("activity_posters", "missing.png") is False


def test_local_delete_removes_file(local_root):
    backend = LocalStorage()
    backend.save("activity_posters", "doomed.png", b"X", "image/png")

    backend.delete("activity_posters", "doomed.png")

    assert backend.exists("activity_posters", "doomed.png") is False


def test_local_delete_missing_file_is_idempotent(local_root):
    backend = LocalStorage()
    # 應該不 raise
    backend.delete("activity_posters", "never_existed.png")
```

- [ ] **Step 2：跑測試確認 FAIL**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_storage_backend.py -v`
Expected: 5 個 ImportError 或 AttributeError（`LocalStorage`、`StorageBackend`、`get_backend` 尚不存在）

- [ ] **Step 3：commit 失敗測試**

```bash
cd ~/Desktop/ivy-backend
git add tests/test_storage_backend.py
git commit -m "test(storage): 新增 LocalStorage save/read/delete/exists 失敗測試"
```

---

### Task 1.2：改寫 `utils/storage.py` 加入 `StorageBackend` Protocol + `LocalStorage` save/read/delete/exists

**Files:**
- Modify: `utils/storage.py`（從 24 行重寫，保留 `get_storage_path` 為 legacy shim）

- [ ] **Step 1：完整重寫 `utils/storage.py`（先實作前 4 個方法，public_url/signed_url 下個 task）**

```python
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

import os
from pathlib import Path
from typing import Protocol

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads"


def get_storage_root() -> Path:
    """取得本機儲存根目錄（不建立）。僅 LocalStorage 與 legacy shim 用。"""
    raw = os.getenv("STORAGE_ROOT")
    return Path(raw).expanduser().resolve() if raw else _DEFAULT_ROOT


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

    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        ...

    def read(self, module: str, key: str) -> bytes:
        ...

    def delete(self, module: str, key: str) -> None:
        ...

    def exists(self, module: str, key: str) -> bool:
        ...

    def public_url(self, module: str, key: str) -> str:
        ...

    def signed_url(self, module: str, key: str, ttl_seconds: int) -> str:
        ...


class LocalStorage:
    """本機檔案系統實作。

    寫入根目錄由 `STORAGE_ROOT` env var 控制，預設 `<repo>/data/uploads/`。
    `public_url` / `signed_url` 對 caller 透明回傳原本的 API 路徑
    （供 router 維持「對外 URL 不改變」的相容性）。
    """

    # local 模式下，cloud 不存在的概念用內建 API 路徑代替，讓 caller 不必區分後端
    _API_PATH_PREFIX = {
        "activity_posters": "/api/activity/public/poster",
        "leave_attachments": "/api/leaves",       # admin
        "leave_attachments_portal": "/api/portal/leaves",  # portal（不同 prefix）
        "attendance_imports": None,               # 內部用，不對外 URL
    }

    def _path(self, module: str, key: str) -> Path:
        base = get_storage_root() / module
        full = (base / key).resolve()
        # 路徑穿越守衛（雖然 caller 通常已驗，雙保險）
        if not str(full).startswith(str(base.resolve())):
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

    name = os.getenv("STORAGE_BACKEND", "local").lower()
    if name == "supabase":
        from utils.supabase_storage import SupabaseStorage

        _BACKEND_SINGLETON = SupabaseStorage()
    elif name == "local":
        _BACKEND_SINGLETON = LocalStorage()
    else:
        raise ValueError(f"未知的 STORAGE_BACKEND: {name}")

    return _BACKEND_SINGLETON
```

- [ ] **Step 2：跑前 4 個測試確認 PASS**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_storage_backend.py -v -k "save_and_read or save_creates_sub or exists_true or delete_removes or delete_missing"`
Expected: 5 個 PASS

- [ ] **Step 3：跑既有測試確認沒打壞**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_parent_leave_attachments.py tests/test_parent_medications.py -q`
Expected: 既有測試全綠（`get_storage_path` shim 還在）

- [ ] **Step 4：commit**

```bash
git add utils/storage.py
git commit -m "feat(storage): 新增 StorageBackend Protocol 與 LocalStorage save/read/delete/exists"
```

---

### Task 1.3：寫 `public_url / signed_url` 失敗測試

**Files:**
- Modify: `tests/test_storage_backend.py`

- [ ] **Step 1：append 4 個測試**

```python
# tests/test_storage_backend.py 末尾 append

def test_local_public_url_activity_poster(local_root):
    backend = LocalStorage()
    url = backend.public_url("activity_posters", "abc123.png")
    assert url == "/api/activity/public/poster/abc123.png"


def test_local_public_url_leave_attachment(local_root):
    backend = LocalStorage()
    url = backend.public_url("leave_attachments", "42/photo.jpg")
    assert url == "/api/leaves/42/photo.jpg"


def test_local_signed_url_returns_public_path(local_root):
    """local 模式下 signed 與 public 等價（後端 JWT 守衛）。"""
    backend = LocalStorage()
    url = backend.signed_url("leave_attachments", "42/photo.jpg", ttl_seconds=3600)
    assert url == "/api/leaves/42/photo.jpg"


def test_local_public_url_rejects_unknown_module(local_root):
    backend = LocalStorage()
    with pytest.raises(ValueError):
        backend.public_url("attendance_imports", "x.xlsx")
```

- [ ] **Step 2：跑測試確認全 PASS（這些方法在 Task 1.2 已實作）**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_storage_backend.py -v`
Expected: 全 9 個 PASS

- [ ] **Step 3：commit**

```bash
git add tests/test_storage_backend.py
git commit -m "test(storage): 補 LocalStorage public_url 與 signed_url 行為測試"
```

---

### Task 1.4：寫 `get_backend()` singleton + env var 切換測試

**Files:**
- Modify: `tests/test_storage_backend.py`

- [ ] **Step 1：append 3 個測試**

```python
# tests/test_storage_backend.py 末尾 append

def test_get_backend_default_is_local(local_root):
    backend = get_backend()
    assert isinstance(backend, LocalStorage)


def test_get_backend_singleton_within_process(local_root):
    b1 = get_backend()
    b2 = get_backend()
    assert b1 is b2


def test_get_backend_invalid_value_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "not_a_real_backend")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    import utils.storage as storage_mod
    storage_mod._BACKEND_SINGLETON = None

    with pytest.raises(ValueError, match="未知的 STORAGE_BACKEND"):
        get_backend()
```

- [ ] **Step 2：跑測試確認全 PASS**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_storage_backend.py -v`
Expected: 全 12 個 PASS

- [ ] **Step 3：跑全部 backend 測試確認 0 regression**

Run: `cd ~/Desktop/ivy-backend && python -m pytest -q --timeout=60`
Expected: 全綠（既有 815+ 條 + 新增 12 條）

- [ ] **Step 4：commit**

```bash
git add tests/test_storage_backend.py
git commit -m "test(storage): get_backend() singleton 與 env var 切換測試"
```

---

## Phase 2：SupabaseStorage 實作

### Task 2.1：加 supabase SDK 到 requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1：插入 `supabase>=2.0.0` 在 `requests` 之後**

執行 Edit：在 `requirements.txt` 中尋找 `requests>=2.33.0  # CVE-2026-25645`，在其後追加：

```
supabase>=2.0.0  # 物件儲存（activity_posters / leave_attachments）
```

- [ ] **Step 2：本機安裝**

Run: `cd ~/Desktop/ivy-backend && pip install supabase>=2.0.0`
Expected: 成功安裝 supabase 與其相依（storage3、postgrest、gotrue、realtime）

- [ ] **Step 3：commit**

```bash
git add requirements.txt
git commit -m "build(deps): 加入 supabase 2.x SDK 供物件儲存使用"
```

---

### Task 2.2：寫 SupabaseStorage 失敗測試（用 mock）

**Files:**
- Create: `tests/test_supabase_storage.py`

- [ ] **Step 1：建立測試檔**

```python
# tests/test_supabase_storage.py
"""
測試 SupabaseStorage：mock storage3 client，不打真 Supabase。

策略：
- 用 unittest.mock 替換 supabase.create_client，回傳 MagicMock
- 驗證 save/delete/public_url/signed_url 傳給 client 的參數正確
- 驗證 module → bucket 名稱對應正確
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_supabase(monkeypatch, tmp_path):
    """準備乾淨的 SupabaseStorage 實例，內部 client 為 MagicMock。"""
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")

    import utils.storage as storage_mod
    storage_mod._BACKEND_SINGLETON = None

    with patch("utils.supabase_storage.create_client") as mock_create:
        client = MagicMock()
        mock_create.return_value = client
        from utils.supabase_storage import SupabaseStorage
        backend = SupabaseStorage()
        yield backend, client


def test_save_calls_upload_with_correct_bucket_and_key(mock_supabase):
    backend, client = mock_supabase
    backend.save("activity_posters", "abc.png", b"PNGDATA", "image/png")

    client.storage.from_.assert_called_with("activity-posters")
    bucket = client.storage.from_.return_value
    bucket.upload.assert_called_once()
    call_args = bucket.upload.call_args
    # supabase-py 接受 (path, file, file_options) — 驗證 path 與 file 內容
    assert call_args.kwargs.get("path") == "abc.png" or call_args.args[0] == "abc.png"


def test_save_uses_correct_bucket_for_leave_attachments(mock_supabase):
    backend, client = mock_supabase
    backend.save("leave_attachments", "42/photo.jpg", b"X", "image/jpeg")

    client.storage.from_.assert_called_with("leave-attachments")


def test_delete_calls_remove(mock_supabase):
    backend, client = mock_supabase
    backend.delete("activity_posters", "old.png")

    bucket = client.storage.from_.return_value
    bucket.remove.assert_called_once_with(["old.png"])


def test_public_url_returns_supabase_cdn_url(mock_supabase):
    backend, client = mock_supabase
    bucket = client.storage.from_.return_value
    bucket.get_public_url.return_value = "https://example.supabase.co/storage/v1/object/public/activity-posters/abc.png"

    url = backend.public_url("activity_posters", "abc.png")

    client.storage.from_.assert_called_with("activity-posters")
    bucket.get_public_url.assert_called_once_with("abc.png")
    assert url.startswith("https://example.supabase.co/")


def test_signed_url_passes_ttl_to_supabase(mock_supabase):
    backend, client = mock_supabase
    bucket = client.storage.from_.return_value
    bucket.create_signed_url.return_value = {
        "signedURL": "https://example.supabase.co/storage/v1/object/sign/leave-attachments/42/x.jpg?token=xxx"
    }

    url = backend.signed_url("leave_attachments", "42/x.jpg", ttl_seconds=3600)

    bucket.create_signed_url.assert_called_once_with("42/x.jpg", 3600)
    assert "token=" in url


def test_unknown_module_raises(mock_supabase):
    backend, _ = mock_supabase
    with pytest.raises(ValueError, match="未知 module"):
        backend.save("unknown_module", "x", b"X", "application/octet-stream")
```

- [ ] **Step 2：跑測試確認全 FAIL**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_supabase_storage.py -v`
Expected: 6 個 ImportError（`utils.supabase_storage` 尚不存在）

- [ ] **Step 3：commit 失敗測試**

```bash
git add tests/test_supabase_storage.py
git commit -m "test(storage): 新增 SupabaseStorage mock 失敗測試（6 條）"
```

---

### Task 2.3：實作 `utils/supabase_storage.py`

**Files:**
- Create: `utils/supabase_storage.py`

- [ ] **Step 1：建立檔案**

```python
# utils/supabase_storage.py
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
import os

from supabase import create_client

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
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
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
            logger.warning("Supabase Storage delete 失敗（忽略）：module=%s key=%s err=%s", module, key, e)

    def exists(self, module: str, key: str) -> bool:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        # list with prefix=key，比較名稱
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
```

- [ ] **Step 2：跑測試確認 PASS**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_supabase_storage.py -v`
Expected: 全 6 個 PASS

- [ ] **Step 3：跑全部測試確認 0 regression**

Run: `cd ~/Desktop/ivy-backend && python -m pytest -q --timeout=60`
Expected: 全綠

- [ ] **Step 4：commit**

```bash
git add utils/supabase_storage.py
git commit -m "feat(storage): 實作 SupabaseStorage（save/read/delete/public_url/signed_url）"
```

---

## Phase 3：activity_posters 改用 backend

### Task 3.1：改 `api/activity/settings.py` upload 改用 `get_backend()`

**Files:**
- Modify: `api/activity/settings.py`（替換 `upload_activity_poster` 函式內容）

- [ ] **Step 1：定位現有 `upload_activity_poster`（line 107~158），替換實作**

把原本：

```python
    poster_dir = _poster_dir()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    file_path = poster_dir / stored_name
    file_path.write_bytes(content)

    poster_url = f"/api/activity/public/poster/{stored_name}"

    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)
        # 刪掉前一張避免 data 目錄無限長大
        old = settings.poster_url
        if old and old.startswith("/api/activity/public/poster/"):
            old_name = old.rsplit("/", 1)[-1]
            # 只允許刪 hex + 已知副檔名，防穿越
            if Path(old_name).suffix.lower() in _POSTER_ALLOWED_EXT:
                old_path = poster_dir / old_name
                if old_path.is_file():
                    try:
                        old_path.unlink()
                    except OSError as e:
                        logger.warning("刪除舊海報失敗：%s", e)
        settings.poster_url = poster_url
        session.commit()
        logger.info("活動海報已更新：%s", stored_name)
        return {"message": "海報已更新", "poster_url": poster_url}
```

改為：

```python
    from utils.storage import get_backend
    backend = get_backend()

    stored_name = f"{uuid.uuid4().hex}{ext}"
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    backend.save(_POSTER_MODULE, stored_name, content, content_type)

    # 公開 URL：local 模式回 /api/activity/public/poster/<file>（不變）
    #          supabase 模式回 https://<project>.supabase.co/.../activity-posters/<file>
    poster_url = backend.public_url(_POSTER_MODULE, stored_name)

    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)
        # 刪掉前一張避免儲存空間無限長大
        old = settings.poster_url
        if old:
            # 從舊 URL 反推檔名（兩種來源：/api/activity/public/poster/<file> 或 https://.../<file>）
            old_name = old.rsplit("/", 1)[-1].split("?", 1)[0]
            # 只允許刪 hex + 已知副檔名，防穿越
            if Path(old_name).suffix.lower() in _POSTER_ALLOWED_EXT and len(old_name) < 80:
                try:
                    backend.delete(_POSTER_MODULE, old_name)
                except Exception as e:
                    logger.warning("刪除舊海報失敗：%s", e)
        settings.poster_url = poster_url
        session.commit()
        logger.info("活動海報已更新：%s", stored_name)
        return {"message": "海報已更新", "poster_url": poster_url}
```

- [ ] **Step 2：移除已不用的 import `_poster_dir`，但 `_POSTER_MODULE` 與 `_POSTER_ALLOWED_EXT` 仍要保留**

可保留 `_poster_dir()` 函式（不再用，但無害）或一併刪掉。建議刪掉以保持簡潔：

```python
# 刪掉這兩行（line 21、29-30）：
# from utils.storage import get_storage_path
# def _poster_dir() -> Path:
#     return get_storage_path(_POSTER_MODULE)
```

- [ ] **Step 3：跑活動測試確認綠**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/ -q -k "activity" --timeout=60`
Expected: 活動相關測試全綠

- [ ] **Step 4：commit**

```bash
git add api/activity/settings.py
git commit -m "refactor(activity): 海報上傳改用 StorageBackend，支援 local 與 supabase 兩後端"
```

---

### Task 3.2：改 `api/activity/public.py` poster 讀取

**Files:**
- Modify: `api/activity/public.py`（替換 `get_public_poster` 函式）

- [ ] **Step 1：替換 line 149~177 的 `get_public_poster`**

原本：

```python
@router.get("/public/poster/{filename}")
async def get_public_poster(filename: str, response: Response):
    """公開端點：回傳已上傳的活動海報圖。

    防穿越：檔名只允許純 hex + 白名單副檔名，同時驗證檔案位於 _POSTER_DIR。
    """
    path = Path(filename)
    if path.name != filename or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="非法檔名")
    ext = path.suffix.lower()
    stem = path.stem
    if (
        ext not in _POSTER_ALLOWED_EXT
        or not stem
        or not all(c in "0123456789abcdef" for c in stem)
    ):
        raise HTTPException(status_code=400, detail="非法檔名")

    poster_dir = _poster_dir()
    full_path = (poster_dir / filename).resolve()
    try:
        full_path.relative_to(poster_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路徑")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="海報不存在")

    response.headers["Cache-Control"] = "public, max-age=300"
    return FileResponse(str(full_path), media_type=_POSTER_MIME.get(ext, "image/*"))
```

改為：

```python
@router.get("/public/poster/{filename}")
async def get_public_poster(filename: str, response: Response):
    """公開端點：回傳已上傳的活動海報圖。

    防穿越：檔名只允許純 hex + 白名單副檔名。
    backend 為 local 時直接 stream bytes；supabase 時 302 redirect 到 CDN URL。
    """
    from fastapi.responses import RedirectResponse
    from utils.storage import get_backend, LocalStorage

    path = Path(filename)
    if path.name != filename or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="非法檔名")
    ext = path.suffix.lower()
    stem = path.stem
    if (
        ext not in _POSTER_ALLOWED_EXT
        or not stem
        or not all(c in "0123456789abcdef" for c in stem)
    ):
        raise HTTPException(status_code=400, detail="非法檔名")

    backend = get_backend()
    if not backend.exists(_POSTER_MODULE, filename):
        raise HTTPException(status_code=404, detail="海報不存在")

    if isinstance(backend, LocalStorage):
        # local：直接吐 bytes 維持 e2e 測試簡單
        data = backend.read(_POSTER_MODULE, filename)
        response.headers["Cache-Control"] = "public, max-age=300"
        return PlainResponse(
            content=data,
            media_type=_POSTER_MIME.get(ext, "image/*"),
            headers={"Cache-Control": "public, max-age=300"},
        )

    # supabase：redirect 到 CDN URL（瀏覽器後續直接從 Supabase 拿）
    url = backend.public_url(_POSTER_MODULE, filename)
    return RedirectResponse(url, status_code=302)
```

- [ ] **Step 2：清理不用的 `_poster_dir()` 與 import**

刪除 `api/activity/public.py` 的 `_poster_dir` 函式（line 27~28）與 `from utils.storage import get_storage_path`（line 21）。`_POSTER_MODULE`、`_POSTER_ALLOWED_EXT`、`_POSTER_MIME` 保留。

- [ ] **Step 3：跑活動測試確認綠**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/ -q -k "activity" --timeout=60`
Expected: 全綠

- [ ] **Step 4：commit**

```bash
git add api/activity/public.py
git commit -m "refactor(activity): 海報讀取改用 StorageBackend；supabase 模式 redirect 到 CDN"
```

---

## Phase 4：leave_attachments 改用 backend

### Task 4.1：改 `api/portal/leaves.py` upload_leave_attachments 改用 backend

**Files:**
- Modify: `api/portal/leaves.py`（line 414~492 `upload_leave_attachments` 內部）

- [ ] **Step 1：替換寫檔區塊**

定位原本：

```python
        dir_path = _upload_base() / str(leave_id)
        dir_path.mkdir(parents=True, exist_ok=True)

        saved = []
        for f in files:
            raw_ext = Path(f.filename or "").suffix.lower()
            if not raw_ext or not _EXT_RE.match(raw_ext) or raw_ext not in _ALLOWED_EXT:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支援的檔案格式：{raw_ext or '(無副檔名)'}，僅接受圖片與 PDF",
                )

            content = await f.read()
            if len(content) > _MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400, detail=f"檔案 {f.filename} 超過 5 MB 限制"
                )
            validate_file_signature(content, raw_ext)

            safe_name = f"{uuid.uuid4().hex}{raw_ext}"
            with open(dir_path / safe_name, "wb") as fp:
                fp.write(content)
            saved.append(safe_name)
```

改為：

```python
        from utils.storage import get_backend
        backend = get_backend()

        saved = []
        for f in files:
            raw_ext = Path(f.filename or "").suffix.lower()
            if not raw_ext or not _EXT_RE.match(raw_ext) or raw_ext not in _ALLOWED_EXT:
                raise HTTPException(
                    status_code=400,
                    detail=f"不支援的檔案格式：{raw_ext or '(無副檔名)'}，僅接受圖片與 PDF",
                )

            content = await f.read()
            if len(content) > _MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400, detail=f"檔案 {f.filename} 超過 5 MB 限制"
                )
            validate_file_signature(content, raw_ext)

            safe_name = f"{uuid.uuid4().hex}{raw_ext}"
            content_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".heic": "image/heic",
                ".heif": "image/heif",
                ".pdf": "application/pdf",
            }.get(raw_ext, "application/octet-stream")
            # key 結構：<leave_id>/<safe_name>，與 local 模式目錄結構一致
            backend.save(_UPLOAD_MODULE, f"{leave_id}/{safe_name}", content, content_type)
            saved.append(safe_name)
```

- [ ] **Step 2：跑 portal leave 測試確認綠**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_parent_leave_attachments.py -v --timeout=60`
Expected: 全綠

- [ ] **Step 3：commit**

```bash
git add api/portal/leaves.py
git commit -m "refactor(portal/leaves): 假單附件上傳改用 StorageBackend"
```

---

### Task 4.2：改 `api/portal/leaves.py` delete_leave_attachment 改用 backend

**Files:**
- Modify: `api/portal/leaves.py`（line 495~549 `delete_leave_attachment`）

- [ ] **Step 1：替換刪檔區塊**

定位原本：

```python
        file_path = _safe_attach_path(leave_id, filename)
        if file_path.exists():
            file_path.unlink()
```

改為：

```python
        from utils.storage import get_backend
        backend = get_backend()
        backend.delete(_UPLOAD_MODULE, f"{leave_id}/{filename}")
```

- [ ] **Step 2：跑 portal leave 測試**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_parent_leave_attachments.py -v --timeout=60`
Expected: 全綠

- [ ] **Step 3：commit**

```bash
git add api/portal/leaves.py
git commit -m "refactor(portal/leaves): 假單附件刪除改用 StorageBackend"
```

---

### Task 4.3：改 `api/portal/leaves.py` get_leave_attachment 改 redirect signed_url

**Files:**
- Modify: `api/portal/leaves.py`（line 552~583 `get_leave_attachment`）

- [ ] **Step 1：替換讀檔回傳**

定位原本：

```python
@router.get("/my-leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(get_current_user),
):
    """取得個人假單附件（僅限本人）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = (
            session.query(LeaveRecord)
            .filter(
                LeaveRecord.id == leave_id,
                LeaveRecord.employee_id == emp.id,
            )
            .first()
        )
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        file_path = _safe_attach_path(leave_id, filename)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(str(file_path))
    finally:
        session.close()
```

改為：

```python
@router.get("/my-leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(get_current_user),
):
    """取得個人假單附件（僅限本人）。

    backend 為 local：直接 stream bytes（既有行為）
    backend 為 supabase：302 redirect 到 signed URL（TTL 1 小時）
    """
    from fastapi.responses import RedirectResponse
    from utils.storage import get_backend, LocalStorage

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = (
            session.query(LeaveRecord)
            .filter(
                LeaveRecord.id == leave_id,
                LeaveRecord.employee_id == emp.id,
            )
            .first()
        )
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        backend = get_backend()
        key = f"{leave_id}/{filename}"
        if not backend.exists(_UPLOAD_MODULE, key):
            raise HTTPException(status_code=404, detail="檔案不存在")

        if isinstance(backend, LocalStorage):
            # local：保持既有 stream 行為
            data = backend.read(_UPLOAD_MODULE, key)
            from fastapi.responses import Response as _Response
            return _Response(content=data, media_type="application/octet-stream")

        ttl = int(os.getenv("SUPABASE_STORAGE_SIGNED_URL_TTL", "3600"))
        url = backend.signed_url(_UPLOAD_MODULE, key, ttl)
        return RedirectResponse(url, status_code=302)
    finally:
        session.close()
```

- [ ] **Step 2：頂部加 `import os` 若尚未存在**

Run: `grep "^import os" /Users/yilunwu/Desktop/ivy-backend/api/portal/leaves.py`
若沒有，在 imports 區加 `import os`。

- [ ] **Step 3：跑 portal leave 測試**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_parent_leave_attachments.py -v --timeout=60`
Expected: 全綠（測試在 local backend 模式跑，行為等同既有）

- [ ] **Step 4：commit**

```bash
git add api/portal/leaves.py
git commit -m "refactor(portal/leaves): 附件下載改用 StorageBackend；supabase 模式 redirect signed URL"
```

---

### Task 4.4：改 `api/leaves.py` admin get_leave_attachment 同步

**Files:**
- Modify: `api/leaves.py`（line 2277~2299）

- [ ] **Step 1：替換**

原本：

```python
@router.get("/leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
):
    """取得假單附件（管理後台）"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        file_path = _safe_attach_path(leave_id, filename)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(str(file_path))
    finally:
        session.close()
```

改為：

```python
@router.get("/leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
):
    """取得假單附件（管理後台）。

    backend 為 local：直接 stream bytes（既有行為）
    backend 為 supabase：302 redirect 到 signed URL（TTL 1 小時）
    """
    from fastapi.responses import RedirectResponse, Response as _Response
    from utils.storage import get_backend, LocalStorage

    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        backend = get_backend()
        key = f"{leave_id}/{filename}"
        if not backend.exists(_UPLOAD_MODULE, key):
            raise HTTPException(status_code=404, detail="檔案不存在")

        if isinstance(backend, LocalStorage):
            data = backend.read(_UPLOAD_MODULE, key)
            return _Response(content=data, media_type="application/octet-stream")

        ttl = int(os.getenv("SUPABASE_STORAGE_SIGNED_URL_TTL", "3600"))
        url = backend.signed_url(_UPLOAD_MODULE, key, ttl)
        return RedirectResponse(url, status_code=302)
    finally:
        session.close()
```

- [ ] **Step 2：頂部確保 `import os` 已存在**

Run: `grep "^import os" /Users/yilunwu/Desktop/ivy-backend/api/leaves.py`
若沒有則加。

- [ ] **Step 3：跑 admin leave 測試**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/ -q -k "leave" --timeout=60`
Expected: 全綠

- [ ] **Step 4：commit**

```bash
git add api/leaves.py
git commit -m "refactor(leaves): admin 假單附件下載改用 StorageBackend"
```

---

## Phase 5：attendance_imports 改用 backend

### Task 5.1：改 `api/attendance/upload.py` 寫入 + 讀取改用 backend

**Files:**
- Modify: `api/attendance/upload.py`（line 106~112、line 805）

現有結構（已查過）：
- Line 106~109：寫實體檔到 `file_path`
- Line 111：top-level `try:`，搭配 line 803 的 `finally:` 與 line 805 的 `file_path.unlink(missing_ok=True)`

- [ ] **Step 1：替換 line 106~112，改寫到 backend 並用 BytesIO 餵 pandas**

定位原本（line 106~112）：

```python
    file_path = _upload_dir() / f"{uuid.uuid4().hex}{raw_ext}"

    with open(file_path, "wb") as f:
        f.write(content)

    try:
        df = pd.read_excel(file_path)
        columns = df.columns.tolist()
```

改為：

```python
    from utils.storage import get_backend
    import io

    backend = get_backend()
    stored_name = f"{uuid.uuid4().hex}{raw_ext}"
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if raw_ext == ".xlsx"
        else "application/vnd.ms-excel"
    )
    backend.save(_UPLOAD_MODULE, stored_name, content, content_type)

    # pandas 接受 BytesIO，不需要實體路徑；用記憶體 buffer 餵入避免本地檔依賴
    try:
        df = pd.read_excel(io.BytesIO(content))
        columns = df.columns.tolist()
```

- [ ] **Step 2：替換 line 805 的 finally 清理動作**

定位原本（line 803~805）：

```python
    finally:
        # 處理完畢後刪除暫存檔，無論成功或失敗
        file_path.unlink(missing_ok=True)
```

改為：

```python
    finally:
        # 處理完畢後刪除暫存（無論 local 或 supabase）
        try:
            backend.delete(_UPLOAD_MODULE, stored_name)
        except Exception:
            logger.warning("刪除考勤暫存檔失敗：%s", stored_name)
```

- [ ] **Step 3：跑 attendance 測試**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/ -q -k "attendance" --timeout=60`
Expected: 全綠

- [ ] **Step 4：跑全部測試確認 0 regression**

Run: `cd ~/Desktop/ivy-backend && python -m pytest -q --timeout=120`
Expected: 全綠（815+ 條 + Phase 1~2 新增 18 條）

- [ ] **Step 5：commit**

```bash
git add api/attendance/upload.py
git commit -m "refactor(attendance): 匯入改用 StorageBackend + BytesIO；處理完自動清除暫存"
```

---

## Phase 6：部署設定 + 文件

### Task 6.1：更新 `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1：在檔案末尾追加 storage 設定**

```bash
# ===== 上傳檔案儲存設定 =====
# STORAGE_BACKEND: 切換上傳檔儲存後端
#   local（預設）= 寫到本機 STORAGE_ROOT，dev 環境使用
#   supabase     = 寫到 Supabase Storage，prod 環境使用
STORAGE_BACKEND=local

# 僅 local backend 用：上傳檔根目錄（預設 backend/data/uploads/）
# STORAGE_ROOT=/var/lib/ivy/uploads

# 僅 supabase backend 用：
# SUPABASE_URL=https://<your-project>.supabase.co
# SUPABASE_SERVICE_ROLE_KEY=<service-role-key>   # 機敏！僅後端使用，絕對不可外洩
# SUPABASE_STORAGE_SIGNED_URL_TTL=3600           # 私有檔 signed URL 有效秒數
```

- [ ] **Step 2：commit**

```bash
git add .env.example
git commit -m "docs(env): 加入 STORAGE_BACKEND 與 SUPABASE_* 變數說明"
```

---

### Task 6.2：寫部署 SOP `docs/sop/storage-deployment.md`

**Files:**
- Create: `docs/sop/storage-deployment.md`

- [ ] **Step 1：建立檔案**

```markdown
# 上傳檔案儲存部署 SOP

## 本地開發（預設）

不需任何設定。`STORAGE_BACKEND` 預設 `local`，檔案寫到 `backend/data/uploads/`。

## 上線（Supabase Storage）

### 1. 建立 Supabase Storage buckets

登入 Supabase Dashboard → Storage → New bucket，建立以下 3 個 bucket：

| Bucket name | Public | Purpose |
|-------------|--------|---------|
| `activity-posters`     | ✅ Public  | 活動海報，前台直接從 CDN 抓 |
| `leave-attachments`    | ❌ Private | 假單附件，後端發 signed URL |
| `attendance-imports`   | ❌ Private | 考勤匯入暫存，僅後端短暫使用 |

或使用 Supabase CLI / MCP `supabase` server 自動建。

### 2. 取得 Service Role Key

Supabase Dashboard → Project Settings → API → service_role key

⚠ **這把 key 等同 root 權限。絕對不可：**
- commit 到任何 repo
- 傳給前端
- 寫到日誌
- 分享到 Slack / email

### 3. 設定 backend env vars（以 Zeabur 為例）

Service Settings → Environment Variables 加：

```bash
STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
SUPABASE_STORAGE_SIGNED_URL_TTL=3600
```

`STORAGE_ROOT` 不設（supabase 模式不用）。

### 4. 部署後驗證

1. admin 上傳活動海報 → 前台公開頁顯示 → 檢查圖片 URL 是 `https://<project>.supabase.co/...`
2. 教師 portal 上傳假單附件 → 下載 → 檢查 302 redirect 到 signed URL（帶 token）
3. admin 上傳考勤 Excel → 確認解析成功

### 5. 切換回 local（回滾）

若 Supabase Storage 出問題，可暫時改 `STORAGE_BACKEND=local`，container 必須掛 `/var/lib/ivy/uploads` 持久 volume。
注意：切換後既有 DB 內 `poster_url`、`attachment_paths` 指向的物件還在 Supabase，回 local 後找不到 → 必須先把雲端檔搬下來（人工 `supabase storage download`）。**這條切換不是無縫的**。

## Service Role Key 輪替

建議每 90 天輪替一次：
1. Dashboard → Reset service_role key
2. 更新 prod env var
3. Restart backend service
舊 key 立即失效，container 重啟生效，無 client-side 衝擊（service key 只在後端）。
```

- [ ] **Step 2：commit**

```bash
git add docs/sop/storage-deployment.md
git commit -m "docs(sop): 新增上傳檔案儲存部署 SOP（Supabase Storage）"
```

---

### Task 6.3：更新 `SECURITY_AUDIT.md` 加 finding

**Files:**
- Modify: `SECURITY_AUDIT.md`（若檔不存在則建立簡單版）

- [ ] **Step 1：先確認檔案是否存在**

Run: `ls /Users/yilunwu/Desktop/ivy-backend/SECURITY_AUDIT.md`

若存在，在「Resolved / Open findings」適當區段加：

```markdown
### F-STORAGE-001：Supabase Service Role Key 機敏處理

**Status:** Open（隨上線同步處理）

**Threat:** Service Role Key 等同 Supabase project root 權限。一旦外洩，攻擊者可讀寫所有 bucket、bypass RLS、刪除 DB 資料。

**Mitigation:**
- Key 只放 backend container env var，不 commit 任何 repo
- 後端日誌不輸出 key 值（已用 `os.getenv` 不串接到 log）
- 每 90 天輪替（見 `docs/sop/storage-deployment.md`）
- 前端絕無存取 service role key 的需要（只用 anon/publishable key）

**Verification:** `git log -p | grep -i "service_role\|SUPABASE_SERVICE"` 應只出現於 `.env.example` 註解、不應有實際 key 值。
```

若檔不存在，建立簡單版：

```markdown
# Security Audit

## Open Findings

### F-STORAGE-001：Supabase Service Role Key 機敏處理

（內容同上）
```

- [ ] **Step 2：commit**

```bash
git add SECURITY_AUDIT.md
git commit -m "docs(security): 紀錄 F-STORAGE-001 Service Role Key 機敏處理"
```

---

## Phase 7：前端驗證（在 ivy-frontend 工作）

### Task 7.1：local 模式 e2e 驗證

**Files:** （這個 task 不改 code，只驗證）

- [ ] **Step 1：啟動 dev**

Run:
```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

- [ ] **Step 2：admin 上傳海報並驗證**

1. 瀏覽器開 `http://localhost:5173/admin`，登入 admin/admin123
2. 進「活動報名設定」
3. 上傳一張隨意 png/jpg
4. 確認 toast 顯示「海報已更新」
5. 確認 `poster_url` 欄位變為 `/api/activity/public/poster/<hex>.png`
6. 開無痕視窗訪問 `http://localhost:5173/activity-public/`
7. 確認圖片正確顯示

- [ ] **Step 3：教師 portal 上傳假單附件並驗證**

1. 用測試教師帳號（任一 `is_active=true` 的 employee）登入 portal
2. 開請假申請、上傳一張圖片附件
3. 在假單列表點下載
4. 確認下載成功

- [ ] **Step 4：commit dummy / 不 commit（只是驗證）**

無 code 變更。

---

### Task 7.2：supabase 模式 e2e 驗證（需先建 bucket）

⚠ 此 task 需要實際 Supabase project 存取，**只能在已建好 buckets 的環境跑**。本地若沒設定，跳過此 task。

- [ ] **Step 1：依 `docs/sop/storage-deployment.md` 步驟 1 建 3 個 bucket**

可用 MCP `supabase` server 的 `apply_migration` 或 Dashboard 手動建。

- [ ] **Step 2：本地切到 supabase backend**

修改 `~/Desktop/ivy-backend/.env`：

```bash
STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
```

- [ ] **Step 3：重啟 backend，重複 Task 7.1 的驗證步驟**

關鍵差異點：
1. 上傳海報後 `poster_url` 應為 `https://<project>.supabase.co/storage/v1/object/public/activity-posters/<hex>.png`
2. 前端 `<img>` 直接從 Supabase CDN 抓圖（DevTools Network 確認）
3. 假單附件下載：DevTools Network 看 `GET /api/portal/my-leaves/<id>/attachments/<file>` 回 **302 redirect** 到 `https://<project>.supabase.co/storage/v1/object/sign/...`
4. **重點驗證**：跨 origin redirect 時，Authorization header 不會被帶到 Supabase（否則 400）。若失敗，需改前端 `axios.create({ ... })` 的 redirect 處理：

```js
// ivy-frontend/src/api/index.js（若需要）
axios.interceptors.request.use((config) => {
  // 跨域請求不帶 backend 的 Authorization
  if (config.url && /^https?:\/\//.test(config.url) && !config.url.startsWith(window.location.origin)) {
    delete config.headers.Authorization
  }
  return config
})
```

**注意**：實務上現代瀏覽器跨 origin redirect 預設會 strip Authorization，axios 透過 XHR/fetch 在瀏覽器層遵守此規則 → 通常無須改。此 task 為驗證點，若驗證 OK 則不必動。

- [ ] **Step 4：若有改前端 code，commit 到 `feat/object-storage-migration-frontend` 分支**

---

## Phase 8：最終驗收

### Task 8.1：跑全部 backend 測試

- [ ] **Step 1：全綠驗證**

Run: `cd ~/Desktop/ivy-backend && python -m pytest -q --timeout=120`
Expected: 全綠（815+ 既有條 + 新增 ≈18 條 storage 相關）

- [ ] **Step 2：確認沒有 service role key 留在 repo**

Run:
```bash
cd ~/Desktop/ivy-backend
git grep -i "service_role\|SUPABASE_SERVICE_ROLE_KEY" -- ':!docs/' ':!.env.example' ':!*.md'
```
Expected: 0 結果（只有 docs 與 .env.example 註解出現）

- [ ] **Step 3：lint / 格式檢查（若有設定）**

Run: `cd ~/Desktop/ivy-backend && python -m py_compile utils/storage.py utils/supabase_storage.py`
Expected: 0 錯誤

---

### Task 8.2：開 PR

- [ ] **Step 1：push branch**

```bash
cd ~/Desktop/ivy-backend
git push -u origin feat/object-storage-migration-backend
```

- [ ] **Step 2：開 PR（使用 gh cli）**

```bash
gh pr create --title "feat: 上傳檔案改走 StorageBackend 抽象層（支援 Supabase Storage）" --body "$(cat <<'EOF'
## Summary
- 新增 `utils/storage.py` 中 `StorageBackend` Protocol 與 `LocalStorage`、`SupabaseStorage` 兩實作
- 受影響 router：`api/activity/settings.py`、`api/activity/public.py`、`api/leaves.py`、`api/portal/leaves.py`、`api/attendance/upload.py`
- DB schema 完全不變（仍存邏輯 key 字串）
- 既有測試 0 修改、0 regression；新增 ≈18 條 storage 抽象測試
- prod 上線靠 env var 切換：`STORAGE_BACKEND=supabase` + `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`

## Test plan
- [x] `pytest -q` 全綠
- [x] local backend：admin 上傳海報、前台顯示
- [x] local backend：教師 portal 上傳/下載假單附件
- [ ] supabase backend：須在建 bucket 後另測（見 Task 7.2）

## 部署備忘
參見 `docs/sop/storage-deployment.md`。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3：在 PR 描述補貼 design doc 與 plan 連結**

---

## 自我 review 檢查

完成後人工掃一遍：

1. **Phase 1 是否完全獨立於 router？**  ✓ 是（只動 utils/storage.py、tests/）
2. **每個 router 改完即測試？**  ✓ 是（每個 Task 都跑相對應的 pytest -k）
3. **公開/私有檔的讀取策略一致？**  ✓ 公開檔走 redirect to CDN、私有檔走 redirect to signed URL
4. **既有測試會被影響嗎？**  ✗ 否（既有測試都用 LocalStorage，行為等同舊 `get_storage_path`）
5. **Service Role Key 安全處理？**  ✓ 部署 SOP + SECURITY_AUDIT.md finding

---

## 風險與緩解（再次提醒）

| 風險 | 緩解 |
|------|------|
| supabase-py 2.x API 與 plan 中假設的 method 名不一致 | Task 2.3 實作時對照 [supabase-py 文件](https://github.com/supabase/supabase-py)；若 API 不同微調 |
| `bucket.upload(path=, file=, file_options=)` 參數順序 / 命名隨版本變動 | Mock test 用 `call_args.kwargs.get(...) or call_args.args[0]` 雙重容錯 |
| Service Role Key 外洩 | docs/sop + SECURITY_AUDIT.md + git grep 守衛 |
| 前端跨 origin redirect 帶 Authorization | Task 7.2 step 3 驗證 + 必要時加 axios interceptor |

---

## 進度追蹤

實作者可在每個 Task 結尾把 `- [ ]` 改 `- [x]`，commit 進度。
