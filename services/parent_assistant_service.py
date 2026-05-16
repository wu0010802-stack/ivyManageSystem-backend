"""家長端 FAQ 助手 service：載入 JSON 並以 mtime 做 in-memory cache。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, Optional


class ParentAssistantService:
    """讀取 data/parent_faq.json，以檔案 mtime 偵測變動並 cache 解析結果。

    Why: 園所偶爾會編輯 FAQ 檔（不重啟服務）；mtime 比較足夠輕量。
    """

    _cache: ClassVar[Optional[dict]] = None
    _cached_mtime: ClassVar[Optional[float]] = None
    _path: ClassVar[Path] = (
        Path(__file__).resolve().parent.parent / "data" / "parent_faq.json"
    )

    @classmethod
    def get_faq(cls) -> dict:
        mtime = cls._path.stat().st_mtime
        if cls._cache is None or mtime != cls._cached_mtime:
            with cls._path.open(encoding="utf-8") as f:
                cls._cache = json.load(f)
            cls._cached_mtime = mtime
        return cls._cache
