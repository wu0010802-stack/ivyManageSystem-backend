"""scripts/setup_line_richmenu.py — 部署 LINE Rich Menu v0（Phase 5）

⚠️ 警告：此腳本會操作 LINE Channel 狀態（建立/刪除 Rich Menu、上傳圖檔、
        設為全體預設）。執行前請：
        1. 確認 LINE_CHANNEL_ACCESS_TOKEN 是否已設定且為「家長 Bot」的 token
        2. 跟業主確認可上線
        3. 此腳本「不會」自動執行；必須由人工 `python -m scripts.setup_line_richmenu`

v0 設計：純 Pillow 生成 2500x1686 PNG（白底 + 6 區塊純色塊 + 中文文字）。
正式上線時換為設計師 PNG 即可，本腳本邏輯不變。
"""

from __future__ import annotations

import io
import logging
import os
import sys
import urllib.parse
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 6 格 Rich Menu 規格（LINE 官方範本）
_MENU_W = 2500
_MENU_H = 1686
_CELL_W = _MENU_W // 3  # 833
_CELL_H = _MENU_H // 2  # 843

# 區塊內容；URI 由 LIFF_ID 動態填入
_CELLS = [
    # (label, hash_path, bg_color)
    ("🏠 首頁", "/home", "#f5f5f5"),
    ("💬 訊息", "/messages", "#fff5e6"),
    ("📋 出席", "/attendance", "#f5f5f5"),
    ("📢 公告", "/announcements", "#fff5e6"),
    ("📅 簽收", "/events", "#f5f5f5"),
    ("⋯ 更多", "/more", "#fff5e6"),
]

_LINE_API = "https://api.line.me/v2/bot/richmenu"
_LINE_API_CONTENT = "https://api-data.line.me/v2/bot/richmenu/{rid}/content"


def _hex_to_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def render_v0_png(liff_id: str) -> bytes:
    """用 Pillow 生成 v0 PNG。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError(
            "Pillow 未安裝；請先 pip install Pillow，再執行 Rich Menu 部署"
        ) from e

    img = Image.new("RGB", (_MENU_W, _MENU_H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 嘗試載入中文字型；找不到就退回 default（emoji 可能變方框）
    font: Optional[ImageFont.FreeTypeFont] = None
    for path in [
        "/System/Library/Fonts/PingFang.ttc",  # macOS
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, 110)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    for idx, (label, _path, bg) in enumerate(_CELLS):
        col = idx % 3
        row = idx // 3
        x0 = col * _CELL_W
        y0 = row * _CELL_H
        x1 = x0 + _CELL_W
        y1 = y0 + _CELL_H
        draw.rectangle([x0, y0, x1, y1], fill=_hex_to_rgb(bg))
        # 文字置中
        tw, th = draw.textbbox((0, 0), label, font=font)[2:]
        draw.text(
            ((x0 + x1 - tw) / 2, (y0 + y1 - th) / 2),
            label,
            fill=(40, 50, 60),
            font=font,
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_areas_payload(liff_id: str) -> list[dict]:
    """組 LINE Rich Menu areas（6 區塊）的 JSON。"""
    areas = []
    for idx, (_label, hash_path, _bg) in enumerate(_CELLS):
        col = idx % 3
        row = idx // 3
        url = f"https://liff.line.me/{liff_id}/#{hash_path}"
        areas.append(
            {
                "bounds": {
                    "x": col * _CELL_W,
                    "y": row * _CELL_H,
                    "width": _CELL_W,
                    "height": _CELL_H,
                },
                "action": {"type": "uri", "uri": url},
            }
        )
    return areas


def deploy(token: str, liff_id: str, *, replace: bool = True) -> str:
    """部署 Rich Menu 並設為全體預設；回傳 richmenu id。

    流程：
    1. 若 replace：列出舊 menu 全刪
    2. POST /richmenu 建立 menu（含 size + areas）
    3. POST /richmenu/{id}/content 上傳 PNG
    4. POST /user/all/richmenu/{id} 設為全體預設

    任一步失敗會 raise；caller 看 traceback 處理。
    """
    headers = {"Authorization": f"Bearer {token}"}

    if replace:
        resp = requests.get(f"{_LINE_API}/list", headers=headers, timeout=10)
        resp.raise_for_status()
        for m in resp.json().get("richmenus", []):
            rid = m["richMenuId"]
            requests.delete(f"{_LINE_API}/{rid}", headers=headers, timeout=10)
            logger.info("已刪除舊 Rich Menu %s", rid)

    # 1. create menu
    payload = {
        "size": {"width": _MENU_W, "height": _MENU_H},
        "selected": True,
        "name": "ParentMenuV0",
        "chatBarText": "選單",
        "areas": build_areas_payload(liff_id),
    }
    resp = requests.post(
        _LINE_API,
        headers={**headers, "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    rid = resp.json()["richMenuId"]
    logger.info("已建立 Rich Menu %s", rid)

    # 2. upload PNG
    png = render_v0_png(liff_id)
    upload_url = _LINE_API_CONTENT.format(rid=urllib.parse.quote(rid))
    resp = requests.post(
        upload_url,
        headers={**headers, "Content-Type": "image/png"},
        data=png,
        timeout=15,
    )
    resp.raise_for_status()
    logger.info("已上傳 Rich Menu PNG（%d bytes）", len(png))

    # 3. set as default for all users
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{urllib.parse.quote(rid)}",
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Rich Menu %s 已設為全體預設", rid)

    return rid


def main() -> None:
    """CLI 入口；需明確 import 才執行。

    require: LINE_CHANNEL_ACCESS_TOKEN, LIFF_ID 環境變數
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    liff_id = os.environ.get("LIFF_ID") or os.environ.get("VITE_LIFF_ID")
    if not token:
        print("ERROR: 環境變數 LINE_CHANNEL_ACCESS_TOKEN 未設定", file=sys.stderr)
        sys.exit(1)
    if not liff_id:
        print("ERROR: 環境變數 LIFF_ID 未設定", file=sys.stderr)
        sys.exit(1)
    rid = deploy(token, liff_id, replace=True)
    print(f"✅ Rich Menu 部署完成：{rid}")


if __name__ == "__main__":
    main()
