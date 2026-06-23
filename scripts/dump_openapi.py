"""Dump FastAPI OpenAPI schema → JSON for frontend codegen.

Usage:
    python scripts/dump_openapi.py
    python scripts/dump_openapi.py --out custom/path.json
    python scripts/dump_openapi.py --keep-api-prefix

Why strip `/api` by default:
    Backend routers use `prefix="/api"`, so OpenAPI paths look like
    `/api/employees`. Frontend axios uses `baseURL = '/api'` and calls
    `api.get('/employees')` — without stripping, the generated typed
    helpers (`ApiResponse<'/employees', 'get'>`) would miss every key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Force docs on so app.openapi() returns the full schema even if ENV=production.
# 守衛：禁止在 prod env 跑 —— 此 script 會把整支 router 的 schema 寫到本地檔，
# 且 ENABLE_API_DOCS 改變會影響後續同進程 import 的行為。dev/CI build 才允許。
if os.environ.get("ENV", "").lower() in ("production", "prod"):
    print(
        "[dump_openapi] 拒絕在 ENV=production 執行；只允許 dev/CI build 環境。",
        file=sys.stderr,
    )
    raise SystemExit(2)
os.environ.setdefault("ENABLE_API_DOCS", "1")


def _strip_api_prefix(schema: dict) -> dict:
    paths = schema.get("paths", {})
    new_paths: dict = {}
    for raw_path, ops in paths.items():
        if raw_path == "/api":
            new_path = "/"
        elif raw_path.startswith("/api/"):
            new_path = raw_path[len("/api") :]
        else:
            new_path = raw_path
        if new_path in new_paths:
            raise RuntimeError(
                f"Path collision after stripping /api prefix: {new_path!r} "
                f"(both {raw_path!r} and an earlier route map here)"
            )
        new_paths[new_path] = ops
    schema["paths"] = new_paths
    return schema


def _strip_dev_paths(schema: dict) -> dict:
    """移除 dev-only 路由（/api/dev/* 與剝 prefix 後可能的 /dev/*）。

    dev 別名 / 除錯端點僅在 ENV=development 由 main.py 掛載，不屬於前端 typed 契約。
    CI 的 OpenAPI Drift Check 用 ENV=development dump，若不剝這些路由會把它們寫進
    openapi.json → schema.d.ts，與（刻意排除 dev 的）committed schema.d.ts 永久 drift。
    """
    paths = schema.get("paths", {})
    schema["paths"] = {
        p: ops
        for p, ops in paths.items()
        if not (
            p == "/dev"
            or p.startswith("/dev/")
            or p == "/api/dev"
            or p.startswith("/api/dev/")
        )
    }
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "openapi.json",
        help="Output JSON path (default: ivy-backend/openapi.json)",
    )
    parser.add_argument(
        "--keep-api-prefix",
        action="store_true",
        help="Do NOT strip /api prefix from paths",
    )
    args = parser.parse_args()

    # Import after env tweaks so the FastAPI() constructor sees docs enabled.
    from main import app  # noqa: WPS433

    schema = app.openapi()
    # dev-only 路由（/api/dev/*）僅 ENV=development 掛載，不屬前端 typed 契約；一律剝除，
    # 避免 CI（ENV=development）dump 把 /dev/* 污染進 schema.d.ts 造成永久 drift。
    schema = _strip_dev_paths(schema)
    if not args.keep_api_prefix:
        schema = _strip_api_prefix(schema)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"[dump_openapi] wrote {args.out} "
        f"({len(schema.get('paths', {}))} paths, "
        f"{len(schema.get('components', {}).get('schemas', {}))} schemas)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
