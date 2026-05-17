"""server.py — FastMCP server bootstrap。

啟動方式（stdio transport）：
    python -m mcp_server.activity_crud

必要環境變數：
    IVY_API_BASE_URL  預設 http://localhost:8088
    IVY_MCP_USERNAME  ivy-backend 員工帳號（建議專開 mcp-bot）
    IVY_MCP_PASSWORD  該帳號密碼
"""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from .client import IvyApiClient, IvyApiError
from .tools import register_tools


def _build_mcp() -> FastMCP:
    """建立 FastMCP instance、註冊 12 個 tool。

    Client 在這裡 lazy 建構（不會立刻打 login）；第一次 tool 呼叫才登入。
    """
    mcp = FastMCP("ivy-activity-crud")
    try:
        client = IvyApiClient()
    except IvyApiError as exc:
        # env 缺失 → 啟動時就吐到 stderr，方便除錯
        print(f"[ivy-activity-crud] 啟動失敗：{exc.message}", file=sys.stderr)
        raise SystemExit(2) from exc
    register_tools(mcp, client)
    return mcp


def main() -> None:
    """stdio MCP server 入口。"""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout 走 MCP protocol，log 一律走 stderr
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    mcp = _build_mcp()
    mcp.run()  # 預設 stdio transport


if __name__ == "__main__":
    main()
