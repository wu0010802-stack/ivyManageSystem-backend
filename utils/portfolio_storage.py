"""Portfolio 附件儲存抽象層。

v1 僅實作 LocalStorage（寫到 utils.storage.get_storage_root() / "portfolio" / yyyy / mm / {uuid}{ext}），
之後可新增 R2Storage 等 backend，上層 router 不需改動。

附件同時生成三個變體（僅針對影像）：
- 原檔：  portfolio/YYYY/MM/{uuid}{ext}
- display：portfolio/YYYY/MM/{uuid}_display.jpg  (1024px 長邊 JPEG q85)
- thumb：  portfolio/YYYY/MM/{uuid}_thumb.jpg    (256px 長邊 JPEG q75)

HEIC 支援：若 pillow_heif 已安裝則自動 register、可產生 JPG 變體；未安裝則拒絕 HEIC。
影片：不生成變體，原檔直接存。
"""

from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

from PIL import Image

from utils.storage import get_storage_root

logger = logging.getLogger(__name__)

# 延遲偵測 pillow-heif（避免部署環境無 libheif 時 import 失敗）
_HEIF_REGISTERED = False
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIF_REGISTERED = True
    logger.info("pillow_heif 已註冊：HEIC 上傳會自動轉 JPG")
except Exception:  # pragma: no cover — 部署環境依賴
    logger.warning("pillow_heif 未安裝：HEIC 上傳將被拒絕")


PORTFOLIO_MODULE = "portfolio"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_IMAGE_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
_VIDEO_MIME_MAP = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

DISPLAY_MAX_DIM = 1024
THUMB_MAX_DIM = 256
JPEG_DISPLAY_QUALITY = 85
JPEG_THUMB_QUALITY = 75


def is_image_extension(ext: str) -> bool:
    return ext.lower() in _IMAGE_EXTS


def is_video_extension(ext: str) -> bool:
    return ext.lower() in _VIDEO_EXTS


def is_heic_extension(ext: str) -> bool:
    return ext.lower() in {".heic", ".heif"}


def heic_supported() -> bool:
    return _HEIF_REGISTERED


def infer_mime_type(ext: str) -> str:
    ext = ext.lower()
    return (
        _IMAGE_MIME_MAP.get(ext)
        or _VIDEO_MIME_MAP.get(ext)
        or "application/octet-stream"
    )


@dataclass
class StoredAttachment:
    """上傳結果：三個 key（display/thumb 對影片為 None）與 mime。"""

    storage_key: str
    display_key: Optional[str]
    thumb_key: Optional[str]
    mime_type: str


class StorageBackend(Protocol):
    def put_attachment(
        self,
        content: bytes,
        extension: str,
        *,
        today: Optional[date] = None,
    ) -> StoredAttachment: ...

    def read(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def absolute_path(self, key: str) -> Path: ...


class LocalStorage:
    """寫到 utils.storage.get_storage_root()/portfolio/YYYY/MM/... 的 backend。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root or (get_storage_root() / PORTFOLIO_MODULE)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def absolute_path(self, key: str) -> Path:
        """把 storage_key（相對路徑）轉為絕對路徑，並防 path traversal。"""
        # key 形式為 "YYYY/MM/{uuid}{ext}" — 不可含 ../ 或絕對路徑
        candidate = (self._root / key).resolve()
        root_resolved = self._root.resolve()
        if (
            not str(candidate).startswith(str(root_resolved) + "/")
            and candidate != root_resolved
        ):
            raise ValueError(f"非法的 storage_key: {key!r}")
        return candidate

    def read(self, key: str) -> bytes:
        return self.absolute_path(key).read_bytes()

    def delete(self, key: str) -> None:
        p = self.absolute_path(key)
        if p.exists():
            p.unlink()

    def put_attachment(
        self,
        content: bytes,
        extension: str,
        *,
        today: Optional[date] = None,
    ) -> StoredAttachment:
        """寫入原檔 + 影像變體（display/thumb）。回傳 StoredAttachment。

        Args:
            content: 檔案原始 bytes
            extension: 副檔名含點號（如 ".jpg"），會被 lower()
            today:    測試注入用，預設 date.today()
        """
        ext = extension.lower()
        today = today or date.today()

        # 目錄：portfolio/YYYY/MM/
        year_str = f"{today.year:04d}"
        month_str = f"{today.month:02d}"
        dir_rel = Path(year_str) / month_str
        dir_abs = self._root / dir_rel
        dir_abs.mkdir(parents=True, exist_ok=True)

        file_id = uuid.uuid4().hex
        original_key = str(dir_rel / f"{file_id}{ext}").replace("\\", "/")
        self.absolute_path(original_key).write_bytes(content)

        display_key: Optional[str] = None
        thumb_key: Optional[str] = None

        if is_image_extension(ext):
            display_key, thumb_key = self._generate_image_variants(
                content=content,
                ext=ext,
                dir_rel=dir_rel,
                file_id=file_id,
            )

        return StoredAttachment(
            storage_key=original_key,
            display_key=display_key,
            thumb_key=thumb_key,
            mime_type=infer_mime_type(ext),
        )

    def _generate_image_variants(
        self,
        *,
        content: bytes,
        ext: str,
        dir_rel: Path,
        file_id: str,
    ) -> tuple[str, str]:
        """生成 display (1024px q85) 與 thumb (256px q75) 兩個 JPG 變體。"""
        try:
            image = Image.open(io.BytesIO(content))
        except Exception as exc:  # pragma: no cover — 上游應已 magic bytes 驗證
            logger.warning("無法解析影像（%s）：%s", ext, exc)
            raise

        image = _apply_exif_orientation(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        display_key = str(dir_rel / f"{file_id}_display.jpg").replace("\\", "/")
        thumb_key = str(dir_rel / f"{file_id}_thumb.jpg").replace("\\", "/")

        display_img = _resize_max_dim(image, DISPLAY_MAX_DIM)
        thumb_img = _resize_max_dim(image, THUMB_MAX_DIM)

        display_path = self.absolute_path(display_key)
        thumb_path = self.absolute_path(thumb_key)

        display_img.save(
            display_path, format="JPEG", quality=JPEG_DISPLAY_QUALITY, optimize=True
        )
        thumb_img.save(
            thumb_path, format="JPEG", quality=JPEG_THUMB_QUALITY, optimize=True
        )
        return display_key, thumb_key


def _apply_exif_orientation(image: Image.Image) -> Image.Image:
    """依 EXIF Orientation 標記自動旋轉（iPhone 拍橫拍直常需要）。"""
    try:
        from PIL import ImageOps

        return ImageOps.exif_transpose(image)
    except Exception:  # pragma: no cover
        return image


def _resize_max_dim(image: Image.Image, max_dim: int) -> Image.Image:
    w, h = image.size
    if max(w, h) <= max_dim:
        return image.copy()
    ratio = max_dim / float(max(w, h))
    new_size = (int(round(w * ratio)), int(round(h * ratio)))
    return image.resize(new_size, Image.LANCZOS)


_STORAGE_SINGLETON: Optional[StorageBackend] = None


def get_portfolio_storage() -> StorageBackend:
    """回傳 portfolio storage singleton（v1 固定 LocalStorage）。"""
    global _STORAGE_SINGLETON
    if _STORAGE_SINGLETON is None:
        _STORAGE_SINGLETON = LocalStorage()
    return _STORAGE_SINGLETON


def set_portfolio_storage(backend: StorageBackend) -> None:
    """測試注入用：替換 singleton。"""
    global _STORAGE_SINGLETON
    _STORAGE_SINGLETON = backend


def reset_portfolio_storage() -> None:
    """測試 teardown 用：清空 singleton，下次呼叫 get 會重建。"""
    global _STORAGE_SINGLETON
    _STORAGE_SINGLETON = None
